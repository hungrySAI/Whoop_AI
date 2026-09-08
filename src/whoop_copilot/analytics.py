"""Registered deterministic calculations, independent of transport and model providers."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import fmean

from .contracts import timestamp


@dataclass(frozen=True)
class MetricDefinition:
    unit: str
    description: str
    official: bool = False


METRICS = {
    "whoop.hrv_rmssd": MetricDefinition("ms", "WHOOP reported HRV (RMSSD)", True),
    "whoop.recovery_score": MetricDefinition("%", "WHOOP reported recovery score", True),
    "whoop.resting_heart_rate": MetricDefinition("bpm", "WHOOP reported resting heart rate", True),
    "whoop.spo2": MetricDefinition("%", "WHOOP reported blood oxygen percentage", True),
    "whoop.skin_temp": MetricDefinition("degC", "WHOOP reported skin temperature", True),
    "body.weight": MetricDefinition("kg", "Manual synthetic body weight"),
    "whoop.strain": MetricDefinition(
        "score", "WHOOP strain; select cycle or workout explicitly", True
    ),
    "whoop.average_heart_rate": MetricDefinition("bpm", "WHOOP activity average heart rate", True),
    "whoop.max_heart_rate": MetricDefinition("bpm", "WHOOP activity maximum heart rate", True),
    "whoop.kilojoule": MetricDefinition("kJ", "WHOOP reported energy", True),
    "whoop.sleep_performance": MetricDefinition("%", "WHOOP sleep performance", True),
    "whoop.sleep_efficiency": MetricDefinition("%", "WHOOP sleep efficiency", True),
    "whoop.sleep_consistency": MetricDefinition("%", "WHOOP sleep consistency", True),
    "whoop.respiratory_rate": MetricDefinition("breaths/min", "WHOOP sleep respiratory rate", True),
}


def list_metrics() -> dict:
    return {key: asdict(value) for key, value in METRICS.items()}


def mean_change(observations: list[dict], start: str, end: str) -> dict:
    """Compare means of equal-duration halves, never recompute an official score."""
    valid = [row for row in observations if row["quality"] == "valid"]
    begin = datetime.fromisoformat(timestamp(start))
    finish = datetime.fromisoformat(timestamp(end))
    midpoint = begin + (finish - begin) / 2
    early = [row["value"] for row in valid if datetime.fromisoformat(row["start_at"]) < midpoint]
    late = [row["value"] for row in valid if datetime.fromisoformat(row["start_at"]) >= midpoint]
    values = [row["value"] for row in valid]
    first = fmean(early) if early else None
    second = fmean(late) if late else None
    return {
        "count": len(values),
        "excluded_quality_count": len(observations) - len(values),
        "mean": fmean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "first_half": {"count": len(early), "mean": first},
        "second_half": {"count": len(late), "mean": second},
        "change": second - first if first is not None and second is not None else None,
        "midpoint": timestamp(midpoint.isoformat()),
        "status": "descriptive" if min(len(early), len(late)) >= 2 else "insufficient_data",
    }


@dataclass(frozen=True)
class Algorithm:
    version: str
    calculate: Callable[[list[dict], str, str], dict]


ALGORITHMS = {"mean_change": Algorithm("mean_change/1", mean_change)}
