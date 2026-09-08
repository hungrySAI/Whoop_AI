"""Fabricated data and a loopback-only manual browser QA runner; never opens real data."""

import argparse
import copy
import csv
import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.contracts import timestamp
from whoop_copilot.cycles import CycleReviewService
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.exports import parse_export
from whoop_copilot.journal import JournalService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService
from whoop_copilot.web import DashboardRuntime, create_app
from whoop_copilot.weekly import WeeklyService

NOW = "2026-09-07T12:00:00Z"
SCENARIOS = (
    "short-history",
    "pending",
    "unscorable",
    "calibrating",
    "missing-metric",
    "empty",
    "sync-failure",
    "status-unavailable",
    "stale",
    "stale-failure",
    "authorization-required",
    "narrow-window",
    "window-unavailable",
    "journal",
    "journal-empty",
    "journal-legacy",
    "journal-pages",
    "journal-unavailable",
    "brief-open-cycle",
    "brief-history",
    "brief-mixed",
    "brief-small-change",
    "weekly-complete",
    "weekly-pages",
    "weekly-unavailable",
    "weekly-delayed",
    "cycles-pages",
    "cycles-mixed",
    "cycles-unavailable",
    "cycles-delayed",
    "catchup-long-gap",
    "catchup-limited",
    "catchup-failure",
)


def scenario_resources(scenario="short-history"):
    if scenario in ("cycles-pages", "cycles-unavailable", "cycles-delayed"):
        data = weekly_resources()
        for row in data["sleep"]:
            row["score"].update(
                sleep_efficiency_percentage=91.25,
                sleep_consistency_percentage=82.2,
                respiratory_rate=14.5,
            )
        return data
    if scenario == "cycles-mixed":
        data = scenario_resources("stale")
        data["cycle"][-1]["end"] = None
        data["sleep"][-1]["cycle_id"] += 100
        data["sleep"][-2]["nap"] = True
        data["sleep"][-3]["score"].pop("sleep_performance_percentage", None)
        data["recovery"] = [
            row for row in data["recovery"] if row["cycle_id"] != data["cycle"][1]["id"]
        ]
        data["sleep"] = [row for row in data["sleep"] if row["cycle_id"] != data["cycle"][2]["id"]]
        return data
    if scenario.startswith("weekly"):
        return weekly_resources(pages=scenario in ("weekly-pages", "weekly-unavailable"))
    fixture = json.loads((Path(__file__).parent / "fixtures/whoop_api_snapshot.json").read_text())
    assert fixture["synthetic"] is True
    source = fixture["resources"]
    result = {key: [] for key in source}
    result["profile"], result["body"] = source["profile"], source["body"]
    if scenario == "empty":
        return result
    anchor = datetime.fromisoformat(timestamp(NOW)).replace(hour=0)
    for offset in range(7):
        if offset == 3:
            continue
        day = anchor - timedelta(days=6 - offset)
        cycle_id = 810000 + offset
        sleep_id = str(uuid5(NAMESPACE_URL, f"browser-qa-synthetic-sleep-{offset}"))
        for resource in ("cycle", "recovery", "sleep", "workout"):
            if resource == "workout" and offset not in (1, 5):
                continue
            record = copy.deepcopy(source[resource][0])
            record.update(
                created_at=timestamp(day.isoformat()),
                updated_at=timestamp((day + timedelta(hours=8)).isoformat()),
            )
            if resource == "recovery":
                record.update(cycle_id=cycle_id, sleep_id=sleep_id)
                record["score"].update(
                    hrv_rmssd_milli=40 + offset * 2, recovery_score=60 + offset * 3
                )
            else:
                record.update(
                    start=timestamp(day.isoformat()),
                    end=timestamp((day + timedelta(hours=7)).isoformat()),
                    timezone_offset="Z",
                )
                if resource == "cycle":
                    record["id"] = cycle_id
                    record["score"]["strain"] = 7 + offset / 2
                elif resource == "sleep":
                    record.update(id=sleep_id, cycle_id=cycle_id)
                    if scenario != "missing-metric":
                        record["score"]["sleep_performance_percentage"] = 80 + offset
                else:
                    record["id"] = str(
                        uuid5(NAMESPACE_URL, f"browser-qa-synthetic-workout-{offset}")
                    )
            if resource in ("recovery", "sleep"):
                if scenario in ("pending", "unscorable") or (
                    scenario in ("short-history", "sync-failure") and offset == 6
                ):
                    record["score_state"] = (
                        "UNSCORABLE" if scenario == "unscorable" else "PENDING_SCORE"
                    )
                    record.pop("score", None)
                elif scenario == "calibrating" and resource == "recovery":
                    record["score"]["user_calibrating"] = True
            result[resource].append(record)
            if resource == "workout" and offset == 5:
                extra = copy.deepcopy(record)
                extra.update(
                    id=str(uuid5(NAMESPACE_URL, "browser-qa-synthetic-workout-extra")),
                    start=timestamp((day + timedelta(hours=9)).isoformat()),
                    end=timestamp((day + timedelta(hours=10)).isoformat()),
                )
                extra["score"]["strain"] = 11
                result[resource].append(extra)
    if scenario == "brief-open-cycle":
        result["cycle"][-1]["end"] = None
    if scenario == "brief-history":
        for key in ("cycle", "recovery", "sleep"):
            result[key] = result[key][:-1]
    if scenario == "brief-mixed":
        result["recovery"] = result["recovery"][:-2]
        result["sleep"] = result["sleep"][:-1]
        result["sleep"][-1]["nap"] = True
    if scenario == "brief-small-change":
        for record in result["recovery"]:
            record["score"]["recovery_score"] = 60 + (0.01 if record["cycle_id"] >= 810004 else 0)
    return result


