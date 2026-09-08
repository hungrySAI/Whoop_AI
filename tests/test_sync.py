import copy
import json
from pathlib import Path

import pytest

from whoop_copilot.api_ingestion import normalize_api, validate_resource
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService, sync_lock

FIXTURE = Path(__file__).parent / "fixtures/whoop_api_snapshot.json"
START, END = "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"


class Client:
    def __init__(self, resources=None, fail_resource=None, split_cycle=False):
        self.resources = resources or json.loads(FIXTURE.read_text())["resources"]
        self.calls = []
        self.fail_resource = fail_resource
        self.split_cycle = split_cycle

    def list_records(self, resource, start, end, next_token):
        self.calls.append((resource, start, end, next_token))
        if resource == self.fail_resource:
            raise ValueError("simulated network failure with no personal data")
        records = self.resources[resource]
        following = None
        if resource == "cycle" and self.split_cycle:
            following = "next-cycle-page" if next_token is None else None
            records = records if next_token is None else []
        return {
            "records": copy.deepcopy(records),
            "next_token": following,
            "headers": {"cache-control": "private"},
        }

    def cycle_by_id(self, identity):
        cycle = json.loads(FIXTURE.read_text())["resources"]["cycle"][0]
        assert cycle["id"] == identity
        return {"records": [cycle], "next_token": None, "headers": {}}


def test_six_resources_keep_official_contract_and_measurement_semantics():
    data = json.loads(FIXTURE.read_text())["resources"]
    records = normalize_api(data, acquired_at="2026-09-05T00:00:00Z", synthetic=True)
    assert {r.resource for r in records} == {
        "profile",
        "body",
        "cycle",
        "recovery",
        "sleep",
        "workout",
    }
    assert all(not r.observations for r in records if r.resource in {"profile", "body"})
    assert (
        next(r for r in records if r.resource == "sleep").payload["record"]["score"][
            "stage_summary"
        ]
        == data["sleep"][0]["score"]["stage_summary"]
    )
    data["cycle"][0].pop("end")
    data["cycle"][0]["timezone_offset"] = "Z"
    normalized = normalize_api(data, acquired_at="2026-09-05T00:00:00Z", synthetic=False)
    assert next(r for r in normalized if r.resource == "cycle").activities[0].end_at is None
    assert all(r.payload["synthetic"] is False for r in normalized)


def test_contract_rejects_wrong_uuid_and_mixed_account_without_raw_error():
    data = json.loads(FIXTURE.read_text())["resources"]
    data["workout"][0]["id"] = "sensitive-invalid-value"
    with pytest.raises(ValueError, match="contract") as error:
        validate_resource("workout", data["workout"][0])
    assert "sensitive-invalid-value" not in str(error.value)
    data = json.loads(FIXTURE.read_text())["resources"]
    data["sleep"][0]["user_id"] = 1234
    with pytest.raises(ValueError, match="mixes"):
        normalize_api(data, acquired_at="2026-09-05T00:00:00Z", synthetic=True)


def test_workout_missing_geometry_resumes_and_preserves_raw_nulls(tmp_path):
    data = json.loads(FIXTURE.read_text())["resources"]
    optional_fields = ("distance_meter", "altitude_gain_meter", "altitude_change_meter")
    for field in optional_fields:
        data["workout"][0]["score"][field] = None
    with Store(tmp_path / "sync.sqlite3") as store:
        client = Client(data, fail_resource="workout")
        service = SyncService(store, client)
        with pytest.raises(ValueError, match="paused"):
            service.run(START, END)
        run_id = store.db.execute("SELECT id FROM sync_runs").fetchone()[0]
        assert service.status(run_id)["resources_completed"] == 5
        client.fail_resource = None
        done = service.run(run_id=run_id)
        assert done["ingestion"]["inserted"] == 6
        payload = json.loads(
            store.db.execute(
                "SELECT payload FROM source_revisions WHERE resource='workout'"
            ).fetchone()[0]
        )
        assert payload["record"] == data["workout"][0]
        assert all(payload["record"]["score"][field] is None for field in optional_fields)
        analysis = CopilotService(store).analyze("whoop.strain", START, END, resource="workout")
        assert analysis["result"]["count"] == 1
        assert analysis["result"]["mean"] == data["workout"][0]["score"]["strain"]


