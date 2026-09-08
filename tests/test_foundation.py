"""Cross-layer invariants, using fabricated source data and temporary databases."""

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from whoop_copilot.adapters import parse_csv, parse_whoop
from whoop_copilot.analytics import ALGORITHMS, METRICS, Algorithm, MetricDefinition
from whoop_copilot.commands import CommandService
from whoop_copilot.contracts import ObservationInput, SourceRecordInput, timestamp
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import SCHEMA_VERSION, Store, restore_backup

FIXTURES = Path(__file__).parent / "fixtures"
START = "2026-08-01T00:00:00Z"
END = "2026-08-15T00:00:00Z"
QUERY = {"metric": "whoop.hrv_rmssd", "start": START, "end": END, "provider": "whoop"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 5, 12, tzinfo=UTC)

    def __call__(self):
        return self.now.isoformat()

    def advance(self, seconds=60):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "copilot.sqlite3", clock=Clock()) as result:
        yield result


@pytest.fixture
def whoop_records():
    return parse_whoop(FIXTURES / "synthetic_whoop.json")


def corrected(record, *, value=54, updated="2026-09-05T12:01:00Z"):
    payload = deepcopy(record.payload)
    payload["recovery"]["updated_at"] = updated
    payload["recovery"]["score"]["hrv_rmssd_milli"] = value
    observations = tuple(
        replace(observation, value=value, original_value=value)
        if observation.metric == "whoop.hrv_rmssd"
        else observation
        for observation in record.observations
    )
    metadata = deepcopy(record.metadata)
    metadata.setdefault("source_version_parts", {})["recovery"] = timestamp(updated)
    return replace(
        record,
        source_updated_at=updated,
        payload=payload,
        observations=observations,
        metadata=metadata,
    )


def paired_revision(record, *, value, recovery_updated, cycle_updated):
    revised = corrected(record, value=value, updated=recovery_updated)
    payload = deepcopy(revised.payload)
    payload["cycle"]["updated_at"] = cycle_updated
    metadata = deepcopy(revised.metadata)
    metadata["source_version_parts"]["cycle"] = timestamp(cycle_updated)
    return replace(
        revised,
        payload=payload,
        metadata=metadata,
        source_updated_at=max(timestamp(recovery_updated), timestamp(cycle_updated)),
    )


def count(store, table):
    # Table names are test-owned constants, never request inputs.
    return store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_whoop_and_csv_ingest_deduplicate_without_side_effects(store, whoop_records):
    body = parse_csv(FIXTURES / "synthetic_body.csv")
    first = store.ingest(whoop_records + body)
    assert first["inserted"] == 28
    assert first["duplicates"] == 0
    assert count(store, "source_connections") == 2
    assert count(store, "activities") == 14
    assert count(store, "observations") == 84
    before_tasks = count(store, "tasks")
    second = store.ingest(whoop_records + body)
    assert second["inserted"] == 0
    assert second["duplicates"] == 28
    assert second["task_id"] is None
    assert count(store, "source_revisions") == 28
    assert count(store, "tasks") == before_tasks
    weight = CopilotService(store).analyze("body.weight", START, END, provider="manual")
    assert weight["result"]["count"] == 14
    assert weight["result"]["mean"] == pytest.approx(74.35)
    assert weight["evidence"]["unit"] == "kg"
    assert weight["evidence"]["source_metric_is_official"] is False
    assert any(row["original_unit"] == "lb" for row in weight["evidence"]["observations"])


def test_registered_trend_preserves_sources_and_known_expected_values(store, whoop_records):
    store.ingest(whoop_records)
    service = CopilotService(store)
    run = service.analyze(**QUERY)
    result = run["result"]
    assert result["count"] == 14
    assert result["mean"] == 53
    assert result["first_half"] == {"count": 7, "mean": 46}
    assert result["second_half"] == {"count": 7, "mean": 60}
    assert result["change"] == 14
    assert result["status"] == "descriptive"
    assert run["stale"] is False
    assert len(run["evidence"]["references"]) == 14
    assert all(
        ref["content_hash"] and ref["parser_version"] for ref in run["evidence"]["references"]
    )
    assert run["evidence"]["source_metric_is_official"] is True
    assert run["evidence"]["model"] is None
    assert service.analyze(**QUERY)["run_id"] == run["run_id"]
    assert count(store, "analysis_runs") == 1
    assert service.reproduce(run["run_id"])["matches"] is True


