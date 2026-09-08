"""Bounded WHOOP CSV/ZIP import with an explicit, user-reviewed mapping.

WHOOP documents four export categories, but does not publish a versioned CSV
schema. This module therefore never guesses headers, units, identity, or time
zones. The example profile and fixture are synthetic examples, not an assertion
that a real WHOOP export uses those headers. Real-file interoperability: not_run.

Profile version 1 requires resource, identity_columns, start, timezone, metrics,
and (for ZIPs) an exact member. Start/end specifications contain a column and
format ("iso8601" or a strptime format). timezone is an IANA name, fixed offset,
or "from_timestamp". An explicit offset in a timestamp takes precedence over
the configured fallback; ambiguous/nonexistent naive local times are rejected.

Profile version 2 is reserved for Journal: explicit question/answer bindings,
date basis and precision, stable identity, timezone, and local-subject confirmation.
Date-only entries preserve their calendar label without inventing a measured
instant. Their day bounds are comparison metadata, not metric observations.

No archive is extracted. Limits are intentionally conservative: 32 MiB input
and total uncompressed bytes, 32 archive entries, a 100:1 compression ratio,
10,000 rows per selected CSV, and 256 columns. Inspection returns schema/counts
only, including which recognizable ISO datetime columns contain naive times.
"""

import csv
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import UTC, date, datetime, time, timedelta, timezone
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ActivityInput, ObservationInput, SourceRecordInput, timestamp

MAX_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 32
MAX_ROWS = 10_000
MAX_COLUMNS = 256
MAX_COMPRESSION_RATIO = 100
RESOURCES = {"physiological_cycles", "sleep", "workout", "journal"}

