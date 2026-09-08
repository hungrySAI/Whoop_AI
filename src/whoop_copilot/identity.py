"""WHOOP UUID identities and replay keys without rewriting raw source evidence."""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, replace
from uuid import UUID

from .contracts import ActivityInput, ObservationInput, SourceRecordInput, timestamp

UUID_RESOURCES = frozenset({"sleep", "workout"})
INGEST_KEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_ingest_keys(
 connection_id TEXT NOT NULL REFERENCES source_connections(id),
 resource TEXT NOT NULL, external_id TEXT NOT NULL, canonical_hash TEXT NOT NULL,
 revision_id INTEGER NOT NULL REFERENCES source_revisions(id) ON DELETE CASCADE,
 PRIMARY KEY(connection_id,resource,external_id,canonical_hash)
);
CREATE INDEX IF NOT EXISTS ingest_keys_revision ON source_ingest_keys(revision_id);
"""


def canonical_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("WHOOP sleep/workout identity must be a UUID") from None


def normalize_source_identity(record: SourceRecordInput) -> SourceRecordInput:
    """Normalize internal identity only; source payload remains exactly as received."""
    if record.provider != "whoop" or record.resource not in UUID_RESOURCES:
        return record
    identity = canonical_uuid(record.external_id)
    activities = tuple(
        replace(activity, external_id=canonical_uuid(activity.external_id))
        if activity.kind == f"whoop_{record.resource}"
        else activity
        for activity in record.activities
    )
    if any(
        activity.kind == f"whoop_{record.resource}" and activity.external_id != identity
        for activity in activities
    ):
        raise ValueError("WHOOP activity identity differs from its source identity")
    raw = record.payload.get("record")
    if isinstance(raw, dict) and "id" in raw and canonical_uuid(raw["id"]) != identity:
        raise ValueError("WHOOP payload identity differs from its source identity")
    return replace(record, external_id=identity, activities=activities)


def source_dedup_hash(record: SourceRecordInput) -> str | None:
    """A separate replay key; existing evidence content_hash values stay unchanged.

    Use the persisted normalization of times and numeric values, so a key rebuilt
    from a schema-2 row matches a fresh ingestion. Only the known UUID field in a
    copy of the raw payload is canonicalized; the stored raw payload is untouched.
    """
    if record.provider != "whoop" or record.resource not in UUID_RESOURCES:
        return None
    record = normalize_source_identity(record)
    fingerprint = asdict(record)
    fingerprint["source_updated_at"] = timestamp(record.source_updated_at)
    fingerprint["metadata"].pop("captured_at", None)
    raw = fingerprint["payload"].get("record")
    if isinstance(raw, dict) and "id" in raw:
        raw["id"] = canonical_uuid(raw["id"])
    for observation in fingerprint["observations"]:
        for key in ("value", "original_value"):
            observation[key] = float(observation[key])
        observation["start_at"] = timestamp(observation["start_at"])
        if observation["end_at"]:
            observation["end_at"] = timestamp(observation["end_at"])
    for activity in fingerprint["activities"]:
        activity["start_at"] = timestamp(activity["start_at"])
        if activity["end_at"]:
            activity["end_at"] = timestamp(activity["end_at"])
    encoded = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def find_ingest_duplicate(db, connection_id: str, record: SourceRecordInput) -> int | None:
    key = source_dedup_hash(record)
    if key is None:
        return None
    row = db.execute(
        """SELECT revision_id FROM source_ingest_keys
        WHERE connection_id=? AND resource=? AND external_id=? AND canonical_hash=?""",
        (connection_id, record.resource, canonical_uuid(record.external_id), key),
    ).fetchone()
    return row[0] if row else None


def register_ingest_key(db, connection_id: str, record: SourceRecordInput, revision_id: int):
    key = source_dedup_hash(record)
    if key is not None:
        db.execute(
            """INSERT INTO source_ingest_keys VALUES (?,?,?,?,?)
            ON CONFLICT(connection_id,resource,external_id,canonical_hash)
            DO UPDATE SET revision_id=excluded.revision_id""",
            (connection_id, record.resource, canonical_uuid(record.external_id), key, revision_id),
        )


def _stored_record(db, row) -> SourceRecordInput:
    observations = tuple(
        ObservationInput(
            **{key: item[key] for key in ObservationInput.__dataclass_fields__}
            | {"official": bool(item["official"])}
        )
        for item in db.execute(
            "SELECT * FROM observations WHERE revision_id=? ORDER BY id", (row["id"],)
        )
    )
    activities = tuple(
        ActivityInput(**{key: item[key] for key in ActivityInput.__dataclass_fields__})
        for item in db.execute(
            "SELECT * FROM activities WHERE revision_id=? ORDER BY id", (row["id"],)
        )
    )
    return SourceRecordInput(
        provider="whoop",
        resource=row["resource"],
        external_id=row["external_id"],
        source_updated_at=row["source_updated_at"],
        payload=json.loads(row["payload"]),
        observations=observations,
        activities=activities,
        parser_version=row["parser_version"],
        deleted=bool(row["deleted"]),
        metadata=json.loads(row["metadata"]),
    )


def migrate_whoop_identities(db) -> dict:
    """Merge schema-2 UUID aliases inside the caller's migration transaction.

    All historical rows, IDs, hashes, raw payloads and capture/expiry times stay.
    Replay-key collisions select one retained row without deleting other history.
    At equal provider timestamps, a deletion wins over a nondeleted alias.
    """
    if not db.in_transaction:
        raise ValueError("WHOOP identity migration requires a caller-owned transaction")
    for statement in INGEST_KEY_SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    groups = defaultdict(list)
    for row in db.execute(
        """SELECT r.* FROM source_revisions r
        JOIN source_connections c ON c.id=r.connection_id
        WHERE c.provider='whoop' AND r.resource IN ('sleep','workout') ORDER BY r.id"""
    ).fetchall():
        groups[(row["connection_id"], row["resource"], canonical_uuid(row["external_id"]))].append(
            dict(row)
        )
    affected = set()
    for (connection_id, resource, identity), rows in groups.items():
        changed = any(row["external_id"] != identity for row in rows)
        current = None
        for row in rows:
            record = normalize_source_identity(_stored_record(db, row))
            if changed:
                incoming = (timestamp(row["source_updated_at"]), bool(row["deleted"]))
                becomes_current = current is None or incoming >= current
                if becomes_current:
                    current = incoming
                db.execute(
                    "UPDATE source_revisions SET external_id=?,became_current=? WHERE id=?",
                    (identity, int(becomes_current), row["id"]),
                )
                affected.add(row["id"])
            for activity in record.activities:
                if activity.kind == f"whoop_{resource}":
                    db.execute(
                        "UPDATE activities SET external_id=? WHERE revision_id=? AND kind=?",
                        (activity.external_id, row["id"], activity.kind),
                    )
            register_ingest_key(db, connection_id, record, row["id"])
    invalidated = 0
    if affected:
        for row in db.execute("SELECT id,input_revision_ids FROM analysis_runs").fetchall():
            if affected.intersection(json.loads(row["input_revision_ids"])):
                db.execute("DELETE FROM analysis_runs WHERE id=?", (row["id"],))
                invalidated += 1
        # Recompute jobs carry only requests, not source IDs. Rebuild them on the
        # next query/import rather than replaying old duplicate-identity results.
        db.execute("DELETE FROM tasks WHERE kind='recompute'")
    return {"identity_revisions": len(affected), "invalidated_analyses": invalidated}
