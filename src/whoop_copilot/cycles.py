"""Join current official sources by their explicit identities, never by calendar proximity."""

from collections import defaultdict
from uuid import UUID

from .contracts import timestamp
from .dashboard import EVIDENCE_METRICS, DashboardService
from .storage import canonical

PAGE_SIZE = 10
LINK_LABELS = {
    "linked": "按 WHOOP 关联记录",
    "unavailable": "关联记录尚未采集或已不在本机保留范围",
    "recovery_unavailable": "恢复记录不可用，暂无法确定其关联睡眠",
    "inconsistent": "关联信息不一致，暂不组合显示",
    "version_mismatch": "周期与恢复中的周期版本不同，暂不组合显示",
}


class CycleReviewService:
    def __init__(self, store):
        self.store = store
        self.dashboard = DashboardService(store)

    def _head(self):
        return tuple(
            self.store.db.execute("SELECT MAX(id),COUNT(*) FROM source_revisions").fetchone()
        )

    def _metrics(self, revision, keys):
        observations = {
            row["metric"]: dict(row)
            for row in self.store.db.execute(
                "SELECT * FROM observations WHERE revision_id=?", (revision["id"],)
            )
        }
        return [
            {
                "key": key,
                "label": EVIDENCE_METRICS[key].label,
                "unit_label": EVIDENCE_METRICS[key].unit_label,
                **self.dashboard._point(
                    revision,
                    EVIDENCE_METRICS[key],
                    observations.get(EVIDENCE_METRICS[key].metric),
                ),
            }
            for key in keys
        ]

    @staticmethod
    def _group(state, **content):
        return {"state": state, "state_label": LINK_LABELS[state], "metrics": [], **content}

    @staticmethod
    def _sleep_id(value):
        try:
            return str(UUID(value)) if isinstance(value, str) else None
        except ValueError:
            return None

    def _entry(self, revision, recoveries, sleeps, as_of):
        cycle, _ = self.dashboard._source_info(revision)
        cycle_point = self._metrics(revision, ("strain",))[0]
        entry = {
            "revision_id": revision["id"],
            "start": cycle_point["measured_at"],
            "end": cycle_point["measured_end"],
            "source_timezone": cycle_point["source_timezone"],
            "open": cycle_point["measured_end"] is None or cycle_point["measured_end"] > as_of,
            "cycle": self._group("linked", metrics=[cycle_point]),
            "recovery": self._group("unavailable"),
            "sleep": self._group("recovery_unavailable"),
        }
        recovery_revision = recoveries.get((revision["connection_id"], str(cycle["id"])))
        if recovery_revision is None:
            return entry
        recovery, embedded_cycle = self.dashboard._source_info(recovery_revision)
        if recovery["cycle_id"] != cycle["id"] or recovery["user_id"] != cycle["user_id"]:
            entry["recovery"] = self._group("inconsistent")
            return entry
        # Recovery observations retain their original interval from this embedded source.
        # Do not silently retime or combine them with a different current cycle version.
        if canonical(embedded_cycle) != canonical(cycle):
            entry["recovery"] = self._group("version_mismatch")
            return entry
        entry["recovery"] = self._group(
            "linked", metrics=self._metrics(recovery_revision, ("recovery", "hrv", "rhr"))
        )
        sleep_id = self._sleep_id(recovery.get("sleep_id"))
        matches = sleeps.get((revision["connection_id"], sleep_id), []) if sleep_id else []
        if not matches:
            entry["sleep"] = self._group("unavailable")
            return entry
        if len(matches) != 1:
            entry["sleep"] = self._group("inconsistent")
            return entry
        sleep_revision = matches[0]
        if sleep_revision["deleted"]:
            entry["sleep"] = self._group("unavailable")
            return entry
        sleep, _ = self.dashboard._source_info(sleep_revision)
        if (
            self._sleep_id(sleep["id"]) != sleep_id
            or sleep["cycle_id"] != cycle["id"]
            or sleep["user_id"] != cycle["user_id"]
        ):
            entry["sleep"] = self._group("inconsistent")
            return entry
        metrics = self._metrics(
            sleep_revision, ("sleep", "sleep_efficiency", "sleep_consistency", "respiratory_rate")
        )
        entry["sleep"] = self._group(
            "linked",
            metrics=metrics,
            start=metrics[0]["measured_at"],
            end=metrics[0]["measured_end"],
            source_timezone=metrics[0]["source_timezone"],
            nap=sleep["nap"],
        )
        return entry

    def review(self, days=7, page=1):
        start, end = self.dashboard.window(days)
        if type(page) is not int or not 1 <= page <= 1000:
            raise ValueError("Select an available cycle page")
        for _ in range(2):
            self.store.purge_expired()
            head = self._head()
            cycles = self.store.current_sources("cycle")
            # Only the anchor cycle is windowed. Its explicitly linked sleep can start earlier.
            candidates = []
            for row in cycles:
                _, interval = self.dashboard._source_info(row)
                measured = timestamp(interval["start"])
                if start <= measured < end:
                    candidates.append((measured, row))
            if len(candidates) > 10000:
                raise ValueError("Cycle record budget exceeded")
            candidates.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
            pages = max(1, (len(candidates) + PAGE_SIZE - 1) // PAGE_SIZE)
            current_page = min(page, pages)
            chosen = candidates[(current_page - 1) * PAGE_SIZE : current_page * PAGE_SIZE]
            recoveries = {
                (row["connection_id"], row["external_id"]): row
                for row in self.store.current_sources("recovery")
            }
            sleeps = defaultdict(list)
            for row in self.store.current_sources("sleep", include_deleted=True):
                sleep_id = self._sleep_id(row["external_id"])
                if sleep_id:
                    sleeps[(row["connection_id"], sleep_id)].append(row)
            entries = [self._entry(row, recoveries, sleeps, end) for _, row in chosen]
            self.store.purge_expired()
            if head == self._head():
                return {
                    "environment": self.store.environment,
                    "days": days,
                    "start": start,
                    "end": end,
                    "timezone": "UTC",
                    "generated_at": end,
                    "basis": "current_retained_records",
                    "total": len(candidates),
                    "page": current_page,
                    "pages": pages,
                    "page_size": PAGE_SIZE,
                    "entries": entries,
                }
        raise ValueError("Cycle sources changed; refresh")
