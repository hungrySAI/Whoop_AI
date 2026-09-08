"""Read-only, curated Journal timeline over explicitly mapped export source revisions."""

import json
from datetime import date

from .contracts import timestamp
from .dashboard import DashboardService

PAGE_SIZE = 20
DATE_LABELS = {"reported_date": "日志归属日期", "cycle_start": "周期起始日期"}


class JournalService:
    def __init__(self, store):
        self.store = store
        self.dashboard = DashboardService(store)

    @staticmethod
    def _project(row):
        payload = json.loads(row["payload"])
        journal = payload.get("journal")
        if journal is None:
            return None  # Version 1 imports remain raw evidence; never guess display columns.
        if (
            journal.get("version") != 1
            or journal.get("subject_confirmation") != "operator_confirmed_local_subject"
        ):
            raise ValueError("Journal needs a supported, operator-confirmed mapping")
        if journal["date_basis"] not in DATE_LABELS or journal["time_precision"] not in (
            "date",
            "instant",
        ):
            raise ValueError("Journal date semantics are unavailable")
        answers = [
            {"question": item["question"], "answer": item["answer"]} for item in journal["answers"]
        ]
        if not 1 <= len(answers) <= 128 or any(
            not isinstance(item["question"], str)
            or len(item["question"]) > 1000
            or (
                item["answer"] is not None
                and (not isinstance(item["answer"], str) or len(item["answer"]) > 8000)
            )
            for item in answers
        ):
            raise ValueError("Journal text exceeds the display budget")
        return {
            "revision_id": row["id"],
            "date": date.fromisoformat(journal["date"]).isoformat(),
            "date_basis": journal["date_basis"],
            "date_label": DATE_LABELS[journal["date_basis"]],
            "time_precision": journal["time_precision"],
            "source_at": timestamp(journal["source_at"]) if journal["source_at"] else None,
            "timezone": journal["timezone"],
            "day_start": timestamp(journal["day_start"]),
            "day_end": timestamp(journal["day_end"]),
            "answers": answers,
            "source": "WHOOP Journal · 官方导出",
            "exported_at": row["source_updated_at"],
            "imported_at": row["known_at"],
            "expires_at": row["expires_at"],
            "association": "本机操作者确认属于此人；未通过 OAuth 自动核验导出账户。",
        }

    def _head(self):
        return tuple(
            self.store.db.execute("SELECT MAX(id),COUNT(*) FROM source_revisions").fetchone()
        )

    def timeline(self, days=7, page=1):
        start, end = self.dashboard.window(days)
        start_date, end_date = start[:10], end[:10]
        if type(page) is not int or not 1 <= page <= 500:
            raise ValueError("Select a valid Journal page")
        for _ in range(2):
            self.store.purge_expired()
            head = self._head()
            rows = self.store.current_sources("journal", provider="whoop_export")
            if len(rows) > 10000:
                raise ValueError("Journal source budget exceeded")
            projected = [self._project(row) for row in rows]
            mapped = [entry for entry in projected if entry is not None]
            entries = [entry for entry in mapped if start_date <= entry["date"] <= end_date]
            entries.sort(
                key=lambda entry: (entry["date"], entry["source_at"] or "", entry["revision_id"]),
                reverse=True,
            )
            total = len(entries)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            current_page = min(page, pages)
            entries = entries[(current_page - 1) * PAGE_SIZE : current_page * PAGE_SIZE]
            if entries:
                first = min(entry["day_start"] for entry in entries)
                last = max(entry["day_end"] for entry in entries)
                views = [
                    self.dashboard._metric_view(key, first, last)
                    for key in ("recovery", "sleep", "strain")
                ]
                for entry in entries:
                    entry["metrics"] = []
                    for view in views:
                        records = [
                            point
                            for point in view["records"]
                            if entry["day_start"] <= point["measured_at"] < entry["day_end"]
                        ]
                        entry["metrics"].append(
                            {
                                "key": view["key"],
                                "label": view["label"],
                                "unit_label": view["unit_label"],
                                "latest": records[-1] if records else None,
                                "record_count": len(records),
                            }
                        )
            self.store.purge_expired()
            if head == self._head():
                return {
                    "environment": self.store.environment,
                    "days": days,
                    "start_date": start_date,
                    "end_date": end_date,
                    "page": current_page,
                    "pages": pages,
                    "page_size": PAGE_SIZE,
                    "total": total,
                    "mapped_total": len(mapped),
                    "unmapped_total": len(rows) - len(mapped),
                    "state": "ready"
                    if total
                    else "outside_window"
                    if mapped
                    else "mapping_required"
                    if rows
                    else "no_import",
                    "latest_export_at": max(
                        (row["source_updated_at"] for row in rows), default=None
                    ),
                    "latest_import_at": max((row["known_at"] for row in rows), default=None),
                    "entries": entries,
                    "generated_at": self.store.clock(),
                }
        raise ValueError("Data changed while reading the Journal; refresh")

    def evidence(self, revision_id):
        if type(revision_id) is not int or revision_id <= 0:
            raise ValueError("Select a valid Journal source")
        # A stale detail request cannot resurrect superseded, deleted or expired Journal text.
        for _ in range(2):
            self.store.purge_expired()
            head = self._head()
            row = next(
                (
                    row
                    for row in self.store.current_sources("journal", provider="whoop_export")
                    if row["id"] == revision_id
                ),
                None,
            )
            entry = self._project(row) if row else None
            self.store.purge_expired()
            if entry is None:
                break
            if head == self._head():
                return entry
        raise ValueError("This Journal source is unavailable; refresh the timeline")
