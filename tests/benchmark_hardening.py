"""Technical hardening benchmark, never collected as a pytest test.

Run ``uv run python tests/benchmark_hardening.py`` for 1,000 page reads and
10,000 extra historical profile versions. Add ``--large`` for 100,000 and
200,000 versions. Every database and record is synthetic and automatically
removed. No runtime database or credential is opened.

Historical rows are constructed directly to isolate query shape, not to model
ingestion correctness. Timing is informational; stable checks concern selected
row counts, persisted derived data, task counts, and indexed lookup plans.
Python allocation peaks exclude SQLite's native allocations and OS caches.
"""

import argparse
import json
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.dashboard_demo import seed_demo
from whoop_copilot.storage import Store, atomic

NOW = "2026-08-03T12:00:00+00:00"
FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"


def emit(**values):
    print(json.dumps(values, sort_keys=True), flush=True)


def stored_counts(store):
    analyses, text_bytes = store.db.execute(
        """SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(evidence AS BLOB))
        + LENGTH(CAST(result AS BLOB)) + LENGTH(CAST(request AS BLOB))),0)
        FROM analysis_runs"""
    ).fetchone()
    return {
        "analysis_runs": analyses,
        "analysis_text_bytes": text_bytes,
        "tasks": store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
    }


def repeated_reads(root):
    now = [NOW]
    with Store(root / "reads.sqlite3", clock=lambda: now[0], environment="synthetic") as store:
        seed_demo(store)
        started = time.perf_counter()
        for index in range(1_000):
            now[0] = (datetime.fromisoformat(NOW) + timedelta(seconds=index)).isoformat()
            result = DashboardService(store).overview("hrv", 30)
            assert len(result["trend"]["records"]) == 28
        after_reads = stored_counts(store)
        assert after_reads == {"analysis_runs": 0, "analysis_text_bytes": 0, "tasks": 0}
        emit(
            measurement="repeated_reads",
            environment="synthetic",
            page_reads=1_000,
            elapsed_seconds=time.perf_counter() - started,
            **after_reads,
        )
        data = json.loads(FIXTURE.read_text())["resources"]
        data["recovery"][0]["updated_at"] = now[0]
        ingestion = store.ingest(normalize_api(data, acquired_at=now[0], synthetic=True))
        after_import = stored_counts(store)
        assert after_import == after_reads
        assert ingestion["task_id"] is None
        emit(
            measurement="next_import_after_transient_reads",
            environment="synthetic",
            task_id_present=False,
            **after_import,
        )


def query_plans(store):
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        recovery_count = len(store.current_sources("recovery"))
    finally:
        store.db.set_trace_callback(None)
    source_query = next(statement for statement in statements if "WITH winners AS" in statement)
    source_plan = [row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + source_query)]
    observation_plan = [
        row[3]
        for row in store.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM observations WHERE revision_id=?", (1,)
        )
    ]
    assert recovery_count == 28
    assert any("revisions_current" in detail for detail in source_plan)
    assert any("observations_revision" in detail for detail in observation_plan)
    assert not any("SCAN observations" in detail for detail in observation_plan)
    return {
        "selected_current_recovery_rows": recovery_count,
        "current_source_query_plan": source_plan,
        "observation_query_plan": observation_plan,
    }


def historical_versions(root, *, large):
    with Store(root / "scale.sqlite3", clock=lambda: NOW, environment="synthetic") as store:
        seed_demo(store)
        template = dict(
            store.db.execute("SELECT * FROM source_revisions WHERE resource='profile'").fetchone()
        )
        base_count = store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]
        columns = [key for key in template if key != "id"]
        statement = (
            "INSERT INTO source_revisions ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")"
        )
        inserted = 0
        for target in (0, 10_000, 100_000, 200_000) if large else (0, 10_000):
            if target > inserted:
                with atomic(store.db):
                    store.db.executemany(
                        statement,
                        (
                            tuple(
                                f"synthetic-history-{index}"
                                if key == "content_hash"
                                else template[key]
                                for key in columns
                            )
                            for index in range(inserted, target)
                        ),
                    )
                inserted = target
            durations = []
            for _ in range(3):
                started = time.perf_counter()
                result = DashboardService(store).overview("hrv", 30)
                durations.append(time.perf_counter() - started)
            tracemalloc.start()
            try:
                DashboardService(store).overview("hrv", 30)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            counts = stored_counts(store)
            assert len(result["trend"]["records"]) == 28
            assert counts == {"analysis_runs": 0, "analysis_text_bytes": 0, "tasks": 0}
            emit(
                measurement="historical_version_query_cost",
                environment="synthetic",
                base_source_revisions=base_count,
                extra_profile_versions=target,
                selected_window_records=len(result["trend"]["records"]),
                overview_seconds=durations,
                python_peak_allocation_bytes=peak,
                **counts,
                **query_plans(store),
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--large", action="store_true", help="also measure 100,000 and 200,000 historical versions"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="whoop-hardening-benchmark-") as directory:
        root = Path(directory)
        repeated_reads(root)
        historical_versions(root, large=args.large)
    emit(measurement="cleanup", temporary_databases_removed=True)


if __name__ == "__main__":
    main()
