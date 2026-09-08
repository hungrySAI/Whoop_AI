"""Exercise the user-facing synthetic demo and trusted approval boundary."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(db, *args):
    return subprocess.run(
        [sys.executable, "-m", "whoop_copilot.cli", "--db", str(db), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input="",
        timeout=20,
    )


def payload(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_demo_is_repeatable_and_keeps_training_unconfirmed(tmp_path):
    db = tmp_path / "demo.sqlite3"
    first = payload(cli(db, "demo"))
    second = payload(cli(db, "demo"))
    assert first["imports"]["whoop"]["inserted"] == 14
    assert first["imports"]["body"]["inserted"] == 14
    assert second["imports"]["whoop"]["duplicates"] == 14
    assert second["imports"]["body"]["duplicates"] == 14
    assert first["analysis"] == second["analysis"]
    assert first["analysis"]["result"]["mean"] == 53
    assert first["training_draft"] == second["training_draft"]
    plan_id = first["training_draft"]["plan_id"]
    assert payload(cli(db, "plan", "get", plan_id))["status"] == "draft"


def test_operator_must_accept_the_exact_reviewed_action_before_token_issue(tmp_path):
    db = tmp_path / "plans.sqlite3"
    draft = payload(cli(db, "plan", "draft", "--title", "合成训练", "--key", "cli-request"))
    action_id = draft["action_id"]
    automatic = cli(db, "plan", "approve", action_id)
    assert automatic.returncode == 2
    assert "interactive operator" in automatic.stderr
    wrong = cli(db, "plan", "approve", action_id, "--accept-hash", "not-reviewed")
    assert wrong.returncode == 2
    assert payload(cli(db, "plan", "inspect", action_id))["status"] == "proposed"
    approval = payload(
        cli(db, "plan", "approve", action_id, "--accept-hash", draft["payload_hash"])
    )
    committed = payload(cli(db, "plan", "commit", action_id, "--token", approval["approval_token"]))
    replay = payload(cli(db, "plan", "commit", action_id, "--token", approval["approval_token"]))
    assert replay == committed
    assert committed["version"] == 1
    assert committed["status"] == "active"


def test_cli_errors_are_structured_and_do_not_report_success(tmp_path):
    result = cli(
        tmp_path / "error.sqlite3",
        "analyze",
        "not.registered",
        "--start",
        "2026-08-01T00:00:00Z",
        "--end",
        "2026-08-15T00:00:00Z",
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "registered" in json.loads(result.stderr)["error"]