def test_correction_invalidates_latest_but_preserves_historical_evidence(store, whoop_records):
    store.ingest(whoop_records)
    service = CopilotService(store)
    service.run_pending()
    original = service.analyze(**QUERY)
    captured_at = store.clock()
    historical = service.analyze(**QUERY, as_of=captured_at)
    store.clock.advance()
    change = store.ingest([corrected(whoop_records[0])])
    assert change["inserted"] == 1
    assert count(store, "source_revisions") == 15
    assert service.get_run(original["run_id"])["stale"] is True
    assert service.get_run(historical["run_id"])["stale"] is False
    current = service.analyze(**QUERY)
    assert current["result"]["count"] == 14
    assert current["result"]["mean"] == 54
    assert current["result"]["change"] == 12
    assert service.analyze(**QUERY, as_of=captured_at)["result"]["mean"] == 53
    assert service.reproduce(original["run_id"])["result"]["mean"] == 53
    assert service.reproduce(original["run_id"])["matches"] is True
    completed = service.run_pending()
    assert len(completed) == 1
    assert completed[0]["status"] == "completed"
    assert completed[0]["run_ids"] == [current["run_id"]]
    assert service.run_pending() == []
    assert count(store, "analysis_runs") == 3


def test_as_of_before_capture_is_unknown_and_future_cutoffs_are_rejected(store, whoop_records):
    store.ingest(whoop_records)
    service = CopilotService(store)
    run = service.analyze(**QUERY, as_of="2026-09-05T11:59:59Z")
    assert run["result"]["count"] == 0
    assert run["result"]["mean"] is None
    assert run["result"]["change"] is None
    assert run["result"]["status"] == "insufficient_data"
    assert any("earlier state is unknown" in item for item in run["evidence"]["limitations"])
    assert run["evidence"]["references"] == []
    with pytest.raises(ValueError, match="future"):
        service.analyze(**QUERY, as_of="2026-09-05T12:00:01Z")


def test_late_arriving_older_revision_cannot_replace_newer_source(store, whoop_records):
    newer = corrected(whoop_records[0], updated="2026-09-05T11:00:00Z")
    store.ingest([newer])
    store.clock.advance()
    store.ingest([whoop_records[0]])
    run = CopilotService(store).analyze(**QUERY)
    assert run["result"]["count"] == 1
    assert run["result"]["mean"] == 54
    assert count(store, "source_revisions") == 2
    assert run["evidence"]["references"][0]["id"] == 1


def test_newer_cycle_timestamp_cannot_hide_regression_of_recovery(store, whoop_records):
    newer = paired_revision(
        whoop_records[0],
        value=77,
        recovery_updated="2026-09-05T10:00:00Z",
        cycle_updated="2026-09-05T11:00:00Z",
    )
    older = paired_revision(
        whoop_records[0],
        value=66,
        recovery_updated="2026-09-05T09:00:00Z",
        cycle_updated="2026-09-05T11:00:00Z",
    )
    assert newer.source_updated_at == older.source_updated_at
    store.ingest([newer])
    captured_at = store.clock()
    store.clock.advance()
    store.ingest([older])
    service = CopilotService(store)
    assert service.analyze(**QUERY)["result"]["mean"] == 77
    assert service.analyze(**QUERY, as_of=captured_at)["result"]["mean"] == 77
    assert count(store, "source_revisions") == 2


def test_mixed_newer_and_older_source_components_are_rejected_atomically(store, whoop_records):
    current = paired_revision(
        whoop_records[0],
        value=77,
        recovery_updated="2026-09-05T10:00:00Z",
        cycle_updated="2026-09-05T11:00:00Z",
    )
    mixed = paired_revision(
        whoop_records[0],
        value=66,
        recovery_updated="2026-09-05T09:00:00Z",
        cycle_updated="2026-09-05T12:00:00Z",
    )
    store.ingest([current])
    store.clock.advance()
    with pytest.raises(ValueError):
        store.ingest([mixed])
    assert count(store, "source_revisions") == 1
    assert CopilotService(store).analyze(**QUERY)["result"]["mean"] == 77


def test_source_update_time_must_match_component_versions(store, whoop_records):
    inconsistent = replace(whoop_records[0], source_updated_at="2026-09-05T11:00:00Z")
    with pytest.raises(ValueError, match="must match its version components"):
        store.ingest([inconsistent])
    assert count(store, "source_revisions") == 0
    assert count(store, "source_connections") == 0


def test_source_deletion_keeps_prior_snapshot_and_independent_training_plan(store, whoop_records):
    store.ingest([whoop_records[0]])
    service = CopilotService(store)
    original = service.analyze(**QUERY)
    commands = CommandService(store.db, clock=store.clock)
    draft = commands.draft("Local plan", "Requested easy training", "training")
    approval = commands.approve(draft["action_id"])
    plan = commands.commit(draft["action_id"], approval["approval_token"])
    store.clock.advance()
    deleted = replace(
        corrected(whoop_records[0], updated=store.clock()),
        deleted=True,
        observations=(),
        activities=(),
        payload={"synthetic": True, "deleted": True},
    )
    store.ingest([deleted])
    assert service.analyze(**QUERY)["result"]["count"] == 0
    assert service.reproduce(original["run_id"])["matches"] is True
    assert commands.get_plan(plan["plan_id"]) == plan


