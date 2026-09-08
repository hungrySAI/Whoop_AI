"""Durable, bounded page staging followed by atomic normalization into the shared store."""

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from .api_ingestion import normalize_api, validate_resource
from .contracts import timestamp
from .credentials import CredentialError
from .oauth import OAuthError
from .storage import Store, atomic, canonical
from .whoop_client import WhoopAPIError

RESOURCES = ("profile", "body", "cycle", "recovery", "sleep", "workout")
FRESHNESS_SECONDS = 30 * 60  # Application policy, not a WHOOP scoring/update guarantee.
CATCH_UP_DAYS = 366  # Automatic request lookback, independent of capture-based retention.
OVERLAP_DAYS = 7  # UTC calendar dates incl. checkpoint day; not an updated-since feed.


class SyncAlreadyRunning(ValueError):
    pass


@contextmanager
def sync_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncAlreadyRunning("A sync is already running for this database") from None
        yield
    finally:
        os.close(fd)


def sync_is_running(path):
    try:
        with sync_lock(path):
            return False
    except SyncAlreadyRunning:
        return True


class SyncService:
    def __init__(self, store: Store, client):
        self.store, self.client = store, client

    def _completed_intervals(self, now: str) -> tuple[bool, list[tuple[str, str]]]:
        """Return only the portion of a successful request that could have been checked.

        Older versions accepted a future request end. Its later completion or the
        passage of time cannot prove that records created after the first fetch
        were observed. Keep the original request for replay and cap coverage here.
        """
        rows = self.store.db.execute(
            "SELECT request,created_at,completed_at FROM sync_runs WHERE status='completed'"
        ).fetchall()
        intervals = []
        for row in rows:
            query = json.loads(row["request"])
            begin = timestamp(query["start"])
            created, completed = timestamp(row["created_at"]), timestamp(row["completed_at"])
            # Future local timestamps cannot prove coverage after a clock rollback.
            if created > now or completed > now:
                continue
            finish = min(timestamp(query["end"]), created, completed)
            if begin < finish:
                intervals.append((begin, finish))
        return bool(rows), intervals

    def plan_catch_up(self, start: str, end: str) -> dict:
        """Bridge retained successful request intervals, never infer gaps from health records.

        Status may preview this plan. Execution recomputes it after purge under the sync lock.
        No checkpoint (including expired history) triggers a bounded rescan.
        """
        start, end = timestamp(start), timestamp(end)
        now = timestamp(self.store.clock())
        if not start < end <= now:
            raise ValueError("Catch-up requires a positive window ending no later than now")
        floor = timestamp((datetime.fromisoformat(end) - timedelta(days=CATCH_UP_DAYS)).isoformat())
        _, completed = self._completed_intervals(now)
        intervals = [(begin, finish) for begin, finish in completed if finish <= end]
        last_end = max((finish for _, finish in intervals), default=None)
        relevant = sorted((begin, finish) for begin, finish in intervals if finish >= floor)
        # Merge from the oldest retained relevant request. A recent narrow CLI sync
        # cannot jump over a known earlier gap, and a late old resume cannot regress
        # the end of an already connected interval.
        through = None
        gap = False
        if relevant and any(finish < floor for _, finish in intervals) and relevant[0][0] > floor:
            through, gap = floor, True
        for begin, finish in relevant:
            if gap:
                break
            if through is not None and begin > through:
                gap = True
                break
            through = max(through or finish, finish)
        if through is None:
            planned = floor
            reason = "no_checkpoint" if last_end is None else "lookback_limit"
            limited = True
        else:
            overlap = timestamp(
                (
                    datetime.fromisoformat(through).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    - timedelta(days=OVERLAP_DAYS - 1)
                ).isoformat()
            )
            wanted = min(start, overlap)
            planned = max(floor, wanted)
            limited = wanted < floor
            reason = "history_gap" if gap else "since_success" if planned < start else "recent"
        return {
            "request": {"start": planned, "end": end},
            "reason": reason,
            "last_success_through": last_end,
            "coverage_through": through,
            "expanded": planned < start,
            "lookback_limited": limited,
            "lookback_days": CATCH_UP_DAYS,
            "overlap_days": OVERLAP_DAYS,
        }

    def freshness(self, start: str, end: str) -> dict:
        """Successful request coverage, independent of measurements and score availability."""
        start, end = timestamp(start), timestamp(end)
        now = timestamp(self.store.clock())
        has_completed, intervals = self._completed_intervals(now)
        candidates = [finish for begin, finish in intervals if begin <= start < finish]
        checked_through = max(candidates) if candidates else None
        fresh_until = (
            datetime.fromisoformat(checked_through) + timedelta(seconds=FRESHNESS_SECONDS)
            if checked_through
            else None
        )
        return {
            "state": ("fresh" if datetime.fromisoformat(now) < fresh_until else "stale")
            if fresh_until
            else "uncovered"
            if has_completed
            else "never",
            "checked_through": checked_through,
            "fresh_until": timestamp(fresh_until.isoformat()) if fresh_until else None,
            "threshold_seconds": FRESHNESS_SECONDS,
        }

    def latest(self):
        row = self.store.db.execute(
            "SELECT id FROM sync_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return self.status(row[0]) if row else None

    def status(self, run_id: str) -> dict:
        row = self.store.db.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown or expired sync run")
        state = json.loads(row["state"])
        return {
            "run_id": run_id,
            "status": row["status"],
            "request": json.loads(row["request"]),
            "resources_completed": min(state["index"], len(RESOURCES)),
            "staged_records": state["total"],
            "last_error": row["last_error"],
            "environment": self.store.environment,
            "catch_up": state.get("catch_up"),
        }

    def _page(self, run_id, resource, page, state):
        records = page["records"]
        if state["total"] + len(records) > 10000:
            raise ValueError("Sync exceeds 10000 records; choose a smaller window")
        for record in records:
            validate_resource(resource, record)
            if resource == "profile":
                state["account"] = record["user_id"]
                prior = self.store.db.execute(
                    "SELECT external_subject FROM source_connections WHERE provider='whoop'"
                ).fetchone()
                if prior and prior[0] != str(state["account"]):
                    raise ValueError("WHOOP account differs from this database connection")
            elif resource != "body" and record.get("user_id") != state.get("account"):
                raise ValueError("WHOOP sync cannot mix accounts")
        following = page["next_token"]
        if following is not None and following in state["seen"]:
            raise ValueError("WHOOP pagination loop; begin a fresh sync window")
        with atomic(self.store.db):
            self.store.db.execute(
                "INSERT INTO sync_pages VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    resource,
                    state["page_no"],
                    canonical(records),
                    canonical(page["headers"]),
                    self.store.clock(),
                ),
            )
            state["total"] += len(records)
            state["page_no"] += 1
            state["next_token"] = following
            if following is not None:
                state["seen"].append(following)
            else:
                state["index"] += 1
                state["seen"] = []
            self.store.db.execute(
                "UPDATE sync_runs SET state=?,last_error=NULL WHERE id=?",
                (canonical(state), run_id),
            )

    def run(
        self,
        start: str | None = None,
        end: str | None = None,
        *,
        run_id: str | None = None,
        max_pages: int = 100,
        if_stale: bool = False,
        catch_up: bool = False,
    ) -> dict:
        if not 1 <= max_pages <= 1000:
            raise ValueError("Sync batch must be between 1 and 1000 pages")
        if type(if_stale) is not bool or (if_stale and run_id is not None):
            raise ValueError("Conditional sync requires a new request")
        if type(catch_up) is not bool or (catch_up and run_id is not None):
            raise ValueError("Catch-up planning requires a new request")
        if self.store.policy and "whoop" not in self.store.policy.sources:
            raise ValueError("WHOOP API is outside this local authorization")
        with sync_lock(self.store.path.with_suffix(".sync.lock")):
            self.store.purge_expired()
            if run_id is None:
                if not start or not end:
                    raise ValueError("A new sync requires explicit start and end timestamps")
                start, end = timestamp(start), timestamp(end)
                if (
                    not timedelta(0)
                    < datetime.fromisoformat(end) - datetime.fromisoformat(start)
                    <= timedelta(days=366)
                ):
                    raise ValueError("Sync window must be positive and at most 366 days")
                if end > timestamp(self.store.clock()):
                    raise ValueError("New sync window must end no later than now")
                if if_stale:
                    latest = self.latest()
                    if latest and latest["status"] != "completed":
                        return {"skipped": "unfinished"}
                plan = self.plan_catch_up(start, end) if catch_up else None
                if plan:
                    start, end = plan["request"]["start"], plan["request"]["end"]
                if if_stale:
                    if self.freshness(start, end)["state"] == "fresh":
                        return {"skipped": "fresh"}
                run_id = str(uuid.uuid4())
                state = {"index": 0, "next_token": None, "seen": [], "total": 0, "page_no": 0}
                if plan:
                    state["catch_up"] = plan
                self.store.db.execute(
                    "INSERT INTO sync_runs VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        canonical({"start": start, "end": end}),
                        canonical(state),
                        "paging",
                        self.store.clock(),
                        None,
                        None,
                    ),
                )
            else:
                status = self.status(run_id)
                if status["status"] == "completed":
                    return status
                if (
                    start
                    and timestamp(start) != status["request"]["start"]
                    or end
                    and timestamp(end) != status["request"]["end"]
                ):
                    raise ValueError("Resume must preserve the original query window")
            row = self.store.db.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
            state, query = json.loads(row["state"]), json.loads(row["request"])
            try:
                used_pages = 0
                while state["index"] < len(RESOURCES) and used_pages < max_pages:
                    resource = RESOURCES[state["index"]]
                    page = self.client.list_records(
                        resource, query["start"], query["end"], state["next_token"]
                    )
                    self._page(run_id, resource, page, state)
                    used_pages += 1
                if state["index"] < len(RESOURCES):
                    return self.status(run_id)
                resources = {name: [] for name in RESOURCES}
                retrieval_times = {}
                for page in self.store.db.execute(
                    "SELECT * FROM sync_pages WHERE run_id=? ORDER BY page_no", (run_id,)
                ):
                    resources[page["resource"]].extend(json.loads(page["records"]))
                    retrieval_times[page["resource"]] = min(
                        retrieval_times.get(page["resource"], page["fetched_at"]),
                        page["fetched_at"],
                    )
                known_cycles = {r["id"] for r in resources["cycle"]}
                missing = {r["cycle_id"] for r in resources["recovery"]} - known_cycles
                # A recovery near a query boundary can reference a cycle outside the cycle listing window.
                for cycle_id in sorted(missing):
                    if used_pages >= max_pages:
                        return self.status(run_id)
                    page = self.client.cycle_by_id(cycle_id)
                    # Linked records are persisted with a checkpoint, but do not advance the six-resource index.
                    saved_index = state["index"]
                    self._page(run_id, "cycle", page, state)
                    state["index"] = saved_index
                    self.store.db.execute(
                        "UPDATE sync_runs SET state=? WHERE id=?", (canonical(state), run_id)
                    )
                    resources["cycle"].extend(page["records"])
                    used_pages += 1
                normalized = normalize_api(
                    resources,
                    acquired_at=row["created_at"],
                    synthetic=self.store.environment == "synthetic",
                    retrieval_times=retrieval_times,
                )
                result = self.store.ingest(normalized)
                # If the process stops after ingest, replay uses the fixed acquired_at and source fingerprints.
                self.store.db.execute(
                    "UPDATE sync_runs SET status='completed',completed_at=?,last_error=NULL WHERE id=?",
                    (self.store.clock(), run_id),
                )
                return {**self.status(run_id), "ingestion": result}
            except Exception as error:
                # Only these boundary exceptions deliberately exclude tokens and response bodies.
                safe_error = (
                    str(error)
                    if isinstance(error, (OAuthError, WhoopAPIError, CredentialError))
                    else "Source validation or storage failed; check the source contract, account and local storage policy"
                )
                self.store.db.execute(
                    "UPDATE sync_runs SET last_error=? WHERE id=?",
                    (safe_error, run_id),
                )
                raise ValueError(f"Sync paused safely; run_id={run_id}. {safe_error}") from None
