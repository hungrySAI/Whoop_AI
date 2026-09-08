"""WHOOP authorization-code OAuth using Authlib; credentials never leave the vault."""

import math
import secrets
import time
import warnings
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.deprecate import AuthlibDeprecationWarning

AUTHORIZATION_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
REVOKE_URL = "https://api.prod.whoop.com/developer/v2/user/access"
READ_SCOPES = (
    "read:cycles",
    "read:recovery",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
)
DEFAULT_SCOPES = (*READ_SCOPES, "offline")


class OAuthError(ValueError):
    """An intentionally sanitized OAuth failure."""


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    def __post_init__(self):
        if not self.client_id or len(self.client_id) > 512:
            raise OAuthError("A WHOOP client ID is required")
        parts = urlsplit(self.redirect_uri)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or (parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise OAuthError("Use an exact registered callback URI without query or fragment")
        if (
            not self.scopes
            or "offline" not in self.scopes
            or not set(self.scopes) <= set(DEFAULT_SCOPES)
            or len(self.scopes) != len(set(self.scopes))
        ):
            raise OAuthError("Only the WHOOP read scopes plus offline may be requested")


class WhoopOAuth:
    def __init__(
        self,
        config: OAuthConfig,
        vault,
        client_secret: str | None = None,
        transport=None,
        clock=time.time,
        state_ttl: int = 600,
    ):
        if not 30 <= state_ttl <= 900:
            raise OAuthError("OAuth state TTL must be between 30 and 900 seconds")
        self.config = config
        self.vault = vault
        self.transport = transport
        self.clock = clock
        self.state_ttl = state_ttl
        if client_secret is not None:
            if not client_secret:
                raise OAuthError("A client secret is required")
            with vault.locked():
                record = self._record()
                record["client_secret"] = client_secret
                vault.write(record)

    def _record(self) -> dict:
        record = self.vault.read()
        binding = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scopes": list(self.config.scopes),
        }
        if record and record.get("config") != binding:
            raise OAuthError("Keychain entry belongs to a different OAuth configuration")
        record["config"] = binding
        return record

    def _client(self, record, state=None):
        # Authlib 1.8's HTTPX compatibility path is covered by our protocol tests.
        # Keep this known import-time notice out of the CLI's JSON error channel.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"The httpx module is deprecated; please use httpx2 instead\.",
                category=AuthlibDeprecationWarning,
            )
            from authlib.integrations.httpx_client import OAuth2Client
        secret = record.get("client_secret")
        if not isinstance(secret, str) or not secret:
            raise OAuthError("Configure the WHOOP client secret in the OS keychain first")
        client = OAuth2Client(
            client_id=self.config.client_id,
            client_secret=secret,
            token_endpoint_auth_method="client_secret_post",
            scope=" ".join(self.config.scopes),
            redirect_uri=self.config.redirect_uri,
            state=state,
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )
        client.register_compliance_hook("access_token_response", self._validate_response)
        client.register_compliance_hook("refresh_token_response", self._validate_response)
        return client

    def _validate_response(self, response):
        if response.status_code != 200:
            raise OAuthError("WHOOP token exchange failed; start authorization again")
        try:
            token = response.json()
            if not isinstance(token, dict):
                raise ValueError
            if not isinstance(token.get("scope"), str):
                raise ValueError
            if not set(self.config.scopes) <= set(token["scope"].split()):
                raise ValueError
            for name in ("access_token", "refresh_token"):
                if not isinstance(token.get(name), str) or not token[name]:
                    raise ValueError
            if str(token.get("token_type", "")).lower() != "bearer":
                raise ValueError
            expiry = float(token["expires_in"])
            if not math.isfinite(expiry) or expiry <= 0:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise OAuthError("WHOOP token response is incomplete; authorize again") from None
        return response

    def begin(self) -> dict:
        with self.vault.locked():
            record = self._record()
            state = secrets.token_urlsafe(32)
            expires_at = self.clock() + self.state_ttl
            with self._client(record, state=state) as client:
                url, returned_state = client.create_authorization_url(
                    AUTHORIZATION_URL, state=state
                )
            record["pending"] = {"state": returned_state, "expires_at": expires_at}
            self.vault.write(record)
            return {"authorization_url": url, "expires_at": expires_at}

    def finish(self, callback_url: str) -> dict:
        with self.vault.locked():
            record = self._record()
            pending = record.get("pending")
            if not isinstance(pending, dict) or pending.get("expires_at", 0) <= self.clock():
                record.pop("pending", None)
                self.vault.write(record)
                raise OAuthError("Authorization is missing or expired; start again")
            try:
                received, expected = urlsplit(callback_url), urlsplit(self.config.redirect_uri)
                if (received.scheme, received.netloc, received.path) != (
                    expected.scheme,
                    expected.netloc,
                    expected.path,
                ) or received.fragment:
                    raise ValueError
                query = parse_qs(received.query, keep_blank_values=True, strict_parsing=True)
                state_values = query.get("state", [])
                if len(state_values) != 1 or not secrets.compare_digest(
                    state_values[0], pending["state"]
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise OAuthError("OAuth callback or state does not match") from None
            # Persist consumption before the network request: even a process crash cannot replay it.
            record.pop("pending", None)
            self.vault.write(record)
            if "error" in query:
                raise OAuthError("WHOOP authorization was declined; start again when ready")
            codes = query.get("code", [])
            if len(codes) != 1 or not codes[0]:
                raise OAuthError("OAuth callback has no single authorization code")
            record["status"] = "requires_reauthorization"
            record.pop("token", None)
            self.vault.write(record)
            try:
                with self._client(record, state=pending["state"]) as client:
                    token = client.fetch_token(
                        TOKEN_URL, grant_type="authorization_code", code=codes[0]
                    )
                self._save_token(record, token)
            except Exception:
                raise OAuthError("WHOOP token exchange failed; start authorization again") from None
            return self._public_status(record)

    def _save_token(self, record, token):
        token = dict(token)
        token["expires_at"] = self.clock() + float(token["expires_in"])
        record["token"] = token
        record["status"] = "connected"
        self.vault.write(record)

    def _public_status(self, record):
        connected = record.get("status") == "connected" and bool(record.get("token"))
        return {
            "connected": connected,
            "status": record.get("status", "not_connected"),
            "scopes": record.get("token", {}).get("scope", "").split() if connected else [],
            "authorization_pending": bool(record.get("pending")),
        }

    def status(self) -> dict:
        with self.vault.locked():
            return self._public_status(self._record())

    def access_token(self, force_refresh: bool = False, rejected_token: str | None = None) -> str:
        with self.vault.locked():
            return self._access_token_locked(self._record(), force_refresh, rejected_token)

    def _access_token_locked(self, record, force_refresh=False, rejected_token=None):
        if record.get("status") != "connected" or not record.get("token"):
            raise OAuthError("WHOOP requires authorization")
        token = record["token"]
        if rejected_token is not None and token["access_token"] != rejected_token:
            force_refresh = False
        if not force_refresh and token.get("expires_at", 0) > self.clock() + 60:
            return token["access_token"]
        # A lost response may have rotated both credentials; never retry the old refresh token.
        record["status"] = "requires_reauthorization"
        self.vault.write(record)
        try:
            with self._client(record) as client:
                updated = client.refresh_token(
                    TOKEN_URL, refresh_token=token["refresh_token"], scope="offline"
                )
            self._save_token(record, updated)
        except Exception:
            raise OAuthError("WHOOP token refresh failed; authorize again") from None
        return record["token"]["access_token"]

    def disconnect(self) -> dict:
        with self.vault.locked():
            record = self._record()
            if not record.get("token"):
                self.vault.delete()
                return {"connected": False, "status": "disconnected"}
            access_token = self._access_token_locked(record)
            try:
                with httpx.Client(
                    transport=self.transport, timeout=20, trust_env=False, follow_redirects=False
                ) as client:
                    response = client.delete(
                        REVOKE_URL, headers={"Authorization": f"Bearer {access_token}"}
                    )
                if response.status_code != 204:
                    raise OAuthError("WHOOP revocation did not succeed; credentials were retained")
            except httpx.HTTPError:
                record["status"] = "requires_reauthorization"
                self.vault.write(record)
                raise OAuthError(
                    "WHOOP revocation could not be verified; credentials retained, authorize again"
                ) from None
            self.vault.delete()
            return {"connected": False, "status": "disconnected"}
