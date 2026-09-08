"""Local dashboard contracts and browser boundaries; all records and keys are fabricated."""

import copy
import json
import re
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DASHBOARD_METRICS, DashboardService
from whoop_copilot.dashboard_demo import seed_demo
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.web import DashboardRuntime, create_app, run_dashboard

FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
NOW = "2026-08-03T12:00:00Z"
ORIGIN = "http://127.0.0.1:8766"


def resources():
    return json.loads(FIXTURE.read_text())["resources"]


def ingest(store, data):
    return store.ingest(
        normalize_api(data, acquired_at=store.clock(), synthetic=store.environment == "synthetic")
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "dashboard.sqlite3", clock=lambda: NOW) as store:
        ingest(store, resources())
        yield store


@pytest.mark.parametrize("key", DASHBOARD_METRICS)
@pytest.mark.parametrize("days", [7, 30])
def test_views_reuse_registered_statistics_and_reproduce(store, key, days):
    dashboard = DashboardService(store)
    view = dashboard.overview(key, days)
    assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
    definition = DASHBOARD_METRICS[key]
    analysis = CopilotService(store).analyze(
        definition.metric,
        view["start"],
        view["end"],
        provider="whoop",
        resource=definition.resource,
    )
    assert view["trend"]["summary"] == analysis["result"]
    assert view["trend"]["analysis_id"] is None
    assert CopilotService(store).reproduce(analysis["run_id"])["matches"]
    assert view["timezone"] == "UTC"
    # No full evidence bundle, account identity or raw WHOOP response crosses this adapter.
    serialized = json.dumps(view)
    for forbidden in ("fixture@example.invalid", "user_id", "content_hash", '"payload"', '"email"'):
        assert forbidden not in serialized


@pytest.mark.parametrize("score_state", ["PENDING_SCORE", "UNSCORABLE", "calibrating"])
def test_latest_unscored_does_not_fall_back_to_old_valid_value(store, score_state):
    data = resources()
    cycle = copy.deepcopy(data["cycle"][0])
    cycle.update(id=900002, start="2026-08-02T13:00:00Z", end=None)
    recovery = copy.deepcopy(data["recovery"][0])
    recovery["cycle_id"] = 900002
    if score_state == "calibrating":
        recovery["score"]["user_calibrating"] = True
    else:
        recovery["score_state"] = score_state
        recovery.pop("score")
    data["cycle"].append(cycle)
    data["recovery"].append(recovery)
    ingest(store, data)
    view = DashboardService(store).overview("hrv")
    for metric in (view["trend"], view["cards"][0]):
        assert metric["latest"]["status"] == score_state
        assert metric["latest"]["value"] is None
        assert metric["summary"]["count"] == 1
        assert any(point["value"] is None for point in metric["points"])


def test_missing_metric_and_gaps_are_explicit_and_strain_resources_separate(store):
    dashboard = DashboardService(store)
    sleep = dashboard.overview("sleep")["trend"]
    assert sleep["latest"]["status"] == "missing_metric"
    assert sleep["latest"]["value"] is None
    assert sleep["summary"]["mean"] is None
    assert sleep["missing_days"] == 6
    assert len([point for point in sleep["points"] if point["status"] == "missing"]) == 6
    cycle = dashboard.overview("strain")["trend"]
    workout = dashboard.overview("workout")["trend"]
    assert cycle["summary"]["mean"] == 8
    assert workout["summary"]["mean"] == 8.2463


