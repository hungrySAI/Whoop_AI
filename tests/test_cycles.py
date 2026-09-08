"""Official identity joins, source lifecycle and bounded local recovery projections."""

import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from dashboard_scenarios import NOW, scenario_resources, weekly_resources

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.contracts import timestamp
from whoop_copilot.cycles import CycleReviewService
from whoop_copilot.dashboard import DASHBOARD_METRICS, DashboardService
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.storage import Store
from whoop_copilot.web import DashboardRuntime, create_app


def ingest(store, data):
    records = normalize_api(
        data, acquired_at=store.clock(), synthetic=store.environment == "synthetic"
    )
    store.ingest(records)
    return records


@contextmanager
def seeded(tmp_path, data=None, **options):
    options.setdefault("clock", lambda: NOW)
    with Store(tmp_path / "cycles.sqlite3", **options) as store:
        ingest(store, data or scenario_resources("stale"))
        yield store


def entry_for(view, start):
    return next(entry for entry in view["entries"] if entry["start"] == timestamp(start))


@pytest.mark.parametrize("days", [7, 30])
def test_exact_join_uses_existing_official_observations_and_source_details(tmp_path, days):
    with seeded(tmp_path) as store:
        view = CycleReviewService(store).review(days)
        assert view["total"] == 6 and view["pages"] == 1
        assert view["basis"] == "current_retained_records"
        for entry in view["entries"]:
            assert all(entry[k]["state"] == "linked" for k in ("cycle", "recovery", "sleep"))
            metrics = [m for key in ("cycle", "recovery", "sleep") for m in entry[key]["metrics"]]
            assert len(metrics) == 8
            for metric in metrics:
                evidence = DashboardService(store).evidence(metric["key"], metric["revision_id"])
                assert evidence["value"] == metric["value"]
                assert evidence["status"] == metric["status"]
                assert evidence["measured_at"] == metric["measured_at"]
                assert evidence["is_current"]
        assert len(DASHBOARD_METRICS) == 6
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_later_nap_does_not_replace_sleep_id_and_off_window_sleep_is_linked(tmp_path):
    data = scenario_resources("stale")
    sleep = data["sleep"][0]
    sleep["start"], sleep["end"] = "2026-08-31T18:00:00Z", "2026-09-01T01:00:00Z"
    nap = copy.deepcopy(sleep)
    nap.update(
        id=str(uuid5(NAMESPACE_URL, "later-nap")),
        nap=True,
        start="2026-09-01T12:00:00Z",
        end="2026-09-01T13:00:00Z",
    )
    nap["score"]["sleep_performance_percentage"] = 99
    data["sleep"].append(nap)
    with seeded(tmp_path, data) as store:
        entry = entry_for(CycleReviewService(store).review(), "2026-09-01T00:00:00Z")
        assert entry["sleep"]["nap"] is False
        assert entry["sleep"]["start"] == timestamp(sleep["start"])
        assert entry["sleep"]["metrics"][0]["value"] == 80


@pytest.mark.parametrize(
    ("key", "field", "value"),
    [
        ("sleep_efficiency", "sleep_efficiency_percentage", 91.25),
        ("sleep_consistency", "sleep_consistency_percentage", 82.2),
        ("respiratory_rate", "respiratory_rate", 14.5),
    ],
)
def test_additional_sleep_values_are_exact_official_normalized_observations(
    tmp_path, key, field, value
):
    data = scenario_resources("stale")
    data["sleep"][0]["score"][field] = value
    with seeded(tmp_path, data) as store:
        entry = entry_for(CycleReviewService(store).review(), "2026-09-01T00:00:00Z")
        point = next(m for m in entry["sleep"]["metrics"] if m["key"] == key)
        assert point["value"] == value and point["status"] == "valid"
        evidence = DashboardService(store).evidence(key, point["revision_id"])
        assert evidence["original_value"] == evidence["value"] == value


@pytest.mark.parametrize("kind", ["sleep", "recovery", "wrong_sleep_cycle", "nap"])
def test_missing_or_inconsistent_links_never_fall_back_to_nearby_records(tmp_path, kind):
    data = scenario_resources("stale")
    if kind in ("sleep", "recovery"):
        data[kind] = data[kind][1:]
    elif kind == "wrong_sleep_cycle":
        data["sleep"][0]["cycle_id"] += 1
    else:
        data["sleep"][0]["nap"] = True
    with seeded(tmp_path, data) as store:
        entry = entry_for(CycleReviewService(store).review(), "2026-09-01T00:00:00Z")
        if kind == "nap":
            assert entry["sleep"]["state"] == "linked" and entry["sleep"]["nap"] is True
        else:
            assert entry["sleep"]["metrics"] == []
            assert (
                entry["sleep"]["state"]
                == {
                    "sleep": "unavailable",
                    "recovery": "recovery_unavailable",
                    "wrong_sleep_cycle": "inconsistent",
                }[kind]
            )
        assert entry["cycle"]["metrics"][0]["value"] == 7


