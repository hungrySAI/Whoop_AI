import json
import subprocess
import sys
from pathlib import Path

import pytest

from whoop_copilot import live_cli
from whoop_copilot.cli import execute, parser

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "whoop_copilot.cli", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_export_inspection_and_explicit_synthetic_import_are_runnable(tmp_path):
    sample = ROOT / "tests/fixtures/export_sample.csv"
    profile = ROOT / "examples/whoop-export-mapping.example.json"
    inspected = run_cli("export-inspect", sample)
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout)["members"][0]["row_count"] == 2
    assert "Invented headers" not in inspected.stdout
    args = [
        "--db",
        tmp_path / "export.sqlite3",
        "import-export",
        sample,
        "--profile",
        profile,
        "--exported-at",
        "2026-09-05T00:00:00Z",
    ]
    rejected = run_cli(*args)
    assert rejected.returncode == 2
    assert "--synthetic" in json.loads(rejected.stderr)["error"]
    imported = run_cli(*args, "--synthetic")
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["inserted"] == 2
    replay = run_cli(*args, "--synthetic")
    assert json.loads(replay.stdout)["duplicates"] == 2


def test_network_sync_cannot_write_to_default_synthetic_environment(tmp_path):
    result = run_cli(
        "--db",
        tmp_path / "synthetic.sqlite3",
        "whoop",
        "sync",
        "--start",
        "2026-08-01T00:00:00Z",
        "--end",
        "2026-08-03T00:00:00Z",
    )
    assert result.returncode == 2
    assert "--environment real" in json.loads(result.stderr)["error"]
    assert not (tmp_path / "synthetic.sqlite3").exists()


def test_local_storage_authorization_precedes_any_keychain_access(tmp_path, monkeypatch):
    monkeypatch.setattr(
        live_cli, "database_key", lambda *a, **k: pytest.fail("must not access keychain")
    )
    args = parser().parse_args(
        ["--db", str(tmp_path / "real.sqlite3"), "init-real", "--retention-days", "30"]
    )
    with pytest.raises(ValueError, match="authorization"):
        execute(args)
    assert not (tmp_path / "real.sqlite3").exists()


def test_encrypted_cli_init_and_query_use_injected_test_key_only(tmp_path, monkeypatch):
    monkeypatch.setattr(live_cli, "database_key", lambda *a, **k: bytes(range(32)))
    path = tmp_path / "real.sqlite3"
    args = parser().parse_args(
        ["--db", str(path), "init-real", "--retention-days", "30", "--accept-local-storage"]
    )
    assert execute(args)["environment"] == "real"
    query = parser().parse_args(
        [
            "--environment",
            "real",
            "--db",
            str(path),
            "analyze",
            "whoop.hrv_rmssd",
            "--start",
            "2026-08-01T00:00:00Z",
            "--end",
            "2026-08-03T00:00:00Z",
        ]
    )
    result = execute(query)
    assert result["evidence"]["environment"] == "real"
    assert result["result"]["mean"] is None


def test_forget_requires_matching_source_before_opening_store(tmp_path, monkeypatch):
    monkeypatch.setattr(live_cli, "store_for", lambda *a: pytest.fail("must not open store"))
    args = parser().parse_args(["forget-source", "whoop", "--confirm-provider", "whoop_export"])
    with pytest.raises(ValueError, match="exact source"):
        execute(args)
