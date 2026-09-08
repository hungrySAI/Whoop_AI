"""Synthetic review probes: passing assertions document observed weaknesses.

These are intentionally outside the regular suite. When hardening lands, turn the
assertions into desired bounds/invalidation behavior rather than preserving them.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.dashboard_demo import seed_demo
from whoop_copilot.storage import Store
from whoop_copilot.weekly import WeeklyService

NOW = "2026-08-03T12:00:00+00:00"
FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/whoop_api_snapshot.json"


def test_dashboard_reads_accumulate_analysis_and_recompute_requests(tmp_path):
    now = [NOW]
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: now[0]) as store:
        seed_demo(store)
        source_count = store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]
        dashboard = DashboardService(store)
        for index in range(10):
            now[0] = (datetime.fromisoformat(NOW) + timedelta(seconds=index)).isoformat()
            dashboard.overview("hrv", 30)
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == source_count
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 40
        data = json.loads(FIXTURE.read_text())["resources"]
        result = store.ingest(normalize_api(data, acquired_at=now[0], synthetic=True))
        payload = json.loads(
            store.db.execute("SELECT payload FROM tasks WHERE id=?", (result["task_id"],)).fetchone()[0]
        )
        assert len(payload["requests"]) == 40


def test_identical_profile_and_body_retrievals_invalidate_weekly_analyses(tmp_path):
    now = [NOW]
    data = json.loads(FIXTURE.read_text())["resources"]
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: now[0]) as store:
        store.ingest(normalize_api(data, acquired_at=now[0], synthetic=True))
        initial = WeeklyService(store).review()
        before_ids = {
            name: [row["id"] for row in store.current_sources(name)]
            for name in ("cycle", "recovery", "sleep", "workout")
        }
        now[0] = (datetime.fromisoformat(NOW) + timedelta(minutes=30)).isoformat()
        ingest = store.ingest(normalize_api(data, acquired_at=now[0], synthetic=True))
        assert ingest["inserted"] == 2  # Only profile/body acquisition clocks changed.
        assert before_ids == {
            name: [row["id"] for row in store.current_sources(name)] for name in before_ids
        }
        after = WeeklyService(store).review()
        assert [row["selected"]["mean"] for row in initial["metrics"]] == [
            row["selected"]["mean"] for row in after["metrics"]
        ]
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 12
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs WHERE stale=1").fetchone()[0] == 6


def test_cycle_metric_lookup_scans_the_observation_table(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        seed_demo(store)
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM observations WHERE revision_id=?", (1,)
        ).fetchall()
        assert any("SCAN observations" in row[3] for row in plan)