def test_account_binding_is_independent_and_import_mismatch_rolls_back(store, whoop_records):
    store.ingest(whoop_records)
    subject = store.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[0]
    connection = store.db.execute(
        "SELECT * FROM source_connections WHERE provider='whoop'"
    ).fetchone()
    assert subject != "999999"
    assert connection["subject_id"] == subject
    assert connection["external_subject"] == "999999"
    mismatched = replace(whoop_records[0], metadata={"synthetic": True, "whoop_user_id": "111111"})
    before_revisions, before_tasks = count(store, "source_revisions"), count(store, "tasks")
    body = parse_csv(FIXTURES / "synthetic_body.csv")
    with pytest.raises(ValueError, match="account mismatch"):
        store.ingest(body + [mismatched])
    assert count(store, "source_revisions") == before_revisions
    assert count(store, "tasks") == before_tasks
    assert count(store, "source_connections") == 1
    assert not store.db.in_transaction
    store.ingest(body)
    assert {row[0] for row in store.db.execute("SELECT subject_id FROM source_connections")} == {
        subject
    }


def test_new_provider_and_metric_need_no_whoop_specific_changes(store, whoop_records, monkeypatch):
    store.ingest(whoop_records)
    monkeypatch.setitem(METRICS, "body.height", MetricDefinition("cm", "Synthetic height"))
    observations = tuple(
        SourceRecordInput(
            provider="height_fixture",
            resource="measurement",
            external_id=f"height-{day}",
            source_updated_at=f"2026-08-{day:02d}T10:01:00Z",
            payload={"synthetic": True, "cm": 175},
            observations=(
                ObservationInput(
                    metric="body.height",
                    value=175,
                    unit="cm",
                    original_value=175,
                    original_unit="cm",
                    start_at=f"2026-08-{day:02d}T10:00:00Z",
                ),
            ),
        )
        for day in (1, 3, 8, 11)
    )
    store.ingest(list(observations))
    run = CopilotService(store).analyze("body.height", START, END, provider="height_fixture")
    assert run["result"]["count"] == 4
    assert run["result"]["mean"] == 175
    assert run["result"]["change"] == 0
    assert {ref["provider"] for ref in run["evidence"]["references"]} == {"height_fixture"}
    assert CopilotService(store).analyze(**QUERY)["result"]["mean"] == 53


def test_multiple_providers_require_explicit_selection(store):
    body = parse_csv(FIXTURES / "synthetic_body.csv")
    alternative = [replace(record, provider="second_scale") for record in body]
    store.ingest(body + alternative)
    service = CopilotService(store)
    with pytest.raises(ValueError, match="Multiple providers"):
        service.analyze("body.weight", START, END)
    assert count(store, "analysis_runs") == 0
    for provider in ("manual", "second_scale"):
        run = service.analyze("body.weight", START, END, provider=provider)
        assert run["result"]["count"] == 14
        assert {ref["provider"] for ref in run["evidence"]["references"]} == {provider}


def test_quality_exclusions_and_empty_windows_are_not_zero(store, whoop_records):
    altered = replace(
        whoop_records[0],
        observations=tuple(
            replace(obs, quality="calibrating") for obs in whoop_records[0].observations
        ),
    )
    store.ingest([altered])
    run = CopilotService(store).analyze(**QUERY)
    assert run["result"]["count"] == 0
    assert run["result"]["excluded_quality_count"] == 1
    assert run["result"]["mean"] is None
    assert run["result"]["status"] == "insufficient_data"
    assert len(run["evidence"]["observations"]) == 1


def test_lease_expiry_restart_and_repeated_worker_produce_one_result(tmp_path, whoop_records):
    path, clock = tmp_path / "jobs.sqlite3", Clock()
    with Store(path, clock=clock) as store:
        store.ingest(whoop_records)
        service = CopilotService(store)
        service.run_pending()
        service.analyze(**QUERY)
        clock.advance()
        store.ingest([corrected(whoop_records[0])])
        abandoned = service.claim_task(lease_seconds=5)
        assert abandoned is not None
        assert service.claim_task() is None
        # Simulate the actual crash window: computed result persisted, task completion lost.
        computed = service.analyze(**abandoned["payload"]["requests"][0])
        clock.advance(5)
        assert service.finish_task(abandoned) is False
    with Store(path, clock=clock) as reopened:
        service = CopilotService(reopened)
        reclaimed = service.claim_task()
        assert reclaimed["id"] == abandoned["id"]
        assert reclaimed["lease_token"] != abandoned["lease_token"]
        assert service.finish_task(abandoned) is False
        duplicate = service.analyze(**reclaimed["payload"]["requests"][0])
        assert duplicate["run_id"] == computed["run_id"]
        assert service.finish_task(reclaimed) is True
        assert service.finish_task(reclaimed) is False
        assert count(reopened, "analysis_runs") == 2
        task = next(task for task in service.list_tasks() if task["id"] == reclaimed["id"])
        assert task["attempts"] == 2
        assert task["status"] == "completed"
        assert service.run_pending() == []


