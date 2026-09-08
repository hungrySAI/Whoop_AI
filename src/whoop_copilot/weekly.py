"""Bounded calendar-week reviews of current, locally retained official API records."""

from datetime import date, datetime, timedelta

from .contracts import timestamp
from .dashboard import DASHBOARD_METRICS, DashboardService

WEEKS = 12
PAGE_SIZE = 30
KEYS = ("recovery", "sleep", "strain", "hrv", "rhr", "workout")


class WeeklyService:
    def __init__(self, store):
        self.store = store
        self.dashboard = DashboardService(store)

    def _window(self, week):
        now = datetime.fromisoformat(timestamp(self.store.clock()))
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=now.weekday()
        )
        choices = [monday - timedelta(weeks=index) for index in range(WEEKS)]
        if week is None:
            chosen = choices[1]
        else:
            if not isinstance(week, str) or len(week) != 10:
                raise ValueError("Choose an available week")
            parsed = date.fromisoformat(week)
            if parsed.isoformat() != week or parsed not in {value.date() for value in choices}:
                raise ValueError("Choose an available week")
            chosen = next(value for value in choices if value.date() == parsed)
        end = chosen + timedelta(weeks=1)
        return {
            "week": chosen.date().isoformat(),
            "start": timestamp(chosen.isoformat()),
            "calendar_end": timestamp(end.isoformat()),
            "end": timestamp(min(end, now).isoformat()),
            "previous_start": timestamp((chosen - timedelta(weeks=1)).isoformat()),
            "partial": chosen == monday,
            "as_of": timestamp(now.isoformat()),
            "timezone": "UTC",
            "choices": [
                {
                    "week": value.date().isoformat(),
                    "last_date": (value + timedelta(days=6)).date().isoformat(),
                    "partial": value == monday,
                }
                for value in choices
            ],
        }

    @staticmethod
    def _period(records, stats, key, as_of, analysis_id):
        return {
            "mean": stats["mean"] if stats else None,
            "coverage": DashboardService.coverage(records, stats["count"] if stats else 0),
            "open_cycles": sum(
                key == "strain" and (row["measured_end"] is None or row["measured_end"] > as_of)
                for row in records
            ),
            "analysis_id": analysis_id,
        }

    def _metric(self, key, window):
        self.dashboard.definition(key)
        start, end, previous = window["start"], window["end"], window["previous_start"]
        if not window["partial"]:
            view = self.dashboard._metric_view(key, previous, end)
            summary = view["summary"]
            assert summary["midpoint"] == start
            before = [row for row in view["records"] if row["measured_at"] < start]
            after = [row for row in view["records"] if row["measured_at"] >= start]
            old_stats, new_stats = summary["first_half"], summary["second_half"]
            old_id = new_id = comparison_id = view["analysis_id"]
            comparison_state = summary["status"]
            change = summary["change"] if comparison_state == "descriptive" else None
        else:
            old = self.dashboard._metric_view(key, previous, start)
            current = self.dashboard._metric_view(key, start, end) if start < end else None
            before, after = old["records"], current["records"] if current else []
            old_stats, new_stats = old["summary"], current["summary"] if current else None
            old_id, new_id = old["analysis_id"], current["analysis_id"] if current else None
            comparison_id, change, comparison_state = None, None, "week_in_progress"
        if len(before) + len(after) > 10000:
            raise ValueError("Weekly record budget exceeded")
        definition = DASHBOARD_METRICS[key]
        unit = (
            "百分点"
            if definition.unit_label == "%"
            else "分"
            if definition.unit_label == "/ 21"
            else definition.unit_label
        )
        return {
            "key": key,
            "label": definition.label,
            "unit_label": definition.unit_label,
            "selected": self._period(after, new_stats, key, window["as_of"], new_id),
            "previous": self._period(before, old_stats, key, window["as_of"], old_id),
            "comparison": {
                "state": comparison_state,
                "change": change,
                "unit_label": unit,
                "analysis_id": comparison_id,
                "origin": "本应用计算 · mean_change/1",
            },
        }, [
            {**row, "period": period}
            for period, rows in (("previous", before), ("selected", after))
            for row in rows
        ]

    def _head(self):
        return tuple(
            self.store.db.execute("SELECT MAX(id),COUNT(*) FROM source_revisions").fetchone()
        )

    def _read(self, week, keys):
        window = self._window(week)
        for _ in range(2):
            self.store.purge_expired()
            head = self._head()
            metrics = [self._metric(key, window) for key in keys]
            self.store.purge_expired()
            if head == self._head():
                return window, metrics
        raise ValueError("Weekly sources changed; refresh")

    def review(self, week=None):
        window, metrics = self._read(week, KEYS)
        return {
            **window,
            "environment": self.store.environment,
            "metrics": [item for item, _ in metrics],
            "basis": "current_retained_records",
        }

    def records(self, week=None, key="recovery", page=1):
        if type(page) is not int or not 1 <= page <= 334:
            raise ValueError("Choose a valid record page")
        self.dashboard.definition(key)
        # Details and the entire report must share a read, including across Monday
        # rollover or a source correction/deletion since the overview was opened.
        window, metrics = self._read(week, KEYS)
        metric, rows = metrics[KEYS.index(key)]
        rows.sort(key=lambda row: (row["measured_at"], row["revision_id"]), reverse=True)
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        return {
            **window,
            "environment": self.store.environment,
            "metric": metric,
            "metrics": [item for item, _ in metrics],
            "basis": "current_retained_records",
            "page": page,
            "pages": pages,
            "total": len(rows),
            "records": rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        }
