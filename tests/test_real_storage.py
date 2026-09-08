import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.commands import CommandService
from whoop_copilot.protection import DATABASE_ERRORS, LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store, restore_backup

KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
QUERY = {
    "metric": "whoop.hrv_rmssd",
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-08-03T00:00:00Z",
}


def records():
    return normalize_api(
        json.loads(FIXTURE.read_text())["resources"],
        acquired_at="2026-09-05T00:00:00Z",
        synthetic=False,
    )


def real(path, clock=lambda: "2026-09-05T12:00:00Z"):
    return Store(
        path,
        environment="real",
        encryption_key=KEY,
        policy=LocalPolicy(1, owner_authorized=True),
        clock=clock,
    )


def test_real_requires_key_and_explicit_policy_before_creating_files(tmp_path):
    path = tmp_path / "private.sqlite3"
    with pytest.raises(ValueError, match="protected encryption key"):
        Store(path, environment="real")
    assert not path.exists()
    with pytest.raises(ValueError, match="explicit local storage policy"):
        Store(path, environment="real", encryption_key=KEY)
    assert not path.exists()
    with pytest.raises(ValueError, match="authorization"):
        LocalPolicy(1)


def test_sqlcipher_encrypts_data_wal_and_rejects_wrong_keys(tmp_path):
    path = tmp_path / "private.sqlite3"
    with real(path) as store:
        store.ingest(records())
        assert CopilotService(store).analyze(**QUERY)["evidence"]["environment"] == "real"
        for file in tmp_path.glob("*.sqlite3*"):
            assert b"fixture@example.invalid" not in file.read_bytes()
            assert b"SQLite format 3" not in file.read_bytes()[:16]
    with sqlite3.connect(path) as plaintext:
        with pytest.raises(sqlite3.DatabaseError):
            plaintext.execute("SELECT * FROM settings").fetchall()
    with pytest.raises(DATABASE_ERRORS):
        Store(path, environment="real", encryption_key=OTHER_KEY)
    with real(path) as reopened:
        assert reopened.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 6


def test_environments_and_authorized_sources_cannot_be_mixed(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3") as synthetic:
        with pytest.raises(ValueError, match="environment"):
            synthetic.ingest(records())
    with real(tmp_path / "real.sqlite3") as store:
        samples = normalize_api(
            json.loads(FIXTURE.read_text())["resources"],
            acquired_at="2026-09-05T00:00:00Z",
            synthetic=True,
        )
        with pytest.raises(ValueError, match="environment"):
            store.ingest(samples)
    with Store(
        tmp_path / "restricted.sqlite3",
        environment="real",
        encryption_key=KEY,
        policy=LocalPolicy(1, sources=("whoop_export",), owner_authorized=True),
    ) as store:
        with pytest.raises(ValueError, match="outside"):
            store.ingest(records())


def test_retention_starts_at_capture_and_retrieval_does_not_extend_existing_version(tmp_path):
    record = next(r for r in records() if r.resource == "recovery")
    with real(tmp_path / "real.sqlite3") as store:
        store.ingest([record])
        row = store.db.execute("SELECT known_at,expires_at FROM source_revisions").fetchone()
        assert row["known_at"] == "2026-09-05T12:00:00.000000+00:00"
        assert row["expires_at"] == "2026-09-06T00:00:00.000000+00:00"
        fetched_again = replace(
            record, metadata={**record.metadata, "captured_at": "2026-09-05T06:00:00Z"}
        )
        assert store.ingest([fetched_again])["duplicates"] == 1
        assert (
            store.db.execute("SELECT expires_at FROM source_revisions").fetchone()[0]
            == row["expires_at"]
        )


@pytest.mark.parametrize("captured_at", ["2026-09-03T00:00:00Z", "2026-09-06T00:00:00Z"])
def test_expired_or_future_capture_cannot_be_normalized_into_live_storage(tmp_path, captured_at):
    record = next(r for r in records() if r.resource == "recovery")
    with real(tmp_path / "real.sqlite3") as store:
        with pytest.raises(ValueError, match="expired|future"):
            store.ingest(
                [replace(record, metadata={**record.metadata, "captured_at": captured_at})]
            )
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM source_connections").fetchone()[0] == 0


def test_encrypted_backup_rekeys_and_restore_preserves_results(tmp_path):
    path, backup, restored = [
        tmp_path / name for name in ("real.sqlite3", "backup.sqlite3", "restored.sqlite3")
    ]
    with real(path) as store:
        store.ingest(records())
        run = CopilotService(store).analyze(**QUERY)
        store.backup(backup, target_key=OTHER_KEY)
    restore_backup(
        backup,
        restored,
        environment="real",
        encryption_key=OTHER_KEY,
        target_key=KEY,
        clock=lambda: "2026-09-05T14:00:00Z",
    )
    with real(restored) as store:
        assert CopilotService(store).reproduce(run["run_id"])["matches"]
    assert b"fixture@example.invalid" not in backup.read_bytes()


@pytest.mark.parametrize("cleanup", ["forget", "expiry"])
def test_restore_owns_only_new_backups_and_cannot_clean_source_backups(tmp_path, cleanup):
    original_path = tmp_path / "original.sqlite3"
    first_backup = tmp_path / "first-backup.sqlite3"
    restore_source = tmp_path / "restore-source.sqlite3"
    restored_path = tmp_path / "restored.sqlite3"
    restored_backup = tmp_path / "restored-backup.sqlite3"
    now = ["2026-09-05T12:00:00Z"]
    with real(original_path) as original:
        original.ingest(records())
        CopilotService(original).analyze(**QUERY)
        CommandService(original.db).draft("Local plan", "Synthetic detail", "restore-plan")
        original.backup(first_backup)
        original.backup(restore_source)
        inventory = [tuple(row) for row in original.db.execute("SELECT * FROM managed_backups")]
        originals = {path: path.read_bytes() for path in (first_backup, restore_source)}
        table_names = [
            row[0]
            for row in original.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] != "managed_backups"
        ]
        original_rows = {
            table: [tuple(row) for row in original.db.execute(f'SELECT * FROM "{table}"')]
            for table in table_names
        }

        restore_backup(
            restore_source,
            restored_path,
            environment="real",
            encryption_key=KEY,
            clock=lambda: now[0],
        )
        with real(restored_path, lambda: now[0]) as restored:
            assert not restored.db.execute("SELECT * FROM managed_backups").fetchall()
            assert {
                table: [tuple(row) for row in restored.db.execute(f'SELECT * FROM "{table}"')]
                for table in table_names
            } == original_rows
            assert not restored.db.execute("PRAGMA foreign_key_check").fetchall()
            restored.backup(restored_backup)
            assert [row[0] for row in restored.db.execute("SELECT path FROM managed_backups")] == [
                str(restored_backup.resolve())
            ]
            if cleanup == "forget":
                assert restored.forget_source("whoop")["removed_revisions"] == 6
            else:
                now[0] = "2026-09-07T12:00:00Z"
                assert restored.purge_expired()["purged_revisions"] == 6
            assert not restored_backup.exists()
            assert not restored.db.execute("SELECT * FROM managed_backups").fetchall()

        assert {path: path.read_bytes() for path in originals} == originals
        assert [
            tuple(row) for row in original.db.execute("SELECT * FROM managed_backups")
        ] == inventory
        assert {
            table: [tuple(row) for row in original.db.execute(f'SELECT * FROM "{table}"')]
            for table in table_names
        } == original_rows
        # The original database still owns and cleans its own inventory.
        original.forget_source("whoop")
        assert not first_backup.exists()
        assert not restore_source.exists()
        assert restored_path.exists()