def weekly_resources(*, pages=False):
    data = scenario_resources("stale")
    older = copy.deepcopy(data)
    for key in ("cycle", "recovery", "sleep", "workout"):
        for row in older[key]:
            for field in ("created_at", "updated_at", "start", "end"):
                if row.get(field):
                    row[field] = timestamp(
                        (datetime.fromisoformat(row[field]) - timedelta(days=7)).isoformat()
                    )
            if key == "cycle":
                row["id"] -= 1000
            if key in ("recovery", "sleep"):
                row["cycle_id"] -= 1000
            if key == "recovery":
                row["sleep_id"] = str(uuid5(NAMESPACE_URL, row["sleep_id"]))
                row["score"]["recovery_score"] -= 20
            if key in ("sleep", "workout"):
                row["id"] = str(uuid5(NAMESPACE_URL, row["id"]))
        data[key].extend(older[key])
    if pages:
        sample = copy.deepcopy(data["workout"][0])
        data["workout"] = []
        for index in range(35):
            row = copy.deepcopy(sample)
            row["id"] = str(uuid5(NAMESPACE_URL, f"weekly-workout-{index}"))
            row["score"]["strain"] = 2 + index % 10
            data["workout"].append(row)
    return data


class ScenarioClient:
    def __init__(self, scenario, *, fail=False, delay=0):
        self.resources, self.fail, self.delay = scenario_resources(scenario), fail, delay

    def list_records(self, resource, *_):
        if self.delay:
            time.sleep(self.delay)
        if self.fail and resource == "sleep":
            raise RuntimeError("fabricated-simulation-error")
        return {
            "records": copy.deepcopy(self.resources[resource]),
            "next_token": None,
            "headers": {},
        }


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="short-history")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    started = time.monotonic()

    def clock():
        return (
            datetime.fromisoformat(timestamp(NOW)) + timedelta(seconds=time.monotonic() - started)
        ).isoformat()

    with tempfile.TemporaryDirectory(prefix="whoop-synthetic-browser-") as directory:
        path = Path(directory) / "synthetic.sqlite3"

        def seed_clock():
            return (
                datetime.fromisoformat(clock())
                - timedelta(hours=1 if args.scenario in ("stale", "stale-failure") else 0)
                - timedelta(
                    days=400
                    if args.scenario == "catchup-limited"
                    else 45
                    if args.scenario.startswith("catchup-")
                    else 0
                )
            ).isoformat()

        with Store(path, clock=seed_clock) as store:
            SyncService(
                store,
                ScenarioClient(
                    "short-history"
                    if args.scenario.startswith("journal")
                    else "empty"
                    if args.scenario.startswith("catchup-")
                    else args.scenario
                ),
            ).run(
                *DashboardService(store).window(
                    7 if args.scenario in ("narrow-window", "window-unavailable") else 30
                )
            )
            if args.scenario.startswith("journal") and args.scenario != "journal-empty":
                mapping = json.loads(
                    (
                        Path(__file__).resolve().parents[1]
                        / "examples/whoop-journal-mapping.example.json"
                    ).read_text()
                )
                sample = Path(__file__).parent / "fixtures/journal_sample.csv"
                if args.scenario == "journal-legacy":
                    mapping["version"] = 1
                    del mapping["journal"]
                if args.scenario == "journal-pages":
                    with sample.open(newline="") as handle:
                        rows = list(csv.DictReader(handle))
                    sample = Path(directory) / "fabricated-journal.csv"
                    with sample.open("w", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                        writer.writeheader()
                        writer.writerows(
                            {**rows[0], "Synthetic journal key": f"fabricated-{index}"}
                            for index in range(25)
                        )
                store.ingest(parse_export(sample, mapping, "2026-09-07T10:00:00Z", synthetic=True))
            if args.scenario == "cycles-mixed":
                data = scenario_resources(args.scenario)
                data["cycle"][0]["updated_at"] = NOW
                store.ingest(
                    [
                        row
                        for row in normalize_api(data, acquired_at=clock(), synthetic=True)
                        if row.resource == "cycle"
                    ]
                )
        attempts = 0

        def client_factory():
            nonlocal attempts
            attempts += 1
            print(f"Synthetic sync attempt: {attempts}", flush=True)
            return ScenarioClient(
                args.scenario,
                fail=args.scenario
                in ("sync-failure", "stale-failure", "catchup-failure", "catchup-limited")
                and attempts == 1,
                delay=0.6,
            )

        runtime = DashboardRuntime(
            lambda: Store(path, clock=clock),
            client_factory=client_factory,
            oauth_status=(lambda: {"connected": False})
            if args.scenario == "authorization-required"
            else None,
        )
        if args.scenario == "window-unavailable":
            original_overview = DashboardService.overview

            def overview(self, key="hrv", days=7):
                if days == 30:
                    raise ValueError("Fabricated unavailable window")
                return original_overview(self, key, days)

            DashboardService.overview = overview
        if args.scenario == "journal-unavailable":
            original_timeline = JournalService.timeline

            def timeline(self, days=7, page=1):
                if days == 30:
                    raise ValueError("Fabricated unavailable Journal window")
                return original_timeline(self, days, page)

            JournalService.timeline = timeline
        if args.scenario == "status-unavailable":
            original_status, failures = runtime.status, 2

            def status():
                nonlocal failures
                if failures:
                    failures -= 1
                    raise RuntimeError("fabricated status read failure")
                return original_status()

            runtime.status = status
        if args.scenario in ("weekly-unavailable", "weekly-delayed"):
            original_review, original_records = WeeklyService.review, WeeklyService.records

            def review(self, week=None):
                if args.scenario == "weekly-delayed":
                    time.sleep(1.5)
                elif week == "2026-08-24":
                    raise ValueError("Fabricated unavailable week")
                return original_review(self, week)

            def records(self, week=None, key="recovery", page=1):
                if args.scenario == "weekly-unavailable" and page == 2:
                    raise ValueError("Fabricated unavailable record page")
                return original_records(self, week, key, page)

            WeeklyService.review, WeeklyService.records = review, records
        if args.scenario in ("cycles-unavailable", "cycles-delayed"):
            original_cycles, cycle_failures = CycleReviewService.review, 1

            def cycle_review(self, days=7, page=1):
                nonlocal cycle_failures
                if args.scenario == "cycles-delayed":
                    time.sleep(1.5)
                elif page == 2 or (days == 30 and cycle_failures):
                    cycle_failures = 0
                    raise ValueError("Fabricated unavailable cycle view")
                return original_cycles(self, days, page)

            CycleReviewService.review = cycle_review
        print(f"Synthetic browser QA: http://127.0.0.1:{args.port} · {args.scenario}", flush=True)
        uvicorn.run(
            create_app(runtime, args.port),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            proxy_headers=False,
            ws="none",
            log_level="warning",
        )


if __name__ == "__main__":
    main()
