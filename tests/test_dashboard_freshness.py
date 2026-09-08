"""Conditional sync and recovery regressions: fabricated sources, clocks and keys only."""

import json
import threading
from datetime import datetime, timedelta

import pytest
from dashboard_scenarios import NOW, ScenarioClient
from test_dashboard import client, unlock

from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.storage import Store, canonical, restore_backup
from whoop_copilot.sync import SyncAlreadyRunning, SyncService, sync_lock
from whoop_copilot.web import DashboardRuntime


def shifted(seconds):
    return timestamp(
        (datetime.fromisoformat(timestamp(NOW)) + timedelta(seconds=seconds)).isoformat()
    )


def join_workers():
    for thread in threading.enumerate():
        if thread.name == "whoop-dashboard-sync":
            thread.join(5)
            assert not thread.is_alive()


@pytest.fixture
def setup(tmp_path):
    clock = [NOW]
    path = tmp_path / "synthetic.sqlite3"

    def factory():
        return Store(path, clock=lambda: clock[0])

    with factory():
        pass
    return factory, clock


def seed(factory, days=30, scenario="short-history"):
    with factory() as store:
        return SyncService(store, ScenarioClient(scenario)).run(
            *DashboardService(store).window(days)
        )


@pytest.mark.parametrize("scenario", ["short-history", "pending", "calibrating", "empty"])
def test_freshness_does_not_depend_on_record_days_or_scores(setup, scenario):
    factory, clock = setup
    seed(factory, scenario=scenario)
    runtime = DashboardRuntime(factory, client_factory=lambda: pytest.fail("Fresh: no client"))
    assert runtime.start_sync(7, if_stale=True) == {"accepted": False, "reason": "fresh"}
    assert runtime.status()["freshness"]["30"]["state"] == "fresh"
    clock[0] = shifted(1800)
    assert runtime.status()["freshness"]["7"]["state"] == "stale"


def test_window_coverage_searches_all_successes_and_uses_request_age(setup):
    factory, clock = setup
    clock[0] = shifted(-600)
    seed(factory, 30)
    clock[0] = NOW
    seed(factory, 7)
    runtime = DashboardRuntime(factory)
    status = runtime.status()
    assert status["freshness"]["30"]["checked_through"] == shifted(-600)
    assert status["freshness"]["30"]["state"] == "fresh"
    assert status["freshness"]["7"]["checked_through"] == timestamp(NOW)
    clock[0] = shifted(1200)
    assert runtime.status()["freshness"]["30"]["state"] == "stale"
    assert runtime.status()["freshness"]["7"]["state"] == "fresh"


def test_seven_days_cannot_certify_thirty_days_and_clock_rollback_is_not_fresh(setup):
    factory, clock = setup
    seed(factory, 7)
    runtime = DashboardRuntime(factory)
    assert runtime.status()["freshness"]["30"]["state"] == "uncovered"
    clock[0] = shifted(-1)
    assert runtime.status()["freshness"]["7"]["state"] != "fresh"


def test_midnight_does_not_force_duplicate_if_history_start_is_covered(setup):
    factory, clock = setup
    clock[0] = "2026-09-07T23:59:00Z"
    seed(factory, 30)
    clock[0] = "2026-09-08T00:01:00Z"
    assert DashboardRuntime(factory).status()["freshness"]["7"]["state"] == "fresh"


def test_late_resume_is_completed_but_not_fresh(setup):
    factory, clock = setup
    clock[0] = shifted(-3600)
    with factory() as store:
        service = SyncService(store, ScenarioClient("short-history"))
        run = service.run(*DashboardService(store).window(7), max_pages=2)
    clock[0] = NOW
    with factory() as store:
        SyncService(store, ScenarioClient("short-history")).run(run_id=run["run_id"])
    status = DashboardRuntime(factory).status()
    assert status["last_run"]["status"] == "completed"
    assert status["freshness"]["7"]["state"] == "stale"