def test_evidence_distinguishes_three_times_and_current_version_then_delete(store):
    service = DashboardService(store)
    before = service.overview()["trend"]["latest"]
    original = service.evidence("hrv", before["revision_id"])
    assert original["measured_at"] == timestamp("2026-08-01T13:00:00Z")
    assert original["source_updated_at"] == timestamp("2026-08-02T13:15:00Z")
    assert original["metric_updated_at"] == timestamp("2026-08-01T13:10:00Z")
    assert original["interval_updated_at"] == timestamp("2026-08-02T13:15:00Z")
    assert original["known_at"] == original["captured_at"] == timestamp(NOW)
    assert original["original_value"] == original["value"] == 40
    assert original["source_timezone"] == "-07:00"
    data = resources()
    data["recovery"][0]["updated_at"] = "2026-08-03T10:00:00Z"
    data["recovery"][0]["score"]["hrv_rmssd_milli"] = 52
    ingest(store, data)
    assert service.overview()["trend"]["latest"]["value"] == 52
    assert service.evidence("hrv", before["revision_id"])["is_current"] is False
    # A late old response must not replace the source selected by existing ordering rules.
    ingest(store, resources())
    assert service.overview()["trend"]["latest"]["value"] == 52
    record = next(
        r for r in normalize_api(data, acquired_at=NOW, synthetic=True) if r.resource == "recovery"
    )
    store.ingest(
        [
            replace(
                record,
                deleted=True,
                source_updated_at="2026-08-03T11:00:00Z",
                observations=(),
                activities=(),
                payload={"synthetic": True, "deleted": True},
                metadata={
                    **record.metadata,
                    "source_version_parts": {
                        "recovery": timestamp("2026-08-03T11:00:00Z"),
                        "cycle": timestamp("2026-08-02T13:15:00Z"),
                    },
                },
            )
        ]
    )
    assert service.overview()["trend"]["records"] == []


def test_same_day_workouts_are_not_averaged_into_a_fake_official_point(store):
    data = resources()
    other = copy.deepcopy(data["workout"][0])
    other.update(
        id="ecfc6a15-4661-442f-a9a4-f160dd7afae9",
        start="2026-08-01T15:00:00Z",
        end="2026-08-01T16:00:00Z",
    )
    other["score"]["strain"] = 12
    data["workout"].append(other)
    ingest(store, data)
    trend = DashboardService(store).overview("workout")["trend"]
    assert [r["value"] for r in trend["records"]] == [8.2463, 12]
    assert trend["summary"]["count"] == 2
    assert trend["missing_days"] == 6


def test_cleanup_during_view_does_not_mix_deleted_card_with_current_trend(tmp_path, monkeypatch):
    with Store(
        tmp_path / "protected.sqlite3",
        environment="real",
        encryption_key=bytes(range(32)),
        clock=lambda: NOW,
        policy=LocalPolicy(90, owner_authorized=True),
    ) as store:
        ingest(store, resources())
        service = DashboardService(store)
        original = service._metric_view
        calls = []

        def cleanup_after_first_metric(*args):
            result = original(*args)
            if not calls:
                # Expire a lower revision ID during the read; MAX(id) stays unchanged.
                store.db.execute(
                    "UPDATE source_revisions SET expires_at=? WHERE resource='recovery'",
                    (timestamp(NOW),),
                )
                store.purge_expired()
            calls.append(args[0])
            return result

        monkeypatch.setattr(service, "_metric_view", cleanup_after_first_metric)
        view = service.overview()
        assert view["cards"][0]["latest"] is None
        assert view["trend"]["summary"]["count"] == 0
        assert len(calls) > 4


@pytest.mark.parametrize("days", [0, 8, 31, True, "7"])
def test_window_rejects_unbounded_or_ambiguous_input(store, days):
    with pytest.raises(ValueError):
        DashboardService(store).overview(days=days)


def test_demo_is_packaged_fabricated_and_never_allowed_in_real(store, tmp_path):
    seed_demo(store)
    assert DashboardService(store).overview(days=30)["trend"]["summary"]["count"] > 20
    with Store(
        tmp_path / "protected.sqlite3",
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(1, owner_authorized=True),
    ) as real:
        with pytest.raises(ValueError, match="synthetic"):
            seed_demo(real)
        assert real.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0


def test_real_read_purges_expired_source_and_derived_evidence(tmp_path):
    clock = [NOW]
    with Store(
        tmp_path / "protected.sqlite3",
        clock=lambda: clock[0],
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(1, owner_authorized=True),
    ) as store:
        ingest(store, resources())
        service = DashboardService(store)
        before = service.overview()["trend"]
        assert before["summary"]["count"] == 1
        explicit = CopilotService(store).analyze(
            "whoop.hrv_rmssd", *service.window(7), provider="whoop", resource="recovery"
        )
        clock[0] = "2026-08-04T13:00:00Z"
        assert service.overview()["trend"]["summary"]["count"] == 0
        with pytest.raises(ValueError, match="unavailable"):
            service.evidence("hrv", before["latest"]["revision_id"])
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM analysis_runs WHERE id=?", (explicit["run_id"],)
            ).fetchone()[0]
            == 0
        )


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "http.sqlite3"
    with Store(path, clock=lambda: NOW) as store:
        ingest(store, resources())
    return DashboardRuntime(lambda: Store(path, clock=lambda: NOW))