def test_account_in_payload_must_match_even_when_local_identity_matches(tmp_path):
    with seeded(tmp_path) as store:
        data = scenario_resources("stale")
        inputs = normalize_api(data, acquired_at=NOW, synthetic=True)
        sleep = next(row for row in inputs if row.resource == "sleep")
        payload = copy.deepcopy(sleep.payload)
        payload["record"]["user_id"] += 1
        payload["record"]["updated_at"] = "2026-09-07T10:00:00Z"
        store.ingest(
            [replace(sleep, payload=payload, source_updated_at=payload["record"]["updated_at"])]
        )
        entry = entry_for(CycleReviewService(store).review(), "2026-09-01T00:00:00Z")
        assert entry["sleep"]["state"] == "inconsistent"
        assert entry["sleep"]["metrics"] == []


def test_cycle_revision_mismatch_is_explicit_and_corrected_pair_recovers(tmp_path):
    data = scenario_resources("stale")
    with seeded(tmp_path, data) as store:
        service = CycleReviewService(store)
        data["cycle"][0].update(updated_at="2026-09-07T10:00:00Z", end="2026-09-01T09:00:00Z")
        changed = normalize_api(data, acquired_at=NOW, synthetic=True)
        store.ingest([row for row in changed if row.resource == "cycle"])
        entry = entry_for(service.review(), "2026-09-01T00:00:00Z")
        assert entry["recovery"]["state"] == "version_mismatch"
        assert entry["recovery"]["metrics"] == entry["sleep"]["metrics"] == []
        ingest(store, data)
        entry = entry_for(service.review(), "2026-09-01T00:00:00Z")
        assert entry["recovery"]["state"] == entry["sleep"]["state"] == "linked"
        assert entry["recovery"]["metrics"][0]["measured_end"] == entry["end"]


def test_new_sleep_revision_and_tombstone_replace_link_without_resurrection(tmp_path):
    data = scenario_resources("stale")
    with seeded(tmp_path, data) as store:
        service = CycleReviewService(store)
        before = entry_for(service.review(), "2026-09-01T00:00:00Z")
        old_id = before["sleep"]["metrics"][0]["revision_id"]
        data["sleep"][0]["updated_at"] = "2026-09-07T10:00:00Z"
        data["sleep"][0]["score"]["sleep_performance_percentage"] = 95
        inputs = ingest(store, data)
        after = entry_for(service.review(), "2026-09-01T00:00:00Z")
        assert after["sleep"]["metrics"][0]["value"] == 95
        assert after["sleep"]["metrics"][0]["revision_id"] != old_id
        row = next(row for row in inputs if row.resource == "sleep")
        store.ingest(
            [
                replace(
                    row,
                    deleted=True,
                    source_updated_at="2026-09-07T11:00:00Z",
                    observations=(),
                    activities=(),
                    payload={"synthetic": True, "deleted": True},
                )
            ]
        )
        ingest(store, scenario_resources("stale"))
        latest = entry_for(service.review(), "2026-09-01T00:00:00Z")
        assert latest["sleep"]["state"] == "unavailable"
        assert latest["sleep"]["metrics"] == []


@pytest.mark.parametrize("scenario", ["pending", "unscorable", "calibrating", "missing-metric"])
def test_official_score_states_are_preserved_per_metric(tmp_path, scenario):
    with seeded(tmp_path, scenario_resources(scenario)) as store:
        entry = CycleReviewService(store).review()["entries"][0]
        key = "sleep" if scenario == "missing-metric" else "recovery"
        metric = entry[key]["metrics"][0]
        assert metric["value"] is None
        assert (
            metric["status"]
            == {
                "pending": "PENDING_SCORE",
                "unscorable": "UNSCORABLE",
                "calibrating": "calibrating",
                "missing-metric": "missing_metric",
            }[scenario]
        )
        assert entry[key]["state"] == "linked"


def test_open_cycles_empty_views_pagination_and_explicit_window(tmp_path):
    data = weekly_resources()
    data["cycle"][5]["end"] = None
    with seeded(tmp_path, data) as store:
        service = CycleReviewService(store)
        narrow, wide = service.review(), service.review(30)
        assert narrow["total"] == 6 and wide["total"] == 12
        assert wide["entries"][0]["open"]
        second = service.review(30, 2)
        assert len(wide["entries"]) == 10 and len(second["entries"]) == 2
        assert not {x["revision_id"] for x in wide["entries"]} & {
            x["revision_id"] for x in second["entries"]
        }
        assert service.review(30, 1000)["page"] == 2
        store.forget_source("whoop")
        empty = service.review(30, 2)
        assert empty["entries"] == [] and empty["page"] == empty["pages"] == 1


@pytest.mark.parametrize(
    ("days", "page"), [(0, 1), (8, 1), (True, 1), ("7", 1), (7, 0), (7, 1001), (7, True), (7, "1")]
)
def test_query_bounds(tmp_path, days, page):
    with seeded(tmp_path) as store:
        with pytest.raises(ValueError):
            CycleReviewService(store).review(days, page)