def test_two_runtimes_observe_one_worker_and_reopened_runtime_skips_success(setup):
    factory, _ = setup
    entered, release = threading.Event(), threading.Event()
    calls = []

    class SlowClient(ScenarioClient):
        def list_records(self, resource, *args):
            calls.append(resource)
            if resource == "profile":
                entered.set()
                assert release.wait(5)
            return super().list_records(resource, *args)

    a = DashboardRuntime(factory, client_factory=lambda: SlowClient("empty"))
    b = DashboardRuntime(factory, client_factory=lambda: pytest.fail("Duplicate client"))
    try:
        assert a.start_sync(7, if_stale=True)["accepted"]
        assert entered.wait(5)
        assert b.status()["running"]
        assert b.start_sync(7, if_stale=True)["already_running"]
    finally:
        release.set()
        join_workers()
    assert calls == ["profile", "body", "cycle", "recovery", "sleep", "workout"]
    assert b.start_sync(7, if_stale=True)["reason"] == "fresh"


def test_conditional_decision_is_rechecked_under_service_lock(setup):
    factory, _ = setup
    with factory() as store:
        service = SyncService(store, None)
        query = DashboardService(store).window(7)
        assert service.freshness(*query)["state"] == "never"
        seed(factory, 30)  # Another opener finishes after an earlier status check.
        assert service.run(*query, if_stale=True) == {"skipped": "fresh"}
        with sync_lock(store.path.with_suffix(".sync.lock")):
            with pytest.raises(SyncAlreadyRunning):
                service.run(*query, if_stale=True)


@pytest.mark.parametrize("failure", ["network", "factory", "page_budget"])
def test_failed_or_paused_run_blocks_auto_until_explicit_resume(setup, failure):
    factory, clock = setup
    seed(factory, 30)
    clock[0] = shifted(3600)

    def failed_factory():
        if failure == "factory":
            raise RuntimeError("fabricated-sensitive-value")
        return ScenarioClient("short-history", fail=True)

    runtime = DashboardRuntime(factory, client_factory=failed_factory)
    if failure == "page_budget":
        with factory() as store:
            SyncService(store, ScenarioClient("short-history")).run(
                *DashboardService(store).window(7), max_pages=2
            )
    else:
        runtime.start_sync(7, if_stale=True)
        join_workers()
    status = runtime.status()
    original = status["last_run"]
    assert original["status"] == "paging"
    assert "fabricated-sensitive-value" not in json.dumps(status)
    reopened = DashboardRuntime(factory, client_factory=lambda: pytest.fail("No automatic retry"))
    assert reopened.start_sync(30, if_stale=True)["reason"] == "unfinished"
    with factory() as store:
        assert DashboardService(store).overview("hrv", 30)["trend"]["records"]
    reopened.client_factory = lambda: ScenarioClient("short-history")
    reopened.start_sync(30, resume=original["run_id"])
    join_workers()
    final = reopened.status()["last_run"]
    assert final["status"] == "completed" and final["request"] == original["request"]
    assert reopened.start_sync(7, resume="no-longer-existing")["reason"] == "checkpoint_unavailable"


def test_manual_new_window_recovers_without_reusing_paused_query(setup):
    factory, _ = setup
    with factory() as store:
        old = SyncService(store, ScenarioClient("empty")).run(
            *DashboardService(store).window(7), max_pages=1
        )
    runtime = DashboardRuntime(factory, client_factory=lambda: ScenarioClient("empty"))
    runtime.start_sync(30)
    join_workers()
    status = runtime.status()
    assert status["last_run"]["run_id"] != old["run_id"]
    assert status["freshness"]["30"]["state"] == "fresh"


