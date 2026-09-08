"""All credentials and HTTP exchanges in this module are fabricated test values."""

import copy
import select
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from whoop_copilot.credentials import CredentialError, KeychainVault
from whoop_copilot.oauth import DEFAULT_SCOPES, OAuthConfig, OAuthError, WhoopOAuth


class MemoryVault:
    def __init__(self):
        self.value = {}
        self.lock = threading.RLock()

    def read(self):
        return copy.deepcopy(self.value)

    def write(self, value):
        self.value = copy.deepcopy(value)

    def delete(self):
        self.value = {}

    @contextmanager
    def locked(self):
        with self.lock:
            yield


def fake_token(access="synthetic-access", refresh="synthetic-refresh", expires=3600):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires,
        "scope": " ".join(DEFAULT_SCOPES),
        "token_type": "bearer",
    }


def build(handler, now=None, vault=None):
    return WhoopOAuth(
        OAuthConfig("synthetic-client", "http://127.0.0.1:8765/callback"),
        vault or MemoryVault(),
        "synthetic-secret",
        transport=httpx.MockTransport(handler),
        clock=(lambda: now[0]) if now is not None else (lambda: 1000),
    )


def callback(oauth, **overrides):
    auth = oauth.begin()
    values = parse_qs(urlsplit(auth["authorization_url"]).query)
    query = {"code": "synthetic-code", "state": values["state"][0], **overrides}
    return oauth.config.redirect_uri + "?" + urlencode(query)


def connect(oauth):
    return oauth.finish(callback(oauth))


def test_authlib_code_flow_is_bound_and_secrets_stay_in_vault():
    requests = []

    def handler(request):
        requests.append(request)
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["authorization_code"]
        assert fields["client_secret"] == ["synthetic-secret"]
        assert fields["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
        return httpx.Response(200, json=fake_token())

    oauth = build(handler)
    cb = callback(oauth)
    pending = oauth.vault.read()["pending"]
    assert len(pending["state"]) >= 32
    assert pending["expires_at"] == 1600
    result = oauth.finish(cb)
    assert result["connected"] is True
    assert set(result["scopes"]) == set(DEFAULT_SCOPES)
    assert "synthetic-access" not in str(result)
    assert oauth.access_token() == "synthetic-access"
    assert len(requests) == 1
    with pytest.raises(OAuthError, match="missing or expired"):
        oauth.finish(cb)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "replace",
    [
        lambda value: value.replace("127.0.0.1", "evil.example"),
        lambda value: value.replace("8765", "8766"),
        lambda value: value.replace("/callback?", "/other?"),
        lambda value: value + "&state=duplicate",
        lambda value: value + "#fragment",
        lambda value: value.replace("state=", "state=wrong"),
    ],
)
def test_callback_destination_and_state_rejected_before_network(replace):
    requests = []
    oauth = build(lambda r: requests.append(r))
    cb = callback(oauth)
    with pytest.raises(OAuthError, match="does not match"):
        oauth.finish(replace(cb))
    assert requests == []


def test_state_expiry_and_cancellation_consume_pending_without_exchange():
    now = [1000]
    oauth = build(lambda r: pytest.fail("Network must not be called"), now=now)
    cb = callback(oauth)
    now[0] = 1600
    with pytest.raises(OAuthError, match="expired"):
        oauth.finish(cb)
    cb = callback(oauth, error="access_denied", error_description="sensitive")
    with pytest.raises(OAuthError, match="declined") as error:
        oauth.finish(cb)
    assert "sensitive" not in str(error.value)
    assert "pending" not in oauth.vault.read()


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "offline"},
        {"scope": None},
        {"refresh_token": None},
        {"expires_in": -1},
        {"expires_in": "NaN"},
        {"token_type": "unknown"},
    ],
)
def test_incomplete_token_fails_closed_without_secret_error(change):
    payload = {**fake_token(), **change}
    oauth = build(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(OAuthError) as error:
        connect(oauth)
    assert "synthetic" not in str(error.value)
    assert oauth.status()["status"] == "requires_reauthorization"


def test_lost_authorization_response_is_never_replayed():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic-secret synthetic-code", request=request)

    oauth = build(handler)
    cb = callback(oauth)
    with pytest.raises(OAuthError) as error:
        oauth.finish(cb)
    assert "synthetic" not in str(error.value)
    with pytest.raises(OAuthError):
        oauth.finish(cb)
    assert len(requests) == 1
    assert oauth.status()["status"] == "requires_reauthorization"


def test_rotation_serializes_concurrent_clients_and_401_reuses_new_token():
    requests = []
    now = [1000]

    def handler(request):
        requests.append(request)
        fields = parse_qs(request.content.decode())
        if fields["grant_type"] == ["refresh_token"]:
            assert fields["refresh_token"] == ["synthetic-refresh"]
            assert fields["scope"] == ["offline"]
            return httpx.Response(200, json=fake_token("rotated-access", "rotated-refresh"))
        return httpx.Response(200, json=fake_token())

    vault = MemoryVault()
    first = build(handler, now=now, vault=vault)
    second = build(handler, now=now, vault=vault)
    connect(first)
    now[0] = 4600
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda o: o.access_token(), [first, second]))
    assert outputs == ["rotated-access", "rotated-access"]
    assert len(requests) == 2
    assert second.access_token(force_refresh=True, rejected_token="synthetic-access") == (
        "rotated-access"
    )
    assert len(requests) == 2
    assert vault.read()["token"]["refresh_token"] == "rotated-refresh"


