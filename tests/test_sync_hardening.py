"""Synthetic regressions for truthful request coverage and legacy sync checkpoints."""

import json

import pytest
from test_sync import Client

from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.storage import Store, canonical
from whoop_copilot.sync import SyncService

NOW = "2026-10-02T12:00:00Z"


def legacy_run(store, *, start, end, created, completed=None):
    """Model a persisted request from a version that allowed a future end."""
    identity = "fabricated-legacy-run"
    state = {
        "index": 6 if completed else 0,
        "next_token": None,
        "seen": [],
        "total": 0,
        "page_no": 0,
    }
    store.db.execute(
        "INSERT INTO sync_runs VALUES (?,?,?,?,?,?,?)",
        (
            identity,
            canonical({"start": timestamp(start), "end": timestamp(end)}),
            canonical(state),
            "completed" if completed else "paging",
            timestamp(created),
            timestamp(completed) if completed else None,
            None,
        ),
    )
    return identity


@pytest.mark.parametrize("catch_up", [False, True])
@pytest.mark.parametrize("if_stale", [False, True])
def test_new_future_window_is_rejected_before_staging_or_network(tmp_path, catch_up, if_stale):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        client = Client()
        with pytest.raises(ValueError, match="no later than now"):
            SyncService(store, client).run(
                "2026-10-01T00:00:00Z",
                "2026-10-02T12:00:00.000001Z",
                catch_up=catch_up,
                if_stale=if_stale,
            )
        assert client.calls == []
        assert store.db.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM sync_pages").fetchone()[0] == 0


def test_new_window_can_end_at_current_time(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        result = SyncService(store, Client()).run("2026-09-01T00:00:00Z", NOW)
        assert result["status"] == "completed"


def test_legacy_future_end_does_not_skip_history_after_first_fetch(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        legacy_run(
            store,
            start="2026-08-01T00:00:00Z",
            end="2026-10-01T12:00:00Z",
            created="2026-09-01T12:00:00Z",
            completed="2026-09-01T13:00:00Z",
        )
        service = SyncService(store, None)
        plan = service.plan_catch_up(*DashboardService(store).window(7))
        assert plan["coverage_through"] == timestamp("2026-09-01T12:00:00Z")
        assert plan["last_success_through"] == plan["coverage_through"]
        assert plan["request"]["start"] == timestamp("2026-08-26T00:00:00Z")
        freshness = service.freshness(**plan["request"])
        assert freshness["checked_through"] == plan["coverage_through"]
        assert freshness["state"] == "stale"


@pytest.mark.parametrize("earliest", ["end", "created", "completed"])
def test_planning_and_freshness_share_the_earliest_credible_end(tmp_path, earliest):
    values = dict.fromkeys(("end", "created", "completed"), "2026-10-02T12:00:00Z")
    values[earliest] = "2026-10-02T11:50:00Z"
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        legacy_run(store, start="2026-09-01T00:00:00Z", **values)
        service = SyncService(store, None)
        window = DashboardService(store).window(7)
        plan, freshness = service.plan_catch_up(*window), service.freshness(*window)
        expected = timestamp("2026-10-02T11:50:00Z")
        assert plan["coverage_through"] == freshness["checked_through"] == expected
        assert freshness["state"] == "fresh"
        # The nominal query reaches now, but it cannot prove a later start was checked.
        assert service.freshness("2026-10-02T11:55:00Z", NOW)["state"] == "uncovered"


def test_legacy_future_request_keeps_only_the_already_possible_slice(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        legacy_run(
            store,
            start="2026-09-01T00:00:00Z",
            end="2026-11-01T12:00:00Z",
            created="2026-10-02T11:50:00Z",
            completed="2026-10-02T11:55:00Z",
        )
        service = SyncService(store, None)
        window = DashboardService(store).window(7)
        assert service.plan_catch_up(*window)["coverage_through"] == timestamp(
            "2026-10-02T11:50:00Z"
        )
        assert service.freshness(*window)["state"] == "fresh"


@pytest.mark.parametrize("future_field", ["created", "completed"])
def test_future_local_timestamp_does_not_prove_coverage_after_clock_rollback(
    tmp_path, future_field
):
    values = dict.fromkeys(("end", "created", "completed"), NOW)
    values[future_field] = "2026-10-03T12:00:00Z"
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        legacy_run(store, start="2026-09-01T00:00:00Z", **values)
        service = SyncService(store, None)
        window = DashboardService(store).window(7)
        assert service.plan_catch_up(*window)["coverage_through"] is None
        assert service.freshness(*window)["state"] == "uncovered"


def test_entirely_future_legacy_request_proves_no_interval(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        legacy_run(
            store,
            start="2026-09-02T00:00:00Z",
            end="2026-10-01T00:00:00Z",
            created="2026-09-01T12:00:00Z",
            completed="2026-09-01T13:00:00Z",
        )
        service = SyncService(store, None)
        window = DashboardService(store).window(7)
        assert service.plan_catch_up(*window)["reason"] == "no_checkpoint"
        assert service.freshness(*window)["state"] == "uncovered"


def test_late_resume_preserves_legacy_window_without_promoting_coverage(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as store:
        identity = legacy_run(
            store,
            start="2026-08-01T00:00:00Z",
            end="2026-11-01T12:00:00Z",
            created="2026-09-01T12:00:00Z",
        )
        before = store.db.execute(
            "SELECT request FROM sync_runs WHERE id=?", (identity,)
        ).fetchone()[0]
        client = Client()
        service = SyncService(store, client)
        done = service.run(run_id=identity)
        assert done["status"] == "completed"
        assert done["request"] == json.loads(before)
        assert all(
            call[1:3] == tuple(done["request"][key] for key in ("start", "end"))
            for call in client.calls
        )
        assert service.plan_catch_up(*DashboardService(store).window(7))["coverage_through"] == (
            timestamp("2026-09-01T12:00:00Z")
        )
