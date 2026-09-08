"""Curated dashboard views over the same source selection and calculation services as CLI/MCP."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from .analytics import METRICS
from .briefing import build_daily_brief
from .contracts import timestamp
from .service import CopilotService
from .storage import Store


@dataclass(frozen=True)
class DashboardMetric:
    metric: str
    resource: str
    label: str
    unit_label: str


DASHBOARD_METRICS = {
    "hrv": DashboardMetric("whoop.hrv_rmssd", "recovery", "HRV", "ms"),
    "rhr": DashboardMetric("whoop.resting_heart_rate", "recovery", "静息心率", "bpm"),
    "recovery": DashboardMetric("whoop.recovery_score", "recovery", "恢复分", "%"),
    "sleep": DashboardMetric("whoop.sleep_performance", "sleep", "睡眠表现", "%"),
    "strain": DashboardMetric("whoop.strain", "cycle", "周期负荷", "/ 21"),
    "workout": DashboardMetric("whoop.strain", "workout", "训练负荷", "/ 21"),
}
EVIDENCE_METRICS = {
    **DASHBOARD_METRICS,
    "sleep_efficiency": DashboardMetric("whoop.sleep_efficiency", "sleep", "睡眠效率", "%"),
    "sleep_consistency": DashboardMetric("whoop.sleep_consistency", "sleep", "睡眠一致性", "%"),
    "respiratory_rate": DashboardMetric("whoop.respiratory_rate", "sleep", "睡眠呼吸率", "次/分钟"),
}
STATUS_LABELS = {
    "valid": "已评分",
    "calibrating": "校准中",
    "PENDING_SCORE": "等待评分",
    "UNSCORABLE": "无法评分",
    "missing_metric": "此指标未提供",
    "missing": "无已采集记录",
}


class DashboardService:
    def __init__(self, store: Store):
        self.store = store
        self.service = CopilotService(store)

    def window(self, days: int):
        if type(days) is not int or days not in (7, 30):
            raise ValueError("Dashboard window must be 7 or 30 days")
        end = datetime.fromisoformat(timestamp(self.store.clock()))
        start = end.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        return timestamp(start.isoformat()), timestamp(end.isoformat())

    @staticmethod
    def definition(key: str):
        if key not in DASHBOARD_METRICS:
            raise ValueError("Select a dashboard metric")
        return DASHBOARD_METRICS[key]

    @staticmethod
    def _source_info(revision):
        payload = json.loads(revision["payload"])
        if revision["resource"] == "recovery":
            record, interval = payload["recovery"], payload["cycle"]
        else:
            record = interval = payload["record"]
        return record, interval

    def _point(self, revision: dict, definition: DashboardMetric, observation: dict | None):
        record, interval = self._source_info(revision)
        status = record.get("score_state", "missing_metric")
        if observation:
            status = observation["quality"]
        elif status == "SCORED":
            status = "missing_metric"
        return {
            "revision_id": revision["id"],
            "measured_at": timestamp(interval["start"]),
            "measured_end": timestamp(interval["end"]) if interval.get("end") else None,
            "source_timezone": interval.get("timezone_offset"),
            "source_updated_at": revision["source_updated_at"],
            "metric_updated_at": timestamp(record["updated_at"]),
            "interval_updated_at": timestamp(interval["updated_at"]),
            "known_at": revision["known_at"],
            "status": status,
            "status_label": STATUS_LABELS.get(status, "不参与统计"),
            "value": observation["value"] if observation and status == "valid" else None,
            "unit": METRICS[definition.metric].unit,
        }

    def _metric_view(self, key: str, start: str, end: str):
        definition = self.definition(key)
        run = self.service.analyze(
            definition.metric,
            start,
            end,
            provider="whoop",
            resource=definition.resource,
            persist=False,
        )
        observations = {row["revision_id"]: row for row in run["evidence"]["observations"]}
        records = []
        for revision in self.store.current_sources(definition.resource):
            point = self._point(revision, definition, observations.get(revision["id"]))
            if start <= point["measured_at"] < end:
                records.append(point)
        records.sort(key=lambda row: (row["measured_at"], row["revision_id"]))
        if len(records) > 10000:
            raise ValueError("Dashboard record budget exceeded")
        seen = {row["measured_at"][:10] for row in records}
        gaps = []
        day = datetime.fromisoformat(start)
        finish = datetime.fromisoformat(end)
        while day < finish:
            if day.date().isoformat() not in seen:
                gaps.append(
                    {
                        "measured_at": timestamp(day.isoformat()),
                        "value": None,
                        "revision_id": None,
                        "status": "missing",
                        "status_label": STATUS_LABELS["missing"],
                    }
                )
            day += timedelta(days=1)
        coverage = self.coverage(records, run["result"]["count"])
        return {
            "key": key,
            "label": definition.label,
            "unit_label": definition.unit_label,
            "metric": definition.metric,
            "resource": definition.resource,
            "latest": records[-1] if records else None,
            "points": sorted(records + gaps, key=lambda point: point["measured_at"]),
            "records": records,
            "missing_days": len(gaps),
            "coverage": coverage,
            "summary": run["result"],
            "analysis_id": run["run_id"],
            "summary_origin": "本应用计算 · mean_change/1",
        }

    @staticmethod
    def coverage(records, valid_count):
        """Source coverage metadata shared by rolling views and calendar-week reviews."""
        status_counts = Counter(record["status"] for record in records)
        return {
            "first_record_at": records[0]["measured_at"] if records else None,
            "last_record_at": records[-1]["measured_at"] if records else None,
            "record_days": len({record["measured_at"][:10] for record in records}),
            "record_count": len(records),
            "valid_observation_count": valid_count,
            "states": [
                {"status": status, "label": STATUS_LABELS.get(status, "不参与统计"), "count": count}
                for status, count in sorted(status_counts.items())
            ],
            "state": "no_records"
            if not records
            else "no_valid_observations"
            if not valid_count
            else "has_valid_observations",
        }

    def overview(self, key: str = "hrv", days: int = 7):
        self.definition(key)
        start, end = self.window(days)
        # A concurrent CLI import must not leave cards and the selected trend on different versions.
        for _ in range(2):
            self.store.purge_expired()
            catalog = tuple(
                self.store.db.execute(
                    "SELECT COALESCE(MAX(id),0),COUNT(*) FROM source_revisions"
                ).fetchone()
            )
            views = {
                name: self._metric_view(name, start, end)
                for name in dict.fromkeys(("recovery", "sleep", "strain", key))
            }
            cards = [views[name] for name in ("recovery", "sleep", "strain")]
            briefing = build_daily_brief(cards, start=start, end=end, days=days)
            self.store.purge_expired()
            if catalog == tuple(
                self.store.db.execute(
                    "SELECT COALESCE(MAX(id),0),COUNT(*) FROM source_revisions"
                ).fetchone()
            ):
                return {
                    "environment": self.store.environment,
                    "days": days,
                    "start": start,
                    "end": end,
                    "timezone": "UTC",
                    "generated_at": self.store.clock(),
                    "metrics": [
                        {"key": name, "label": value.label}
                        for name, value in DASHBOARD_METRICS.items()
                    ],
                    "cards": cards,
                    "briefing": briefing,
                    "trend": views[key],
                }
        raise ValueError("Data changed while reading; refresh the dashboard")

    def evidence(self, key: str, revision_id: int):
        if key not in EVIDENCE_METRICS:
            raise ValueError("Select an available source metric")
        definition = EVIDENCE_METRICS[key]
        if type(revision_id) is not int or revision_id <= 0:
            raise ValueError("Select a valid source record")
        self.store.purge_expired()
        row = self.store.db.execute(
            """SELECT r.* FROM source_revisions r JOIN source_connections c ON c.id=r.connection_id
            WHERE r.id=? AND r.resource=? AND c.provider='whoop' AND r.deleted=0""",
            (revision_id, definition.resource),
        ).fetchone()
        if row is None:
            raise ValueError("This source record is unavailable or expired")
        revision = dict(row)
        observation = self.store.db.execute(
            "SELECT * FROM observations WHERE revision_id=? AND metric=?",
            (revision_id, definition.metric),
        ).fetchone()
        observation = dict(observation) if observation else None
        current = {item["id"] for item in self.store.current_sources(definition.resource)}
        metadata = json.loads(revision["metadata"])
        return {
            **self._point(revision, definition, observation),
            "label": definition.label,
            "unit_label": definition.unit_label,
            "source": "WHOOP API v2",
            "resource": definition.resource,
            "is_current": revision_id in current,
            "captured_at": metadata.get("captured_at", revision["known_at"]),
            "expires_at": revision["expires_at"],
            "original_value": observation["original_value"] if observation else None,
            "original_unit": observation["original_unit"] if observation else None,
            "interpretation": "官方观测直接保留；窗口均值和半区间差值由本应用计算。",
        }