def test_refresh_response_loss_requires_new_authorization():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=fake_token())
        raise httpx.ReadTimeout("secret response details")

    oauth = build(handler)
    connect(oauth)
    with pytest.raises(OAuthError, match="authorize again"):
        oauth.access_token(force_refresh=True)
    with pytest.raises(OAuthError, match="requires authorization"):
        oauth.access_token()
    assert len(calls) == 2
    assert oauth.status()["status"] == "requires_reauthorization"


def test_missing_rotated_refresh_token_never_reuses_old_one():
    calls = []

    def handler(request):
        calls.append(request)
        payload = fake_token()
        if len(calls) > 1:
            del payload["refresh_token"]
        return httpx.Response(200, json=payload)

    oauth = build(handler)
    connect(oauth)
    with pytest.raises(OAuthError):
        oauth.access_token(force_refresh=True)
    assert oauth.status()["status"] == "requires_reauthorization"


@pytest.mark.parametrize("status", [204, 401, 500, 302])
def test_revoke_clears_only_on_confirmed_success(status):
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "DELETE":
            assert str(request.url).endswith("/developer/v2/user/access")
            assert request.headers["Authorization"] == "Bearer synthetic-access"
            return httpx.Response(status, headers={"Location": "https://evil.example"})
        return httpx.Response(200, json=fake_token())

    oauth = build(handler)
    connect(oauth)
    if status == 204:
        assert oauth.disconnect()["status"] == "disconnected"
        assert oauth.vault.read() == {}
    else:
        with pytest.raises(OAuthError):
            oauth.disconnect()
        assert oauth.vault.read()["token"]
    assert len(seen) == 2


def test_vault_config_binding_and_scope_allowlist():
    oauth = build(lambda r: httpx.Response(200, json=fake_token()))
    with pytest.raises(OAuthError, match="different OAuth"):
        WhoopOAuth(OAuthConfig("another", oauth.config.redirect_uri), oauth.vault, "secret")
    with pytest.raises(OAuthError, match="Only"):
        OAuthConfig("client", oauth.config.redirect_uri, ("write:anything", "offline"))


def test_plaintext_or_chained_backend_is_rejected(tmp_path):
    class UnsafeBackend:
        def get_password(self, *_):
            pytest.fail("Unsafe backend must not be accessed")

    with pytest.raises(CredentialError, match="OS secure"):
        KeychainVault("synthetic", tmp_path / "lock", backend=UnsafeBackend())


def test_credential_lock_excludes_other_processes_without_accessing_keychain(tmp_path):
    # Exercise the real locking method without constructing/accessing an OS backend.
    vault = KeychainVault.__new__(KeychainVault)
    vault.lock_path = tmp_path / "oauth.lock"
    code = """
import sys
from pathlib import Path
from whoop_copilot.credentials import KeychainVault
vault = KeychainVault.__new__(KeychainVault)
vault.lock_path = Path(sys.argv[1])
print('attempt', flush=True)
with vault.locked():
    print('acquired', flush=True)
"""
    with vault.locked():
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(vault.lock_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "attempt"
            readable, _, _ = select.select([process.stdout], [], [], 0.1)
            assert readable == []
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
    output, errors = process.communicate(timeout=5)
    assert process.returncode == 0, errors
    assert output.strip() == "acquired"
    assert vault.lock_path.stat().st_mode & 0o777 == 0o600


def test_credential_lock_does_not_follow_symlink(tmp_path):
    target = tmp_path / "untouched"
    target.write_text("original")
    vault = KeychainVault.__new__(KeychainVault)
    vault.lock_path = tmp_path / "lock"
    vault.lock_path.symlink_to(target)
    with pytest.raises(CredentialError, match="lock"):
        with vault.locked():
            pytest.fail("A symlink must never be opened as the lock file")
    assert target.read_text() == "original"


def test_database_key_uses_a_dedicated_entry_and_never_regenerates_missing_key(
    tmp_path,
    monkeypatch,
):
    from whoop_copilot import credentials

    vaults = {}

    def factory(account, lock_path):
        assert account.startswith("database:")
        assert Path(lock_path).parent == tmp_path
        return vaults.setdefault(account, MemoryVault())

    monkeypatch.setattr(credentials, "KeychainVault", factory)
    path = tmp_path / "encrypted.db"
    with pytest.raises(CredentialError, match="unavailable"):
        credentials.database_key(path)
    key = credentials.database_key(path, create=True)
    assert len(key) == 32
    assert credentials.database_key(path, create=True) == key
    assert credentials.database_key(path) == key
    assert credentials.database_key(tmp_path / "different.db", create=True) != key
    first = next(iter(vaults.values()))
    first.write({"database_key": "invalid"})
    with pytest.raises(CredentialError, match="invalid"):
        credentials.database_key(path, create=True)


def test_lost_revoke_response_does_not_claim_success_or_refresh_again():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "DELETE":
            raise httpx.ReadTimeout("sensitive details", request=request)
        return httpx.Response(200, json=fake_token())

    oauth = build(handler)
    connect(oauth)
    with pytest.raises(OAuthError, match="could not be verified"):
        oauth.disconnect()
    assert oauth.status()["status"] == "requires_reauthorization"
    assert oauth.vault.read()["token"]
    with pytest.raises(OAuthError):
        oauth.access_token()
    assert len(calls) == 2


def test_oauth_http_configuration_disables_environment_and_redirects(monkeypatch):
    import authlib.integrations.httpx_client as module

    original, settings = module.OAuth2Client, []

    def factory(**kwargs):
        settings.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module, "OAuth2Client", factory)
    oauth = build(lambda r: httpx.Response(200, json=fake_token()))
    connect(oauth)
    assert all(s["trust_env"] is False and s["follow_redirects"] is False for s in settings)
    assert all(s.get("verify", True) is True for s in settings)
