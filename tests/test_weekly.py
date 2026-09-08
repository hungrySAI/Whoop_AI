"""Calendar reviews use fabricated records, registered analyses and the local boundary."""

from contextlib import contextmanager
from datetime import datetime, timedelta

import httpx
import pytest
from dashboard_scenarios import NOW, scenario_resources, weekly_resources

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DASHBOARD_METRICS
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.web import DashboardRuntime, create_app
from whoop_copilot.weekly import KEYS, WeeklyService


def ingest(store, data):
    store.ingest(
        normalize_api(data, acquired_at=store.clock(), synthetic=store.environment == "synthetic")
    )


@contextmanager
def seeded(tmp_path, data=None, clock=lambda: NOW, **options):
    with Store(tmp_path / "weekly.sqlite3", clock=clock, **options) as store:
        ingest(store, weekly_resources() if data is None else data)
        yield store


@pytest.mark.parametrize("key", KEYS)
def test_complete_week_exactly_reuses_two_week_registered_halves(tmp_path, key):
    with seeded(tmp_path) as store:
        view = WeeklyService(store).review()
        assert view["week"] == "2026-08-31"
        assert view["partial"] is False
        metric = next(row for row in view["metrics"] if row["key"] == key)
        definition = DASHBOARD_METRICS[key]
        analysis = CopilotService(store).analyze(
            definition.metric,
            view["previous_start"],
            view["end"],
            provider="whoop",
            resource=definition.resource,
        )
        assert analysis["result"]["midpoint"] == view["start"]
        assert metric["selected"]["mean"] == analysis["result"]["second_half"]["mean"]
        assert metric["previous"]["mean"] == analysis["result"]["first_half"]["mean"]
        assert metric["comparison"]["analysis_id"] is None
        assert metric["comparison"]["change"] == analysis["result"]["change"]
        assert CopilotService(store).reproduce(analysis["run_id"])["matches"]
        records = WeeklyService(store).records(view["week"], key)
        assert records["metric"] == metric
        assert records["metrics"] == view["metrics"]
        for record in records["records"]:
            assert view["previous_start"] <= record["measured_at"] < view["end"]
            assert (record["period"] == "selected") == (record["measured_at"] >= view["start"])


def test_short_history_reports_samples_without_inventing_previous_week(tmp_path):
    with seeded(tmp_path, scenario_resources("short-history")) as store:
        view = WeeklyService(store).review()
        for row in view["metrics"]:
            assert row["previous"]["mean"] is None
            assert row["previous"]["coverage"]["record_days"] == 0
            assert row["comparison"]["change"] is None
            assert row["comparison"]["state"] == "insufficient_data"
        assert view["metrics"][0]["selected"]["coverage"]["record_days"] == 5


@pytest.mark.parametrize(
    "week",
    ["", "2026-09-01", "2026-09-14", "2026-06-08", "2026-W37-1", "20260907", "x" * 100, True, 3],
)
def test_only_twelve_exact_monday_dates_are_accepted(tmp_path, week):
    with seeded(tmp_path) as store:
        with pytest.raises(ValueError):
            WeeklyService(store).review(week)


@pytest.mark.parametrize(
    ("now", "monday"),
    [("2027-01-01T12:00:00Z", "2026-12-28"), ("2026-03-08T22:00:00Z", "2026-03-02")],
)
def test_week_boundaries_are_utc_across_year_and_dst(tmp_path, now, monday):
    with Store(tmp_path / "empty.sqlite3", clock=lambda: now) as store:
        view = WeeklyService(store).review(monday)
        assert view["week"] == monday
        assert view["partial"]
        assert len(view["choices"]) == 12
        assert datetime.fromisoformat(view["calendar_end"]) - datetime.fromisoformat(
            view["start"]
        ) == timedelta(days=7)
        assert view["timezone"] == "UTC"


@pytest.mark.parametrize("now", [NOW, "2026-09-07T00:00:00Z"])
def test_current_week_is_progress_including_zero_length_monday(tmp_path, now):
    with Store(tmp_path / "empty.sqlite3", clock=lambda: now) as store:
        view = WeeklyService(store).review("2026-09-07")
        assert view["partial"]
        for row in view["metrics"]:
            assert row["comparison"]["state"] == "week_in_progress"
            assert row["comparison"]["change"] is None
            assert row["selected"]["mean"] is None
            assert row["selected"]["coverage"]["record_count"] == 0
            if now.endswith("00:00:00Z"):
                assert row["selected"]["analysis_id"] is None


def test_current_week_mean_is_clipped_not_compared_to_full_previous_week(tmp_path):
    data = weekly_resources()
    data["cycle"][5]["end"] = None
    with seeded(tmp_path, data) as store:
        view = WeeklyService(store).review("2026-09-07")
        for row in view["metrics"]:
            assert row["comparison"]["analysis_id"] is None
            assert row["comparison"]["change"] is None
        assert view["metrics"][2]["selected"]["mean"] == 10
        assert view["metrics"][2]["selected"]["open_cycles"] == 1


