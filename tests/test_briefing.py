"""Daily summaries over fabricated API records, never over a personal database."""

import json
from contextlib import contextmanager

import httpx
import pytest
from dashboard_scenarios import NOW, scenario_resources

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DASHBOARD_METRICS, DashboardService
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.web import DashboardRuntime, create_app


def ingest(store, data):
    return store.ingest(
        normalize_api(data, acquired_at=store.clock(), synthetic=store.environment == "synthetic")
    )


@contextmanager
def seeded(tmp_path, data=None, **options):
    with Store(tmp_path / "brief.sqlite3", clock=lambda: NOW, **options) as store:
        ingest(store, scenario_resources("stale") if data is None else data)
        yield store


@pytest.mark.parametrize("key", ["recovery", "sleep", "strain"])
@pytest.mark.parametrize("days", [7, 30])
def test_comparison_reuses_reproducible_registered_result(tmp_path, key, days):
    with seeded(tmp_path) as store:
        view = DashboardService(store).overview(key, days)
        brief = next(item for item in view["briefing"]["items"] if item["key"] == key)
        summary, comparison = view["trend"]["summary"], brief["comparison"]
        assert comparison["analysis_id"] == view["trend"]["analysis_id"]
        assert comparison["first_half"] == summary["first_half"]
        assert comparison["second_half"] == summary["second_half"]
        assert comparison["midpoint"] == summary["midpoint"]
        assert comparison["analysis_id"] is None
        definition = DASHBOARD_METRICS[key]
        analysis = CopilotService(store).analyze(
            definition.metric,
            view["start"],
            view["end"],
            provider="whoop",
            resource=definition.resource,
        )
        assert analysis["result"] == summary
        assert CopilotService(store).reproduce(analysis["run_id"])["matches"]
        if days == 7:
            assert comparison["change"] == summary["change"]
            assert comparison["direction"] == "higher"
        else:
            # Short history is useful now; no invented observations in the earlier half.
            assert comparison["first_half"]["count"] == 0
            assert comparison["change"] is None
            assert comparison["direction"] == "unavailable"
        assert comparison["unit_label"] == ("分" if key == "strain" else "个百分点")


@pytest.mark.parametrize(
    ("scenario", "index", "expected"),
    [
        ("short-history", 0, "PENDING_SCORE"),
        ("pending", 1, "PENDING_SCORE"),
        ("unscorable", 0, "UNSCORABLE"),
        ("calibrating", 0, "calibrating"),
        ("missing-metric", 1, "missing_metric"),
    ],
)
def test_latest_unscored_stays_unscored_while_past_comparison_is_separate(
    tmp_path, scenario, index, expected
):
    with seeded(tmp_path, scenario_resources(scenario)) as store:
        view = DashboardService(store).overview()
        card, item = view["cards"][index], view["briefing"]["items"][index]
        assert card["latest"]["status"] == expected
        assert card["latest"]["value"] is None
        assert item["latest_revision_id"] == card["latest"]["revision_id"]
        assert item["recency"] == "today"
        if scenario == "short-history":
            assert item["comparison"]["status"] == "descriptive"
        else:
            assert item["comparison"]["change"] is None


def test_different_record_dates_are_not_combined_into_today(tmp_path):
    data = scenario_resources("stale")
    data["sleep"] = data["sleep"][:-1]
    data["recovery"] = data["recovery"][:-2]
    with seeded(tmp_path, data) as store:
        view = DashboardService(store).overview()
        brief = view["briefing"]
        assert brief["state"] == "partial_today"
        assert [item["recency"] for item in brief["items"]] == ["earlier", "earlier", "today"]
        assert len({card["latest"]["measured_at"] for card in view["cards"]}) == 3
        assert brief["timezone"] == "UTC"
        assert brief["as_of"] == view["end"]


@pytest.mark.parametrize("empty", [False, True])
def test_no_today_is_not_a_wearing_or_sync_diagnosis(tmp_path, empty):
    data = scenario_resources("empty" if empty else "stale")
    if not empty:
        for key in ("cycle", "recovery", "sleep"):
            data[key] = data[key][:-1]
    with seeded(tmp_path, data) as store:
        brief = DashboardService(store).overview()["briefing"]
        assert brief["state"] == ("empty" if empty else "historical")
        assert all(item["recency"] != "today" for item in brief["items"])
        assert all(item["latest_revision_id"] is None for item in brief["items"]) is empty
        assert "今天已有" not in brief["headline"]
        assert "佩戴" not in json.dumps(brief, ensure_ascii=False)


