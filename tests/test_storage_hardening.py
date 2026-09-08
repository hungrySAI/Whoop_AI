"""Lifecycle, bounded recomputation, and migration checks on fabricated data only."""

import copy
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from whoop_copilot.analytics import ALGORITHMS, Algorithm
from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.commands import COMMAND_SCHEMA
from whoop_copilot.contracts import timestamp
from whoop_copilot.protection import LocalPolicy, connect
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import (
    APPLICATION_ID,
    F1_SCHEMA,
    SCHEMA,
    SCHEMA_VERSION,
    Store,
    canonical,
    restore_backup,
)
from whoop_copilot.sync import SyncService

FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
START, END = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"
KEY = bytes(range(32))


def samples(*, acquired_at="2026-09-05T00:00:00Z", synthetic=True):
    return normalize_api(
        json.loads(FIXTURE.read_text())["resources"],
        acquired_at=acquired_at,
        synthetic=synthetic,
    )


def test_successful_fetch_crosses_expiry_without_losing_unchanged_sources(tmp_path):
    resources = json.loads(FIXTURE.read_text())["resources"]
    now = ["2026-09-05T00:00:00Z"]

    class Client:
        def list_records(self, resource, start, end, next_token):
            now[0] = "2026-09-06T00:00:01Z"
            return {
                "records": copy.deepcopy(resources[resource]),
                "next_token": None,
                "headers": {},
            }

    with Store(
        tmp_path / "fabricated.sqlite3",
        environment="real",
        encryption_key=KEY,
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: now[0],
    ) as store:
        first = store.ingest(samples(synthetic=False))
        analysis = CopilotService(store).analyze("whoop.hrv_rmssd", START, END)
        now[0] = "2026-09-05T23:59:59Z"
        result = SyncService(store, Client()).run(START, END)
        assert result["status"] == "completed"
        assert result["ingestion"]["inserted"] == 6
        assert result["ingestion"]["duplicates"] == 0
        for resource in resources:
            current = store.current_sources(resource)
            assert len(current) == 1
            assert current[0]["id"] not in first["revision_ids"]
            assert current[0]["known_at"] == timestamp(now[0])
            assert current[0]["expires_at"] == timestamp("2026-09-07T00:00:01Z")
        with pytest.raises(ValueError, match="Unknown analysis"):
            CopilotService(store).get_run(analysis["run_id"])
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_response_before_old_expiry_can_be_ingested_after_old_expiry(tmp_path):
    now = ["2026-09-05T00:00:00Z"]
    with Store(
        tmp_path / "fabricated.sqlite3",
        environment="real",
        encryption_key=KEY,
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: now[0],
    ) as store:
        first = store.ingest(samples(synthetic=False))
        fresh = samples(acquired_at="2026-09-05T23:59:59Z", synthetic=False)
        now[0] = "2026-09-06T00:00:01Z"
        second = store.ingest(fresh)
        assert second["inserted"] == 6
        assert not set(first["revision_ids"]) & set(second["revision_ids"])
        assert store.db.execute("SELECT COUNT(*) FROM source_ingest_keys").fetchone()[0] == 2
        assert store.current_sources("sleep")[0]["expires_at"] == timestamp("2026-09-06T23:59:59Z")


