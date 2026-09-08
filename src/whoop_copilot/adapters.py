"""Synthetic-only adapters using WHOOP v2 fields and provider-neutral contracts.

WHOOP computes its own scores. This adapter retains those numbers and the entire
linked source records; it does not estimate recovery or derive sleep measurements.
See https://developer.whoop.com/api/ for the upstream field definitions.
"""

import csv
import json
import re
from math import isfinite
from pathlib import Path
from typing import Any

from .contracts import ActivityInput, ObservationInput, SourceRecordInput, timestamp

WHOOP_METRICS = {
    "hrv_rmssd_milli": ("whoop.hrv_rmssd", "ms"),
    "recovery_score": ("whoop.recovery_score", "%"),
    "resting_heart_rate": ("whoop.resting_heart_rate", "bpm"),
    "spo2_percentage": ("whoop.spo2", "%"),
    "skin_temp_celsius": ("whoop.skin_temp", "degC"),
}
SCORE_STATES = {"SCORED", "PENDING_SCORE", "UNSCORABLE"}
CSV_FIELDS = {"synthetic", "external_id", "metric", "measured_at", "updated_at", "value", "unit"}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")  # noqa: TRY004 - malformed source data
    return value


def _required(record: dict[str, Any], field: str, label: str) -> Any:
    if field not in record:
        raise ValueError(f"{label} is missing {field}")
    return record[field]


