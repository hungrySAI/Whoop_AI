import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from whoop_copilot.commands import COMMAND_SCHEMA, CommandError, CommandService

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def connect(path=":memory:"):
    db = sqlite3.connect(path, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(COMMAND_SCHEMA)
    return db


@pytest.fixture
def service():
    db = connect()
    yield CommandService(db, clock=lambda: NOW)
    db.close()


def pending(service, key="plan"):
    proposal = service.draft("Easy ride", "User request: ride for 30 minutes.", key)
    approval = service.approve(proposal["action_id"])
    return proposal, approval["approval_token"]


def test_draft_requires_real_approval_and_commit_is_once(service):
    proposal = service.draft("Easy ride", "Requested training", "one")
    assert service.get_plan(proposal["plan_id"])["version"] == 0
    assert service.get_plan(proposal["plan_id"])["status"] == "draft"
    with pytest.raises(CommandError, match="Invalid approval"):
        service.commit(proposal["action_id"], "forged_approval_token_12345")
    with pytest.raises(TypeError):
        service.commit(proposal["action_id"], approved=True)
    approval = service.approve(proposal["action_id"])
    token = approval["approval_token"]
    with pytest.raises(CommandError, match="Invalid approval"):
        service.commit(proposal["action_id"], "forged_approval_token_12345")
    result = service.commit(proposal["action_id"], token)
    assert result["status"] == "active"
    assert result["version"] == 1
    assert service.commit(proposal["action_id"], token) == result
    assert (
        service.db.execute("SELECT count(*) FROM command_audit WHERE event='committed'").fetchone()[
            0
        ]
        == 1
    )
    assert token not in "\n".join(service.db.iterdump())
    with pytest.raises(CommandError, match="Invalid approval"):
        service.commit(proposal["action_id"], "forged_approval_token_12345")


def test_tokens_are_bound_to_the_action_and_rotated(service):
    first, token = pending(service, "first")
    second, second_token = pending(service, "second")
    with pytest.raises(CommandError, match="Invalid approval"):
        service.commit(second["action_id"], token)
    replacement = service.approve(first["action_id"])["approval_token"]
    assert replacement != token
    with pytest.raises(CommandError, match="Invalid approval"):
        service.commit(first["action_id"], token)
    assert service.commit(first["action_id"], replacement)["version"] == 1
    assert service.commit(second["action_id"], second_token)["version"] == 1


@pytest.mark.parametrize("rehash", [False, True])
def test_payload_tampering_never_changes_the_plan(service, rehash):
    proposal, token = pending(service)
    payload = dict(proposal["payload"], details="Unapproved hard intervals")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if rehash:
        service.db.execute(
            "UPDATE command_actions SET payload_json=?,payload_hash=? WHERE action_id=?",
            (serialized, hashlib.sha256(serialized.encode()).hexdigest(), proposal["action_id"]),
        )
    else:
        service.db.execute(
            "UPDATE command_actions SET payload_json=? WHERE action_id=?",
            (serialized, proposal["action_id"]),
        )
    with pytest.raises(CommandError, match="altered|does not match"):
        service.commit(proposal["action_id"], token)
    assert service.get_plan(proposal["plan_id"])["version"] == 0
    assert service.get_plan(proposal["plan_id"])["details"] == proposal["payload"]["details"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_version", 1),
        ("scope", "whoop:write"),
        ("operation", "update"),
    ],
)
def test_target_metadata_tampering_is_rejected(service, field, value):
    proposal, token = pending(service)
    service.db.execute(
        f"UPDATE command_actions SET {field}=? WHERE action_id=?", (value, proposal["action_id"])
    )
    with pytest.raises(CommandError, match="altered"):
        service.commit(proposal["action_id"], token)
    assert service.get_plan(proposal["plan_id"])["version"] == 0


def test_expiration_and_revocation(service):
    proposal, token = pending(service)
    service.clock = lambda: NOW + timedelta(seconds=300)
    with pytest.raises(CommandError, match="expired"):
        service.commit(proposal["action_id"], token)
    assert service.get_plan(proposal["plan_id"])["version"] == 0
    token = service.approve(proposal["action_id"])["approval_token"]
    assert service.revoke(proposal["action_id"])["status"] == "revoked"
    assert service.revoke(proposal["action_id"])["status"] == "revoked"
    with pytest.raises(CommandError, match="revoked"):
        service.commit(proposal["action_id"], token)
    with pytest.raises(CommandError, match="pending"):
        service.approve(proposal["action_id"])