def client(runtime, *, address="127.0.0.1", raise_errors=True):
    # HTTPX ASGI transport exercises HTTP handlers without browser UI automation.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(runtime), client=(address, 1234), raise_app_exceptions=raise_errors
        ),
        base_url=ORIGIN,
    )


async def unlock(http):
    home = await http.get("/")
    assert home.status_code == 200
    assert "HttpOnly" in home.headers["set-cookie"]
    assert "SameSite=strict" in home.headers["set-cookie"]
    return {
        "Origin": ORIGIN,
        "X-Whoop-CSRF": re.search('name="whoop-csrf" content="([^"]+)"', home.text)[1],
    }


async def test_http_session_curated_evidence_and_static_assets(runtime):
    async with client(runtime) as http:
        assert (await http.get("/api/dashboard")).status_code == 401
        assert (await http.get("/api/status")).status_code == 401
        await unlock(http)
        response = await http.get("/api/dashboard?days=30&metric=hrv")
        assert response.status_code == 200
        revision = response.json()["trend"]["latest"]["revision_id"]
        detail = await http.get(f"/api/evidence?metric=hrv&revision={revision}")
        assert detail.json()["is_current"]
        assert "fixture@example.invalid" not in response.text + detail.text
        for asset in ("app.css", "app.js", "chart.js"):
            loaded = await http.get("/assets/" + asset)
            assert loaded.status_code == 200 and len(loaded.content) > 1000
        status = (await http.get("/api/status")).json()
        assert status["environment"] == "synthetic" and not status["sync_enabled"]
        assert status["connected"] is None


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.test:8766"},
        {"Host": "localhost:8766"},
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
async def test_rebinding_and_cross_site_rejected_before_store_access(headers):
    def forbidden():
        pytest.fail("Rejected request must not read the protected database")

    async with client(DashboardRuntime(forbidden)) as http:
        for path in ("/", "/api/dashboard", "/api/status", "/assets/app.js"):
            assert (await http.get(path, headers=headers)).status_code == 403


async def test_non_loopback_peer_rejected(runtime):
    async with client(runtime, address="192.0.2.1") as http:
        assert (await http.get("/")).status_code == 403


async def test_sync_requires_cookie_exact_origin_csrf_and_bounded_json(runtime):
    calls = []
    runtime.start_sync = lambda days, resume: calls.append((days, resume)) or {"accepted": True}
    async with client(runtime) as http:
        assert (
            await http.post("/api/sync", json={"days": 7}, headers={"Origin": ORIGIN})
        ).status_code == 401
        headers = await unlock(http)
        assert (await http.post("/api/sync", json={"days": 7})).status_code == 403
        assert (
            await http.post("/api/sync", json={"days": 7}, headers={"Origin": ORIGIN})
        ).status_code == 403
        assert (
            await http.post(
                "/api/sync", json={"days": 7}, headers={**headers, "Origin": "https://evil.test"}
            )
        ).status_code == 403
        assert (await http.post("/api/sync", content="text", headers=headers)).status_code == 415
        assert (
            await http.post(
                "/api/sync", json={"days": 7, "arbitrary_path": "secret"}, headers=headers
            )
        ).status_code == 400
        assert (
            await http.post(
                "/api/sync",
                content="x" * 2050,
                headers={**headers, "Content-Type": "application/json"},
            )
        ).status_code == 413
        assert not calls
        response = await http.post(
            "/api/sync", json={"days": 30, "resume": "saved-run"}, headers=headers
        )
        assert response.status_code == 202 and calls == [(30, "saved-run")]


@pytest.mark.parametrize(
    "path,code",
    [
        ("/api/dashboard?days=365", 400),
        ("/api/dashboard?metric=weight", 400),
        ("/api/evidence?metric=hrv&revision=abc", 404),
        ("/api/evidence?metric=body&revision=1", 404),
        ("/api/sql", 404),
        ("/assets/../runtime/real/copilot.sqlite3", 404),
        ("/assets/SOURCE.json", 404),
        ("/runtime/real/oauth-config.json", 404),
    ],
)
async def test_invalid_requests_no_generic_file_api_and_security_headers(runtime, path, code):
    async with client(runtime) as http:
        await unlock(http)
        response = await http.get(path)
        assert response.status_code == code
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert "access-control-allow-origin" not in response.headers