# Exact semantic targets; these are not guessed export header names. Values are
# WHOOP-reported results. An empty CSV cell stays absent, never zero.
METRIC_UNITS = {
    "whoop.hrv_rmssd": "ms",
    "whoop.recovery_score": "%",
    "whoop.resting_heart_rate": "bpm",
    "whoop.spo2": "%",
    "whoop.skin_temp": "degC",
    "whoop.strain": "score",
    "whoop.average_heart_rate": "bpm",
    "whoop.max_heart_rate": "bpm",
    "whoop.kilojoule": "kJ",
    "whoop.sleep_performance": "%",
    "whoop.sleep_efficiency": "%",
    "whoop.sleep_consistency": "%",
    "whoop.respiratory_rate": "breaths/min",
}
RECOVERY_METRICS = {
    "whoop.hrv_rmssd",
    "whoop.recovery_score",
    "whoop.resting_heart_rate",
    "whoop.spo2",
    "whoop.skin_temp",
}
LOAD_METRICS = {
    "whoop.strain",
    "whoop.average_heart_rate",
    "whoop.max_heart_rate",
    "whoop.kilojoule",
}
RESOURCE_METRICS = {
    "physiological_cycles": RECOVERY_METRICS | LOAD_METRICS,
    "sleep": {
        "whoop.sleep_performance",
        "whoop.sleep_efficiency",
        "whoop.sleep_consistency",
        "whoop.respiratory_rate",
    },
    "workout": LOAD_METRICS,
    "journal": set(),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read_input(path: Path) -> bytes:
    try:
        if not path.is_file():
            raise ValueError("Export must be a regular file")
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Export must be a regular file")
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError("Export cannot be read") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("Export exceeds the 32 MiB input limit")
    return raw


def _archive(raw: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("Export is not a valid ZIP archive") from exc
    try:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_MEMBERS:
            raise ValueError("ZIP must contain between 1 and 32 entries")
        seen: set[str] = set()
        total = 0
        for entry in entries:
            name = entry.orig_filename
            parts = PurePosixPath(name).parts
            if (
                not name
                or "\x00" in name
                or "\\" in name
                or name.startswith("/")
                or re.match(r"^[A-Za-z]:", name)
                or ".." in parts
                or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
            ):
                raise ValueError("ZIP contains an unsafe member path")
            if name in seen:
                raise ValueError("ZIP contains duplicate member names")
            seen.add(name)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise ValueError("ZIP contains a symlink or unsupported member type")
            if entry.flag_bits & 1:
                raise ValueError("Encrypted ZIP members are not supported")
            if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("ZIP member compression is unsupported")
            total += entry.file_size
            if total > MAX_BYTES or entry.file_size > MAX_BYTES:
                raise ValueError("ZIP exceeds the 32 MiB uncompressed limit")
            if entry.file_size > max(entry.compress_size, 1) * MAX_COMPRESSION_RATIO:
                raise ValueError("ZIP member exceeds the safe compression ratio")
        if not any(
            not entry.is_dir() and entry.filename.lower().endswith(".csv") for entry in entries
        ):
            raise ValueError("ZIP contains no CSV members")
    except Exception:
        archive.close()
        raise
    return archive


def _member_bytes(archive: zipfile.ZipFile, member: str) -> bytes:
    try:
        with archive.open(member) as handle:
            raw = handle.read(MAX_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ValueError("ZIP member cannot be read or failed integrity verification") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("ZIP member exceeds the uncompressed limit")
    return raw


def _csv_rows(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must use UTF-8 (an optional BOM is supported)") from exc
    if "\x00" in text:
        raise ValueError("CSV contains a NUL byte")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        columns = reader.fieldnames
        if not columns or not all(column.strip() for column in columns):
            raise ValueError("CSV requires nonempty column names")
        if len(columns) > MAX_COLUMNS:
            raise ValueError("CSV exceeds the 256-column limit")
        if len(set(columns)) != len(columns):
            raise ValueError("CSV contains duplicate column names")
        rows = []
        for row_number, row in enumerate(reader, start=2):
            if len(rows) >= MAX_ROWS:
                raise ValueError("CSV exceeds the 10000-row limit")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV row {row_number} has the wrong number of columns")
            rows.append(row)
    except csv.Error as exc:
        raise ValueError("CSV syntax is invalid or a field exceeds the parser limit") from exc
    return columns, rows


def _inspect_csv(raw: bytes, member: str | None) -> dict[str, Any]:
    columns, rows = _csv_rows(raw)
    time_columns = []
    for column in columns:
        naive_count = aware_count = 0
        for row in rows:
            value = row[column].strip()
            if not re.search(r"[T ]\d{2}:\d{2}", value):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                naive_count += 1
            else:
                aware_count += 1
        if naive_count or aware_count:
            time_columns.append(
                {
                    "column": column,
                    "naive_count": naive_count,
                    "offset_count": aware_count,
                    "needs_timezone": naive_count > 0,
                }
            )
    return {
        "member": member,
        "format": "csv",
        "columns": columns,
        "row_count": len(rows),
        "recognized_datetime_columns": time_columns,
        "timezone_requirement": "explicit_mapping_required",
    }


def inspect_export(path: Path | str) -> dict[str, Any]:
    """Return only file/member names, columns, counts, and time-zone diagnostics.

    Datetime recognition is diagnostic only. Non-ISO dates may not be recognized;
    every import still requires an explicit mapping and timezone policy.
    """
    path = Path(path)
    raw = _read_input(path)
    if path.suffix.lower() == ".csv":
        members = [_inspect_csv(raw, None)]
        source_format = "csv"
    elif path.suffix.lower() == ".zip":
        with _archive(raw) as archive:
            members = []
            total_rows = 0
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                if entry.filename.lower().endswith(".csv"):
                    info = _inspect_csv(_member_bytes(archive, entry.filename), entry.filename)
                    total_rows += info["row_count"]
                    if total_rows > MAX_ROWS:
                        raise ValueError("ZIP inspection exceeds the 10000-row total limit")
                    members.append(info)
                else:
                    members.append({"member": entry.filename, "format": "unsupported"})
        source_format = "zip"
    else:
        raise ValueError("Export must be a CSV or ZIP file")
    return {"format": source_format, "members": members, "mapping_required": True}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _zone(value: Any) -> timezone | ZoneInfo | None:
    if not isinstance(value, str) or not value:
        raise ValueError("Mapping requires an explicit timezone or from_timestamp")
    if value == "from_timestamp":
        return None
    if re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        hours, minutes = map(int, value[1:].split(":"))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError("Timezone offset is outside the supported range")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if value[0] == "+" else -delta)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Mapping timezone must be an IANA name or +/-HH:MM") from exc


def _field_spec(value: Any, label: str) -> dict[str, str]:
    value = _object(value, label)
    if set(value) != {"column", "format"} or any(
        not isinstance(value[key], str) or not value[key] for key in ("column", "format")
    ):
        raise ValueError(f"{label} requires exactly a nonempty column and format")
    if value["format"] != "iso8601" and not all(
        part in value["format"] for part in ("%Y", "%m", "%d")
    ):
        raise ValueError(f"{label} strptime format must explicitly include year, month and day")
    return value


def _mapping(profile: Any) -> dict[str, Any]:
    profile = _object(profile, "Mapping")
    required = {"version", "resource", "identity_columns", "start", "timezone", "metrics"}
    if not required.issubset(profile) or set(profile) - required - {"member", "end", "journal"}:
        raise ValueError("Mapping has missing required or unknown fields")
    if type(profile["version"]) is not int or profile["version"] not in (1, 2):
        raise ValueError("Only export mapping versions 1 and 2 are supported")
    resource = profile["resource"]
    if not isinstance(resource, str) or resource not in RESOURCES:
        raise ValueError("Mapping resource must be physiological_cycles, sleep, workout or journal")
    identity_columns = profile["identity_columns"]
    if (
        not isinstance(identity_columns, list)
        or not identity_columns
        or any(not isinstance(column, str) or not column for column in identity_columns)
        or len(set(identity_columns)) != len(identity_columns)
    ):
        raise ValueError("Mapping identity_columns must contain distinct explicit column names")
    _field_spec(profile["start"], "Mapping start")
    if profile.get("end") is not None:
        _field_spec(profile["end"], "Mapping end")
    elif resource in {"sleep", "workout"}:
        raise ValueError("Sleep/workout mapping requires an end field")
    _zone(profile["timezone"])
    if profile["version"] == 2:
        _journal_mapping(profile)
    elif "journal" in profile:
        raise ValueError("Journal display bindings require mapping version 2")
    if profile.get("member") is not None and (
        not isinstance(profile["member"], str) or not profile["member"]
    ):
        raise ValueError("Mapping member must be an exact ZIP member name or null")
    metrics = profile["metrics"]
    if not isinstance(metrics, list):
        raise ValueError("Mapping metrics must be a list")
    seen_metrics: set[str] = set()
    for metric in metrics:
        metric = _object(metric, "Metric mapping")
        if set(metric) != {"column", "metric", "unit"} or any(
            not isinstance(value, str) or not value for value in metric.values()
        ):
            raise ValueError("Each metric mapping requires exactly column, metric and unit")
        name = metric["metric"]
        if name not in RESOURCE_METRICS[resource]:
            raise ValueError("Metric is unsupported for the explicitly mapped resource")
        if name in seen_metrics:
            raise ValueError("Mapping contains a duplicate metric")
        seen_metrics.add(name)
        # No inferred units or conversion. Explicit canonical units make the
        # first supported mapping auditable against the source's column labels.
        if metric["unit"] != METRIC_UNITS[name]:
            raise ValueError(f"Metric {name} requires explicit unit {METRIC_UNITS[name]}")
    return profile


def _journal_mapping(profile):
    """Version 2 adds explicit text/date projection, only for Journal exports."""
    if profile["resource"] != "journal" or profile["metrics"] or profile.get("end"):
        raise ValueError("Mapping version 2 is reserved for Journal without numeric metrics or end")
    spec = _object(profile.get("journal"), "Journal mapping")
    if set(spec) != {"date_basis", "time_precision", "subject_confirmation", "answers"}:
        raise ValueError(
            "Journal requires date basis, precision, local subject confirmation and answers"
        )
    if spec["date_basis"] not in ("reported_date", "cycle_start") or spec["time_precision"] not in (
        "date",
        "instant",
    ):
        raise ValueError("Select an explicit Journal date basis and precision")
    if spec["date_basis"] == "cycle_start" and spec["time_precision"] != "instant":
        raise ValueError("A cycle start requires an actual timestamp")
    if spec["subject_confirmation"] != "operator_confirmed_local_subject":
        raise ValueError("The local operator must confirm the export belongs to this local subject")
    if spec["time_precision"] == "date" and profile["timezone"] == "from_timestamp":
        raise ValueError("A date-only Journal requires an explicit timezone")
    fmt = profile["start"]["format"]
    if (
        spec["time_precision"] == "instant"
        and fmt != "iso8601"
        and not ("%M" in fmt and ("%H" in fmt or ("%I" in fmt and "%p" in fmt)))
    ):
        raise ValueError("Journal instant format must include an actual clock time")
    answers = spec["answers"]
    if not isinstance(answers, list) or not 1 <= len(answers) <= 128:
        raise ValueError("Map between 1 and 128 Journal answers")
    seen = set()
    for answer in answers:
        answer = _object(answer, "Journal answer")
        if set(answer) not in ({"question", "answer_column"}, {"question_column", "answer_column"}):
            raise ValueError("Map an answer column and either a question column or question label")
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 1000
            for value in answer.values()
        ):
            raise ValueError("Journal bindings must be nonempty bounded text")
        if (
            answer["answer_column"] in seen
            or answer["answer_column"] in profile["identity_columns"]
        ):
            raise ValueError(
                "Journal answer columns must be distinct and excluded from stable identity"
            )
        seen.add(answer["answer_column"])


def _day_boundary(day, zone):
    """First instant of a calendar date, including a missing/ambiguous local midnight."""
    midnight = datetime.combine(day, time.min)
    candidates = [midnight.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    valid = [
        point for point in candidates if point.astimezone(zone).replace(tzinfo=None) == midnight
    ]
    if valid:
        return min(valid)
    # A forward transition spans midnight. Locate the day boundary in UTC, without
    # requiring a fictitious midnight measurement or shifting the reported date.
    low, high = min(candidates), max(candidates)
    while (high - low).total_seconds() > 1:
        middle = low + timedelta(seconds=int((high - low).total_seconds() // 2))
        if middle.astimezone(zone).date() < day:
            low = middle
        else:
            high = middle
    return high


def _journal_record(row, profile, row_number):
    spec = profile["journal"]
    field, zone = profile["start"], _zone(profile["timezone"])
    raw = row[field["column"]].strip()
    source_at = None
    zone_label = profile["timezone"]
    if spec["time_precision"] == "date":
        try:
            if field["format"] == "iso8601":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                    raise ValueError
                day = date.fromisoformat(raw)
            else:
                # Date precision must not silently discard a clock time or offset.
                if set(re.findall(r"%[A-Za-z]", field["format"])) - {"%Y", "%m", "%d"}:
                    raise ValueError
                day = datetime.strptime(raw, field["format"]).date()
        except ValueError:
            raise ValueError(
                f"CSV row {row_number} does not match the declared Journal date"
            ) from None
    else:
        source_at, offset = _parse_time(raw, field, zone, row_number)
        fixed = _zone(offset)
        moment = datetime.fromisoformat(source_at)
        if (
            zone is None
            or moment.astimezone(zone).utcoffset() != moment.astimezone(fixed).utcoffset()
        ):
            zone, zone_label = fixed, offset
        day = moment.astimezone(zone).date()
    # Bounds are for a calendar-day comparison, never a fabricated measurement time.
    begin = _day_boundary(day, zone)
    finish = _day_boundary(day + timedelta(days=1), zone)
    if finish <= begin:
        raise ValueError(f"CSV row {row_number} date does not exist in the declared timezone")
    answers = []
    for binding in spec["answers"]:
        question = binding.get("question") or row[binding["question_column"]]
        answer = row[binding["answer_column"]]
        if not question.strip() or len(question) > 1000 or len(answer) > 8000:
            raise ValueError(
                f"CSV row {row_number} has an empty question or oversized Journal text"
            )
        answers.append({"question": question, "answer": answer if answer.strip() else None})
    if sum(len(item["question"]) + len(item["answer"] or "") for item in answers) > 64000:
        raise ValueError(f"CSV row {row_number} exceeds the Journal text budget")
    return {
        "version": 1,
        "date": day.isoformat(),
        "date_basis": spec["date_basis"],
        "time_precision": spec["time_precision"],
        "source_at": source_at,
        "timezone": zone_label,
        "day_start": timestamp(begin.isoformat()),
        "day_end": timestamp(finish.isoformat()),
        "subject_confirmation": spec["subject_confirmation"],
        "answers": answers,
    }


def _localize(parsed: datetime, zone: timezone | ZoneInfo | None) -> datetime:
    if parsed.tzinfo is not None:
        return parsed
    if zone is None:
        raise ValueError("Timestamp has no offset; mapping must provide an explicit timezone")
    if isinstance(zone, ZoneInfo):
        candidates = []
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=zone, fold=fold)
            roundtrip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
            if roundtrip == parsed:
                candidates.append(candidate)
        if not candidates:
            raise ValueError("Nonexistent local time; supply a timestamp with an explicit offset")
        if len({candidate.utcoffset() for candidate in candidates}) != 1:
            raise ValueError("Ambiguous local time; supply a timestamp with an explicit offset")
        return candidates[0]
    return parsed.replace(tzinfo=zone)


def _parse_time(
    value: str, field: dict[str, str], zone: timezone | ZoneInfo | None, row_number: int
) -> tuple[str, str]:
    value = value.strip()
    try:
        if field["format"] == "iso8601":
            if not re.search(r"[T ]\d{2}:\d{2}", value):
                raise ValueError("ISO timestamp must include a time of day")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = datetime.strptime(value, field["format"])
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"CSV row {row_number} has a timestamp that does not match its format"
        ) from exc
    parsed = _localize(parsed, zone)
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() % 60:
        raise ValueError("Timestamp offset must use whole minutes")
    minutes = int(offset.total_seconds() / 60)
    if abs(minutes) > 14 * 60:
        raise ValueError("Timestamp offset is outside the supported range")
    sign = "+" if minutes >= 0 else "-"
    offset_text = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    return timestamp(parsed.isoformat()), offset_text


def parse_export(
    path: Path | str, profile: dict[str, Any], exported_at: str, synthetic: bool = False
) -> list[SourceRecordInput]:
    """Parse a selected export atomically; the caller chooses an isolated store.

    exported_at is a caller-supplied, offset-aware export timestamp, never an
    upstream modification time. It supplies the available revision order. File
    paths/names and ZIP labels are excluded from record content, so moving or
    repackaging an identical export does not create another revision. Provider
    whoop_export deliberately cannot silently merge with an API connection.
    """
    if type(synthetic) is not bool:
        raise ValueError("synthetic must be a boolean")
    profile = _mapping(profile)
    try:
        if not isinstance(exported_at, str):
            raise ValueError("exported_at must be a string")
        exported_at = timestamp(exported_at)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("exported_at must include an explicit timezone offset") from exc
    path = Path(path)
    raw = _read_input(path)
    if path.suffix.lower() == ".zip":
        member = profile.get("member")
        if not member:
            raise ValueError("ZIP import requires an exact member in the mapping")
        with _archive(raw) as archive:
            if member not in archive.namelist() or not member.lower().endswith(".csv"):
                raise ValueError("Mapped ZIP member does not name an available CSV")
            raw = _member_bytes(archive, member)
    elif path.suffix.lower() == ".csv":
        if profile.get("member") is not None:
            raise ValueError("A single CSV requires a null or omitted mapping member")
    else:
        raise ValueError("Export must be a CSV or ZIP file")
    columns, rows = _csv_rows(raw)
    needed = set(profile["identity_columns"]) | {profile["start"]["column"]}
    if profile.get("end"):
        needed.add(profile["end"]["column"])
    needed.update(metric["column"] for metric in profile["metrics"])
    if profile.get("journal"):
        for binding in profile["journal"]["answers"]:
            needed.add(binding["answer_column"])
            if "question_column" in binding:
                needed.add(binding["question_column"])
    if not needed.issubset(columns):
        raise ValueError("CSV is missing one or more explicitly mapped columns")
    zone = _zone(profile["timezone"])
    resource = profile["resource"]
    # Persist the semantic mapping, not unstable file/member names. Sorting a
    # list of metric bindings makes mapping presentation order immaterial.
    semantic_mapping = {key: value for key, value in profile.items() if key != "member"}
    semantic_mapping["metrics"] = sorted(profile["metrics"], key=lambda item: item["metric"])
    semantic_mapping["identity_columns"] = sorted(profile["identity_columns"])
    semantic_mapping.setdefault("end", None)
    records: list[SourceRecordInput] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        journal = _journal_record(row, profile, row_number) if profile.get("journal") else None
        if journal:
            start, offset = journal["source_at"], journal["timezone"]
        else:
            start, offset = _parse_time(
                row[profile["start"]["column"]], profile["start"], zone, row_number
            )
        end = None
        if profile.get("end"):
            end, _ = _parse_time(row[profile["end"]["column"]], profile["end"], zone, row_number)
            if end <= start:
                raise ValueError(f"CSV row {row_number} end must follow its start")
        keys = [row[column].strip() for column in semantic_mapping["identity_columns"]]
        if not all(keys):
            raise ValueError(f"CSV row {row_number} has an empty identity column")
        external_id = hashlib.sha256(
            _canonical(
                [resource, list(zip(semantic_mapping["identity_columns"], keys, strict=True))]
            ).encode()
        ).hexdigest()
        if external_id in seen:
            raise ValueError(f"CSV row {row_number} duplicates a source identity")
        seen.add(external_id)
        observations = []
        for metric in semantic_mapping["metrics"]:
            raw_value = row[metric["column"]].strip()
            if not raw_value:
                continue
            try:
                value = float(raw_value)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"CSV row {row_number} mapped metric must be numeric") from exc
            if not isfinite(value):
                raise ValueError(f"CSV row {row_number} mapped metric must be finite")
            observations.append(
                ObservationInput(
                    metric=metric["metric"],
                    value=value,
                    unit=metric["unit"],
                    original_value=value,
                    original_unit=metric["unit"],
                    start_at=start,
                    end_at=end,
                    time_precision="interval" if end else "instant",
                    official=True,
                )
            )
        activities = ()
        if resource in {"physiological_cycles", "sleep", "workout"}:
            kind = "whoop_cycle" if resource == "physiological_cycles" else f"whoop_{resource}"
            activities = (ActivityInput(kind, external_id, start, end, offset),)
        records.append(
            SourceRecordInput(
                provider="whoop_export",
                resource=resource,
                external_id=external_id,
                source_updated_at=exported_at,
                payload={
                    "synthetic": synthetic,
                    "row": dict(row),
                    **({"journal": journal} if journal else {}),
                    "export": {
                        "resource": resource,
                        "format": "csv",
                        "mapping": semantic_mapping,
                    },
                },
                observations=tuple(observations),
                activities=activities,
                parser_version=f"whoop-export-mapped-{profile['version']}",
                metadata={
                    "synthetic": synthetic,
                    "source_format": "csv",
                    "resource": resource,
                    "version_time_basis": "exported_at_not_provider_modified",
                    "account_identity": "operator_confirmed_not_provider_verified"
                    if journal
                    else "not_supplied_by_mapping",
                },
            )
        )
    return records
