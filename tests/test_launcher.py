"""User-triggered local lifecycle; fake credentials and synthetic data only."""

import asyncio
import stat
import time
from types import SimpleNamespace

import httpx
import pytest

from whoop_copilot import launcher
from whoop_copilot.cli import execute, parser
from whoop_copilot.web import DashboardRuntime, create_app


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(
        db=tmp_path / "synthetic.sqlite3",
        environment="synthetic",
        oauth_config=tmp_path / "oauth-config.json",
        demo=False,
        port=18766,
    )


def test_identity_is_private_stable_and_configuration_scoped(args):
    first = launcher.LauncherControl(args)
    again = launcher.LauncherControl(args)
    challenge = "a" * 64
    assert first.ready(challenge) == again.ready(challenge)
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.path.parent.stat().st_mode) == 0o700
    assert len(first.path.read_bytes()) == 32
    args.db = args.db.with_name("other.sqlite3")
    other = launcher.LauncherControl(args)
    assert first.ready(challenge) != other.ready(challenge)
    assert set(first.ready(challenge)) == {"protocol", "proof"}


@pytest.mark.parametrize("change", ["permissions", "symlink", "size"])
def test_unsafe_identity_file_is_rejected(args, change):
    control = launcher.LauncherControl(args)
    if change == "permissions":
        control.path.chmod(0o644)
    elif change == "symlink":
        original = control.path.with_suffix(".original")
        control.path.rename(original)
        control.path.symlink_to(original)
    else:
        control.path.write_bytes(b"bad")
    with pytest.raises((ValueError, OSError)):
        launcher.LauncherControl(args)


@pytest.mark.parametrize("challenge", [None, "", "a" * 63, "a" * 65, "x" * 64, "a" * 64 + "\n"])
def test_readiness_rejects_invalid_challenges(args, challenge):
    with pytest.raises(ValueError):
        launcher.LauncherControl(args).ready(challenge)


def test_stop_proof_is_not_obtainable_from_readiness_and_expires(args, monkeypatch):
    control = launcher.LauncherControl(args)
    now = 2000000000
    monkeypatch.setattr(launcher.time, "time", lambda: now)
    headers = {key.lower(): value for key, value in control.stop_headers().items()}
    assert control.authorize_stop(headers)
    forged = dict(headers, **{"x-whoop-launcher-proof": control.ready("a" * 64)["proof"]})
    assert not control.authorize_stop(forged)
    monkeypatch.setattr(launcher.time, "time", lambda: now + 31)
    assert not control.authorize_stop(headers)
    monkeypatch.setattr(launcher.time, "time", lambda: now)
    control.mark_stopping()
    assert not control.authorize_stop(headers)


def test_probe_requires_exact_identity_and_never_follows_redirects(args):
    control = launcher.LauncherControl(args)

    def correct(request):
        return httpx.Response(200, json=control.ready(request.url.params["challenge"]))

    assert launcher._probe(control, transport=httpx.MockTransport(correct)) == "ready"
    for response in (
        httpx.Response(200, json={"protocol": launcher.PROTOCOL, "proof": "x"}),
        httpx.Response(200, content=b"x" * 2048),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, content=b"", headers={"content-encoding": "gzip"}),
        httpx.Response(302, headers={"location": "https://example.invalid"}),
        httpx.Response(404),
    ):
        assert (
            launcher._probe(control, transport=httpx.MockTransport(lambda _: response))
            == "occupied"
        )


def test_probe_distinguishes_closed_port_from_unresponsive_service(args):
    control = launcher.LauncherControl(args)
    for error, expected in ((httpx.ConnectError, "absent"), (httpx.ReadTimeout, "occupied")):

        def fail(request):
            raise error("fabricated", request=request)

        assert launcher._probe(control, transport=httpx.MockTransport(fail)) == expected


@pytest.mark.parametrize("part", ["headers", "body"])
def test_probe_has_total_deadline_even_with_trickled_response(args, monkeypatch, part):
    control = launcher.LauncherControl(args)
    monkeypatch.setattr(launcher, "REQUEST_SECONDS", 0.03)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

    async def slow(request):
        if part == "headers":
            await asyncio.sleep(1)
        return httpx.Response(200, stream=SlowStream())

    before = time.monotonic()
    assert launcher._probe(control, transport=httpx.MockTransport(slow)) == "occupied"
    assert time.monotonic() - before < 0.5


def test_real_identity_uses_separate_keychain_entry_without_plaintext(args, monkeypatch):
    args.environment = "real"
    entries = {}
    accounts = []

    class Vault:
        def __init__(self, account, path):
            self.account = account
            accounts.append(account)

        def read(self):
            return entries.get(self.account, {})

        def write(self, value):
            entries[self.account] = value

    monkeypatch.setattr(launcher, "KeychainVault", Vault)
    control = launcher.LauncherControl(args)
    second = launcher.LauncherControl(args)
    assert control.ready("a" * 64) == second.ready("a" * 64)
    assert len(entries) == 1
    assert all(account.startswith("launcher:") for account in accounts)
    assert not control.path.exists()
    assert not list(control.path.parent.glob("*.key"))


@pytest.mark.parametrize("record", ["unavailable", {"launcher_key": "bad"}])
def test_real_keychain_failure_has_no_plaintext_fallback(args, monkeypatch, record):
    args.environment = "real"

    class Vault:
        def __init__(self, *args):
            pass

        def read(self):
            if record == "unavailable":
                raise launcher.CredentialError("Unavailable")
            return record

    monkeypatch.setattr(launcher, "KeychainVault", Vault)
    with pytest.raises(launcher.CredentialError):
        launcher.LauncherControl(args)
    assert not list((args.db.parent / "runtime/launcher").glob("*.key"))


