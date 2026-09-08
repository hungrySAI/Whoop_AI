"""Publication races and transient views, using only temporary synthetic records."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from whoop_copilot.analytics import ALGORITHMS, Algorithm
from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.dashboard_demo import seed_demo
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store, atomic, canonical
from whoop_copilot.weekly import WeeklyService

NOW = "2026-08-03T12:00:00+00:00"
EXPIRES = "2026-08-03T12:00:01+00:00"
FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
QUERY = {
    "metric": "whoop.hrv_rmssd",
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-08-03T00:00:00Z",
    "provider": "whoop",
    "resource": "recovery",
}


def resources():
    return json.loads(FIXTURE.read_text())["resources"]


def ingest(store, data):
    return store.ingest(normalize_api(data, acquired_at=store.clock(), synthetic=True))


@pytest.mark.parametrize("persist", [True, False])
@pytest.mark.parametrize("as_of", [None, NOW])
@pytest.mark.parametrize("change", ["forget", "expire", "hash"])
def test_removed_changed_or_expired_snapshot_cannot_be_published(
    tmp_path, monkeypatch, persist, as_of, change
):
    now = [NOW]
    path = tmp_path / "synthetic.sqlite3"
    with Store(path, clock=lambda: now[0]) as store, Store(path, clock=lambda: now[0]) as other:
        ingest(store, resources())
        calculation = ALGORITHMS["mean_change"]

        def interleaved(rows, start, end):
            assert rows  # Ensure this exercises health-value evidence, not an empty query.
            if change == "forget":
                other.forget_source("whoop")
            elif change == "expire":
                with atomic(other.db):
                    other.db.execute("UPDATE source_revisions SET expires_at=?", (EXPIRES,))
                # Synthetic storage does not purge; the publish check must reject
                # expired references even while their physical rows still exist.
                now[0] = EXPIRES
            else:
                with atomic(other.db):
                    other.db.execute(
                        "UPDATE source_revisions SET content_hash=content_hash || '-changed'"
                    )
            return calculation.calculate(rows, start, end)

        monkeypatch.setitem(ALGORITHMS, "mean_change", Algorithm(calculation.version, interleaved))
        with pytest.raises(ValueError, match="missing, changed or expired"):
            CopilotService(store).analyze(**QUERY, as_of=as_of, persist=persist)
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_cached_run_checks_expiry_at_return_time(tmp_path):
    now = [NOW]
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: now[0]) as store:
        ingest(store, resources())
        service = CopilotService(store)
        run = service.analyze(**QUERY, as_of=NOW)
        with atomic(store.db):
            store.db.execute("UPDATE source_revisions SET expires_at=?", (EXPIRES,))
        now[0] = EXPIRES
        with pytest.raises(ValueError, match="expired"):
            service.get_run(run["run_id"])
        with pytest.raises(ValueError, match="expired"):
            service.analyze(**QUERY, as_of=NOW)


def test_transient_and_explicit_analysis_share_exact_results_and_evidence(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        ingest(store, resources())
        service = CopilotService(store)
        preview = service.analyze(**QUERY, persist=False)
        assert preview["run_id"] is None
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
        explicit = service.analyze(**QUERY)
        assert explicit["run_id"]
        assert preview["result"] == explicit["result"]
        assert preview["evidence"] == explicit["evidence"]
        assert service.reproduce(explicit["run_id"])["matches"]


def test_repeated_dashboard_and_weekly_reads_do_not_create_analyses_or_tasks(tmp_path):
    now = [NOW]
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: now[0]) as store:
        seed_demo(store)
        initial_tasks = store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        for index in range(100):
            now[0] = (datetime.fromisoformat(NOW) + timedelta(seconds=index)).isoformat()
            view = DashboardService(store).overview("hrv", 30)
            assert datetime.fromisoformat(view["end"]) == datetime.fromisoformat(now[0])
            assert all(card["analysis_id"] is None for card in view["cards"])
            assert view["trend"]["analysis_id"] is None
        for week in (None, "2026-08-03"):
            view = WeeklyService(store).review(week)
            records = WeeklyService(store).records(view["week"])
            assert all(row["comparison"]["analysis_id"] is None for row in records["metrics"])
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == initial_tasks


def test_unrelated_source_changes_reuse_explicit_analysis_and_original_evidence(tmp_path):
    now = [NOW]
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: now[0]) as store:
        data = resources()
        ingest(store, data)
        service = CopilotService(store)
        before = service.analyze(**QUERY)
        changed = copy.deepcopy(data)
        changed["workout"][0]["updated_at"] = EXPIRES
        changed["workout"][0]["score"]["strain"] += 1
        now[0] = EXPIRES
        ingest(store, changed)
        after = service.analyze(**QUERY)
        assert before["run_id"] == after["run_id"]
        assert before["evidence"] == after["evidence"]
        assert not after["stale"]
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1


def test_revalidated_same_inputs_clear_conservative_invalidation_without_new_run(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        data = resources()
        ingest(store, data)
        service = CopilotService(store)
        before = service.analyze(**QUERY)
        # A new recovery belongs to the same resource but starts outside this
        # explicit analysis window. Resource-level invalidation is conservative.
        cycle = copy.deepcopy(data["cycle"][0])
        cycle.update(
            id=900002,
            start="2026-08-03T00:00:00Z",
            end=None,
            updated_at="2026-08-03T01:00:00Z",
        )
        recovery = copy.deepcopy(data["recovery"][0])
        recovery.update(cycle_id=900002, updated_at="2026-08-03T01:00:00Z")
        data["cycle"].append(cycle)
        data["recovery"].append(recovery)
        ingest(store, data)
        assert service.get_run(before["run_id"])["stale"]
        after = service.analyze(**QUERY)
        assert after["run_id"] == before["run_id"]
        assert not after["stale"]
        assert service.reproduce(after["run_id"])["matches"]


def test_oversized_legacy_recompute_task_is_rejected_before_any_work(tmp_path):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        with atomic(store.db):
            store.db.execute(
                """INSERT INTO tasks(id,idempotency_key,kind,payload,status,created_at)
                VALUES ('oversized','oversized','recompute',?,'pending',?)""",
                (canonical({"requests": [QUERY] * 101}), NOW),
            )
        result = CopilotService(store).run_pending()
        assert result[0]["status"] == "failed"
        assert "100 request budget" in result[0]["error"]
        assert store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