def test_clock_rollback_new_failure_still_blocks_auto_and_another_runtime_clears_error(setup):
    factory, clock = setup
    seed(factory)
    clock[0] = shifted(-60)
    a = DashboardRuntime(factory, client_factory=lambda: ScenarioClient("empty", fail=True))
    a.start_sync(7)
    join_workers()
    paused = a.status()
    assert paused["last_run"]["status"] == "paging" and paused["error"]
    b = DashboardRuntime(factory, client_factory=lambda: ScenarioClient("empty"))
    assert b.start_sync(7, if_stale=True)["reason"] == "unfinished"
    clock[0] = shifted(60)
    b.start_sync(7, resume=paused["last_run"]["run_id"])
    join_workers()
    assert a.status()["last_run"]["status"] == "completed"
    assert a.status()["error"] is None


@pytest.mark.parametrize("connected", [False, "error"])
def test_authorization_blocks_requests_but_restored_authorization_allows_sync(setup, connected):
    factory, _ = setup

    def oauth_status():
        if connected == "error":
            raise RuntimeError("fabricated-sensitive-oauth-error")
        return {"connected": connected}

    runtime = DashboardRuntime(
        factory, client_factory=lambda: pytest.fail("Unauthorized"), oauth_status=oauth_status
    )
    assert runtime.start_sync(7, if_stale=True)["reason"] == "authorization"
    assert not runtime.status()["can_sync"]
    runtime.oauth_status = lambda: {"connected": True}
    runtime.client_factory = lambda: ScenarioClient("empty")
    runtime.start_sync(7, if_stale=True)
    join_workers()
    assert runtime.status()["freshness"]["7"]["state"] == "fresh"


@pytest.mark.parametrize(
    "body",
    [{"if_stale": "true"}, {"if_stale": 1}, {"if_stale": True, "resume": "id"}, {"resume": ""}],
)
async def test_conditional_http_validation(setup, body):
    factory, _ = setup
    runtime = DashboardRuntime(factory, client_factory=lambda: pytest.fail("Invalid request"))
    async with client(runtime) as http:
        headers = await unlock(http)
        assert (await http.post("/api/sync", json=body, headers=headers)).status_code == 400


async def test_status_get_never_syncs_and_conditional_post_reuses_csrf_boundary(setup):
    factory, _ = setup
    seed(factory)
    runtime = DashboardRuntime(factory, client_factory=lambda: pytest.fail("No request needed"))
    async with client(runtime) as http:
        headers = await unlock(http)
        assert (await http.get("/api/status")).status_code == 200
        assert (await http.post("/api/sync", json={"if_stale": True})).status_code == 403
        result = await http.post("/api/sync", json={"if_stale": True}, headers=headers)
        assert result.json() == {"accepted": False, "reason": "fresh"}


def test_first_page_failure_expires_and_managed_backup_and_restore_follow_policy(tmp_path):
    key, clock = bytes(range(32)), [NOW]
    path, backup = tmp_path / "encrypted.sqlite3", tmp_path / "managed.sqlite3"

    def factory():
        return Store(path, environment="real", encryption_key=key, clock=lambda: clock[0])

    with Store(
        path,
        environment="real",
        encryption_key=key,
        clock=lambda: clock[0],
        policy=LocalPolicy(1, owner_authorized=True),
    ) as store:
        store.db.execute(
            "INSERT INTO sync_runs VALUES (?,?,?,?,?,?,?)",
            (
                "expired-no-pages",
                canonical(dict(zip(("start", "end"), DashboardService(store).window(7)))),
                canonical({"index": 0, "total": 0}),
                "paging",
                timestamp(NOW),
                None,
                "safe error",
            ),
        )
        store.backup(backup)
    clock[0] = shifted(86400)
    with pytest.raises(ValueError, match="expired"):
        restore_backup(
            backup,
            tmp_path / "restore.sqlite3",
            environment="real",
            encryption_key=key,
            clock=lambda: clock[0],
        )
    runtime = DashboardRuntime(
        factory,
        client_factory=lambda: pytest.fail("No real WHOOP"),
        oauth_status=lambda: {"connected": True},
    )
    status = runtime.status()
    assert status["last_run"] is None and not backup.exists()
    assert status["freshness"]["7"]["state"] == "never"
