"""Capture-order inversion must never reactivate a superseded source revision."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from whoop_copilot.api_ingestion import normalize_api
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store

FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
START, END = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"
OTHER_ID = "11111111-1111-1111-1111-111111111111"


def workout(captured, updated, *, value=8, identity=None):
    resources = json.loads(FIXTURE.read_text())["resources"]
    raw = resources["workout"][0]
    raw["updated_at"] = updated
    raw["score"]["strain"] = value
    if identity:
        raw["id"] = identity
    return next(
        record
        for record in normalize_api(resources, acquired_at=captured, synthetic=False)
        if record.resource == "workout"
    )


def protected(path, clock):
    return Store(
        path,
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: clock[0],
    )


@pytest.mark.parametrize("deleted", [False, True])
def test_expired_winner_cleans_predecessors_without_reactivating_late_history(tmp_path, deleted):
    now = ["2026-09-05T12:01:00Z"]
    with protected(tmp_path / "lineage.sqlite3", now) as store:
        older = workout(now[0], "2026-08-01T14:00:00Z")
        predecessor = store.ingest([older])["revision_ids"][0]
        now[0] = "2026-09-05T12:02:00Z"
        winner = workout("2026-09-05T12:00:00Z", "2026-08-02T14:00:00Z", value=15)
        if deleted:
            winner = replace(
                winner,
                deleted=True,
                observations=(),
                activities=(),
                payload={"synthetic": False, "deleted": True},
            )
        winner_id = store.ingest([winner])["revision_ids"][0]
        now[0] = "2026-09-05T12:03:00Z"
        late_older = workout(now[0], "2026-08-01T15:00:00Z", value=9)
        independent = workout(now[0], "2026-08-01T14:00:00Z", identity=OTHER_ID)
        late_id, independent_id = store.ingest([late_older, independent])["revision_ids"]
        assert (
            store.db.execute(
                "SELECT became_current FROM source_revisions WHERE id=?", (late_id,)
            ).fetchone()[0]
            == 0
        )
        old_analysis = CopilotService(store).analyze("whoop.strain", START, END)
        backup = tmp_path / "managed.sqlite3"
        store.backup(backup)

        # Provider order and capture order differ: the superseding version expires
        # first while its previous current version still has 30 seconds left.
        now[0] = "2026-09-06T12:00:30Z"
        store.purge_expired()
        retained = {row[0] for row in store.db.execute("SELECT id FROM source_revisions")}
        assert retained == {late_id, independent_id}
        assert not retained.intersection({predecessor, winner_id})
        current = store.current_sources("workout", include_deleted=True)
        assert [row["id"] for row in current] == [independent_id]
        assert not backup.exists()
        assert not store.db.execute("SELECT * FROM tasks").fetchall()
        assert not store.db.execute("SELECT * FROM analysis_runs").fetchall()
        for table in ("observations", "activities", "source_ingest_keys"):
            assert not store.db.execute(
                f"SELECT 1 FROM {table} WHERE revision_id IN (?,?)",
                (predecessor, winner_id),
            ).fetchall()
        with pytest.raises(ValueError, match="Unknown analysis"):
            CopilotService(store).get_run(old_analysis["run_id"])
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_expiring_intermediate_history_does_not_clear_valid_predecessors_or_winner(tmp_path):
    now = ["2026-09-05T12:01:00Z"]
    with protected(tmp_path / "intermediate.sqlite3", now) as store:
        first = store.ingest([workout(now[0], "2026-08-01T14:00:00Z")])["revision_ids"][0]
        now[0] = "2026-09-05T12:02:00Z"
        middle = store.ingest([workout("2026-09-05T12:00:00Z", "2026-08-02T14:00:00Z", value=12)])[
            "revision_ids"
        ][0]
        now[0] = "2026-09-05T12:03:00Z"
        winner = store.ingest([workout(now[0], "2026-08-03T14:00:00Z", value=15)])["revision_ids"][
            0
        ]
        now[0] = "2026-09-06T12:00:30Z"
        store.purge_expired()
        assert {row[0] for row in store.db.execute("SELECT id FROM source_revisions")} == {
            first,
            winner,
        }
        assert store.current_sources("workout")[0]["id"] == winner
        assert not store.db.execute(
            "SELECT 1 FROM source_revisions WHERE id=?", (middle,)
        ).fetchone()
        historical = CopilotService(store).analyze(
            "whoop.strain", START, END, as_of="2026-09-05T12:01:30Z"
        )
        assert historical["result"]["count"] == 1 and historical["result"]["mean"] == 8