def test_only_affected_metrics_invalidate_and_pending_requests_are_coalesced(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3") as store:
        records = samples()
        assert store.ingest(records)["task_ids"] == []
        service = CopilotService(store)
        hrv = service.analyze("whoop.hrv_rmssd", START, END)
        sleep = service.analyze("whoop.respiratory_rate", START, END, resource="sleep")
        stable = store.ingest(samples(acquired_at="2026-09-06T00:00:00Z"))
        assert stable["inserted"] == 2
        assert stable["task_ids"] == []
        assert service.get_run(hrv["run_id"])["stale"] is False
        assert service.analyze("whoop.hrv_rmssd", START, END)["run_id"] == hrv["run_id"]
        recovery = next(record for record in records if record.resource == "recovery")
        for minute in (1, 2):
            updated = f"2026-09-06T00:0{minute}:00Z"
            corrected = replace(
                recovery,
                source_updated_at=updated,
                metadata={
                    **recovery.metadata,
                    "source_version_parts": {
                        **recovery.metadata["source_version_parts"],
                        "recovery": updated,
                    },
                },
                observations=tuple(
                    replace(obs, value=obs.value + minute) for obs in recovery.observations
                ),
            )
            result = store.ingest([corrected])
            if minute == 1:
                assert len(result["task_ids"]) == 1
                # A refreshed explicit analysis creates another invalidation on
                # the second correction, but pending work can already serve it.
                service.analyze("whoop.hrv_rmssd", START, END)
            else:
                assert result["task_ids"] == []
        assert service.get_run(sleep["run_id"])["stale"] is False
        assert len(service.list_tasks()) == 1
        assert service.run_pending()[0]["status"] == "completed"


def test_recomputation_batches_never_drop_explicit_requests(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3") as store:
        records = samples()
        store.ingest(records)
        service = CopilotService(store)
        for index in range(205):
            end = datetime.fromisoformat(timestamp(END)) + timedelta(microseconds=index)
            service.analyze("whoop.hrv_rmssd", START, end.isoformat(), resource="recovery")
        recovery = next(record for record in records if record.resource == "recovery")
        # A scored -> pending transition removes observations and still invalidates.
        pending = replace(
            recovery,
            observations=(),
            source_updated_at="2026-09-06T00:00:00Z",
            metadata={
                **recovery.metadata,
                "source_version_parts": {
                    **recovery.metadata["source_version_parts"],
                    "recovery": "2026-09-06T00:00:00Z",
                },
            },
        )
        result = store.ingest([pending])
        assert len(result["task_ids"]) == 3
        sizes = sorted(
            len(json.loads(row[0])["requests"])
            for row in store.db.execute("SELECT payload FROM tasks")
        )
        assert sizes == [5, 100, 100]
        work = service.run_pending()
        assert len(work) == 3
        assert all(row["status"] == "completed" for row in work)
        assert sum(len(row["run_ids"]) for row in work) == 205
        assert all(
            service.get_run(run)["result"]["count"] == 0 for row in work for run in row["run_ids"]
        )


def test_correction_during_running_recompute_keeps_a_successor_task(tmp_path, monkeypatch):
    now = ["2026-09-05T12:00:00Z"]
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: now[0]) as store:
        record = next(row for row in samples() if row.resource == "recovery")
        store.ingest([record])
        service = CopilotService(store)
        service.analyze("whoop.hrv_rmssd", START, END)

        def correction(minute):
            updated = f"2026-09-05T12:0{minute}:00Z"
            now[0] = updated
            return replace(
                record,
                source_updated_at=updated,
                metadata={
                    **record.metadata,
                    "source_version_parts": {
                        **record.metadata["source_version_parts"],
                        "recovery": updated,
                    },
                },
                observations=tuple(
                    replace(obs, value=obs.value + minute) for obs in record.observations
                ),
            )

        store.ingest([correction(1)])
        calculate = ALGORITHMS["mean_change"].calculate
        later = correction(2)
        now[0] = "2026-09-05T12:01:00Z"

        def concurrent_correction(rows, start, end):
            now[0] = "2026-09-05T12:01:01Z"
            assert len(store.ingest([later])["task_ids"]) == 1
            monkeypatch.setitem(ALGORITHMS, "mean_change", Algorithm("mean_change/1", calculate))
            return calculate(rows, start, end)

        monkeypatch.setitem(
            ALGORITHMS, "mean_change", Algorithm("mean_change/1", concurrent_correction)
        )
        assert service.run_pending(limit=1)[0]["status"] == "completed"
        assert len([task for task in service.list_tasks() if task["status"] == "pending"]) == 1
        completed = service.run_pending()
        fresh = service.get_run(completed[0]["run_ids"][0])
        assert fresh["stale"] is False
        assert fresh["result"]["mean"] == 42


@pytest.mark.parametrize("environment", ["synthetic", "real"])
def test_version_two_backup_remains_restorable_and_migrates_in_target(tmp_path, environment):
    source, target = tmp_path / "v2.sqlite3", tmp_path / "restored.sqlite3"
    key = KEY if environment == "real" else None
    policy = LocalPolicy(30, owner_authorized=True) if environment == "real" else None
    db = connect(source, environment, key)
    db.executescript(SCHEMA + COMMAND_SCHEMA + F1_SCHEMA)
    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    db.execute("INSERT INTO schema_migrations VALUES (2,'2026-09-05T00:00:00Z')")
    db.executemany(
        "INSERT INTO settings VALUES (?,?)",
        [("environment", environment), ("subject_id", "preserved")],
    )
    if policy:
        db.execute("INSERT INTO settings VALUES ('local_policy',?)", (canonical(asdict(policy)),))
    db.close()
    original_bytes = source.read_bytes()
    restore_backup(
        source,
        target,
        environment=environment,
        encryption_key=key,
        clock=lambda: "2026-09-06T00:00:00Z",
    )
    with Store(
        target, environment=environment, encryption_key=key, clock=lambda: "2026-09-06T00:00:00Z"
    ) as store:
        assert (
            store.db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert (
            store.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[0]
            == "preserved"
        )
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM observations WHERE revision_id=?", (1,)
        ).fetchall()
        assert any("observations_revision" in row[3] for row in plan)
    assert source.read_bytes() == original_bytes
