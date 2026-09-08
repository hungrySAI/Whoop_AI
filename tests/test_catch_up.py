"""Long-absence regressions using fabricated timestamps, API data and encryption keys."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from dashboard_scenarios import ScenarioClient, scenario_resources
from test_sync import Client

from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.storage import Store, canonical
from whoop_copilot.sync import SyncAlreadyRunning, SyncService, sync_lock
from whoop_copilot.web import DashboardRuntime, DeferredClient

NOW = "2026-09-07T12:00:00Z"


def at(days=0, *, hours=0, midnight=False):
    value = datetime.fromisoformat(timestamp(NOW)) + timedelta(days=days, hours=hours)
    if midnight:
        value = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return timestamp(value.isoformat())


def success(store, start, end, *, created=None, completed=None, pending=False):
    """Seed only request metadata; its coverage must not require health records."""
    identity = f"fabricated-{store.db.execute('SELECT COUNT(*) FROM sync_runs').fetchone()[0]}"
    store.db.execute(
        "INSERT INTO sync_runs VALUES (?,?,?,?,?,?,?)",
        (
            identity,
            canonical({"start": timestamp(start), "end": timestamp(end)}),
            canonical({"index": 0 if pending else 6, "total": 0}),
            "paging" if pending else "completed",
            timestamp(created or end),
            None if pending else timestamp(completed or end),
            None,
        ),
    )
    return identity


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "fabricated.sqlite3", clock=lambda: NOW) as current:
        yield current


@pytest.mark.parametrize("absence", [3, 45, 120])
def test_reopening_bridges_absence_from_request_end_with_calendar_overlap(store, absence):
    success(store, at(-absence - 20), at(-absence))
    plan = SyncService(store, None).plan_catch_up(*DashboardService(store).window(7))
    assert plan["request"] == {"start": at(-absence - 6, midnight=True), "end": at()}
    assert plan["last_success_through"] == plan["coverage_through"] == at(-absence)
    assert plan["expanded"] and plan["reason"] == "since_success"
    assert not plan["lookback_limited"]


@pytest.mark.parametrize("days", [7, 30])
def test_absent_history_uses_bounded_rescan_independent_of_view(store, days):
    plan = SyncService(store, None).plan_catch_up(*DashboardService(store).window(days))
    assert plan["request"] == {"start": at(-366), "end": at()}
    assert plan["reason"] == "no_checkpoint"
    assert plan["last_success_through"] is plan["coverage_through"] is None
    assert plan["lookback_limited"] and plan["lookback_days"] == 366


def test_window_selection_can_expand_earlier_than_recent_overlap(store):
    success(store, at(-20), at(-1))
    query = DashboardService(store).window(30)
    plan = SyncService(store, None).plan_catch_up(*query)
    assert plan["request"] == dict(zip(("start", "end"), query))
    assert not plan["expanded"]


@pytest.mark.parametrize("gap_hours", [0, -12])
def test_touching_or_overlapping_requests_form_continuous_coverage(store, gap_hours):
    success(store, at(-90), at(-60))
    success(store, at(-60, hours=gap_hours), at(-30))
    success(store, at(-30), at(-1))
    # A late completion of an older contained query must not regress this coverage.
    success(store, at(-80), at(-70), completed=at())
    plan = SyncService(store, None).plan_catch_up(*DashboardService(store).window(7))
    assert plan["coverage_through"] == plan["last_success_through"] == at(-1)
    assert plan["request"]["start"] == at(-7, midnight=True)
    assert plan["reason"] == "since_success"


def test_recent_narrow_success_does_not_hide_an_earlier_known_gap(store):
    success(store, at(-70), at(-45))
    success(store, at(-6, midnight=True), at())
    service = SyncService(store, None)
    view = DashboardService(store).window(7)
    assert service.freshness(*view)["state"] == "fresh"
    plan = service.plan_catch_up(*view)
    assert plan["reason"] == "history_gap"
    assert plan["coverage_through"] == at(-45)
    assert plan["last_success_through"] == at()
    assert plan["request"]["start"] == at(-51, midnight=True)
    assert service.freshness(**plan["request"])["state"] != "fresh"


def test_bounded_rescan_keeps_known_gap_when_older_interval_precedes_floor(store):
    success(store, at(-600), at(-500))
    success(store, at(-6, midnight=True), at())
    plan = SyncService(store, None).plan_catch_up(*DashboardService(store).window(7))
    assert plan["request"]["start"] == at(-366)
    assert plan["reason"] == "history_gap" and plan["lookback_limited"]


@pytest.mark.parametrize("old_end", [-370, -366, -364])
def test_long_absence_never_exceeds_366_day_request_cap(store, old_end):
    success(store, at(old_end - 20), at(old_end))
    plan = SyncService(store, None).plan_catch_up(*DashboardService(store).window(7))
    assert plan["request"]["start"] == at(-366)
    assert plan["lookback_limited"]
    if old_end < -366:
        assert plan["reason"] == "lookback_limit"


@pytest.mark.parametrize("field", ["request_end", "created", "completed"])
def test_clock_rollback_ignores_future_success_timestamps(store, field):
    kwargs = {field: at(1)} if field != "request_end" else {}
    success(store, at(-6), at(1) if field == "request_end" else at(), **kwargs)
    success(store, at(-60), at(-40))
    plan = SyncService(store, None).plan_catch_up(*DashboardService(store).window(7))
    assert plan["last_success_through"] == plan["coverage_through"] == at(-40)
    assert plan["request"]["start"] == at(-46, midnight=True)


def test_late_old_resume_uses_original_request_end_instead_of_completion(store):
    success(store, at(-90), at(-45), created=at(-45), completed=at())
    service = SyncService(store, None)
    plan = service.plan_catch_up(*DashboardService(store).window(7))
    assert plan["coverage_through"] == at(-45)
    assert plan["request"]["start"] == at(-51, midnight=True)
    assert service.freshness(**plan["request"])["state"] == "stale"


@pytest.mark.parametrize("scenario", ["empty", "pending", "calibrating", "short-history"])
def test_complete_request_coverage_does_not_depend_on_health_record_counts(store, scenario):
    service = SyncService(store, ScenarioClient(scenario))
    query = DashboardService(store).window(7)
    first = service.run(*query, catch_up=True)
    assert first["status"] == "completed"
    assert first["request"]["start"] == at(-366)
    assert first["catch_up"]["request"] == first["request"]
    assert service.run(*query, catch_up=True, if_stale=True) == {"skipped": "fresh"}


@pytest.mark.parametrize("retention", [1, 30])
def test_fetch_lookback_does_not_reinterpret_capture_retention(tmp_path, retention):
    clock = [NOW]
    with Store(
        tmp_path / "fabricated-encrypted.sqlite3",
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(retention, owner_authorized=True),
        clock=lambda: clock[0],
    ) as store:
        service = SyncService(store, Client())
        done = service.run(*DashboardService(store).window(7), catch_up=True)
        assert done["request"]["start"] == at(-366)
        # The fixture is older than a 1-day policy, but was captured just now.
        before = store.db.execute(
            "SELECT expires_at FROM source_revisions WHERE resource='sleep'"
        ).fetchone()[0]
        assert before == at(retention)
        clock[0] = at(hours=1)
        service.run(*DashboardService(store).window(7), catch_up=True)
        sleeps = store.db.execute(
            "SELECT expires_at FROM source_revisions WHERE resource='sleep'"
        ).fetchall()
        assert [row[0] for row in sleeps] == [before]


def test_expired_success_history_is_purged_before_execution_chooses_rescan(tmp_path):
    clock = [at(-2)]
    with Store(
        tmp_path / "fabricated-expiring.sqlite3",
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: clock[0],
    ) as store:
        service = SyncService(store, Client())
        previous = service.run(*DashboardService(store).window(7))
        clock[0] = NOW
        # A stale advisory plan sees the retained request; run must purge first.
        assert (
            service.plan_catch_up(*DashboardService(store).window(7))["reason"] != "no_checkpoint"
        )
        current = service.run(*DashboardService(store).window(7), catch_up=True)
        assert current["request"]["start"] == at(-366)
        assert current["catch_up"]["reason"] == "no_checkpoint"
        with pytest.raises(ValueError, match="expired"):
            service.status(previous["run_id"])


def test_conditional_plan_is_recomputed_after_another_request_wins_the_lock(store, monkeypatch):
    service = SyncService(store, None)
    query = DashboardService(store).window(7)
    assert service.plan_catch_up(*query)["reason"] == "no_checkpoint"

    @contextmanager
    def another_opener_finishes(path):
        with sync_lock(path):
            # Simulate completion between the status preview and lock acquisition.
            success(store, at(-366), at())
            yield

    monkeypatch.setattr("whoop_copilot.sync.sync_lock", another_opener_finishes)
    assert service.run(*query, catch_up=True, if_stale=True) == {"skipped": "fresh"}
    assert store.db.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 1


def test_service_lock_prevents_catch_up_planning_and_network_access(store, monkeypatch):
    service = SyncService(store, None)

    def must_not_plan(*args):
        pytest.fail("Planning must wait until the existing sync owner releases its lock")

    monkeypatch.setattr(service, "plan_catch_up", must_not_plan)
    with sync_lock(store.path.with_suffix(".sync.lock")):
        with pytest.raises(SyncAlreadyRunning):
            service.run(*DashboardService(store).window(7), catch_up=True)


@pytest.mark.parametrize("failure", ["network", "factory", "page_budget"])
def test_catch_up_failure_preserves_checkpoint_without_reopen_retry(store, failure):
    def broken_factory():
        raise RuntimeError("fabricated-sensitive-factory-error")

    client = (
        DeferredClient(broken_factory)
        if failure == "factory"
        else Client(fail_resource="sleep" if failure == "network" else None)
    )
    service = SyncService(store, client)
    query = DashboardService(store).window(7)
    if failure == "page_budget":
        paused = service.run(*query, catch_up=True, max_pages=2)
    else:
        with pytest.raises(ValueError, match="paused"):
            service.run(*query, catch_up=True)
        paused = service.latest()
    assert paused["request"]["start"] == at(-366)
    assert paused["status"] == "paging"
    assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
    assert "fabricated-sensitive" not in json.dumps(paused)
    assert SyncService(store, None).run(*query, catch_up=True, if_stale=True) == {
        "skipped": "unfinished"
    }
    store.clock = lambda: at(5)
    resumed = SyncService(store, Client()).run(run_id=paused["run_id"])
    assert resumed["status"] == "completed"
    assert resumed["request"] == paused["request"]
    assert resumed["catch_up"] == paused["catch_up"]
    assert resumed["request"]["end"] == at()


@pytest.mark.parametrize("value", [None, 0, 1, "true", {}, []])
def test_catch_up_option_requires_boolean_before_client_construction(store, value):
    with pytest.raises(ValueError, match="Catch-up"):
        SyncService(store, None).run(*DashboardService(store).window(7), catch_up=value)
    assert store.db.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


def test_resume_cannot_replan_or_change_catch_up_request(store):
    service = SyncService(store, Client())
    paused = service.run(*DashboardService(store).window(7), catch_up=True, max_pages=1)
    with pytest.raises(ValueError, match="Catch-up"):
        service.run(run_id=paused["run_id"], catch_up=True)
    with pytest.raises(ValueError, match="preserve"):
        service.run(start=at(-30), run_id=paused["run_id"])
    assert service.status(paused["run_id"])["request"] == paused["request"]


@pytest.mark.parametrize("start,end", [(at(), at()), (at(1), at()), (at(-7), at(1))])
def test_catch_up_rejects_empty_reversed_and_future_windows(store, start, end):
    with pytest.raises(ValueError, match="positive window"):
        SyncService(store, None).plan_catch_up(start, end)


def test_explicit_cli_window_remains_exact_when_catch_up_is_not_requested(store):
    requested = {"start": at(-45), "end": at(-40)}
    result = SyncService(store, Client()).run(**requested)
    assert result["request"] == requested
    assert result["catch_up"] is None


def test_overlap_correction_creates_one_current_revision_and_replays_idempotently(store):
    client = Client(scenario_resources("short-history"))
    record_count = sum(map(len, client.resources.values()))
    sleep_count = len(client.resources["sleep"])
    service = SyncService(store, client)
    query = DashboardService(store).window(7)
    service.run(*query, catch_up=True)
    client.resources["sleep"][0]["updated_at"] = at()
    client.resources["sleep"][0]["score"]["sleep_performance_percentage"] = 87
    corrected_id = client.resources["sleep"][0]["id"]
    corrected = service.run(*query, catch_up=True)
    assert corrected["ingestion"]["inserted"] == 1
    assert corrected["ingestion"]["duplicates"] == record_count - 1
    rows = [row for row in store.current_sources("sleep") if row["external_id"] == corrected_id]
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["record"]["score"]["sleep_performance_percentage"] == 87
    replay = service.run(*query, catch_up=True)
    assert replay["ingestion"]["inserted"] == 0
    assert replay["ingestion"]["duplicates"] == record_count
    assert (
        store.db.execute("SELECT COUNT(*) FROM source_revisions WHERE resource='sleep'").fetchone()[
            0
        ]
        == sleep_count + 1
    )


def test_catch_up_keeps_related_cycle_fetch_across_collection_boundary(store):
    resources = Client().resources
    resources["cycle"] = []
    service = SyncService(store, Client(resources))
    paused = service.run(*DashboardService(store).window(7), catch_up=True, max_pages=6)
    assert paused["status"] == "paging"
    assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
    resumed = service.run(run_id=paused["run_id"], max_pages=1)
    assert resumed["request"] == paused["request"]
    assert resumed["status"] == "completed"
    assert len(store.current_sources("cycle")) == 1


def test_dashboard_freshness_uses_catch_up_plan_when_current_view_is_already_fresh(tmp_path):
    path = tmp_path / "fabricated-dashboard.sqlite3"

    def factory():
        return Store(path, clock=lambda: NOW)

    with factory() as store:
        success(store, at(-70), at(-45))
        success(store, at(-6, midnight=True), at())
        assert (
            SyncService(store, None).freshness(*DashboardService(store).window(7))["state"]
            == "fresh"
        )
    status = DashboardRuntime(factory).status()
    assert status["freshness"]["7"]["state"] != "fresh"
    assert status["sync_plan"]["7"]["reason"] == "history_gap"
    assert status["sync_plan"]["7"]["request"]["start"] == at(-51, midnight=True)