@pytest.mark.parametrize(
    "field", ["distance_meter", "altitude_gain_meter", "altitude_change_meter"]
)
def test_workout_optional_geometry_still_rejects_non_numeric_values(field):
    record = json.loads(FIXTURE.read_text())["resources"]["workout"][0]
    record["score"][field] = "invalid-value"
    with pytest.raises(ValueError, match="contract") as error:
        validate_resource("workout", record)
    assert "invalid-value" not in str(error.value)


@pytest.mark.parametrize("field", ["kilojoule", "zone_durations"])
def test_workout_required_score_fields_cannot_be_null(field):
    record = json.loads(FIXTURE.read_text())["resources"]["workout"][0]
    record["score"][field] = None
    with pytest.raises(ValueError, match="contract"):
        validate_resource("workout", record)


def test_page_checkpoint_survives_reopen_and_completion_replay(tmp_path):
    path = tmp_path / "sync.sqlite3"
    with Store(path) as store:
        first = SyncService(store, Client(split_cycle=True)).run(START, END, max_pages=3)
        assert first["status"] == "paging"
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
    with Store(path) as store:
        client = Client(split_cycle=True)
        service = SyncService(store, client)
        done = service.run(run_id=first["run_id"])
        assert done["status"] == "completed"
        assert client.calls[0] == (
            "cycle",
            "2026-08-01T00:00:00.000000+00:00",
            "2026-08-03T00:00:00.000000+00:00",
            "next-cycle-page",
        )
        assert done["ingestion"]["inserted"] == 6
        count = len(client.calls)
        assert service.run(run_id=done["run_id"])["status"] == "completed"
        assert len(client.calls) == count
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 6


def test_network_failure_retains_checkpoint_and_does_not_publish_partial_data(tmp_path):
    with Store(tmp_path / "sync.sqlite3") as store:
        with pytest.raises(ValueError, match="run_id="):
            SyncService(store, Client(fail_resource="sleep")).run(START, END)
        run_id = store.db.execute("SELECT id FROM sync_runs").fetchone()[0]
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0
        client = Client()
        done = SyncService(store, client).run(run_id=run_id)
        assert client.calls[0][0] == "sleep"
        assert done["ingestion"]["inserted"] == 6


def test_sync_exposes_sanitized_api_failure_but_not_arbitrary_exception_text(tmp_path):
    from whoop_copilot.whoop_client import WhoopAPIError

    class BrokenClient:
        def __init__(self, error):
            self.error = error

        def list_records(self, *args):
            raise self.error

    with Store(tmp_path / "sync.sqlite3") as store:
        for error, expected in (
            (WhoopAPIError("WHOOP request rejected with HTTP 403"), "HTTP 403"),
            (RuntimeError("sensitive-response-body"), "Source validation or storage failed"),
        ):
            with pytest.raises(ValueError, match=expected) as caught:
                SyncService(store, BrokenClient(error)).run(START, END)
            assert "sensitive-response-body" not in str(caught.value)
        assert "sensitive-response-body" not in str(
            [row[0] for row in store.db.execute("SELECT last_error FROM sync_runs")]
        )


def test_recovery_boundary_fetches_missing_cycle_without_faking_it(tmp_path):
    data = json.loads(FIXTURE.read_text())["resources"]
    data["cycle"] = []
    with Store(tmp_path / "sync.sqlite3") as store:
        service = SyncService(store, Client(data))
        paused = service.run(START, END, max_pages=6)
        assert paused["status"] == "paging"
        done = service.run(run_id=paused["run_id"], max_pages=1)
        assert done["status"] == "completed"
        assert done["ingestion"]["inserted"] == 6


def test_sync_resume_cannot_change_window_and_second_worker_is_rejected(tmp_path):
    path = tmp_path / "sync.sqlite3"
    with Store(path) as store:
        service = SyncService(store, Client())
        paused = service.run(START, END, max_pages=1)
        with pytest.raises(ValueError, match="preserve"):
            service.run(start="2026-07-01T00:00:00Z", run_id=paused["run_id"])
        with sync_lock(path.with_suffix(".sync.lock")):
            with pytest.raises(ValueError, match="already running"):
                service.run(run_id=paused["run_id"])