async def test_unexpected_errors_are_generic(runtime):
    def broken():
        raise RuntimeError("sensitive-fabricated-response")

    runtime.store_factory = broken
    async with client(runtime) as http:
        await unlock(http)
        response = await http.get("/api/dashboard")
        assert response.status_code == 503
        assert "sensitive-fabricated-response" not in response.text


def test_sync_worker_owns_store_deduplicates_and_resumes_after_failure(runtime):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    data, failed = resources(), [True]

    class Client:
        def list_records(self, resource, *_):
            if resource == "profile":
                entered.set()
                assert release.wait(5)
            if resource == "sleep" and failed[0]:
                raise RuntimeError("sensitive-fabricated-response")
            return {"records": copy.deepcopy(data[resource]), "next_token": None, "headers": {}}

    runtime.client_factory = Client
    original_factory = runtime.store_factory

    # Observe worker completion without sleeps or sharing a SQLite connection between threads.
    class WorkerStore:
        def __enter__(self):
            self.store = original_factory()
            return self.store

        def __exit__(self, *args):
            self.store.close()
            finished.set()

    runtime.store_factory = WorkerStore
    assert runtime.start_sync(7)["already_running"] is False
    assert entered.wait(5)
    assert runtime.start_sync(7)["already_running"] is True
    release.set()
    assert finished.wait(5)
    # Join the specific worker so its finally block has also cleared the in-memory busy flag.
    for thread in threading.enumerate():
        if thread.name == "whoop-dashboard-sync":
            thread.join(5)
    runtime.store_factory = original_factory
    status = runtime.status()
    assert not status["running"] and status["last_run"]["resources_completed"] == 4
    assert "sensitive-fabricated-response" not in json.dumps(status)
    run_id = status["last_run"]["run_id"]
    failed[0] = False
    finished.clear()
    runtime.store_factory = WorkerStore
    runtime.start_sync(30, resume=run_id)
    assert finished.wait(5)
    for thread in threading.enumerate():
        if thread.name == "whoop-dashboard-sync":
            thread.join(5)
    runtime.store_factory = original_factory
    status = runtime.status()
    assert status["last_run"]["run_id"] == run_id
    assert status["last_run"]["status"] == "completed"
    assert status["last_success_at"]


@pytest.mark.parametrize(
    "days,resume", [(True, None), (365, None), ("7", None), (7, []), (7, "x" * 65)]
)
def test_sync_rejects_invalid_request_before_launch(runtime, days, resume):
    runtime.client_factory = lambda: pytest.fail("Invalid sync must not access WHOOP")
    with pytest.raises(ValueError):
        runtime.start_sync(days, resume)
    assert not runtime.status()["running"]


@pytest.mark.parametrize("port,demo", [(1023, False), (65536, False), (True, False), (8766, True)])
def test_invalid_real_launch_is_rejected_before_store_or_keychain(monkeypatch, port, demo):
    monkeypatch.setattr(
        "whoop_copilot.live_cli.store_for",
        lambda _: pytest.fail("Must reject before opening the real database"),
    )
    with pytest.raises(ValueError):
        run_dashboard(SimpleNamespace(environment="real", port=port, demo=demo))


async def test_real_http_adapter_uses_existing_encrypted_store_without_plaintext_copy(tmp_path):
    path, key = tmp_path / "protected.sqlite3", bytes(range(32))
    with Store(
        path,
        environment="real",
        encryption_key=key,
        clock=lambda: NOW,
        policy=LocalPolicy(90, owner_authorized=True),
    ) as store:
        ingest(store, resources())
    runtime = DashboardRuntime(
        lambda: Store(path, environment="real", encryption_key=key, clock=lambda: NOW)
    )
    async with client(runtime) as http:
        await unlock(http)
        view = (await http.get("/api/dashboard")).json()
        assert view["environment"] == "real" and view["trend"]["summary"]["count"] == 1
    for file in tmp_path.glob("*.sqlite3*"):
        assert b"SQLite format 3" not in file.read_bytes()[:16]
        assert b"fixture@example.invalid" not in file.read_bytes()