def test_concurrent_calculations_publish_one_durable_run(tmp_path, whoop_records, monkeypatch):
    path, clock = tmp_path / "parallel.sqlite3", Clock()
    with Store(path, clock=clock) as initial:
        initial.ingest(whoop_records)
    barrier = Barrier(2)
    calculation = ALGORITHMS["mean_change"]

    def synchronized_calculation(rows, start, end):
        # Hold both callers after the cache lookup to exercise competing inserts.
        barrier.wait(timeout=10)
        return calculation.calculate(rows, start, end)

    monkeypatch.setitem(
        ALGORITHMS, "mean_change", Algorithm(calculation.version, synchronized_calculation)
    )

    def analyze(_):
        with Store(path, clock=clock) as worker:
            return CopilotService(worker).analyze(**QUERY)

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(analyze, range(2)))
    assert runs[0] == runs[1]
    with Store(path, clock=clock) as final:
        assert count(final, "analysis_runs") == 1


def test_two_workers_cannot_claim_the_same_unexpired_task(tmp_path, whoop_records):
    path, clock = tmp_path / "claims.sqlite3", Clock()
    with Store(path, clock=clock) as initial:
        initial.ingest(whoop_records)
        CopilotService(initial).analyze(**QUERY)
        clock.advance()
        initial.ingest([corrected(whoop_records[0])])
    barrier = Barrier(2)

    def claim(_):
        with Store(path, clock=clock) as worker:
            barrier.wait(timeout=10)
            return CopilotService(worker).claim_task()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert sum(claim is not None for claim in claims) == 1
    with Store(path, clock=clock) as final:
        tasks = CopilotService(final).list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["attempts"] == 1
        assert tasks[0]["status"] == "running"


def test_migration_restart_and_subject_isolation(tmp_path, whoop_records):
    path, clock = tmp_path / "first.sqlite3", Clock()
    with Store(path, clock=clock) as store:
        store.ingest(whoop_records)
        subject = store.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[
            0
        ]
        assert (
            store.db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with Store(path, clock=clock) as store:
        assert (
            store.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[0]
            == subject
        )
        assert count(store, "schema_migrations") == 1
        assert count(store, "source_revisions") == 14
    with Store(tmp_path / "second.sqlite3", clock=clock) as isolated:
        assert (
            isolated.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[0]
            != subject
        )
        assert count(isolated, "source_revisions") == 0


def test_backup_restore_retains_analysis_actions_and_rejects_overwrite(
    store, whoop_records, tmp_path
):
    store.ingest(whoop_records)
    run = CopilotService(store).analyze(**QUERY)
    commands = CommandService(store.db, clock=store.clock)
    draft = commands.draft("Local draft", "User-requested content", "draft")
    backup = tmp_path / "snapshot.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    assert store.backup(backup)["integrity_check"] == "ok"
    assert restore_backup(backup, restored)["integrity_check"] == "ok"
    with Store(restored, clock=store.clock) as clone:
        assert CopilotService(clone).get_run(run["run_id"]) == run
        assert CopilotService(clone).reproduce(run["run_id"])["matches"] is True
        assert CommandService(clone.db).get_plan(draft["plan_id"]) == commands.get_plan(
            draft["plan_id"]
        )
        assert count(clone, "source_revisions") == 14
        assert count(clone, "tasks") == count(store, "tasks")
    original_backup = backup.read_bytes()
    with pytest.raises(FileExistsError):
        store.backup(backup)
    with pytest.raises(FileExistsError):
        restore_backup(backup, restored)
    assert backup.read_bytes() == original_backup
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600


def test_foreign_database_is_never_initialized_or_restored(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as foreign:
        foreign.execute("CREATE TABLE unrelated(value TEXT)")
        foreign.execute("INSERT INTO unrelated VALUES('must survive')")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="not created by this application"):
        Store(path)
    target = tmp_path / "invalid_restore.sqlite3"
    with pytest.raises(ValueError, match="different application"):
        restore_backup(path, target)
    assert not target.exists()
    assert path.read_bytes() == original


def test_backdated_ingestion_cannot_change_historical_knowledge(store, whoop_records):
    store.ingest(whoop_records)
    store.clock.advance(-1)
    with pytest.raises(ValueError, match="Clock moved backwards"):
        store.ingest([corrected(whoop_records[0])])
    assert count(store, "source_revisions") == 14
