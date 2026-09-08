"""Canonical WHOOP UUIDs, replay keys, and lossless legacy alias migration."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.contracts import timestamp
from whoop_copilot.identity import (
    find_ingest_duplicate,
    migrate_whoop_identities,
    normalize_source_identity,
    source_dedup_hash,
)
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import SCHEMA_VERSION, Store, atomic, canonical, digest

FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
NOW = "2026-09-05T12:00:00Z"
START, END = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"


def sample(
    resource="sleep", *, upper=False, value=70, updated="2026-08-01T14:00:00Z", synthetic=True
):
    resources = json.loads(FIXTURE.read_text())["resources"]
    raw = resources[resource][0]
    raw["id"] = raw["id"].upper() if upper else raw["id"].lower()
    raw["updated_at"] = updated
    raw["score"]["sleep_performance_percentage" if resource == "sleep" else "strain"] = value
    return next(
        record
        for record in normalize_api(resources, acquired_at=NOW, synthetic=synthetic)
        if record.resource == resource
    )


def legacy_sample(**options):
    record = sample(**options)
    identity = record.payload["record"]["id"]
    return replace(
        record,
        external_id=identity,
        activities=tuple(replace(activity, external_id=identity) for activity in record.activities),
    )


def insert_legacy(store, record, *, known_at=NOW):
    """Insert the original schema-2 representation, bypassing the new normalizer."""
    subject = store.db.execute("SELECT value FROM settings WHERE key='subject_id'").fetchone()[0]
    store.db.execute(
        "INSERT OR IGNORE INTO source_connections VALUES (?,?,?,?,?,?,?)",
        ("legacy-whoop", subject, "whoop", "999999", store.environment, "fixture", NOW),
    )
    fingerprint = asdict(record)
    fingerprint["metadata"].pop("captured_at", None)
    expires_at = (
        timestamp((datetime.fromisoformat(timestamp(NOW)) + timedelta(days=30)).isoformat())
        if store.environment == "real"
        else None
    )
    cursor = store.db.execute(
        """INSERT INTO source_revisions
        (connection_id,resource,external_id,source_updated_at,known_at,content_hash,
        parser_version,payload,metadata,deleted,became_current,expires_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "legacy-whoop",
            record.resource,
            record.external_id,
            timestamp(record.source_updated_at),
            timestamp(known_at),
            digest(fingerprint),
            record.parser_version,
            canonical(record.payload),
            canonical(record.metadata),
            int(record.deleted),
            1,
            expires_at,
        ),
    )
    for table, values in (("observations", record.observations), ("activities", record.activities)):
        for value in values:
            fields = asdict(value)
            fields["start_at"] = timestamp(fields["start_at"])
            if fields["end_at"]:
                fields["end_at"] = timestamp(fields["end_at"])
            columns = ",".join(["revision_id", *fields])
            marks = ",".join("?" for _ in range(len(fields) + 1))
            store.db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                (cursor.lastrowid, *fields.values()),
            )
    return cursor.lastrowid


@pytest.mark.parametrize("resource", ["sleep", "workout"])
def test_normalization_preserves_raw_uuid_and_rejects_single_batch_aliases(resource):
    record = sample(resource, upper=True)
    assert record.external_id == record.payload["record"]["id"].lower()
    assert record.activities[0].external_id == record.external_id
    assert record.payload["record"]["id"] == record.payload["record"]["id"].upper()
    resources = json.loads(FIXTURE.read_text())["resources"]
    alias = {**resources[resource][0], "id": resources[resource][0]["id"].upper()}
    resources[resource].append(alias)
    with pytest.raises(ValueError, match="duplicate resource identities"):
        normalize_api(resources, acquired_at=NOW, synthetic=True)


