"""Normalize the six public personal resources against the pinned WHOOP OpenAPI."""

import hashlib
import json
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .adapters import _number, _offset, parse_whoop_data
from .contracts import ActivityInput, ObservationInput, SourceRecordInput, timestamp
from .identity import UUID_RESOURCES, canonical_uuid
from .storage import canonical

SCHEMA_HASH = "087501087b4efe5ec28975b89b3b18448dd4eda7b0d524b74fdbb611194293aa"
SCHEMAS = {
    "cycle": "Cycle",
    "recovery": "Recovery",
    "sleep": "Sleep",
    "workout": "WorkoutV2",
    "profile": "UserBasicProfile",
    "body": "UserBodyMeasurement",
}
# Live WHOOP responses use null for these optional WorkoutScore fields, although
# the pinned OpenAPI describes them as omitted when unavailable. Preserve the raw
# response and apply this narrow compatibility rule only to the validation copy.
WORKOUT_NULL_OPTIONALS = frozenset(
    {"distance_meter", "altitude_gain_meter", "altitude_change_meter"}
)
SCORE_METRICS = {
    "strain": ("whoop.strain", "score"),
    "average_heart_rate": ("whoop.average_heart_rate", "bpm"),
    "max_heart_rate": ("whoop.max_heart_rate", "bpm"),
    "kilojoule": ("whoop.kilojoule", "kJ"),
    "sleep_performance_percentage": ("whoop.sleep_performance", "%"),
    "sleep_efficiency_percentage": ("whoop.sleep_efficiency", "%"),
    "sleep_consistency_percentage": ("whoop.sleep_consistency", "%"),
    "respiratory_rate": ("whoop.respiratory_rate", "breaths/min"),
}


@lru_cache
def openapi() -> dict:
    path = Path(__file__).with_name("resources") / "whoop-openapi.json"
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "resources/whoop-openapi.json"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCHEMA_HASH:
        raise ValueError("Pinned WHOOP contract changed; review and version it explicitly")
    return json.loads(raw)


def validate_resource(resource: str, record: dict) -> None:
    if resource not in SCHEMAS:
        raise ValueError("Unknown WHOOP resource")
    schema = openapi()
    fields = schema["components"]["schemas"][SCHEMAS[resource]]
    # WHOOP omits optional fields; tolerate optional top-level JSON null without fabricating values.
    value = {k: v for k, v in record.items() if v is not None or k in fields.get("required", [])}
    if resource == "workout" and isinstance(value.get("score"), dict):
        value["score"] = {
            key: item
            for key, item in value["score"].items()
            if item is not None or key not in WORKOUT_NULL_OPTIONALS
        }
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{SCHEMAS[resource]}", "components": schema["components"]},
        format_checker=FormatChecker(),
    )
    try:
        validator.validate(value)
        canonical(record)  # Includes finite-number verification for unused raw fields.
    except (ValidationError, ValueError):
        raise ValueError(
            f"WHOOP {resource} record does not match the pinned public contract"
        ) from None


def normalize_api(
    resources: dict[str, list[dict]],
    *,
    acquired_at: str,
    synthetic: bool,
    retrieval_times: dict[str, str] | None = None,
) -> list[SourceRecordInput]:
    acquired_at = timestamp(acquired_at)
    captured = {name: timestamp((retrieval_times or {}).get(name, acquired_at)) for name in SCHEMAS}
    if type(synthetic) is not bool or set(resources) != set(SCHEMAS):
        raise ValueError("A complete, explicitly identified six-resource snapshot is required")
    if len(resources["profile"]) != 1 or len(resources["body"]) != 1:
        raise ValueError("Expected one profile and one body measurement response")
    account = resources["profile"][0].get("user_id")
    for resource, records in resources.items():
        seen = set()
        for record in records:
            validate_resource(resource, record)
            if resource != "body" and record.get("user_id") != account:
                raise ValueError("WHOOP snapshot mixes external accounts")
            identity = (
                record.get("cycle_id") if resource == "recovery" else record.get("id", account)
            )
            if resource in UUID_RESOURCES:
                identity = canonical_uuid(identity)
            if identity in seen:
                raise ValueError(
                    "WHOOP snapshot contains duplicate resource identities; retry the window"
                )
            seen.add(identity)
    output = parse_whoop_data(
        {"cycles": resources["cycle"], "recoveries": resources["recovery"]}, synthetic=synthetic
    )
    output = [
        replace(
            record,
            metadata={
                **record.metadata,
                "captured_at": min(captured["cycle"], captured["recovery"]),
            },
        )
        for record in output
    ]
    for resource in ("cycle", "sleep", "workout", "profile", "body"):
        for record in resources[resource]:
            metadata = {
                "whoop_user_id": str(account),
                "openapi_sha256": SCHEMA_HASH,
                "captured_at": captured[resource],
            }
            observations, activities = [], []
            if resource in {"profile", "body"}:
                updated = captured[resource]
                metadata["version_time_basis"] = "retrieved_at_not_provider_modified"
                metadata["measurement_time"] = "not_supplied_by_provider"
                identity = str(account)
            else:
                identity = (
                    canonical_uuid(record["id"])
                    if resource in UUID_RESOURCES
                    else str(record["id"])
                )
                updated = timestamp(record["updated_at"])
                start = timestamp(record["start"])
                end = timestamp(record["end"]) if record.get("end") else None
                activities.append(
                    ActivityInput(
                        f"whoop_{resource}",
                        identity,
                        start,
                        end,
                        _offset(record["timezone_offset"]),
                    )
                )
                if record["score_state"] == "SCORED" and record.get("score"):
                    for field, (metric, unit) in SCORE_METRICS.items():
                        if record["score"].get(field) is not None:
                            value = _number(record["score"][field], field)
                            observations.append(
                                ObservationInput(
                                    metric,
                                    value,
                                    unit,
                                    value,
                                    unit,
                                    start,
                                    end,
                                    "interval",
                                    official=True,
                                )
                            )
            output.append(
                SourceRecordInput(
                    "whoop",
                    resource,
                    identity,
                    updated,
                    {"synthetic": synthetic, "record": record},
                    tuple(observations),
                    tuple(activities),
                    "whoop-api-v2-1",
                    metadata=metadata,
                )
            )
    return output
