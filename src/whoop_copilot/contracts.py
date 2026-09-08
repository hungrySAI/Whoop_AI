"""Small provider-neutral ingestion contracts. All timestamps must carry an offset."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any


def timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class ObservationInput:
    metric: str
    value: float
    unit: str
    original_value: float
    original_unit: str
    start_at: str
    end_at: str | None = None
    time_precision: str = "instant"
    quality: str = "valid"
    official: bool = False

    def __post_init__(self) -> None:
        if not isfinite(self.value) or not isfinite(self.original_value):
            raise ValueError("Observation values must be finite")
        timestamp(self.start_at)
        if self.end_at and timestamp(self.end_at) <= timestamp(self.start_at):
            raise ValueError("Observation interval must end after it starts")


@dataclass(frozen=True)
class ActivityInput:
    kind: str
    external_id: str
    start_at: str
    end_at: str | None
    timezone_offset: str


@dataclass(frozen=True)
class SourceRecordInput:
    provider: str
    resource: str
    external_id: str
    source_updated_at: str
    payload: dict[str, Any]
    observations: tuple[ObservationInput, ...] = ()
    activities: tuple[ActivityInput, ...] = ()
    parser_version: str = "1"
    deleted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        timestamp(self.source_updated_at)
        if self.deleted and (self.observations or self.activities):
            raise ValueError("Deleted revisions cannot contain normalized values")