def test_non_null_one_per_week_difference_is_suppressed(tmp_path):
    data = weekly_resources()
    data["recovery"] = [data["recovery"][0], data["recovery"][6]]
    with seeded(tmp_path, data) as store:
        view = WeeklyService(store).review()
        row = view["metrics"][0]
        assert row["selected"]["coverage"]["valid_observation_count"] == 1
        assert row["previous"]["coverage"]["valid_observation_count"] == 1
        analysis = CopilotService(store).analyze(
            "whoop.recovery_score",
            view["previous_start"],
            view["end"],
            provider="whoop",
            resource="recovery",
        )
        assert analysis["result"]["change"] is not None
        assert row["comparison"]["change"] is None


@pytest.mark.parametrize(
    "scenario", ["pending", "unscorable", "calibrating", "missing-metric", "empty"]
)
def test_unscored_or_empty_records_keep_their_states(tmp_path, scenario):
    with seeded(tmp_path, scenario_resources(scenario)) as store:
        view = WeeklyService(store).review()
        row = view["metrics"][1 if scenario == "missing-metric" else 0]
        assert row["selected"]["mean"] is None
        assert row["selected"]["coverage"]["valid_observation_count"] == 0
        assert row["comparison"]["change"] is None
        assert (row["selected"]["coverage"]["record_count"] == 0) is (scenario == "empty")


def test_source_pagination_keeps_same_day_workouts_individual(tmp_path):
    data = weekly_resources(pages=True)
    with seeded(tmp_path, data) as store:
        service = WeeklyService(store)
        first = service.records(key="workout")
        second = service.records(first["week"], "workout", 2)
        assert first["total"] == 35 and len(first["records"]) == 30
        assert len(second["records"]) == 5 and second["page"] == second["pages"] == 2
        assert first["metric"]["selected"]["coverage"]["record_days"] == 1
        assert first["metric"]["selected"]["coverage"]["record_count"] == 35
        assert not {row["revision_id"] for row in first["records"]} & {
            row["revision_id"] for row in second["records"]
        }
        store.forget_source("whoop")
        assert service.records(first["week"], "workout", 2)["page"] == 1


def test_detail_rollover_updates_entire_report_and_keeps_selected_date(tmp_path):
    clock = ["2026-09-06T23:59:59Z"]
    with Store(tmp_path / "rollover.sqlite3", clock=lambda: clock[0]) as store:
        service = WeeklyService(store)
        before = service.review("2026-08-31")
        assert before["partial"]
        clock[0] = NOW
        after = service.records(before["week"])
        assert after["week"] == before["week"] and not after["partial"]
        assert after["choices"][0]["week"] == "2026-09-07"
        assert all(row["comparison"]["state"] != "week_in_progress" for row in after["metrics"])


def test_whole_report_retries_when_source_changes_during_read(tmp_path, monkeypatch):
    with seeded(tmp_path) as store:
        service = WeeklyService(store)
        original, changed = service._metric, []

        def alter(*args):
            result = original(*args)
            if not changed:
                store.forget_source("whoop")
                changed.append(True)
            return result

        monkeypatch.setattr(service, "_metric", alter)
        view = service.review()
        assert all(row["selected"]["mean"] is None for row in view["metrics"])


def test_detail_refresh_replaces_all_aggregates_after_forget_and_expiry(tmp_path):
    clock = [NOW]
    with seeded(
        tmp_path,
        clock=lambda: clock[0],
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(1, owner_authorized=True),
    ) as store:
        service = WeeklyService(store)
        before = service.review()
        assert before["metrics"][0]["selected"]["mean"] is not None
        clock[0] = "2026-09-08T13:00:00Z"
        after = service.records(before["week"])
        assert all(row["selected"]["mean"] is None for row in after["metrics"])
        assert after["records"] == []
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "query",
    [
        "week=2026-09-01",
        "week=2026-08-31&week=2026-08-24",
        "provider=whoop_export",
        "page=0",
        "metric=bad",
        "page=335",
    ],
)
@pytest.mark.asyncio
async def test_http_query_boundary(tmp_path, query):
    with seeded(tmp_path) as store:
        path = store.path
    runtime = DashboardRuntime(lambda: Store(path, clock=lambda: NOW))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime), client=("127.0.0.1", 1234)),
        base_url="http://127.0.0.1:8766",
    ) as client:
        assert (await client.get("/api/weekly")).status_code == 401
        await client.get("/")
        response = await client.get("/api/weekly/records?" + query)
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
        good = await client.get("/api/weekly/records")
        assert good.status_code == 200
        for forbidden in ('"payload"', '"user_id"', '"external_id"', '"email"', '"answers"'):
            assert forbidden not in good.text
        assert (
            await client.get("/api/weekly", headers={"Origin": "https://example.invalid"})
        ).status_code == 403
