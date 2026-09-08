"""Shared queries and task execution used by CLI and MCP."""

import json
import uuid
from datetime import datetime, timedelta

from .analytics import ALGORITHMS, METRICS
from .contracts import timestamp
from .storage import Store, atomic, canonical, digest


class CopilotService:
    def __init__(self, store: Store):
        self.store = store

    def analyze(
        self,
        metric: str,
        start: str,
        end: str,
        provider: str | None = None,
        as_of: str | None = None,
        algorithm: str = "mean_change",
        resource: str | None = None,
        *,
        persist: bool = True,
    ) -> dict:
        """Calculate registered statistics, optionally retaining an explicit analysis run.

        Local dashboard views use ``persist=False``: exact window semantics and
        evidence selection stay shared, without turning page reads into reports
        or future recomputation work. Their ``run_id`` is therefore ``None``.
        """
        if type(persist) is not bool:
            raise ValueError("Analysis persistence must be explicit")
        if metric not in METRICS or algorithm not in ALGORITHMS:
            raise ValueError("Select a registered metric and algorithm")
        start, end = timestamp(start), timestamp(end)
        duration = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        if not timedelta(0) < duration <= timedelta(days=366):
            raise ValueError("Analysis range must be positive and at most 366 days")
        as_of = timestamp(as_of) if as_of else None
        if as_of and as_of > timestamp(self.store.clock()):
            raise ValueError("as_of cannot be in the future")
        if provider is not None and (not provider or len(provider) > 100):
            raise ValueError("Invalid provider")
        request = {
            "metric": metric,
            "start": start,
            "end": end,
            "provider": provider,
            "as_of": as_of,
            "algorithm": algorithm,
            "resource": resource,
        }
        snapshot = self.store.snapshot(metric, start, end, provider, as_of, resource)
        rows = snapshot["observations"]
        if len(rows) > 10000:
            raise ValueError("Analysis exceeds the 10000 observation budget")
        if len({row["provider"] for row in rows}) > 1:
            raise ValueError(
                "Multiple providers found; select one explicitly to avoid merging sources"
            )
        definition = METRICS[metric]
        if len({row["resource"] for row in rows}) > 1:
            raise ValueError(
                "Multiple resource types found; select resource to avoid mixing cycle and activity metrics"
            )
        if any(row["unit"] != definition.unit for row in rows):
            raise ValueError("Stored units do not match the metric definition")
        calculation = ALGORITHMS[algorithm]
        ids = [ref["id"] for ref in snapshot["revisions"]]
        cache_key = digest(
            {
                "request": request,
                "input_ids": ids,
                "knowledge_from": snapshot["knowledge_from"],
                "algorithm_version": calculation.version,
                "unit": definition.unit,
            }
        )
        cached = (
            self.store.db.execute(
                "SELECT id FROM analysis_runs WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if persist
            else None
        )
        if cached:
            return self._load_run(cached[0], snapshot_head=snapshot["catalog_head"])
        result = calculation.calculate(rows, start, end)
        limitations = [
            "Synthetic demonstration; no personal health conclusion."
            if self.store.environment == "synthetic"
            else "Descriptive personal records; these statistics do not establish cause or medical advice.",
            "Descriptive means and changes are application-derived, not a new WHOOP score.",
            "Windows use UTC and start_at in [start,end); cycle values are assigned to cycle start.",
            "Irregular sampling is not time-weighted; missing values are not imputed.",
            "History begins when this application first captured each source revision.",
        ]
        if not rows:
            limitations.append("No captured observations in this window; missing data is not zero.")
        if as_of and (not snapshot["knowledge_from"] or as_of < snapshot["knowledge_from"]):
            limitations.append(
                "Requested as_of predates captured history; earlier state is unknown."
            )
        evidence = {
            "schema_version": 1,
            "environment": self.store.environment,
            "metric": metric,
            "unit": definition.unit,
            "source_metric_is_official": definition.official,
            "analysis_kind": "registered_descriptive",
            "algorithm_version": calculation.version,
            "catalog_head": snapshot["catalog_head"],
            "knowledge_from": snapshot["knowledge_from"],
            "references": [
                {
                    key: ref[key]
                    for key in (
                        "id",
                        "provider",
                        "resource",
                        "external_id",
                        "content_hash",
                        "parser_version",
                        "source_updated_at",
                        "known_at",
                    )
                }
                for ref in snapshot["revisions"]
            ],
            "observations": rows,
            "limitations": limitations,
            "model": None,
            "prompt_version": None,
        }
        run_id = str(uuid.uuid4()) if persist else None
        with atomic(self.store.db):
            # Snapshot reads and calculation run outside this write transaction.
            # Deletion/expiry must win over any in-flight publication, including
            # historical as_of requests and non-persistent dashboard responses.
            published_at = timestamp(self.store.clock())
            self._validate_references(snapshot["revisions"], published_at)
            # A source update after the read snapshot must not publish a fresh-looking old result.
            head = self.store.db.execute(
                "SELECT COALESCE(MAX(id),0) FROM source_revisions"
            ).fetchone()[0]
            stale = int(as_of is None and head != snapshot["catalog_head"])
            if not persist:
                return {
                    "run_id": None,
                    "request": request,
                    "result": result,
                    "evidence": evidence,
                    "created_at": published_at,
                    "stale": bool(stale),
                }
            self.store.db.execute(
                """INSERT OR IGNORE INTO analysis_runs
                (id,cache_key,request,catalog_head,input_revision_ids,algorithm_version,
                 result,evidence,created_at,stale) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    cache_key,
                    canonical(request),
                    snapshot["catalog_head"],
                    canonical(ids),
                    calculation.version,
                    canonical(result),
                    canonical(evidence),
                    published_at,
                    stale,
                ),
            )
            run_id = self.store.db.execute(
                "SELECT id FROM analysis_runs WHERE cache_key=?", (cache_key,)
            ).fetchone()[0]
        return self.get_run(run_id)

    def _validate_references(self, references: list[dict], now: str) -> None:
        """Validate every retained source inside the caller's publication transaction."""
        for offset in range(0, len(references), 500):
            batch = references[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            retained = {
                row["id"]: row
                for row in self.store.db.execute(
                    f"SELECT id,content_hash,expires_at FROM source_revisions WHERE id IN ({placeholders})",
                    [reference["id"] for reference in batch],
                )
            }
            for reference in batch:
                row = retained.get(reference["id"])
                if (
                    row is None
                    or row["content_hash"] != reference["content_hash"]
                    or (row["expires_at"] and timestamp(row["expires_at"]) <= now)
                ):
                    raise ValueError(
                        "A required source version is missing, changed or expired; refresh"
                    )

    def get_run(self, run_id: str) -> dict:
        return self._load_run(run_id)

    def _load_run(self, run_id: str, *, snapshot_head: int | None = None) -> dict:
        self.store.purge_expired()
        with atomic(self.store.db):
            row = self.store.db.execute(
                "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown analysis run")
            evidence = json.loads(row["evidence"])
            self._validate_references(evidence["references"], timestamp(self.store.clock()))
            request = json.loads(row["request"])
            stale = bool(row["stale"])
            if snapshot_head is not None and request["as_of"] is None:
                # A conservative resource invalidation may leave this request's
                # exact input signature unchanged. Reuse its original evidence
                # once that signature was selected again without a concurrent write.
                head = self.store.db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM source_revisions"
                ).fetchone()[0]
                stale = head != snapshot_head
                self.store.db.execute(
                    "UPDATE analysis_runs SET stale=? WHERE id=?", (int(stale), run_id)
                )
        return {
            "run_id": row["id"],
            "request": request,
            "result": json.loads(row["result"]),
            "evidence": evidence,
            "created_at": row["created_at"],
            "stale": stale,
        }

    def reproduce(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        request = run["request"]
        algorithm = ALGORITHMS.get(request["algorithm"])
        if not algorithm or algorithm.version != run["evidence"]["algorithm_version"]:
            raise ValueError("The original calculation version is unavailable")
        ids = [ref["id"] for ref in run["evidence"]["references"]]
        for ref in run["evidence"]["references"]:
            source = self.store.db.execute(
                "SELECT content_hash FROM source_revisions WHERE id=?", (ref["id"],)
            ).fetchone()
            if not source or source[0] != ref["content_hash"]:
                raise ValueError("A required source version is missing or changed")
        rows = self.store._observations(ids, request["metric"], request["start"], request["end"])
        result = algorithm.calculate(rows, request["start"], request["end"])
        return {
            "run_id": run_id,
            "matches": result == run["result"],
            "result": result,
            "input_revision_ids": ids,
            "algorithm_version": algorithm.version,
        }

    def list_tasks(self) -> list[dict]:
        return [
            dict(row)
            for row in self.store.db.execute(
                "SELECT id,kind,status,attempts,lease_until,last_error,created_at,completed_at FROM tasks ORDER BY created_at,id"
            )
        ]

    def claim_task(self, lease_seconds: int = 30) -> dict | None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("Task lease must be between 1 and 300 seconds")
        now = timestamp(self.store.clock())
        expires = timestamp(
            (datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)).isoformat()
        )
        token = str(uuid.uuid4())
        with atomic(self.store.db):
            task = self.store.db.execute(
                """SELECT * FROM tasks WHERE status='pending'
                OR (status='running' AND lease_until<=?) ORDER BY created_at,id LIMIT 1""",
                (now,),
            ).fetchone()
            if not task:
                return None
            self.store.db.execute(
                "UPDATE tasks SET status='running',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",
                (expires, token, task["id"]),
            )
        return {"id": task["id"], "payload": json.loads(task["payload"]), "lease_token": token}

    def finish_task(self, task: dict, error: str | None = None) -> bool:
        now = timestamp(self.store.clock())
        with atomic(self.store.db):
            cursor = self.store.db.execute(
                """UPDATE tasks SET status=?,last_error=?,completed_at=?,lease_until=NULL,lease_token=NULL
                WHERE id=? AND status='running' AND lease_token=? AND lease_until>?""",
                (
                    "failed" if error else "completed",
                    error,
                    now,
                    task["id"],
                    task["lease_token"],
                    now,
                ),
            )
        return bool(cursor.rowcount)

    def run_pending(self, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Worker limit must be between 1 and 100")
        completed = []
        for _ in range(limit):
            task = self.claim_task()
            if task is None:
                break
            try:
                if len(task["payload"]["requests"]) > 100:
                    raise ValueError("Recomputation task exceeds the 100 request budget")
                runs = [
                    self.analyze(**request)["run_id"] for request in task["payload"]["requests"]
                ]
                owned = self.finish_task(task)
                completed.append(
                    {
                        "task_id": task["id"],
                        "status": "completed" if owned else "lease_lost",
                        "run_ids": runs,
                    }
                )
            except (ValueError, KeyError) as error:
                owned = self.finish_task(task, str(error))
                completed.append(
                    {
                        "task_id": task["id"],
                        "status": "failed" if owned else "lease_lost",
                        "error": str(error),
                    }
                )
        return completed
