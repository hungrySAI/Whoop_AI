"""Reproduce review measurements using automatically removed synthetic databases.

Run with ``uv run python reviews/2026-09-07/benchmark_views.py``. Add ``--large``
to include the 200,000 extra profile-version case. This script is not a pytest
test and never reads a runtime database, credentials, or a real account.

The scale case inserts fabricated historical rows directly to isolate query
cost; it is not an ingestion correctness test or a SQLCipher latency forecast.
Reported text bytes exclude SQLite pages, indexes, WAL, and task storage.
"""

import argparse
import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.dashboard_demo import seed_demo
from whoop_copilot.storage import Store, atomic

NOW = "2026-08-03T12:00:00+00:00"
FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/whoop_api_snapshot.json"


def emit(**values):
    print(json.dumps(values, sort_keys=True), flush=True)


def repeated_reads(root):
    now = [NOW]
    with Store(root / "reads.sqlite3", clock=lambda: now[0], environment="synthetic") as store:
        seed_demo(store)
        source_count = store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]
        dashboard = DashboardService(store)
        started = time.perf_counter()
        for index in range(100):
            now[0] = (datetime.fromisoformat(NOW) + timedelta(seconds=index)).isoformat()
            dashboard.overview("hrv", 30)
        elapsed = time.perf_counter() - started
        count, size = store.db.execute(
            """SELECT COUNT(*), SUM(LENGTH(CAST(evidence AS BLOB))
            + LENGTH(CAST(result AS BLOB)) + LENGTH(CAST(request AS BLOB)))
            FROM analysis_runs"""
        ).fetchone()
        emit(
            measurement="repeated_reads",
            environment="synthetic",
            page_reads=100,
            source_revisions=source_count,
            analysis_runs=count,
            analysis_text_bytes=size,
            elapsed_seconds=elapsed,
        )
        data = json.loads(FIXTURE.read_text())["resources"]
        data["recovery"][0]["updated_at"] = now[0]
        result = store.ingest(normalize_api(data, acquired_at=now[0], synthetic=True))
        raw = store.db.execute(
            "SELECT payload FROM tasks WHERE id=?", (result["task_id"],)
        ).fetchone()[0]
        emit(
            measurement="next_import_recompute",
            environment="synthetic",
            recompute_requests=len(json.loads(raw)["requests"]),
            stored_task_payload_bytes=len(raw.encode("utf-8")),
            stale_analysis_runs=store.db.execute(
                "SELECT COUNT(*) FROM analysis_runs WHERE stale=1"
            ).fetchone()[0],
        )


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
        for target in ((0, 20_000, 200_000) if large else (0, 20_000)):
            if target > inserted:
                # Distinct synthetic fingerprints prevent the identity/hash unique
                # constraint from collapsing the fabricated historical versions.
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
            emit(
                measurement="historical_version_query_cost",
                environment="synthetic",
                base_source_revisions=base_count,
                extra_profile_versions=target,
                selected_window_records=len(result["trend"]["records"]),
                overview_seconds=durations,
                cache_note="first read per size computes analyses; next two reuse those runs",
            )
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM observations WHERE revision_id=?", (1,)
        ).fetchall()
        emit(measurement="cycle_metric_query_plan", query_plan=[tuple(row) for row in plan])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--large", action="store_true", help="also measure 200,000 extra synthetic profile versions"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="whoop-review-views-") as directory:
        root = Path(directory)
        repeated_reads(root)
        historical_versions(root, large=args.large)
    emit(measurement="cleanup", temporary_databases_removed=True)


if __name__ == "__main__":
    main()
