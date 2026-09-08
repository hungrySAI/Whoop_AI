"""SQLite source revisions, normalized records, analysis snapshots and durable work.

Synthetic SQLite and explicitly authorized, SQLCipher-protected real environments.
"""

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import SourceRecordInput, timestamp, utc_now
from .protection import LocalPolicy, connect

APPLICATION_ID = 0x57485043
SCHEMA_VERSION = 3
RECOMPUTE_BATCH_SIZE = 100
DEFAULT_DB = Path("runtime/synthetic/copilot.sqlite3")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def atomic(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


SCHEMA = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_connections(
 id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, provider TEXT NOT NULL UNIQUE,
 external_subject TEXT, environment TEXT NOT NULL CHECK(environment IN ('synthetic','real')),
 policy TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE source_revisions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 connection_id TEXT NOT NULL REFERENCES source_connections(id),
 resource TEXT NOT NULL, external_id TEXT NOT NULL, source_updated_at TEXT NOT NULL,
 known_at TEXT NOT NULL, content_hash TEXT NOT NULL, parser_version TEXT NOT NULL,
 payload TEXT NOT NULL, metadata TEXT NOT NULL, deleted INTEGER NOT NULL,
 became_current INTEGER NOT NULL, expires_at TEXT,
 UNIQUE(connection_id, resource, external_id, content_hash)
);
CREATE INDEX revisions_identity ON source_revisions(connection_id,resource,external_id);
CREATE INDEX revisions_known ON source_revisions(known_at);
CREATE TABLE observations(
 id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES source_revisions(id),
 metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
 original_value REAL NOT NULL, original_unit TEXT NOT NULL,
 start_at TEXT NOT NULL, end_at TEXT, time_precision TEXT NOT NULL,
 quality TEXT NOT NULL, official INTEGER NOT NULL
);
CREATE INDEX observations_metric ON observations(metric,start_at);
CREATE TABLE activities(
 id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES source_revisions(id),
 kind TEXT NOT NULL, external_id TEXT NOT NULL, start_at TEXT NOT NULL,
 end_at TEXT, timezone_offset TEXT NOT NULL
);
CREATE TABLE analysis_runs(
 id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, request TEXT NOT NULL,
 catalog_head INTEGER NOT NULL, input_revision_ids TEXT NOT NULL,
 algorithm_version TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT NOT NULL,
 created_at TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE tasks(
 id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_until TEXT, lease_token TEXT, last_error TEXT,
 created_at TEXT NOT NULL, completed_at TEXT
);
"""

F1_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs(
 id TEXT PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, completed_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS sync_pages(
 run_id TEXT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
 resource TEXT NOT NULL, page_no INTEGER NOT NULL, records TEXT NOT NULL,
 headers TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(run_id,resource,page_no)
);
CREATE TABLE IF NOT EXISTS managed_backups(
 path TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT
);
"""

HARDENING_SCHEMA = """
CREATE INDEX IF NOT EXISTS observations_revision ON observations(revision_id,metric);
CREATE INDEX IF NOT EXISTS activities_revision ON activities(revision_id);
CREATE INDEX IF NOT EXISTS revisions_current
 ON source_revisions(connection_id,resource,external_id,id DESC) WHERE became_current=1;
CREATE INDEX IF NOT EXISTS revisions_expiry ON source_revisions(expires_at);
CREATE INDEX IF NOT EXISTS sync_runs_created ON sync_runs(created_at);
CREATE INDEX IF NOT EXISTS sync_pages_fetched ON sync_pages(fetched_at);
"""


class Store:
    def __init__(
        self,
        path: Path | str = DEFAULT_DB,
        clock: Callable[[], str] = utc_now,
        *,
        environment: str = "synthetic",
        encryption_key: bytes | None = None,
        policy: LocalPolicy | None = None,
    ):
        self.path = Path(path)
        self.clock = clock
        self.environment = environment
        self.encryption_key = encryption_key
        self.policy = policy
        if environment == "real" and (
            not isinstance(encryption_key, bytes) or len(encryption_key) != 32
        ):
            raise ValueError("A real database requires a protected encryption key")
        if environment not in {"synthetic", "real"}:
            raise ValueError("Unknown environment")
        if not self.path.exists() and environment == "real" and policy is None:
            raise ValueError("Creating a real database requires an explicit local storage policy")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        new = not self.path.exists()
        if self.path.is_symlink():
            raise ValueError("Database path cannot be a symlink")
        if new:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.db = connect(self.path, environment, encryption_key)
        try:
            if not new:
                self._validate()
            else:
                self._migrate()
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=10000")
            if self.environment == "real":
                self.purge_expired()
        except BaseException:
            self.db.close()
            raise

    def _migrate(self) -> None:
        from .commands import COMMAND_SCHEMA
        from .identity import migrate_whoop_identities

        self.db.executescript(
            "BEGIN IMMEDIATE;\n" + SCHEMA + COMMAND_SCHEMA + F1_SCHEMA + HARDENING_SCHEMA
        )
        try:
            migrate_whoop_identities(self.db)
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute(
                "INSERT INTO schema_migrations VALUES (?,?)", (SCHEMA_VERSION, self.clock())
            )
            self.db.executemany(
                "INSERT INTO settings VALUES (?,?)",
                [("environment", self.environment), ("subject_id", str(uuid.uuid4()))],
            )
            if self.policy:
                self.db.execute(
                    "INSERT INTO settings VALUES ('local_policy',?)",
                    (canonical(asdict(self.policy)),),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _validate(self) -> None:
        if self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Refusing a database not created by this application")
        version = self.db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version == 1:
            self._upgrade_v1()
            version = 2
        if version not in (2, SCHEMA_VERSION):
            raise ValueError(f"Unsupported database schema {version}; expected {SCHEMA_VERSION}")
        if (
            self.db.execute("SELECT value FROM settings WHERE key='environment'").fetchone()[0]
            != self.environment
        ):
            raise ValueError("Database environment mismatch; environments cannot be relabeled")
        if self.environment == "real":
            saved = self.db.execute(
                "SELECT value FROM settings WHERE key='local_policy'"
            ).fetchone()
            if not saved:
                raise ValueError("Real storage policy is missing")
            config = json.loads(saved[0])
            config["sources"] = tuple(config["sources"])
            persisted = LocalPolicy(**config)
            if self.policy and self.policy != persisted:
                raise ValueError("Existing authorization cannot be silently replaced")
            self.policy = persisted
        if version == 2:
            self._upgrade_v2()

    def _upgrade_v2(self) -> None:
        from .identity import migrate_whoop_identities

        # Validate environment/policy first. The migration keeps original source
        # revisions and their hashes; canonical identity is a lookup concern.
        with atomic(self.db):
            # A second process may have migrated while this connection waited.
            version = self.db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version != 2:
                raise ValueError("Database schema changed while opening")
            for statement in HARDENING_SCHEMA.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            migrate_whoop_identities(self.db)
            if self.db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Migration failed foreign key validation")
            self.db.execute(
                "INSERT INTO schema_migrations VALUES (?,?)", (SCHEMA_VERSION, self.clock())
            )

    def _upgrade_v1(self) -> None:
        # Foreign keys are still disabled during constructor validation. Preserve all F0 rows.
        self.db.executescript(
            """BEGIN IMMEDIATE;
          CREATE TABLE source_connections_v2(
            id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, provider TEXT NOT NULL UNIQUE,
            external_subject TEXT, environment TEXT NOT NULL CHECK(environment IN ('synthetic','real')),
            policy TEXT NOT NULL, created_at TEXT NOT NULL);
          INSERT INTO source_connections_v2 SELECT * FROM source_connections;
          DROP TABLE source_connections;
          ALTER TABLE source_connections_v2 RENAME TO source_connections;
          ALTER TABLE source_revisions ADD COLUMN expires_at TEXT;
        """
            + F1_SCHEMA
        )
        try:
            if self.db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Migration failed foreign key validation")
            self.db.execute("INSERT INTO schema_migrations VALUES (?,?)", (2, self.clock()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _becomes_current(self, connection_id: str, record: SourceRecordInput) -> bool:
        current = self.db.execute(
            """SELECT source_updated_at,metadata,deleted FROM source_revisions
            WHERE connection_id=? AND resource=? AND external_id=? AND became_current=1
            ORDER BY id DESC LIMIT 1""",
            (connection_id, record.resource, record.external_id),
        ).fetchone()
        incoming = record.metadata.get("source_version_parts", {"source": record.source_updated_at})
        if not isinstance(incoming, dict) or not incoming:
            raise ValueError("Source version components must be a nonempty mapping")
        incoming = {key: timestamp(value) for key, value in incoming.items()}
        if timestamp(record.source_updated_at) != max(incoming.values()):
            raise ValueError("Source update time must match its version components")
        if current is None:
            return True
        previous = json.loads(current["metadata"]).get(
            "source_version_parts",
            {"source": current["source_updated_at"]},
        )
        previous = {key: timestamp(value) for key, value in previous.items()}
        if incoming.keys() != previous.keys():
            raise ValueError(
                "Source version component definitions changed; an explicit migration is required"
            )
        advances = any(incoming[key] > previous[key] for key in incoming)
        regresses = any(incoming[key] < previous[key] for key in incoming)
        if advances and regresses:
            raise ValueError(
                "Mixed source versions: refresh the related records as a consistent pair"
            )
        if not advances and not regresses and current["deleted"] and not record.deleted:
            return False
        return not regresses

    def ingest(self, records: list[SourceRecordInput]) -> dict:
        from .identity import (
            find_ingest_duplicate,
            normalize_source_identity,
            register_ingest_key,
        )

        if len(records) > 10000:
            raise ValueError("Import exceeds 10000 source records")
        inserted: list[int] = []
        changed_sources: set[tuple[str, str, str]] = set()
        with atomic(self.db):
            known_at = timestamp(self.clock())
            cleanup = self._purge_expired_locked(known_at)
            latest_known = self.db.execute("SELECT MAX(known_at) FROM source_revisions").fetchone()[
                0
            ]
            if latest_known and known_at < latest_known:
                raise ValueError("Clock moved backwards; refusing to backdate source knowledge")
            subject_id = self.db.execute(
                "SELECT value FROM settings WHERE key='subject_id'"
            ).fetchone()[0]
            for record in records:
                record = normalize_source_identity(record)
                if record.payload.get("synthetic") is not (self.environment == "synthetic"):
                    raise ValueError("Source environment does not match the database")
                if self.policy and record.provider not in self.policy.sources:
                    raise ValueError("This source is outside the local storage authorization")
                connection = self.db.execute(
                    "SELECT * FROM source_connections WHERE provider=?", (record.provider,)
                ).fetchone()
                external_subject = record.metadata.get(
                    "external_subject", record.metadata.get("whoop_user_id")
                )
                external_subject = str(external_subject) if external_subject is not None else None
                if connection is None:
                    connection_id = str(uuid.uuid4())
                    self.db.execute(
                        "INSERT INTO source_connections VALUES (?,?,?,?,?,?,?)",
                        (
                            connection_id,
                            subject_id,
                            record.provider,
                            external_subject,
                            self.environment,
                            canonical(asdict(self.policy))
                            if self.policy
                            else "synthetic-only; no real-data retention or transmission grant",
                            known_at,
                        ),
                    )
                else:
                    connection_id = connection["id"]
                    if external_subject != connection["external_subject"]:
                        raise ValueError("External account mismatch for this provider connection")
                fingerprint = asdict(record)
                fingerprint["metadata"].pop("captured_at", None)
                content_hash = digest(fingerprint)
                expires_at = None
                if self.policy:
                    capture_time = timestamp(record.metadata.get("captured_at", known_at))
                    if capture_time > known_at:
                        raise ValueError("Source capture cannot be in the future")
                    expires_at = timestamp(
                        (
                            datetime.fromisoformat(capture_time)
                            + timedelta(days=self.policy.retention_days)
                        ).isoformat()
                    )
                    if expires_at <= known_at:
                        raise ValueError(
                            "Source staging is already expired; fetch a fresh authorized snapshot"
                        )
                if find_ingest_duplicate(self.db, connection_id, record) is not None:
                    continue
                became_current = self._becomes_current(connection_id, record)
                old_metrics = set()
                if became_current:
                    old_metrics = {
                        row[0]
                        for row in self.db.execute(
                            """SELECT metric FROM observations WHERE revision_id=(
                              SELECT MAX(id) FROM source_revisions
                              WHERE connection_id=? AND resource=? AND external_id=?
                                AND became_current=1)""",
                            (connection_id, record.resource, record.external_id),
                        )
                    }
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO source_revisions
                    (connection_id,resource,external_id,source_updated_at,known_at,content_hash,
                     parser_version,payload,metadata,deleted,became_current,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        connection_id,
                        record.resource,
                        record.external_id,
                        timestamp(record.source_updated_at),
                        known_at,
                        content_hash,
                        record.parser_version,
                        canonical(record.payload),
                        canonical(record.metadata),
                        int(record.deleted),
                        int(became_current),
                        expires_at,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                revision_id = cursor.lastrowid
                inserted.append(revision_id)
                register_ingest_key(self.db, connection_id, record, revision_id)
                if became_current:
                    changed_sources.update(
                        (record.provider, record.resource, metric)
                        for metric in old_metrics | {obs.metric for obs in record.observations}
                    )
                for obs in record.observations:
                    self.db.execute(
                        """INSERT INTO observations
                        (revision_id,metric,value,unit,original_value,original_unit,start_at,end_at,
                         time_precision,quality,official) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            revision_id,
                            obs.metric,
                            obs.value,
                            obs.unit,
                            obs.original_value,
                            obs.original_unit,
                            timestamp(obs.start_at),
                            timestamp(obs.end_at) if obs.end_at else None,
                            obs.time_precision,
                            obs.quality,
                            int(obs.official),
                        ),
                    )
                for activity in record.activities:
                    self.db.execute(
                        """INSERT INTO activities
                        (revision_id,kind,external_id,start_at,end_at,timezone_offset)
                        VALUES (?,?,?,?,?,?)""",
                        (
                            revision_id,
                            activity.kind,
                            activity.external_id,
                            timestamp(activity.start_at),
                            timestamp(activity.end_at) if activity.end_at else None,
                            activity.timezone_offset,
                        ),
                    )
            task_ids = self._invalidate_analyses(changed_sources, known_at, inserted)
        self._checkpoint_cleanup(cleanup)
        return {
            "inserted": len(inserted),
            "duplicates": len(records) - len(inserted),
            "revision_ids": inserted,
            "task_id": task_ids[0] if task_ids else None,
            "task_ids": task_ids,
            "environment": self.environment,
        }

    def _invalidate_analyses(self, changed_sources, now, inserted):
        requests = {}

        def affected(request):
            return not request.get("as_of") and any(
                (request.get("provider") in (None, provider))
                and (request.get("resource") in (None, resource))
                and request["metric"] == metric
                for provider, resource, metric in changed_sources
            )

        for row in self.db.execute("SELECT id,request FROM analysis_runs WHERE stale=0").fetchall():
            request = json.loads(row["request"])
            if not affected(request):
                continue
            self.db.execute("UPDATE analysis_runs SET stale=1 WHERE id=?", (row["id"],))
            requests[canonical(request)] = request
        # The previous analysis can already be stale while its worker calculates.
        # A correction in that interval still needs a successor task, even though
        # there is no fresh analysis row to invalidate yet.
        for row in self.db.execute(
            "SELECT payload FROM tasks WHERE kind='recompute' AND status='running'"
        ):
            for request in json.loads(row[0])["requests"]:
                if affected(request):
                    requests[canonical(request)] = request
        if not requests:
            return []
        # Pending work always recomputes the current snapshot. Reuse that work;
        # an already running task may have read before this import and is not reused.
        for row in self.db.execute(
            "SELECT payload FROM tasks WHERE kind='recompute' AND status='pending'"
        ):
            for request in json.loads(row[0])["requests"]:
                requests.pop(canonical(request), None)
        task_ids = []
        pending = list(requests.values())
        for offset in range(0, len(pending), RECOMPUTE_BATCH_SIZE):
            task_id = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO tasks(id,idempotency_key,kind,payload,status,created_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    task_id,
                    digest({"inserted": inserted, "batch": offset}),
                    "recompute",
                    canonical({"requests": pending[offset : offset + RECOMPUTE_BATCH_SIZE]}),
                    "pending",
                    now,
                ),
            )
            task_ids.append(task_id)
        return task_ids

    def _current_revisions(
        self,
        cutoff: str = "9999",
        *,
        include_deleted: bool = False,
        provider: str | None = None,
        resource: str | None = None,
    ):
        filters, params = ["r.known_at<=?", "r.became_current=1"], [cutoff]
        if provider is not None:
            filters.append("c.provider=?")
            params.append(provider)
        if resource is not None:
            filters.append("r.resource=?")
            params.append(resource)
        # Select identities using the narrow index, then load only winning rows.
        # Applying a measurement date before this selection would resurrect a
        # former version when a correction moves the current record out of range.
        return self.db.execute(
            f"""WITH winners AS (
              SELECT MAX(r.id) AS id
              FROM source_revisions r JOIN source_connections c ON c.id=r.connection_id
              WHERE {" AND ".join(filters)}
              GROUP BY r.connection_id,r.resource,r.external_id
            ) SELECT r.*,c.provider,1 AS rank FROM winners w
              JOIN source_revisions r ON r.id=w.id
              JOIN source_connections c ON c.id=r.connection_id
              WHERE (? OR r.deleted=0) ORDER BY r.connection_id,r.resource,r.external_id""",
            [*params, include_deleted],
        ).fetchall()

    def current_sources(
        self, resource: str, provider: str = "whoop", *, include_deleted: bool = False
    ) -> list[dict]:
        """Current versions; tombstones are opt-in for conservative identity association."""
        if type(include_deleted) is not bool:
            raise ValueError("Deleted-source selection must be explicit")
        self.purge_expired()
        return [
            dict(row)
            for row in self._current_revisions(
                include_deleted=include_deleted, provider=provider, resource=resource
            )
        ]

    def snapshot(
        self,
        metric: str,
        start: str,
        end: str,
        provider: str | None = None,
        as_of: str | None = None,
        resource: str | None = None,
    ) -> dict:
        self.purge_expired()
        # One read transaction prevents mixing a concurrent import's versions and head.
        self.db.execute("BEGIN")
        try:
            cutoff = timestamp(as_of) if as_of else "9999"
            selected = self._current_revisions(cutoff, provider=provider, resource=resource)
            ids = [r["id"] for r in selected]
            observations = self._observations(ids, metric, start, end)
            used = {row["revision_id"] for row in observations}
            refs = [dict(r) for r in selected if r["id"] in used]
            head = self.db.execute(
                "SELECT COALESCE(MAX(id),0) FROM source_revisions WHERE known_at<=?", (cutoff,)
            ).fetchone()[0]
            first_known = self.db.execute("SELECT MIN(known_at) FROM source_revisions").fetchone()[
                0
            ]
            return {
                "observations": observations,
                "revisions": refs,
                "catalog_head": head,
                "knowledge_from": first_known,
            }
        finally:
            self.db.commit()

    def _observations(self, ids: list[int], metric: str, start: str, end: str) -> list[dict]:
        # Batch placeholders so large imports do not exceed SQLite's variable limit.
        observations = []
        for offset in range(0, len(ids), 500):
            batch = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.execute(
                f"""SELECT o.*,c.provider,r.resource FROM observations o
                JOIN source_revisions r ON r.id=o.revision_id
                JOIN source_connections c ON c.id=r.connection_id
                WHERE revision_id IN ({placeholders}) AND metric=?
                  AND start_at>=? AND start_at<? ORDER BY start_at,o.id""",
                [*batch, metric, timestamp(start), timestamp(end)],
            ).fetchall()
            observations.extend(dict(row) for row in rows)
        return sorted(observations, key=lambda row: (row["start_at"], row["id"]))

    def _invalidate_backups(self) -> None:
        for row in self.db.execute("SELECT * FROM managed_backups").fetchall():
            path = Path(row["path"])
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if path.is_symlink() or (info.st_dev, info.st_ino) != (row["device"], row["inode"]):
                    raise ValueError("A managed backup was replaced; inspect it before cleanup")
                path.unlink()
            self.db.execute("DELETE FROM managed_backups WHERE path=?", (str(path),))

    def purge_expired(self) -> dict:
        with atomic(self.db):
            result = self._purge_expired_locked(timestamp(self.clock()))
        self._checkpoint_cleanup(result)
        return result

    def _checkpoint_cleanup(self, result):
        if (
            result.get("purged_revisions")
            or result.get("expired_staging_pages")
            or result.get("expired_sync_runs")
        ):
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _purge_expired_locked(self, now: str) -> dict:
        """Lifecycle cleanup in the caller's write transaction and time snapshot."""
        if self.environment != "real":
            return {"purged_revisions": 0}
        cutoff = timestamp(
            (datetime.fromisoformat(now) - timedelta(days=self.policy.retention_days)).isoformat()
        )
        expired_rows = self.db.execute(
            "SELECT id,connection_id,resource,external_id FROM source_revisions WHERE expires_at<=?",
            (now,),
        ).fetchall()
        expired_ids = {row["id"] for row in expired_rows}
        # Capture order can differ from provider/knowledge order. When the
        # authoritative current revision expires before a predecessor, keeping
        # that predecessor would make MAX(id) silently revive an older state.
        # Invalidate its earlier lineage, without retaining an expired tombstone
        # or extending anyone's authorization. Later non-current history stays
        # non-current, and unrelated identities keep their own lifetimes.
        for identity in {
            (row["connection_id"], row["resource"], row["external_id"]) for row in expired_rows
        }:
            winner = self.db.execute(
                """SELECT MAX(id) FROM source_revisions
                WHERE connection_id=? AND resource=? AND external_id=? AND became_current=1""",
                identity,
            ).fetchone()[0]
            if winner in expired_ids:
                expired_ids.update(
                    row[0]
                    for row in self.db.execute(
                        """SELECT id FROM source_revisions
                        WHERE connection_id=? AND resource=? AND external_id=? AND id<=?""",
                        (*identity, winner),
                    )
                )
        expired = len(expired_ids)
        staged = self.db.execute(
            "SELECT COUNT(*) FROM sync_pages WHERE fetched_at<=?", (cutoff,)
        ).fetchone()[0]
        expired_runs = self.db.execute(
            "SELECT COUNT(*) FROM sync_runs WHERE created_at<=?", (cutoff,)
        ).fetchone()[0]
        backups = self.db.execute(
            "SELECT COUNT(*) FROM managed_backups WHERE expires_at<=?", (now,)
        ).fetchone()[0]
        if expired or staged or expired_runs or backups:
            # Exact files created by this application only; independently copied backups are outside this inventory.
            self._invalidate_backups()
        if expired:
            self.db.execute("DELETE FROM analysis_runs")
            self.db.execute("DELETE FROM tasks")
            ordered_ids = sorted(expired_ids)
            for offset in range(0, len(ordered_ids), 500):
                batch = ordered_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                for table in ("observations", "activities"):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE revision_id IN ({placeholders})", batch
                    )
                self.db.execute(f"DELETE FROM source_revisions WHERE id IN ({placeholders})", batch)
        if staged or expired_runs:
            self.db.execute(
                "DELETE FROM sync_runs WHERE created_at<=? OR id IN (SELECT run_id FROM sync_pages WHERE fetched_at<=?)",
                (cutoff, cutoff),
            )
        return {
            "purged_revisions": expired,
            "expired_staging_pages": staged,
            "expired_sync_runs": expired_runs,
            "invalidated_predecessors": expired - len(expired_rows),
        }

    def forget_source(self, provider: str) -> dict:
        if provider not in {"whoop", "whoop_export"}:
            raise ValueError("Select a supported source to remove")
        with atomic(self.db):
            self._invalidate_backups()
            ids = [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM source_connections WHERE provider=?", (provider,)
                )
            ]
            self.db.execute("DELETE FROM analysis_runs")
            self.db.execute("DELETE FROM tasks")
            if provider == "whoop":
                self.db.execute("DELETE FROM sync_runs")
            removed = 0
            for connection_id in ids:
                for table in ("observations", "activities"):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE revision_id IN (SELECT id FROM source_revisions WHERE connection_id=?)",
                        (connection_id,),
                    )
                removed += self.db.execute(
                    "DELETE FROM source_revisions WHERE connection_id=?", (connection_id,)
                ).rowcount
                self.db.execute("DELETE FROM source_connections WHERE id=?", (connection_id,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"provider": provider, "removed_revisions": removed, "local_plans_retained": True}

    def backup(self, target: Path | str, *, target_key: bytes | None = None) -> dict:
        self.purge_expired()
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        destination = connect(target, self.environment, target_key or self.encryption_key)
        try:
            if self.environment == "real":
                info = target.stat()
                expiry = self.db.execute("SELECT MIN(expires_at) FROM source_revisions").fetchone()[
                    0
                ]
                if not expiry:
                    expiry = timestamp(
                        (
                            datetime.fromisoformat(timestamp(self.clock()))
                            + timedelta(days=self.policy.retention_days)
                        ).isoformat()
                    )
                self.db.execute(
                    "INSERT INTO managed_backups VALUES (?,?,?,?,?)",
                    (str(target.resolve()), info.st_dev, info.st_ino, self.clock(), expiry),
                )
            self.db.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"Backup integrity failed: {integrity}")
        finally:
            destination.close()
        return {
            "path": str(target.resolve()),
            "integrity_check": integrity,
            "environment": self.environment,
        }


def restore_backup(
    source: Path | str,
    target: Path | str,
    *,
    environment: str = "synthetic",
    encryption_key: bytes | None = None,
    target_key: bytes | None = None,
    clock: Callable[[], str] = utc_now,
) -> dict:
    source = Path(source)
    if not source.is_file():
        raise ValueError("Backup does not exist")
    # Validate the snapshot through a read-only connection before creating the target.
    db = connect(source, environment, encryption_key, readonly=True)
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Backup belongs to a different application")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup failed integrity check")
        if (
            db.execute("SELECT value FROM settings WHERE key='environment'").fetchone()[0]
            != environment
        ):
            raise ValueError("Backup environment mismatch")
        version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version not in (1, 2, SCHEMA_VERSION) or (
            environment == "real" and version not in (2, SCHEMA_VERSION)
        ):
            raise ValueError("Unsupported backup schema")
        if environment == "real":
            now = timestamp(clock())
            policy = json.loads(
                db.execute("SELECT value FROM settings WHERE key='local_policy'").fetchone()[0]
            )
            cutoff = timestamp(
                (datetime.fromisoformat(now) - timedelta(days=policy["retention_days"])).isoformat()
            )
            if (
                db.execute(
                    "SELECT 1 FROM source_revisions WHERE expires_at<=? LIMIT 1", (now,)
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM sync_pages WHERE fetched_at<=? LIMIT 1", (cutoff,)
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM sync_runs WHERE created_at<=? LIMIT 1", (cutoff,)
                ).fetchone()
            ):
                raise ValueError("Backup contains expired source data and cannot be restored")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        restored = connect(target, environment, target_key or encryption_key)
        try:
            db.backup(restored)
            if version >= 2:
                # Paths in the snapshot belong to its source database. A restored
                # database manages only backups it subsequently creates itself.
                restored.execute("DELETE FROM managed_backups")
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database failed integrity check")
        finally:
            restored.close()
    finally:
        db.close()
    return {"path": str(target.resolve()), "integrity_check": "ok", "environment": environment}