def _time(value: Any, label: str) -> str:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"{label} must be an ISO timestamp with a timezone offset")
    try:
        return timestamp(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp with a timezone offset") from exc


def _identifier(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _number(value: Any, label: str, *, from_csv: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, str) if from_csv else (int, float)
    ):
        raise ValueError(f"{label} must be numeric")  # noqa: TRY004 - malformed source data
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _score_state(record: dict[str, Any], label: str) -> str:
    state = _required(record, "score_state", label)
    if not isinstance(state, str) or state not in SCORE_STATES:
        raise ValueError(f"{label}.score_state must be a documented WHOOP score state")
    return state


def _json_number(value: str) -> float:
    # Reject non-finite values anywhere in raw evidence, including unused fields.
    return _number(value, "WHOOP JSON number", from_csv=True)


def _offset(value: Any) -> str:
    if value == "Z":
        return "+00:00"
    if not isinstance(value, str) or not re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        raise ValueError("cycle.timezone_offset must use +HH:MM or -HH:MM")
    hours, minutes = map(int, value[1:].split(":"))
    if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
        raise ValueError("cycle.timezone_offset is outside the supported UTC offset range")
    return value


def parse_whoop(path: Path) -> list[SourceRecordInput]:
    """Read a synthetic bundle of WHOOP v2 cycles and recoveries atomically.

    A recovery revision includes its complete linked cycle. The composite source
    modification time is the later upstream timestamp, so changing the interval
    or timezone can produce a new revision even if the recovery did not change.
    The external WHOOP user ID is provenance only, never the local subject ID.
    """
    with path.open(encoding="utf-8") as handle:
        bundle = _object(
            json.load(handle, parse_float=_json_number, parse_constant=_json_number), "bundle"
        )
    if bundle.get("synthetic") is not True:
        raise ValueError("WHOOP fixture must explicitly declare synthetic: true")
    return parse_whoop_data(bundle, synthetic=True)


def parse_whoop_data(bundle: dict, *, synthetic: bool) -> list[SourceRecordInput]:
    """Normalize paired records in memory; caller establishes the source environment."""
    if type(synthetic) is not bool:
        raise ValueError("Source environment must be explicit")
    for field in ("cycles", "recoveries"):
        if not isinstance(bundle.get(field), list):
            raise ValueError(f"bundle.{field} must be a list")  # noqa: TRY004 - malformed source data

    cycles: dict[int, dict[str, Any]] = {}
    intervals: dict[int, tuple[str, str | None, str, str]] = {}
    external_users: set[int] = set()
    for raw_cycle in bundle["cycles"]:
        cycle = _object(raw_cycle, "cycle")
        cycle_id = _identifier(_required(cycle, "id", "cycle"), "cycle.id")
        if cycle_id in cycles:
            raise ValueError(f"Duplicate WHOOP cycle ID: {cycle_id}")
        external_users.add(_identifier(_required(cycle, "user_id", "cycle"), "cycle.user_id"))
        start = _time(_required(cycle, "start", "cycle"), "cycle.start")
        raw_end = cycle.get("end")
        end = _time(raw_end, "cycle.end") if raw_end is not None else None
        if end is not None and end <= start:
            raise ValueError("cycle.end must be after cycle.start")
        offset = _offset(_required(cycle, "timezone_offset", "cycle"))
        updated = _time(_required(cycle, "updated_at", "cycle"), "cycle.updated_at")
        _time(_required(cycle, "created_at", "cycle"), "cycle.created_at")
        _score_state(cycle, "cycle")
        cycles[cycle_id] = cycle
        intervals[cycle_id] = (start, end, offset, updated)

    records: list[SourceRecordInput] = []
    recovery_ids: set[int] = set()
    for raw_recovery in bundle["recoveries"]:
        recovery = _object(raw_recovery, "recovery")
        cycle_id = _identifier(_required(recovery, "cycle_id", "recovery"), "recovery.cycle_id")
        if cycle_id in recovery_ids:
            raise ValueError(f"Duplicate WHOOP recovery cycle ID: {cycle_id}")
        recovery_ids.add(cycle_id)
        if cycle_id not in cycles:
            raise ValueError(f"Recovery references missing WHOOP cycle: {cycle_id}")
        external_user = _identifier(_required(recovery, "user_id", "recovery"), "recovery.user_id")
        external_users.add(external_user)
        if external_user != cycles[cycle_id]["user_id"]:
            raise ValueError("Recovery and cycle belong to different WHOOP users")
        state = _score_state(recovery, "recovery")
        updated = _time(_required(recovery, "updated_at", "recovery"), "recovery.updated_at")
        _time(_required(recovery, "created_at", "recovery"), "recovery.created_at")
        start, end, offset, cycle_updated = intervals[cycle_id]
        observations: list[ObservationInput] = []
        score = recovery.get("score")
        if score is not None:
            score = _object(score, "recovery.score")
            calibrating = _required(score, "user_calibrating", "recovery.score")
            if not isinstance(calibrating, bool):
                raise ValueError("recovery.score.user_calibrating must be boolean")
            for field, (metric, unit) in WHOOP_METRICS.items():
                if score.get(field) is None:
                    continue
                value = _number(score[field], f"recovery.score.{field}")
                if state == "SCORED":
                    observations.append(
                        ObservationInput(
                            metric=metric,
                            value=value,
                            unit=unit,
                            original_value=value,
                            original_unit=unit,
                            start_at=start,
                            end_at=end,
                            time_precision="interval",
                            quality="calibrating" if calibrating else "valid",
                            official=True,
                        )
                    )
        records.append(
            SourceRecordInput(
                provider="whoop",
                resource="recovery",
                external_id=str(cycle_id),
                source_updated_at=max(updated, cycle_updated),
                payload={"synthetic": synthetic, "recovery": recovery, "cycle": cycles[cycle_id]},
                observations=tuple(observations),
                activities=(
                    ActivityInput(
                        kind="whoop_cycle",
                        external_id=str(cycle_id),
                        start_at=start,
                        end_at=end,
                        timezone_offset=offset,
                    ),
                ),
                parser_version="whoop-v2-synthetic-1" if synthetic else "whoop-v2-api-1",
                metadata={
                    "synthetic": synthetic,
                    "whoop_user_id": str(external_user),
                    "source_version_parts": {"recovery": updated, "cycle": cycle_updated},
                },
            )
        )
    if len(external_users) > 1:
        raise ValueError("A WHOOP fixture cannot mix multiple external users")
    return records


def parse_csv(path: Path) -> list[SourceRecordInput]:
    """Read explicitly synthetic body weights, preserving submitted values/units."""
    records: list[SourceRecordInput] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not CSV_FIELDS.issubset(reader.fieldnames):
            raise ValueError(f"CSV requires columns: {', '.join(sorted(CSV_FIELDS))}")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV contains duplicate column names")
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV row {row_number} has the wrong number of columns")
            if row["synthetic"].strip().lower() != "true":
                raise ValueError(f"CSV row {row_number} must explicitly declare synthetic=true")
            external_id = row["external_id"].strip()
            if not external_id or external_id in seen:
                raise ValueError(f"CSV row {row_number} has an empty or duplicate external_id")
            seen.add(external_id)
            if row["metric"].strip() != "body.weight":
                raise ValueError(
                    f"CSV row {row_number} has unsupported metric; expected body.weight"
                )
            unit = row["unit"].strip()
            if unit not in {"kg", "lb"}:
                raise ValueError(f"CSV row {row_number} has unsupported unit; expected kg or lb")
            original_value = _number(row["value"], f"CSV row {row_number} value", from_csv=True)
            if original_value <= 0:
                raise ValueError(f"CSV row {row_number} body weight must be positive")
            value = original_value if unit == "kg" else original_value * 0.45359237
            measured_at = _time(row["measured_at"], f"CSV row {row_number} measured_at")
            updated_at = _time(row["updated_at"], f"CSV row {row_number} updated_at")
            records.append(
                SourceRecordInput(
                    provider="manual",
                    resource="body_metric",
                    external_id=external_id,
                    source_updated_at=updated_at,
                    payload={"synthetic": True, "row": dict(row)},
                    observations=(
                        ObservationInput(
                            metric="body.weight",
                            value=value,
                            unit="kg",
                            original_value=original_value,
                            original_unit=unit,
                            start_at=measured_at,
                        ),
                    ),
                    parser_version="body-csv-synthetic-1",
                    metadata={"synthetic": True},
                )
            )
    return records
