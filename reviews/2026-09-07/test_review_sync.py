"""Synthetic diagnostic reproductions for the independent durability review.

These assert the observed pre-hardening behavior; they are review evidence, not
the desired invariant after a fix. No local account or health database is read.
"""

import copy
import json
from pathlib import Path

import pytest

from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/whoop_api_snapshot.json"
START, END = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"


class FabricatedClient:
    def __init__(self):
        self.resources = json.loads(FIXTURE.read_text())["resources"]

    def list_records(self, resource, start, end, next_token):
        assert next_token is None
        return {
            "records": copy.deepcopy(self.resources[resource]),
            "next_token": None,
            "headers": {},
        }


@pytest.mark.parametrize(
    "resource,field,metric,initial,corrected",
    [
        ("sleep", "sleep_performance_percentage", "whoop.sleep_performance", 70, 90),
        ("workout", "strain", "whoop.strain", 8, 18),
    ],
)
def test_review_uuid_case_change_duplicates_a_current_metric(
    tmp_path, resource, field, metric, initial, corrected
):
    """A valid UUID spelling change makes one corrected event count twice."""
    client = FabricatedClient()
    record = client.resources[resource][0]
    record["score"][field] = initial
    with Store(tmp_path / "fabricated-review.sqlite3") as store:
        sync = SyncService(store, client)
        sync.run(START, END)
        record["id"] = record["id"].upper()
        record["updated_at"] = "2026-08-02T14:00:00Z"
        record["score"][field] = corrected
        sync.run(START, END)
        current = store.current_sources(resource)
        assert len(current) == 2
        assert len({row["external_id"].lower() for row in current}) == 1
        result = CopilotService(store).analyze(metric, START, END, resource=resource)["result"]
        assert result["count"] == 2
        assert result["mean"] == (initial + corrected) / 2


def test_review_future_explicit_window_becomes_unearned_catch_up_checkpoint(tmp_path):
    """A future end accepted today can later skip a month of unfetched history."""
    clock = ["2026-09-01T12:00:00Z"]
    with Store(tmp_path / "fabricated-review.sqlite3", clock=lambda: clock[0]) as store:
        sync = SyncService(store, FabricatedClient())
        # The server cannot return September records that do not yet exist.
        done = sync.run("2026-08-01T12:00:00Z", "2026-10-01T12:00:00Z")
        assert done["status"] == "completed"
        clock[0] = "2026-10-02T12:00:00Z"
        plan = sync.plan_catch_up(*DashboardService(store).window(7))
        assert plan["coverage_through"] == timestamp("2026-10-01T12:00:00Z")
        assert plan["request"]["start"] == timestamp("2026-09-25T00:00:00Z")
        assert plan["request"]["start"] > timestamp("2026-09-01T12:00:00Z")


def test_review_unchanged_profile_body_still_create_revisions_and_invalidate(tmp_path):
    """Documented retrieval-time versions amplify the review's cache-growth risk."""
    clock = ["2026-09-01T12:00:00Z"]
    with Store(tmp_path / "fabricated-review.sqlite3", clock=lambda: clock[0]) as store:
        sync = SyncService(store, FabricatedClient())
        sync.run(START, END)
        analysis = CopilotService(store).analyze("whoop.strain", START, END, resource="workout")
        clock[0] = "2026-09-01T13:00:00Z"
        repeated = sync.run(START, END)
        assert repeated["ingestion"]["inserted"] == 2
        assert repeated["ingestion"]["duplicates"] == 4
        assert store.db.execute(
            "SELECT stale FROM analysis_runs WHERE id=?", (analysis["run_id"],)
        ).fetchone()[0] == 1
        assert {
            row[0]
            for row in store.db.execute(
                "SELECT resource FROM source_revisions WHERE id IN (?,?)",
                repeated["ingestion"]["revision_ids"],
            )
        } == {"profile", "body"}