async def test_readiness_never_opens_database_and_keeps_local_boundary(args):
    control = launcher.LauncherControl(args)
    runtime = DashboardRuntime(lambda: pytest.fail("Readiness must not inspect a database"))
    app = create_app(runtime, args.port, launcher=control)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url=control.url
    ) as client:
        response = await client.get("/api/ready", params={"challenge": "a" * 64})
        assert response.status_code == 200
        assert response.json() == control.ready("a" * 64)
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        for query in ("", "challenge=bad", "challenge=" + "a" * 64 + "&challenge=" + "a" * 64):
            assert (await client.get("/api/ready?" + query)).status_code == 400
        assert (
            await client.get("/api/ready", headers={"Origin": "https://example.invalid"})
        ).status_code == 403


async def test_authenticated_stop_refuses_active_sync_and_gracefully_stops(args):
    control = launcher.LauncherControl(args)
    runtime = DashboardRuntime(lambda: pytest.fail("Not needed for synthetic lifecycle check"))
    running = True
    runtime.status = lambda: {"running": running}
    stopped = []
    app = create_app(runtime, args.port, launcher=control, shutdown=lambda: stopped.append(True))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url=control.url
    ) as client:
        assert (
            await client.post("/api/launcher/stop", headers={"Origin": control.url})
        ).status_code == 403
        response = await client.post("/api/launcher/stop", headers=control.stop_headers())
        assert response.status_code == 409
        assert not stopped
        running = False
        response = await client.post("/api/launcher/stop", headers=control.stop_headers())
        assert response.status_code == 202
        assert response.json() == {"stopping": True}
        assert stopped == [True]
        assert runtime._stopping
        assert (
            await client.post("/api/launcher/stop", headers=control.stop_headers())
        ).status_code == 403


def test_preparing_shutdown_is_atomic_with_local_sync():
    runtime = DashboardRuntime(lambda: None)
    runtime._busy = True
    assert not runtime.prepare_shutdown()
    assert not runtime._stopping
    runtime._busy = False
    assert runtime.prepare_shutdown()
    assert runtime._stopping


class Child:
    def __init__(self):
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode


def test_launch_detaches_then_opens_only_after_verified_readiness(args, monkeypatch):
    states = iter(["absent", "absent", "ready"])
    monkeypatch.setattr(launcher, "_probe", lambda *a: next(states))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    calls = []
    child = Child()

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return child

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    result = launcher.open_dashboard(args)
    assert result == {
        "started": True,
        "reused": False,
        "opened": True,
        "url": "http://127.0.0.1:18766/",
    }
    command, options = calls[0]
    assert command[-3:] == ["dashboard", "--port", "18766"]
    assert options["start_new_session"] and options["close_fds"]
    assert all(options[key] == launcher.subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    assert opened == [result["url"]]
    assert not child.terminated


def test_repeat_launch_reuses_verified_service_without_child(args, monkeypatch):
    monkeypatch.setattr(launcher, "_probe", lambda *a: "ready")
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("Duplicate child")
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: True)
    result = launcher.open_dashboard(args)
    assert result["reused"] and not result["started"]


def test_port_collision_never_opens_or_terminates_unknown_service(args, monkeypatch):
    monkeypatch.setattr(launcher, "_probe", lambda *a: "occupied")
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected child")
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("Unknown service"))
    for operation in (launcher.open_dashboard, launcher.stop_dashboard):
        with pytest.raises(ValueError, match="端口"):
            operation(args)


def test_readiness_failure_terminates_only_own_child_and_does_not_open(args, monkeypatch):
    states = iter(["absent", "occupied"])
    monkeypatch.setattr(launcher, "_probe", lambda *a: next(states))
    child = Child()
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("Service not ready"))
    with pytest.raises(ValueError, match="端口"):
        launcher.open_dashboard(args)
    assert child.terminated


def test_startup_timeout_is_bounded_and_cleans_up_child(args, monkeypatch):
    monkeypatch.setattr(launcher, "WAIT_SECONDS", -1)
    monkeypatch.setattr(launcher, "_probe", lambda *a: "absent")
    child = Child()
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _: pytest.fail("Service not ready"))
    with pytest.raises(ValueError, match="20 秒"):
        launcher.open_dashboard(args)
    assert child.terminated


def test_stopping_absent_service_is_idempotent(args, monkeypatch):
    monkeypatch.setattr(launcher, "_probe", lambda *a: "absent")
    assert launcher.stop_dashboard(args) == {"stopped": True, "already_stopped": True}


def test_launch_lock_prevents_overlapping_startup(args):
    first = launcher.LauncherControl(args)
    second = launcher.LauncherControl(args)
    with first.lock():
        with pytest.raises(ValueError, match="另一个"):
            with second.lock(timeout=0):
                pytest.fail("Must not enter two startup operations")


@pytest.mark.parametrize(
    "command,function", [("dashboard-open", "open_dashboard"), ("dashboard-stop", "stop_dashboard")]
)
def test_cli_lifecycle_routes_preserve_explicit_environment(monkeypatch, command, function):
    received = []
    monkeypatch.setattr(launcher, function, lambda args: received.append(args.environment) or {})
    assert execute(parser().parse_args([command])) == {}
    assert execute(parser().parse_args(["--environment", "real", command])) == {}
    assert received == ["synthetic", "real"]