def test_cycle_and_workout_strain_are_never_silently_averaged_together(tmp_path):
    with Store(tmp_path / "sync.sqlite3") as store:
        SyncService(store, Client()).run(START, END)
        service = CopilotService(store)
        with pytest.raises(ValueError, match="resource types"):
            service.analyze("whoop.strain", START, END)
        result = service.analyze("whoop.strain", START, END, resource="workout")
        assert result["result"]["count"] == 1
        assert (
            result["result"]["mean"]
            == json.loads(FIXTURE.read_text())["resources"]["workout"][0]["score"]["strain"]
        )


def test_crash_after_ingest_before_completion_does_not_duplicate_source_rows(tmp_path, monkeypatch):
    with Store(tmp_path / "sync.sqlite3") as store:
        service = SyncService(store, Client())
        original = store.ingest

        def interrupted(records):
            original(records)
            raise RuntimeError("simulated stop after durable ingestion")

        monkeypatch.setattr(store, "ingest", interrupted)
        with pytest.raises(ValueError, match="paused"):
            service.run(START, END)
        run_id = store.db.execute("SELECT id FROM sync_runs").fetchone()[0]
        monkeypatch.setattr(store, "ingest", original)
        replay = service.run(run_id=run_id)
        assert replay["ingestion"]["duplicates"] == 6
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 6


def test_http_client_to_sqlcipher_sync_integration_never_stores_credentials(tmp_path):
    import httpx

    from whoop_copilot.protection import LocalPolicy
    from whoop_copilot.whoop_client import RESOURCE_PATHS, WhoopClient

    resources = json.loads(FIXTURE.read_text())["resources"]
    reverse = {"/developer/v2" + path: name for name, path in RESOURCE_PATHS.items()}

    class OAuth:
        def access_token(self):
            return "synthetic-sensitive-token"

    def handler(request):
        assert request.headers["authorization"] == "Bearer synthetic-sensitive-token"
        resource = reverse[request.url.path]
        body = (
            resources[resource][0]
            if resource in {"profile", "body"}
            else {"records": resources[resource]}
        )
        return httpx.Response(
            200, json=body, headers={"Cache-Control": "private", "Set-Cookie": "synthetic-cookie"}
        )

    with Store(
        tmp_path / "cipher.sqlite3",
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(30, owner_authorized=True),
    ) as store:
        client = WhoopClient(OAuth(), transport=httpx.MockTransport(handler))
        result = SyncService(store, client).run(START, END)
        assert result["status"] == "completed"
        assert result["ingestion"]["inserted"] == 6
        captured = json.dumps([dict(row) for row in store.db.execute("SELECT * FROM sync_pages")])
        assert "synthetic-sensitive-token" not in captured
        assert "synthetic-cookie" not in captured
        assert "cache-control" in captured


def test_resumed_sync_keeps_first_capture_deadline_and_expired_staging_cannot_resume(tmp_path):
    from whoop_copilot.protection import LocalPolicy

    now = ["2026-09-05T08:00:00Z"]
    with Store(
        tmp_path / "cipher.sqlite3",
        environment="real",
        encryption_key=bytes(range(32)),
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: now[0],
    ) as store:
        service = SyncService(store, Client())
        paused = service.run(START, END, max_pages=3)
        now[0] = "2026-09-05T12:00:00Z"
        service.run(run_id=paused["run_id"])
        expiry = {
            row["resource"]: row["expires_at"]
            for row in store.db.execute("SELECT resource,expires_at FROM source_revisions")
        }
        assert expiry["recovery"] == expiry["cycle"] == "2026-09-06T08:00:00.000000+00:00"
        assert expiry["sleep"] == "2026-09-06T12:00:00.000000+00:00"
        interrupted = service.run(START, END, max_pages=2)
        now[0] = "2026-09-07T12:00:00Z"
        with pytest.raises(ValueError, match="expired sync"):
            service.run(run_id=interrupted["run_id"])
        assert store.db.execute("SELECT COUNT(*) FROM sync_pages").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 0


def test_forgetting_export_preserves_unrelated_api_staging(tmp_path):
    with Store(tmp_path / "sync.sqlite3") as store:
        service = SyncService(store, Client())
        paused = service.run(START, END, max_pages=2)
        store.forget_source("whoop_export")
        assert service.status(paused["run_id"])["staged_records"] == 2
        assert service.run(run_id=paused["run_id"])["status"] == "completed"
