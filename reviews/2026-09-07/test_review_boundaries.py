"""Reproduce an acknowledged synchronization limit, not desired regression behavior.

All records are fabricated; this never opens a user database or calls WHOOP.
"""

from test_sync import Client

from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService


def test_completed_rescan_does_not_propagate_upstream_absence(tmp_path):
    now = ["2026-08-03T12:00:00Z"]
    with Store(tmp_path / "audit.sqlite3", clock=lambda: now[0]) as store:
        client = Client()
        sync = SyncService(store, client)
        start, end = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"
        assert sync.run(start, end)["status"] == "completed"
        client.resources["workout"] = []
        now[0] = "2026-08-03T12:01:00Z"
        assert sync.run(start, end)["status"] == "completed"
        # Absence is correctly not assumed to be a deletion, but the current
        # implementation has no second-stage check to resolve this discrepancy.
        assert len(store.current_sources("workout")) == 1
        analysis = CopilotService(store).analyze(
            "whoop.strain", start, end, provider="whoop", resource="workout"
        )
        assert analysis["result"]["count"] == 1
