"""Explicit approval for local training plans; this module never writes to WHOOP.

Only a trusted local interface may call ``approve``. The MCP interface exposes
proposals and commit, but must not issue its own approval tokens. Plan text is
user-requested content, not a verified health fact or a performed workout.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .contracts import timestamp, utc_now

SCOPE = "local_training_plan:write"
RISK_CATEGORY = "low_local_write"
COMMAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_training_plans (
    plan_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'active')),
    version INTEGER NOT NULL CHECK(version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_actions (
    action_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES local_training_plans(plan_id),
    operation TEXT NOT NULL CHECK(operation IN ('create', 'update')),
    expected_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'approved', 'committed', 'revoked')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_approvals (
    action_id TEXT PRIMARY KEY REFERENCES command_actions(action_id),
    token_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES command_actions(action_id),
    event TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class CommandError(ValueError):
    """A proposal or commit failed a validation or authorization check."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: str, name: str, maximum: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise CommandError(f"{name} must be text of at most {maximum} characters")
    if nonempty and not value.strip():
        raise CommandError(f"{name} must not be empty")
    return value


class CommandService:
    """A durable single-user service using the application's SQLite connection."""

    def __init__(self, db: sqlite3.Connection, clock: Callable = utc_now):
        if db.isolation_level is not None:
            raise CommandError("CommandService requires an autocommit SQLite connection")
        self.db = db
        self.clock = clock

    def _now(self) -> str:
        value = self.clock()
        if isinstance(value, datetime):
            value = value.isoformat()
        return timestamp(value)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.db.in_transaction:
            raise CommandError("A command cannot run inside another transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _action(self, action_id: str) -> sqlite3.Row:
        _text(action_id, "action_id", 200, nonempty=True)
        row = self.db.execute(
            "SELECT * FROM command_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise CommandError("Unknown action")
        return row

    def _verified_payload(self, action: sqlite3.Row) -> dict:
        try:
            payload = json.loads(action["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CommandError("Action payload has been altered") from exc
        if not isinstance(payload, dict) or _hash(_json(payload)) != action["payload_hash"]:
            raise CommandError("Action payload has been altered")
        binding = {
            "action_id": action["action_id"],
            "plan_id": action["plan_id"],
            "operation": action["operation"],
            "expected_version": action["expected_version"],
            "scope": action["scope"],
        }
        if any(payload.get(key) != value for key, value in binding.items()):
            raise CommandError("Action target or authorization scope has been altered")
        if payload["scope"] != SCOPE:
            raise CommandError("Unsupported authorization scope")
        if payload.get("risk_category") != RISK_CATEGORY:
            raise CommandError("Unsupported action risk category")
        _text(payload.get("title"), "title", 200, nonempty=True)
        _text(payload.get("details"), "details", 8000)
        return payload

    def _view_action(self, action: sqlite3.Row) -> dict:
        payload = self._verified_payload(action)
        return {
            "action_id": action["action_id"],
            "plan_id": action["plan_id"],
            "status": action["status"],
            "payload_hash": action["payload_hash"],
            "expected_version": action["expected_version"],
            "scope": action["scope"],
            "risk_category": payload["risk_category"],
            "payload": payload,
        }

    def _audit(self, action_id: str, event: str, details: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO command_audit(action_id,event,details_json,created_at) VALUES(?,?,?,?)",
            (action_id, event, _json(details or {}), self._now()),
        )

    def _existing(self, idempotency_key: str, request_hash: str) -> dict | None:
        existing = self.db.execute(
            "SELECT * FROM command_actions WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is None:
            return None
        if not hmac.compare_digest(existing["request_hash"], request_hash):
            raise CommandError("Idempotency key was already used for a different request")
        return self._view_action(existing)

    def _insert_action(
        self,
        operation: str,
        plan_id: str,
        title: str,
        details: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> dict:
        action_id = str(uuid4())
        payload = {
            "action_id": action_id,
            "operation": operation,
            "plan_id": plan_id,
            "title": title,
            "details": details,
            "expected_version": expected_version,
            "scope": SCOPE,
            "risk_category": RISK_CATEGORY,
        }
        payload_json = _json(payload)
        now = self._now()
        self.db.execute(
            """INSERT INTO command_actions(
                action_id,plan_id,operation,expected_version,scope,idempotency_key,
                request_hash,payload_json,payload_hash,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'proposed',?,?)""",
            (
                action_id,
                plan_id,
                operation,
                expected_version,
                SCOPE,
                idempotency_key,
                request_hash,
                payload_json,
                _hash(payload_json),
                now,
                now,
            ),
        )
        self._audit(action_id, "proposed", {"payload_hash": _hash(payload_json)})
        return self._view_action(self._action(action_id))

    def draft(self, title: str, details: str, idempotency_key: str) -> dict:
        """Save a version-zero draft and an unapproved proposal to activate it."""
        _text(title, "title", 200, nonempty=True)
        _text(details, "details", 8000)
        _text(idempotency_key, "idempotency_key", 200, nonempty=True)
        request_hash = _hash(_json({"operation": "create", "title": title, "details": details}))
        with self._transaction():
            existing = self._existing(idempotency_key, request_hash)
            if existing is not None:
                return existing
            plan_id = str(uuid4())
            now = self._now()
            self.db.execute(
                "INSERT INTO local_training_plans VALUES(?,?,?,'draft',0,?,?)",
                (plan_id, title, details, now, now),
            )
            return self._insert_action(
                "create", plan_id, title, details, 0, idempotency_key, request_hash
            )

    def propose_update(
        self,
        plan_id: str,
        title: str,
        details: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        """Propose changes without changing the active plan."""
        _text(plan_id, "plan_id", 200, nonempty=True)
        _text(title, "title", 200, nonempty=True)
        _text(details, "details", 8000)
        _text(idempotency_key, "idempotency_key", 200, nonempty=True)
        if type(expected_version) is not int or expected_version < 1:
            raise CommandError("expected_version must be a positive integer")
        request_hash = _hash(
            _json(
                {
                    "operation": "update",
                    "plan_id": plan_id,
                    "title": title,
                    "details": details,
                    "expected_version": expected_version,
                }
            )
        )
        with self._transaction():
            existing = self._existing(idempotency_key, request_hash)
            if existing is not None:
                return existing
            plan = self.get_plan(plan_id)
            if plan["status"] != "active" or plan["version"] != expected_version:
                raise CommandError(
                    "Plan version conflict; inspect the current plan and propose again"
                )
            return self._insert_action(
                "update", plan_id, title, details, expected_version, idempotency_key, request_hash
            )

    def approve(self, action_id: str, ttl_seconds: int = 300) -> dict:
        """Issue an opaque approval capability after review in the trusted local CLI.

        Issuing approval again rotates the token. No plaintext token is persisted.
        This method must not be registered as a model-callable tool.
        """
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise CommandError("ttl_seconds must be an integer between 1 and 3600")
        with self._transaction():
            action = self._action(action_id)
            result = self._view_action(action)
            if action["status"] not in ("proposed", "approved"):
                raise CommandError("Only a pending action can be approved")
            plan = self.get_plan(action["plan_id"])
            if plan["version"] != action["expected_version"]:
                raise CommandError(
                    "Plan version conflict; inspect the current plan and propose again"
                )
            now = self._now()
            expires_at = (
                (datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds))
                .astimezone(UTC)
                .isoformat(timespec="microseconds")
            )
            token = secrets.token_urlsafe(32)
            self.db.execute(
                """INSERT INTO command_approvals VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(action_id) DO UPDATE SET
                token_hash=excluded.token_hash,payload_hash=excluded.payload_hash,
                plan_id=excluded.plan_id,expected_version=excluded.expected_version,
                scope=excluded.scope,expires_at=excluded.expires_at,created_at=excluded.created_at""",
                (
                    action_id,
                    _hash(token),
                    action["payload_hash"],
                    action["plan_id"],
                    action["expected_version"],
                    action["scope"],
                    expires_at,
                    now,
                ),
            )
            self.db.execute(
                "UPDATE command_actions SET status='approved', updated_at=? WHERE action_id=?",
                (now, action_id),
            )
            self._audit(action_id, "approved", {"expires_at": expires_at})
            result.update(status="approved", approval_token=token, expires_at=expires_at)
            return result

    def commit(self, action_id: str, approval_token: str) -> dict:
        """Atomically apply the exact approved payload, once, to a local plan."""
        if not isinstance(approval_token, str) or not 20 <= len(approval_token) <= 256:
            raise CommandError("A valid approval token is required")
        with self._transaction():
            action = self._action(action_id)
            payload = self._verified_payload(action)
            approval = self.db.execute(
                "SELECT * FROM command_approvals WHERE action_id=?", (action_id,)
            ).fetchone()
            if approval is None or not hmac.compare_digest(
                approval["token_hash"], _hash(approval_token)
            ):
                raise CommandError("Invalid approval token")
            for field in ("payload_hash", "plan_id", "expected_version", "scope"):
                if approval[field] != action[field]:
                    raise CommandError("Approval does not match this action's payload and target")
            if action["status"] == "revoked":
                raise CommandError("Action approval has been revoked")
            if action["status"] == "committed":
                # A valid capability may retrieve its previous result after expiry;
                # replay never executes a write or replaces it with a newer plan.
                return json.loads(action["result_json"])
            if action["status"] != "approved":
                raise CommandError("Action has not been approved")
            now = self._now()
            if now >= timestamp(approval["expires_at"]):
                raise CommandError("Approval token has expired")
            expected_status = "draft" if action["operation"] == "create" else "active"
            changed = self.db.execute(
                """UPDATE local_training_plans SET title=?,details=?,status='active',
                version=version+1,updated_at=? WHERE plan_id=? AND version=? AND status=?""",
                (
                    payload["title"],
                    payload["details"],
                    now,
                    action["plan_id"],
                    action["expected_version"],
                    expected_status,
                ),
            )
            if changed.rowcount != 1:
                raise CommandError(
                    "Plan version conflict; inspect the current plan and propose again"
                )
            result = self.get_plan(action["plan_id"])
            self.db.execute(
                """UPDATE command_actions SET status='committed',result_json=?,updated_at=?
                WHERE action_id=?""",
                (_json(result), now, action_id),
            )
            self._audit(
                action_id, "committed", {"plan_id": result["plan_id"], "version": result["version"]}
            )
            return result

    def revoke(self, action_id: str) -> dict:
        """Permanently revoke a pending action; an executed write cannot be revoked."""
        with self._transaction():
            action = self._action(action_id)
            result = self._view_action(action)
            if action["status"] == "committed":
                raise CommandError("Committed actions cannot be revoked; propose a new update")
            if action["status"] != "revoked":
                self.db.execute(
                    "UPDATE command_actions SET status='revoked',updated_at=? WHERE action_id=?",
                    (self._now(), action_id),
                )
                self._audit(action_id, "revoked")
            result["status"] = "revoked"
            return result

    def get_plan(self, plan_id: str) -> dict:
        _text(plan_id, "plan_id", 200, nonempty=True)
        row = self.db.execute(
            "SELECT * FROM local_training_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise CommandError("Unknown training plan")
        return dict(row)

    def get_action(self, action_id: str) -> dict:
        """Return the reviewable payload without exposing stored authorization data."""
        return self._view_action(self._action(action_id))