def test_approval_cannot_be_redirected_to_another_plan(service):
    first, token = pending(service, "first")
    other = service.draft("Other plan", "Other content", "other")
    service.db.execute(
        "UPDATE command_actions SET plan_id=? WHERE action_id=?",
        (other["plan_id"], first["action_id"]),
    )
    with pytest.raises(CommandError, match="altered"):
        service.commit(first["action_id"], token)
    assert service.get_plan(first["plan_id"])["version"] == 0
    assert service.get_plan(other["plan_id"])["version"] == 0


def test_storage_failure_rolls_back_plan_action_and_audit_together(service):
    proposal, token = pending(service)
    service.db.execute("""CREATE TRIGGER fail_commit_audit BEFORE INSERT ON command_audit
        WHEN NEW.event='committed' BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
        service.commit(proposal["action_id"], token)
    assert service.get_plan(proposal["plan_id"])["version"] == 0
    assert service.get_action(proposal["action_id"])["status"] == "approved"
    assert not service.db.in_transaction
    service.db.execute("DROP TRIGGER fail_commit_audit")
    assert service.commit(proposal["action_id"], token)["version"] == 1


def test_idempotency_across_restarts_and_original_commit_result(tmp_path):
    path = tmp_path / "commands.sqlite3"
    db = connect(path)
    original = CommandService(db, clock=lambda: NOW)
    proposal, token = pending(original)
    result = original.commit(proposal["action_id"], token)
    db.close()
    reopened = connect(path)
    try:
        service = CommandService(reopened, clock=lambda: NOW + timedelta(days=1))
        repeat = service.draft("Easy ride", "User request: ride for 30 minutes.", "plan")
        assert repeat["action_id"] == proposal["action_id"]
        assert repeat["status"] == "committed"
        with pytest.raises(CommandError, match="different request"):
            service.draft("Different title", "User request: ride for 30 minutes.", "plan")
        update = service.propose_update(result["plan_id"], "Rest", "User request: rest.", 1, "edit")
        update_token = service.approve(update["action_id"])["approval_token"]
        assert service.get_plan(result["plan_id"])["title"] == "Easy ride"
        assert service.commit(update["action_id"], update_token)["version"] == 2
        assert service.commit(proposal["action_id"], token) == result
        assert service.get_plan(result["plan_id"])["version"] == 2
        assert (
            service.propose_update(result["plan_id"], "Rest", "User request: rest.", 1, "edit")[
                "action_id"
            ]
            == update["action_id"]
        )
        with pytest.raises(CommandError, match="different request"):
            service.propose_update(result["plan_id"], "Rest", "Changed content", 2, "edit")
    finally:
        reopened.close()


def test_concurrent_updates_cannot_overwrite_each_other(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    db = connect(path)
    service = CommandService(db, clock=lambda: NOW)
    proposal, token = pending(service)
    plan = service.commit(proposal["action_id"], token)
    requests = []
    for title in ("Ride", "Rest"):
        update = service.propose_update(plan["plan_id"], title, "User request", 1, title)
        requests.append(
            (update["action_id"], service.approve(update["action_id"])["approval_token"])
        )
    barrier = Barrier(2)

    def commit(request):
        connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            worker = CommandService(connection, clock=lambda: NOW)
            barrier.wait(timeout=10)
            try:
                return worker.commit(*request)
            except CommandError as exc:
                return str(exc)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, requests))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any(isinstance(result, str) and "version conflict" in result for result in results)
    assert service.get_plan(plan["plan_id"])["version"] == 2
    assert (
        db.execute("SELECT count(*) FROM command_actions WHERE status='committed'").fetchone()[0]
        == 2
    )
    db.close()


def test_reject_invalid_lengths_and_unbounded_expiration(service):
    for title in ("", " ", "x" * 201):
        with pytest.raises(CommandError):
            service.draft(title, "Details", "key")
    with pytest.raises(CommandError):
        service.draft("Valid", "x" * 8001, "key")
    with pytest.raises(CommandError):
        service.draft("Valid", "Details", " ")
    proposal = service.draft("Valid", "Details", "key")
    for ttl in (0, 3601, True):
        with pytest.raises(CommandError):
            service.approve(proposal["action_id"], ttl_seconds=ttl)
    assert service.get_action(proposal["action_id"])["status"] == "proposed"