@pytest.mark.parametrize("resource", ["sleep", "workout"])
def test_direct_record_alias_normalization_and_replay_key_preserve_raw(resource):
    upper, lower = legacy_sample(resource=resource, upper=True), sample(resource)
    raw = canonical(upper.payload)
    normalized = normalize_source_identity(upper)
    assert normalized.external_id == lower.external_id
    assert normalized.activities[0].external_id == lower.external_id
    assert canonical(upper.payload) == raw == canonical(normalized.payload)
    assert source_dedup_hash(upper) == source_dedup_hash(lower)
    assert source_dedup_hash(upper) != source_dedup_hash(sample(resource, value=90))
    assert normalize_source_identity(replace(upper, provider="whoop_export")) == replace(
        upper, provider="whoop_export"
    )
    assert source_dedup_hash(replace(upper, provider="whoop_export")) is None
    with pytest.raises(ValueError, match="differs"):
        normalize_source_identity(
            replace(upper, external_id="11111111-1111-1111-1111-111111111111")
        )


@pytest.mark.parametrize(
    "resource,metric", [("sleep", "whoop.sleep_performance"), ("workout", "whoop.strain")]
)
def test_store_direct_alias_replay_and_correction_count_once(tmp_path, resource, metric):
    with Store(tmp_path / "aliases.sqlite3") as store:
        first = legacy_sample(resource=resource, upper=True)
        assert store.ingest([first])["inserted"] == 1
        assert store.ingest([sample(resource)])["duplicates"] == 1
        corrected = legacy_sample(
            resource=resource, upper=True, value=90, updated="2026-08-02T14:00:00Z"
        )
        assert store.ingest([corrected])["inserted"] == 1
        result = CopilotService(store).analyze(metric, START, END, resource=resource)["result"]
        assert result["count"] == 1 and result["mean"] == 90
        assert len(store.current_sources(resource)) == 1


@pytest.mark.parametrize("environment", ["synthetic", "real"])
def test_migration_preserves_history_and_corrects_late_older_alias(tmp_path, environment):
    options = (
        {}
        if environment == "synthetic"
        else {
            "environment": "real",
            "encryption_key": bytes(range(32)),
            "policy": LocalPolicy(30, owner_authorized=True),
        }
    )
    with Store(
        tmp_path / "legacy.sqlite3", clock=lambda: "2026-09-05T12:04:00Z", **options
    ) as store:
        values = [
            legacy_sample(synthetic=environment == "synthetic"),
            legacy_sample(
                upper=True,
                value=90,
                updated="2026-08-03T14:00:00Z",
                synthetic=environment == "synthetic",
            ),
            legacy_sample(
                value=80, updated="2026-08-02T14:00:00Z", synthetic=environment == "synthetic"
            ),
        ]
        with atomic(store.db):
            ids = [
                insert_legacy(store, value, known_at=f"2026-09-05T12:0{index}:00Z")
                for index, value in enumerate(values, start=1)
            ]
        old_run = CopilotService(store).analyze("whoop.sleep_performance", START, END)
        immutable = [
            tuple(row)
            for row in store.db.execute(
                """SELECT id,content_hash,payload,metadata,source_updated_at,known_at,expires_at
            FROM source_revisions ORDER BY id"""
            )
        ]
        observations = [tuple(row) for row in store.db.execute("SELECT * FROM observations")]
        with atomic(store.db):
            migrated = migrate_whoop_identities(store.db)
        assert migrated == {"identity_revisions": 3, "invalidated_analyses": 1}
        assert [
            tuple(row)
            for row in store.db.execute(
                """SELECT id,content_hash,payload,metadata,source_updated_at,known_at,expires_at
            FROM source_revisions ORDER BY id"""
            )
        ] == immutable
        assert [
            tuple(row) for row in store.db.execute("SELECT * FROM observations")
        ] == observations
        current = store.current_sources("sleep")
        assert len(current) == 1 and current[0]["id"] == ids[1]
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(ValueError, match="Unknown analysis"):
            CopilotService(store).get_run(old_run["run_id"])
        assert find_ingest_duplicate(store.db, "legacy-whoop", values[1]) == ids[1]
        assert store.ingest([values[1]])["duplicates"] == 1
        past = CopilotService(store).analyze(
            "whoop.sleep_performance", START, END, as_of="2026-09-05T12:01:30Z"
        )
        assert past["result"]["count"] == 1 and past["result"]["mean"] == 70
        assert past["evidence"]["references"][0]["id"] == ids[0]


