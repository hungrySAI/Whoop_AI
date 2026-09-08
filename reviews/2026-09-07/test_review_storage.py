"""Independent audit reproducers; assertions document observed defects, not desired behavior.

Only repository fixtures, temporary databases, fabricated clocks, and fabricated
encryption keys are used. These are deliberately outside the regression suite.
"""

import copy
import json
from pathlib import Path

from whoop_copilot.analytics import ALGORITHMS, Algorithm
from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/whoop_api_snapshot.json"
START, END = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"
KEY = bytes(range(32))


def test_successful_sync_crossing_expiry_loses_freshly_retrieved_unchanged_records(tmp_path):
    resources = json.loads(FIXTURE.read_text())["resources"]
    now = ["2026-09-05T00:00:00Z"]

    class Client:
        def list_records(self, resource, start, end, next_token):
            # The fetch starts before the old capture expires and finishes after it.
            now[0] = "2026-09-06T00:00:01Z"
            return {
                "records": copy.deepcopy(resources[resource]),
                "next_token": None,
                "headers": {},
            }

    with Store(
        tmp_path / "fabricated-encrypted.sqlite3",
        environment="real",
        encryption_key=KEY,
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: now[0],
    ) as store:
        store.ingest(normalize_api(resources, acquired_at=now[0], synthetic=False))
        assert len(store.current_sources("recovery")) == 1
        now[0] = "2026-09-05T23:59:59Z"
        result = SyncService(store, Client()).run(START, END)
        assert result["status"] == "completed"
        assert result["ingestion"]["duplicates"] == 4
        assert result["ingestion"]["inserted"] == 2  # New profile/body retrieval versions.

        # Observed defect: despite a fresh successful fetch, every timed resource
        # disappears when the first reader removes the expired duplicate rows.
        assert store.current_sources("recovery") == []
        assert store.current_sources("cycle") == []
        assert store.current_sources("sleep") == []
        assert store.current_sources("workout") == []
        assert len(store.current_sources("profile")) == 1
        assert store.db.execute("SELECT COUNT(*) FROM sync_pages").fetchone()[0] == 6


def test_analysis_publish_after_forget_recreates_deleted_source_evidence(tmp_path, monkeypatch):
    resources = json.loads(FIXTURE.read_text())["resources"]
    path = tmp_path / "fabricated.sqlite3"
    calculate = ALGORITHMS["mean_change"].calculate
    with Store(path) as store, Store(path) as deleting_store:
        store.ingest(
            normalize_api(resources, acquired_at="2026-09-05T00:00:00Z", synthetic=True)
        )

        def calculate_after_source_was_deleted(rows, start, end):
            # Deterministically interleave another connection's source deletion
            # after the first connection has obtained its read snapshot.
            removed = deleting_store.forget_source("whoop")
            assert removed["removed_revisions"] == 6
            return calculate(rows, start, end)

        monkeypatch.setitem(
            ALGORITHMS,
            "mean_change",
            Algorithm("mean_change/1", calculate_after_source_was_deleted),
        )
        result = CopilotService(store).analyze("whoop.hrv_rmssd", START, END)
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert result["stale"] is True

        # Observed defect: deletion completed, but the in-flight analysis puts
        # deleted numerical observations and references back in persistent storage.
        assert result["result"]["count"] == 1
        assert len(result["evidence"]["observations"]) > 0
        assert len(result["evidence"]["references"]) == 1
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1
        assert CopilotService(store).get_run(result["run_id"])["evidence"] == result["evidence"]