@pytest.mark.parametrize("end", [None, "2026-09-07T20:00:00Z", "2026-09-07T07:00:00Z"])
def test_unfinished_cycle_is_not_final_daily_load(tmp_path, end):
    data = scenario_resources("stale")
    data["cycle"][-1]["end"] = end
    with seeded(tmp_path, data) as store:
        view = DashboardService(store).overview()
        item = view["briefing"]["items"][2]
        assert item["interval_open"] is (end != "2026-09-07T07:00:00Z")
        assert ("尚未结束" in item["context"]) is item["interval_open"]
        assert view["cards"][2]["latest"]["value"] == 10


def test_latest_sleep_may_be_a_nap_not_last_nights_sleep(tmp_path):
    data = scenario_resources("stale")
    data["sleep"][-1]["nap"] = True
    with seeded(tmp_path, data) as store:
        item = DashboardService(store).overview()["briefing"]["items"][1]
        assert "小睡" in item["context"]
        assert "昨夜" not in item["context"]


def test_non_null_difference_with_one_per_half_is_not_reported_as_change(tmp_path):
    data = scenario_resources("stale")
    for key in ("cycle", "recovery", "sleep"):
        data[key] = [data[key][0], data[key][-1]]
    with seeded(tmp_path, data) as store:
        view = DashboardService(store).overview("recovery")
        assert view["trend"]["summary"]["change"] is not None
        item = view["briefing"]["items"][0]
        assert item["comparison"]["status"] == "insufficient_data"
        assert item["comparison"]["change"] is None
        assert item["comparison"]["direction"] == "unavailable"


@pytest.mark.parametrize("change", [0, -5, 0.01])
def test_neutral_change_language_and_small_differences(tmp_path, change):
    data = scenario_resources("stale")
    for record in data["recovery"]:
        record["score"]["recovery_score"] = 60 + (change if record["cycle_id"] >= 810004 else 0)
    with seeded(tmp_path, data) as store:
        comparison = DashboardService(store).overview()["briefing"]["items"][0]["comparison"]
        assert comparison["direction"] == (
            "unchanged" if change == 0 else "lower" if change < 0 else "higher"
        )
        assert "改善" not in comparison["statement"] and "恶化" not in comparison["statement"]
        if 0 < change < 0.1:
            assert "不足 0.1" in comparison["statement"]


def test_snapshot_retry_keeps_brief_and_cards_current(tmp_path, monkeypatch):
    with seeded(tmp_path) as store:
        service = DashboardService(store)
        original = service._metric_view
        calls = []

        def changed_during_read(*args):
            view = original(*args)
            if not calls:
                data = scenario_resources("stale")
                data["recovery"][-1]["updated_at"] = "2026-09-07T11:00:00Z"
                data["recovery"][-1]["score"]["recovery_score"] = 33
                ingest(store, data)
            calls.append(args[0])
            return view

        monkeypatch.setattr(service, "_metric_view", changed_during_read)
        view = service.overview()
        assert view["cards"][0]["latest"]["value"] == 33
        for card, item in zip(view["cards"], view["briefing"]["items"], strict=True):
            assert item["latest_revision_id"] == card["latest"]["revision_id"]
            assert item["comparison"]["analysis_id"] == card["analysis_id"]
        assert len(calls) > 4


def test_expiry_at_end_of_read_cannot_leave_stale_brief(tmp_path, monkeypatch):
    with seeded(
        tmp_path,
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(90, owner_authorized=True),
    ) as store:
        service = DashboardService(store)
        original = service._metric_view
        changed = []

        def expire_last_metric(*args):
            view = original(*args)
            if args[0] == "hrv" and not changed:
                store.db.execute(
                    "UPDATE source_revisions SET expires_at=? WHERE resource='recovery'",
                    (view["records"][0]["known_at"],),
                )
                changed.append(True)
            return view

        monkeypatch.setattr(service, "_metric_view", expire_last_metric)
        view = service.overview()
        assert view["cards"][0]["latest"] is None
        assert view["briefing"]["items"][0]["latest_revision_id"] is None
        assert view["briefing"]["items"][0]["comparison"]["change"] is None


@pytest.mark.asyncio
async def test_brief_uses_existing_http_session_and_curated_projection(tmp_path):
    with seeded(tmp_path) as store:
        path = store.path
    runtime = DashboardRuntime(lambda: Store(path, clock=lambda: NOW))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime), client=("127.0.0.1", 1234)),
        base_url="http://127.0.0.1:8766",
    ) as client:
        assert (await client.get("/api/dashboard")).status_code == 401
        await client.get("/")
        response = await client.get("/api/dashboard")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["briefing"]["items"]) == 3
        for forbidden in ('"payload"', '"user_id"', '"email"', '"answers"'):
            assert forbidden not in response.text