def test_expiry_removes_raw_derived_tasks_and_managed_backups_without_resurrection(tmp_path):
    now = ["2026-09-05T12:00:00Z"]
    path, backup, copied = [
        tmp_path / name for name in ("real.sqlite3", "backup.sqlite3", "unmanaged-copy.sqlite3")
    ]
    with real(path, lambda: now[0]) as store:
        store.ingest(records())
        run = CopilotService(store).analyze(**QUERY)
        plan = CommandService(store.db).draft("Own plan", "User-authored note", "retain-plan")
        store.backup(backup)
        copied.write_bytes(backup.read_bytes())
        now[0] = "2026-09-07T12:00:00Z"
        assert store.purge_expired()["purged_revisions"] == 6
        for table in ("source_revisions", "observations", "activities", "analysis_runs", "tasks"):
            assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert not backup.exists()
        assert CommandService(store.db).get_plan(plan["plan_id"])["title"] == "Own plan"
        with pytest.raises(ValueError, match="Unknown analysis"):
            CopilotService(store).get_run(run["run_id"])
    with pytest.raises(ValueError, match="expired"):
        restore_backup(
            copied,
            tmp_path / "must-not-exist.sqlite3",
            environment="real",
            encryption_key=KEY,
            clock=lambda: now[0],
        )
    assert not (tmp_path / "must-not-exist.sqlite3").exists()


def test_forget_source_clears_references_and_refuses_replaced_backup(tmp_path):
    backup = tmp_path / "backup.sqlite3"
    with real(tmp_path / "real.sqlite3") as store:
        store.ingest(records())
        store.backup(backup)
        replacement = tmp_path / "replacement"
        replacement.write_text("unrelated file")
        replacement.replace(backup)
        with pytest.raises(ValueError, match="replaced"):
            store.forget_source("whoop")
        assert backup.read_text() == "unrelated file"
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 6
        backup.unlink()
        assert store.forget_source("whoop")["removed_revisions"] == 6


def test_v1_migration_preserves_existing_f0_evidence(tmp_path):
    # Construct the released v1 layout by removing only F1 additions from the schema.
    from whoop_copilot.commands import COMMAND_SCHEMA
    from whoop_copilot.storage import APPLICATION_ID, SCHEMA

    path = tmp_path / "v1.sqlite3"
    db = sqlite3.connect(path)
    schema = SCHEMA.replace(", expires_at TEXT", "").replace(
        "environment IN ('synthetic','real')", "environment='synthetic'"
    )
    db.executescript(schema + COMMAND_SCHEMA)
    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    db.execute("INSERT INTO schema_migrations VALUES (1,'2026-09-05T00:00:00Z')")
    db.executemany(
        "INSERT INTO settings VALUES (?,?)",
        [("environment", "synthetic"), ("subject_id", "preserved-subject")],
    )
    db.commit()
    db.close()
    with Store(path) as migrated:
        assert (
            migrated.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[0]
            == "preserved-subject"
        )
        assert [
            r[0]
            for r in migrated.db.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [1, 2, 3]
        assert not migrated.db.execute("PRAGMA foreign_key_check").fetchall()