def test_migration_key_collision_keeps_all_rows_and_cascades_on_expiry(tmp_path):
    with Store(tmp_path / "collision.sqlite3") as store:
        with atomic(store.db):
            ids = [insert_legacy(store, legacy_sample(upper=upper)) for upper in (False, True)]
            migrate_whoop_identities(store.db)
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 2
        assert store.db.execute("SELECT COUNT(*) FROM source_ingest_keys").fetchone()[0] == 1
        assert find_ingest_duplicate(store.db, "legacy-whoop", sample()) == ids[1]
        assert len(store.current_sources("sleep")) == 1
        with atomic(store.db):
            store.db.execute("DELETE FROM observations WHERE revision_id=?", (ids[1],))
            store.db.execute("DELETE FROM activities WHERE revision_id=?", (ids[1],))
            store.db.execute("DELETE FROM source_revisions WHERE id=?", (ids[1],))
        assert not store.db.execute("SELECT * FROM source_ingest_keys").fetchall()
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1


def test_migration_tombstone_wins_same_source_time_despite_later_live_alias(tmp_path):
    with Store(tmp_path / "tombstone.sqlite3") as store:
        live = legacy_sample(updated="2026-08-02T14:00:00Z")
        deleted = replace(
            legacy_sample(upper=True, updated=live.source_updated_at),
            deleted=True,
            observations=(),
            activities=(),
            payload={"synthetic": True},
        )
        with atomic(store.db):
            deleted_id = insert_legacy(store, deleted)
            insert_legacy(store, live)
            migrate_whoop_identities(store.db)
        assert store.current_sources("sleep") == []
        assert store.current_sources("sleep", include_deleted=True)[0]["id"] == deleted_id
        store.ingest([sample(value=95, updated=live.source_updated_at)])
        assert store.current_sources("sleep") == []
        store.ingest([sample(value=95, updated="2026-08-03T14:00:00Z")])
        assert len(store.current_sources("sleep")) == 1


@pytest.mark.parametrize("environment", ["synthetic", "real"])
def test_schema_two_reopen_migrates_aliases_once_without_replay_duplicates(tmp_path, environment):
    options = (
        {}
        if environment == "synthetic"
        else {
            "environment": "real",
            "encryption_key": bytes(range(32)),
            "policy": LocalPolicy(30, owner_authorized=True),
        }
    )
    path = tmp_path / "schema-two.sqlite3"
    with Store(path, clock=lambda: NOW, **options) as store:
        with atomic(store.db):
            ids = [
                insert_legacy(
                    store, legacy_sample(upper=upper, synthetic=environment == "synthetic")
                )
                for upper in (False, True)
            ]
            store.db.execute("DROP TABLE source_ingest_keys")
            store.db.execute("DELETE FROM schema_migrations")
            store.db.execute("INSERT INTO schema_migrations VALUES (2,?)", (NOW,))
    with Store(path, clock=lambda: NOW, **options) as store:
        assert store.db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (
            SCHEMA_VERSION
        )
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 2
        assert store.current_sources("sleep")[0]["id"] == ids[1]
        assert store.ingest([sample(synthetic=environment == "synthetic")])["duplicates"] == 1
        expected = [tuple(row) for row in store.db.execute("SELECT * FROM source_revisions")]
    with Store(path, clock=lambda: NOW, **options) as store:
        assert [
            tuple(row) for row in store.db.execute("SELECT * FROM source_revisions")
        ] == expected
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
