"""Coverage is metadata about captured records, never inferred wearing history."""

import pytest
from dashboard_scenarios import NOW, ScenarioClient, scenario_resources

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService


def load(store, scenario):
    store.ingest(normalize_api(scenario_resources(scenario), acquired_at=NOW, synthetic=True))


def test_short_history_counts_distinct_dates_not_span_or_valid_values(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        load(store, "short-history")
        view = DashboardService(store).overview(days=30)
        coverage = view["trend"]["coverage"]
        assert view["start"].startswith("2026-08-09")
        assert coverage["first_record_at"].startswith("2026-09-01")
        assert coverage["last_record_at"].startswith("2026-09-07")
        assert coverage["record_days"] == coverage["record_count"] == 6
        assert coverage["valid_observation_count"] == 5
        assert coverage["state"] == "has_valid_observations"
        assert view["trend"]["summary"]["first_half"]["count"] == 0
        assert view["trend"]["summary"]["change"] is None
        assert view["trend"]["latest"]["value"] is None
        assert "wearing" not in coverage and "wear_start" not in coverage
        training = DashboardService(store).overview("workout", 30)["trend"]["coverage"]
        assert training["record_days"] == 2
        assert training["record_count"] == training["valid_observation_count"] == 3


@pytest.mark.parametrize(
    "scenario,key,status",
    [
        ("pending", "hrv", "PENDING_SCORE"),
        ("unscorable", "hrv", "UNSCORABLE"),
        ("calibrating", "hrv", "calibrating"),
        ("missing-metric", "sleep", "missing_metric"),
    ],
)
def test_captured_records_without_valid_scores_retain_dates_and_reason(
    tmp_path, scenario, key, status
):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        load(store, scenario)
        trend = DashboardService(store).overview(key, 30)["trend"]
        assert trend["coverage"]["state"] == "no_valid_observations"
        assert trend["coverage"]["record_days"] == trend["coverage"]["record_count"] == 6
        assert trend["coverage"]["valid_observation_count"] == 0
        assert trend["coverage"]["states"][0]["status"] == status
        assert trend["coverage"]["states"][0]["count"] == 6
        assert trend["summary"]["mean"] is None
        assert all(point["value"] is None for point in trend["points"])


def test_empty_coverage_does_not_fabricate_a_start_date(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        load(store, "empty")
        coverage = DashboardService(store).overview(days=30)["trend"]["coverage"]
        assert coverage == {
            "first_record_at": None,
            "last_record_at": None,
            "record_days": 0,
            "record_count": 0,
            "valid_observation_count": 0,
            "states": [],
            "state": "no_records",
        }


def test_failure_does_not_discard_old_coverage_or_successful_request(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        service = DashboardService(store)
        original = SyncService(store, ScenarioClient("short-history")).run(*service.window(30))
        before = service.overview(days=30)["trend"]
        with pytest.raises(ValueError):
            SyncService(store, ScenarioClient("sync-failure", fail=True)).run(*service.window(7))
        after = service.overview(days=30)["trend"]
        assert after["coverage"] == before["coverage"]
        assert after["summary"] == before["summary"]
        assert service.evidence("hrv", before["latest"]["revision_id"])["is_current"]
        success = store.db.execute("SELECT id FROM sync_runs WHERE status='completed'").fetchone()
        assert success[0] == original["run_id"]


def test_midnight_end_is_excluded_from_records_and_gap_markers(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: "2026-09-07T00:00:00Z") as store:
        data = scenario_resources("short-history")
        store.ingest(normalize_api(data, acquired_at=store.clock(), synthetic=True))
        view = DashboardService(store).overview(days=7)
        assert all(point["measured_at"] < view["end"] for point in view["trend"]["points"])
        assert view["trend"]["coverage"]["record_days"] == 5
