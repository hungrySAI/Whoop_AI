"""Clearly fabricated dashboard samples; never derive fixtures from the user's real database."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .api_ingestion import normalize_api
from .contracts import timestamp


def seed_demo(store):
    if store.environment != "synthetic":
        raise ValueError("Dashboard demo requires the synthetic environment")
    fixture = json.loads(
        (Path(__file__).with_name("resources") / "dashboard-synthetic.json").read_text()
    )
    assert fixture["synthetic"] is True
    source = fixture["resources"]
    resources = {name: [] for name in source}
    resources["profile"], resources["body"] = source["profile"], source["body"]
    anchor = datetime.fromisoformat(timestamp(store.clock())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    scores = [66, 72, 59, 81, 76, 68, 83, 74, 62, 85, 79, 71, 88, 82]
    for offset in range(30):
        if offset in (11, 23):
            continue
        day = anchor - timedelta(days=29 - offset)
        cycle_id = 800000 + day.toordinal()
        sleep_id = str(uuid5(NAMESPACE_URL, "synthetic-sleep:" + day.date().isoformat()))
        for resource in ("cycle", "recovery", "sleep", "workout"):
            if resource == "workout" and offset % 3 == 0:
                continue
            record = copy.deepcopy(source[resource][0])
            record["created_at"] = timestamp(day.isoformat())
            record["updated_at"] = timestamp((day + timedelta(hours=1)).isoformat())
            if resource == "recovery":
                record.update(cycle_id=cycle_id, sleep_id=sleep_id)
                record["score"].update(
                    recovery_score=scores[offset % len(scores)],
                    hrv_rmssd_milli=44 + scores[offset % len(scores)] / 4,
                    resting_heart_rate=56 + offset % 5,
                )
            else:
                record["start"] = timestamp(day.isoformat())
                record["end"] = timestamp(
                    (day + timedelta(hours=7 if resource == "sleep" else 1)).isoformat()
                )
                record["timezone_offset"] = "Z"
                if resource == "cycle":
                    record["id"] = cycle_id
                    record["end"] = (
                        timestamp((day + timedelta(days=1)).isoformat()) if offset < 29 else None
                    )
                    record["score"]["strain"] = round(7.2 + (offset % 8) * 0.9, 1)
                elif resource == "sleep":
                    record.update(id=sleep_id, cycle_id=cycle_id)
                    record["score"].update(
                        sleep_performance_percentage=79 + offset % 15,
                        sleep_efficiency_percentage=91.0,
                        sleep_consistency_percentage=82.0,
                        respiratory_rate=15.2,
                    )
                else:
                    record["id"] = str(
                        uuid5(NAMESPACE_URL, "synthetic-workout:" + day.date().isoformat())
                    )
                    record["score"]["strain"] = round(6.1 + (offset % 5) * 1.2, 1)
            if offset == 26 and resource in {"recovery", "sleep"}:
                record["score_state"] = "PENDING_SCORE"
                record.pop("score", None)
            resources[resource].append(record)
    return store.ingest(normalize_api(resources, acquired_at=store.clock(), synthetic=True))
