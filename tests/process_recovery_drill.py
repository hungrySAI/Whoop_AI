"""Run real process-death/restart acceptance using only fabricated WHOOP fixtures.

Run with ``uv run python tests/process_recovery_drill.py``. This is deliberately
separate from the fast unit suite. It creates and removes isolated temporary
stores, never opens the project runtime, and prints only technical booleans.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from test_sync import Client

from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService, sync_is_running
from whoop_copilot.web import DashboardRuntime

NOW = "2026-09-07T12:00:00Z"
SCRIPT = Path(__file__).resolve()


def at(days=0, *, midnight=False):
    value = datetime.fromisoformat(timestamp(NOW)) + timedelta(days=days)
    if midnight:
        value = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return timestamp(value.isoformat())


def wait_until(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Synthetic process drill timed out")
        time.sleep(0.02)


def hold_for_forced_exit(marker):
    marker.write_text("checkpoint-ready", encoding="ascii")
    while True:
        time.sleep(1)


def interruptible_worker(path, marker, phase):
    class InterruptedClient(Client):
        def list_records(self, resource, start, end, next_token):
            if phase == "page" and resource == "cycle" and next_token is not None:
                hold_for_forced_exit(marker)
            return super().list_records(resource, start, end, next_token)

    with Store(path, clock=lambda: NOW) as store:
        if phase == "ingest":
            original_ingest = store.ingest

            def interrupted_ingest(records):
                original_ingest(records)
                hold_for_forced_exit(marker)

            store.ingest = interrupted_ingest
        SyncService(store, InterruptedClient(split_cycle=True)).run(
            *DashboardService(store).window(7), catch_up=True
        )
    raise AssertionError("The checkpoint worker must be terminated before completion")


def resume_worker(path, phase):
    calls = []

    def factory():
        return Store(path, clock=lambda: at(3))

    def client_factory():
        client = Client(split_cycle=True)
        client.calls = calls
        return client

    runtime = DashboardRuntime(factory, client_factory=client_factory)
    initial = runtime.status()
    paused = initial["last_run"]
    assert paused["status"] == "paging"
    assert not initial["running"]
    assert paused["request"] == {"start": at(-366), "end": at()}
    with factory() as store:
        count = store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]
        assert count == (0 if phase == "page" else 6)
        state = json.loads(store.db.execute("SELECT state FROM sync_runs").fetchone()[0])
        assert state["index"] == (2 if phase == "page" else 6)
        if phase == "page":
            assert state["next_token"] == "next-cycle-page"
        # Both public entry points must refuse to restart an unfinished request.
        assert SyncService(store, None).run(
            *DashboardService(store).window(7), catch_up=True, if_stale=True
        ) == {"skipped": "unfinished"}
    assert runtime.start_sync(7, if_stale=True) == {
        "accepted": False,
        "reason": "unfinished",
    }
    assert calls == []
    accepted = runtime.start_sync(30, resume=paused["run_id"])
    assert accepted == {"accepted": True, "already_running": False}
    wait_until(lambda: not runtime.status()["running"])
    completed = runtime.status()
    assert completed["error"] is None
    assert completed["last_run"]["status"] == "completed"
    assert completed["last_run"]["run_id"] == paused["run_id"]
    assert completed["last_run"]["request"] == paused["request"]
    assert completed["last_run"]["catch_up"] == paused["catch_up"]
    if phase == "page":
        assert calls[0] == ("cycle", at(-366), at(), "next-cycle-page")
    else:
        assert calls == []
    assert all(start == at(-366) and end == at() for _, start, end, _ in calls)
    # Finishing an old request today cannot claim the current view is fresh.
    assert completed["freshness"]["7"]["state"] != "fresh"
    with factory() as store:
        before = store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0]
        assert before == 6
        assert SyncService(store, None).run(run_id=paused["run_id"])["status"] == "completed"
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == before
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return {
        "checkpoint_survived_process_death": True,
        "sync_lock_released_on_process_death": True,
        "staging_or_durable_ingestion_state_correct": True,
        "conditional_open_did_not_retry": True,
        "shared_service_conditional_gate_did_not_retry": True,
        "explicit_resume_completed": True,
        "original_window_and_catchup_plan_preserved": True,
        "cursor_resumed_without_restarting_pages": True,
        "completed_replay_idempotent": True,
        "old_completion_not_current_freshness": True,
        "store_integrity_ok": True,
    }


def catchup_worker(path):
    calls = []

    def factory():
        return Store(path, clock=lambda: at(45))

    def client_factory():
        client = Client(split_cycle=True)
        client.calls = calls
        return client

    runtime = DashboardRuntime(factory, client_factory=client_factory)
    initial = runtime.status()
    previous = initial["last_run"]
    plan = initial["sync_plan"]["7"]
    assert plan["coverage_through"] == at()
    assert plan["request"] == {"start": at(-6, midnight=True), "end": at(45)}
    assert runtime.start_sync(7, if_stale=True) == {
        "accepted": True,
        "already_running": False,
    }
    wait_until(lambda: not runtime.status()["running"])
    current = runtime.status()
    assert current["error"] is None
    assert current["last_run"]["status"] == "completed"
    assert current["last_run"]["run_id"] != previous["run_id"]
    assert current["last_run"]["request"] == plan["request"]
    assert current["freshness"]["7"]["state"] == "fresh"
    assert len(calls) == 7
    assert all(
        start == plan["request"]["start"] and end == plan["request"]["end"]
        for _, start, end, _ in calls
    )
    assert runtime.start_sync(7, if_stale=True) == {"accepted": False, "reason": "fresh"}
    assert len(calls) == 7
    with factory() as store:
        # Profile/body have no provider modification timestamp; a later retrieval
        # creates a new snapshot. Stable scored resource versions stay unique.
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 8
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM source_revisions WHERE resource NOT IN ('profile','body')"
            ).fetchone()[0]
            == 4
        )
        assert all(
            row[0] == 2
            for row in store.db.execute(
                "SELECT COUNT(*) FROM source_revisions WHERE resource IN ('profile','body') "
                "GROUP BY resource"
            )
        )
        assert store.db.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 2
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return {
        "later_process_open_bridged_45_day_absence": True,
        "catchup_used_request_end_not_late_completion": True,
        "overlap_calendar_window_preserved": True,
        "same_provider_version_replay_did_not_duplicate": True,
        "timestamp_free_profile_body_retained_new_retrieval_snapshots": True,
        "repeated_open_skipped_when_fresh": True,
        "store_integrity_ok_after_catchup": True,
    }


def run_worker(mode, path, phase):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--worker", mode, "--path", str(path), "--phase", phase],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def run_drill():
    report = {"synthetic_fixture_only": True, "external_network_not_used": True}
    with tempfile.TemporaryDirectory(prefix="whoop-synthetic-process-drill-") as temporary:
        base = Path(temporary)
        for phase in ("page", "ingest"):
            path, marker = base / f"{phase}.sqlite3", base / f"{phase}.ready"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--worker",
                    "interrupt",
                    "--path",
                    str(path),
                    "--phase",
                    phase,
                    "--marker",
                    str(marker),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:

                def ready():
                    assert process.poll() is None, "Checkpoint child exited before interruption"
                    return marker.exists()

                wait_until(ready)
                assert sync_is_running(path.with_suffix(".sync.lock"))
                process.kill()
                assert process.wait(timeout=10) == -9
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            report[f"sigkill_after_{phase}"] = {
                "actual_sigkill_received": True,
                **run_worker("resume", path, phase),
                **run_worker("catchup", path, phase),
            }
    report["temporary_stores_removed"] = not base.exists()
    report["all_child_processes_reaped"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("interrupt", "resume", "catchup"))
    parser.add_argument("--path", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--phase", choices=("page", "ingest"))
    args = parser.parse_args()
    if args.worker == "interrupt":
        interruptible_worker(args.path, args.marker, args.phase)
    elif args.worker == "resume":
        print(json.dumps(resume_worker(args.path, args.phase), sort_keys=True))
    elif args.worker == "catchup":
        print(json.dumps(catchup_worker(args.path), sort_keys=True))
    else:
        print(json.dumps(run_drill(), sort_keys=True))


if __name__ == "__main__":
    main()