def test_source_change_during_composition_retries_whole_page(tmp_path, monkeypatch):
    with seeded(tmp_path) as store:
        service = CycleReviewService(store)
        original, calls = service._entry, []

        def compose(*args):
            entry = original(*args)
            if not calls:
                store.forget_source("whoop")
            calls.append(True)
            return entry

        monkeypatch.setattr(service, "_entry", compose)
        assert service.review()["entries"] == []
        assert calls


def test_expired_link_is_removed_even_when_cycle_is_still_retained(tmp_path, monkeypatch):
    with seeded(
        tmp_path,
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(90, owner_authorized=True),
    ) as store:
        service = CycleReviewService(store)
        original, calls = service._entry, []

        def compose(*args):
            entry = original(*args)
            if not calls:
                store.db.execute(
                    "UPDATE source_revisions SET expires_at=? WHERE resource='sleep'",
                    (timestamp(NOW),),
                )
            calls.append(True)
            return entry

        monkeypatch.setattr(service, "_entry", compose)
        view = service.review()
        assert view["total"] == 6
        assert all(entry["sleep"]["state"] == "unavailable" for entry in view["entries"])
        assert all(entry["recovery"]["state"] == "linked" for entry in view["entries"])
        assert len(calls) > 6


@pytest.mark.parametrize(
    "query",
    ["days=14", "days=7&days=30", "page=0", "page=1&page=2", "provider=manual", "path=/tmp/test"],
)
@pytest.mark.asyncio
async def test_local_http_boundary_and_source_only_metric_whitelist(tmp_path, query):
    with seeded(tmp_path) as store:
        path = store.path
    runtime = DashboardRuntime(lambda: Store(path, clock=lambda: NOW))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime), client=("127.0.0.1", 1234)),
        base_url="http://127.0.0.1:8766",
    ) as client:
        assert (await client.get("/api/cycles")).status_code == 401
        await client.get("/")
        assert (await client.get("/api/cycles?" + query)).status_code == 400
        response = await client.get("/api/cycles")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        for forbidden in (
            '"payload"',
            '"user_id"',
            '"external_id"',
            '"sleep_id"',
            '"email"',
            '"answers"',
        ):
            assert forbidden not in response.text
        metric = response.json()["entries"][0]["sleep"]["metrics"][1]
        source = await client.get(
            "/api/evidence", params={"metric": metric["key"], "revision": metric["revision_id"]}
        )
        assert source.status_code == 200 and source.json()["value"] == metric["value"]
        assert (await client.get("/api/dashboard?metric=sleep_efficiency")).status_code == 400
        assert (
            await client.get("/api/cycles", headers={"Origin": "https://example.invalid"})
        ).status_code == 403
        assert (await client.post("/api/cycles", json={})).status_code == 403
        assert (
            await client.post("/api/cycles", json={}, headers={"Origin": "http://127.0.0.1:8766"})
        ).status_code == 405
        assert json.loads(response.text)["environment"] == "synthetic"


@pytest.mark.parametrize("variant", ["sleep_upper", "recovery_upper", "duplicate"])
def test_uuid_identity_is_case_insensitive_and_duplicate_aliases_are_rejected(tmp_path, variant):
    data = scenario_resources("stale")
    if variant == "recovery_upper":
        data["recovery"][0]["sleep_id"] = data["recovery"][0]["sleep_id"].upper()
    elif variant == "sleep_upper":
        data["sleep"][0]["id"] = data["sleep"][0]["id"].upper()
    else:
        duplicate = copy.deepcopy(data["sleep"][0])
        duplicate["id"] = duplicate["id"].upper()
        data["sleep"].append(duplicate)
        with pytest.raises(ValueError, match="duplicate resource identities"):
            normalize_api(data, acquired_at=NOW, synthetic=True)
        return
    with seeded(tmp_path, data) as store:
        entry = entry_for(CycleReviewService(store).review(), "2026-09-01T00:00:00Z")
        assert entry["sleep"]["state"] == "linked"
        assert entry["sleep"]["metrics"]


@pytest.mark.parametrize("uppercase_tombstone", [False, True])
def test_deleted_uuid_alias_cannot_reactivate_another_old_alias(tmp_path, uppercase_tombstone):
    data = scenario_resources("stale")
    with seeded(tmp_path, data) as store:
        inputs = normalize_api(data, acquired_at=NOW, synthetic=True)
        original = next(row for row in inputs if row.resource == "sleep")
        store.ingest(
            [
                replace(
                    original,
                    external_id=original.external_id.upper()
                    if uppercase_tombstone
                    else original.external_id,
                    deleted=True,
                    source_updated_at="2026-09-07T11:00:00Z",
                    observations=(),
                    activities=(),
                    payload={"synthetic": True, "deleted": True},
                )
            ]
        )
        assert not any(row["deleted"] for row in store.current_sources("sleep"))
        assert any(row["deleted"] for row in store.current_sources("sleep", include_deleted=True))
        entry = entry_for(CycleReviewService(store).review(), "2026-09-01T00:00:00Z")
        assert entry["sleep"]["state"] == "unavailable"
        assert entry["sleep"]["metrics"] == []
