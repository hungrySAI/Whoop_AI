"""Journal export/timeline contracts, using fabricated answers and test keys only."""

import copy
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from dashboard_scenarios import NOW, ScenarioClient
from test_dashboard import client, unlock
from test_exports import write_csv, write_zip

from whoop_copilot.contracts import timestamp
from whoop_copilot.dashboard import DashboardService
from whoop_copilot.exports import parse_export
from whoop_copilot.journal import JournalService
from whoop_copilot.protection import LocalPolicy
from whoop_copilot.storage import Store
from whoop_copilot.sync import SyncService
from whoop_copilot.web import DashboardRuntime

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/fixtures/journal_sample.csv"
EXPORTED = "2026-09-07T10:00:00Z"


@pytest.fixture
def mapping():
    return json.loads((ROOT / "examples/whoop-journal-mapping.example.json").read_text())


@pytest.fixture
def row():
    return {
        "Synthetic journal key": "synth-1",
        "Synthetic reported date": "2026-09-06",
        "Synthetic question": "合成问题",
        "Synthetic answer": "否",
        "private identity": "not-in-response",
    }


@pytest.fixture
def store(tmp_path, mapping):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        SyncService(store, ScenarioClient("short-history")).run(*DashboardService(store).window(30))
        store.ingest(parse_export(SAMPLE, mapping, EXPORTED, synthetic=True))
        yield store


def test_projection_preserves_original_date_blank_and_explicit_no(store):
    view = JournalService(store).timeline()
    assert view["total"] == 5 and view["state"] == "ready"
    assert view["entries"][0]["date"] == "2026-09-06"
    assert view["entries"][0]["source_at"] is None
    assert all(entry["time_precision"] == "date" for entry in view["entries"])
    answers = [item["answer"] for entry in view["entries"] for item in entry["answers"]]
    assert "否" in answers and None in answers
    assert "DO_NOT_EXPOSE_SYNTHETIC_IDENTITY" not in json.dumps(view)
    assert "Synthetic journal key" not in json.dumps(view)
    assert not store.db.execute(
        "SELECT 1 FROM observations o JOIN source_revisions r ON r.id=o.revision_id WHERE r.resource='journal'"
    ).fetchone()


@pytest.mark.parametrize(
    "day,offset,hours", [("2026-03-08", "08:00:00", 23), ("2026-11-01", "07:00:00", 25)]
)
def test_calendar_day_comparison_bounds_follow_dst_without_fabricated_measurement(
    tmp_path, mapping, row, day, offset, hours
):
    row["Synthetic reported date"] = day
    entry = parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)[0].payload["journal"]
    assert entry["date"] == day and entry["source_at"] is None
    assert offset in entry["day_start"]
    assert (
        datetime.fromisoformat(entry["day_end"]) - datetime.fromisoformat(entry["day_start"])
    ).total_seconds() == hours * 3600


def test_positive_offset_date_is_not_shifted_to_previous_utc_day(tmp_path, mapping, row):
    mapping["timezone"] = "+14:00"
    entry = parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)[0].payload["journal"]
    assert entry["date"] == "2026-09-06"
    assert entry["day_start"] == timestamp("2026-09-05T10:00:00Z")


@pytest.mark.parametrize("day,hours", [("2026-09-05", 24), ("2026-09-06", 23)])
def test_missing_midnight_is_a_valid_calendar_day(tmp_path, mapping, row, day, hours):
    mapping["timezone"] = "America/Santiago"
    row["Synthetic reported date"] = day
    entry = parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)[0].payload["journal"]
    assert entry["date"] == day and entry["source_at"] is None
    assert (
        datetime.fromisoformat(entry["day_end"]) - datetime.fromisoformat(entry["day_start"])
    ).total_seconds() == hours * 3600


@pytest.mark.parametrize("basis", ["reported_date", "cycle_start"])
def test_instant_requires_a_real_clock_time(tmp_path, mapping, row, basis):
    mapping["journal"].update(date_basis=basis, time_precision="instant")
    with pytest.raises(ValueError, match="clock time"):
        parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)


def test_instant_cycle_date_respects_explicit_offset_over_fallback(tmp_path, mapping, row):
    mapping["journal"].update(date_basis="cycle_start", time_precision="instant")
    mapping["start"]["format"] = "iso8601"
    row["Synthetic reported date"] = "2026-09-06T00:30:00+09:00"
    entry = parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)[0].payload["journal"]
    assert entry["date"] == "2026-09-06" and entry["timezone"] == "+09:00"
    assert entry["source_at"] == timestamp("2026-09-05T15:30:00Z")


@pytest.mark.parametrize("value", ["2026-11-01T01:30:00", "2026-03-08T02:30:00"])
def test_journal_ambiguous_or_nonexistent_local_timestamp_rejected(tmp_path, mapping, row, value):
    mapping["journal"].update(date_basis="cycle_start", time_precision="instant")
    mapping["start"]["format"] = "iso8601"
    row["Synthetic reported date"] = value
    with pytest.raises(ValueError, match="local time"):
        parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)


@pytest.mark.parametrize(
    "case",
    [
        "no_confirmation",
        "no_timezone",
        "answer_identity",
        "unsupported_basis",
        "cycle_without_time",
        "date_with_time",
        "missing_answer",
        "empty_question",
        "oversized_answer",
        "numeric_journal",
        "v1_with_projection",
    ],
)
def test_explicit_mapping_rejects_unsafe_or_ambiguous_inputs(tmp_path, mapping, row, case):
    if case == "no_confirmation":
        mapping["journal"]["subject_confirmation"] = "guess"
    elif case == "no_timezone":
        mapping["timezone"] = "from_timestamp"
    elif case == "answer_identity":
        mapping["identity_columns"] += ["Synthetic answer"]
    elif case == "unsupported_basis":
        mapping["journal"]["date_basis"] = "automatically_previous_day"
    elif case == "cycle_without_time":
        mapping["journal"]["date_basis"] = "cycle_start"
    elif case == "date_with_time":
        mapping["start"]["format"] = "%Y-%m-%d %H:%M"
    elif case == "missing_answer":
        mapping["journal"]["answers"][0]["answer_column"] = "not-present"
    elif case == "empty_question":
        row["Synthetic question"] = " "
    elif case == "oversized_answer":
        row["Synthetic answer"] = "x" * 8001
    elif case == "numeric_journal":
        mapping["metrics"] = [
            {"column": "Synthetic answer", "metric": "whoop.recovery_score", "unit": "%"}
        ]
    elif case == "v1_with_projection":
        mapping["version"] = 1
    with pytest.raises(ValueError):
        parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)


def test_wide_format_and_exact_zip_member_reuse_existing_parser(tmp_path, mapping, row):
    mapping["journal"]["answers"] = [
        {"question": "已核对的合成问题", "answer_column": "Synthetic answer"}
    ]
    csv = write_csv(tmp_path, [row])
    records = parse_export(csv, mapping, EXPORTED, True)
    archive = write_zip(tmp_path, [("journal.csv", csv.read_bytes())])
    mapping["member"] = "journal.csv"
    assert records == parse_export(archive, mapping, EXPORTED, True)
    assert records[0].payload["journal"]["answers"][0]["question"] == "已核对的合成问题"


def test_versions_dedup_corrections_older_import_and_stale_detail(tmp_path, mapping, row):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        old = parse_export(write_csv(tmp_path, [row]), mapping, "2026-09-07T08:00:00Z", True)
        first = store.ingest(old)
        assert store.ingest(old)["duplicates"] == 1
        row["Synthetic answer"] = "是"
        corrected = parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True)
        store.ingest(corrected)
        assert corrected[0].external_id == old[0].external_id
        store.ingest(old)
        service = JournalService(store)
        view = service.timeline()
        assert view["total"] == 1 and view["entries"][0]["answers"][0]["answer"] == "是"
        with pytest.raises(ValueError, match="unavailable"):
            service.evidence(first["revision_ids"][0])
        store.ingest(
            [
                replace(
                    corrected[0], deleted=True, source_updated_at=timestamp("2026-09-07T11:00:00Z")
                )
            ]
        )
        assert service.timeline()["state"] == "no_import"
        with pytest.raises(ValueError):
            service.evidence(view["entries"][0]["revision_id"])


def test_legacy_raw_journal_is_explicitly_unmapped_and_outside_window_is_distinct(
    tmp_path, mapping, row
):
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        legacy = copy.deepcopy(mapping)
        legacy["version"] = 1
        del legacy["journal"]
        store.ingest(parse_export(write_csv(tmp_path, [row]), legacy, EXPORTED, True))
        assert JournalService(store).timeline()["state"] == "mapping_required"
        row["Synthetic reported date"] = "2026-08-20"
        row["Synthetic journal key"] = "older-synth"
        store.ingest(parse_export(write_csv(tmp_path, [row]), mapping, EXPORTED, True))
        assert JournalService(store).timeline(7)["state"] == "outside_window"
        thirty = JournalService(store).timeline(30)
        assert thirty["total"] == 1 and thirty["unmapped_total"] == 1


def test_day_companions_use_api_source_and_keep_latest_unscored(store):
    view = JournalService(store).timeline()
    for entry in view["entries"]:
        for metric in entry["metrics"]:
            if metric["latest"]:
                assert entry["day_start"] <= metric["latest"]["measured_at"] < entry["day_end"]
                source = DashboardService(store).evidence(
                    metric["key"], metric["latest"]["revision_id"]
                )
                assert source["source"] == "WHOOP API v2"
    # Sep 6 in the declared -07 timezone includes the next UTC midnight's pending record.
    day = next(entry for entry in view["entries"] if entry["date"] == "2026-09-06")
    assert day["metrics"][0]["latest"]["status"] == "PENDING_SCORE"
    assert day["metrics"][0]["latest"]["value"] is None


def test_pagination_clamps_after_removal_without_duplicating_entries(tmp_path, mapping, row):
    rows = [{**row, "Synthetic journal key": f"synth-{index}"} for index in range(25)]
    with Store(tmp_path / "synthetic.sqlite3", clock=lambda: NOW) as store:
        store.ingest(parse_export(write_csv(tmp_path, rows), mapping, EXPORTED, True))
        service = JournalService(store)
        a, b = service.timeline(page=1), service.timeline(page=2)
        assert len(a["entries"]) == 20 and len(b["entries"]) == 5
        assert not {r["revision_id"] for r in a["entries"]} & {
            r["revision_id"] for r in b["entries"]
        }
        store.forget_source("whoop_export")
        assert service.timeline(page=2)["page"] == 1


def test_encrypted_journal_expiry_cleans_timeline_sources_and_managed_backups(tmp_path, mapping):
    clock = [NOW]
    key = bytes(range(32))
    with Store(
        tmp_path / "encrypted.sqlite3",
        environment="real",
        encryption_key=key,
        policy=LocalPolicy(1, owner_authorized=True),
        clock=lambda: clock[0],
    ) as store:
        result = store.ingest(parse_export(SAMPLE, mapping, EXPORTED, synthetic=False))
        assert JournalService(store).timeline()["environment"] == "real"
        backup = tmp_path / "managed.sqlite3"
        store.backup(backup)
        clock[0] = "2026-09-08T12:00:00Z"
        assert JournalService(store).timeline()["state"] == "no_import"
        assert not backup.exists()
        with pytest.raises(ValueError):
            JournalService(store).evidence(result["revision_ids"][0])


def test_concurrent_source_change_retries_whole_timeline(store, mapping, monkeypatch):
    service = JournalService(store)
    original = service.dashboard._metric_view
    changed = [False]

    def racing(*args):
        view = original(*args)
        if not changed[0]:
            changed[0] = True
            store.forget_source("whoop_export")
        return view

    monkeypatch.setattr(service.dashboard, "_metric_view", racing)
    view = service.timeline()
    assert view["state"] == "no_import" and not view["entries"]


def test_detail_does_not_return_text_deleted_during_projection(store, monkeypatch):
    service = JournalService(store)
    revision = service.timeline()["entries"][0]["revision_id"]
    original = service._project

    def racing(row):
        projected = original(row)
        store.forget_source("whoop_export")
        return projected

    monkeypatch.setattr(service, "_project", racing)
    with pytest.raises(ValueError, match="unavailable"):
        service.evidence(revision)


async def test_http_journal_projects_only_mapped_text_and_reuses_session(store):
    runtime = DashboardRuntime(lambda: Store(store.path, clock=lambda: NOW))
    async with client(runtime) as http:
        assert (await http.get("/api/journal")).status_code == 401
        await unlock(http)
        response = await http.get("/api/journal")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "DO_NOT_EXPOSE_SYNTHETIC_IDENTITY" not in response.text
        assert "Synthetic journal key" not in response.text
        entry = response.json()["entries"][0]
        detail = await http.get(f"/api/journal/evidence?revision={entry['revision_id']}")
        assert detail.json()["answers"] == entry["answers"]
        assert "row" not in detail.json() and "payload" not in detail.json()
        api_revision = store.current_sources("recovery")[0]["id"]
        assert (await http.get(f"/api/journal/evidence?revision={api_revision}")).status_code == 404


@pytest.mark.parametrize(
    "path,status",
    [
        ("/api/journal?days=90", 400),
        ("/api/journal?page=-1", 400),
        ("/api/journal?page=501", 400),
        ("/api/journal?path=private", 400),
        ("/api/journal/evidence?revision=0", 404),
    ],
)
async def test_journal_http_bounds(store, path, status):
    async with client(DashboardRuntime(lambda: Store(store.path, clock=lambda: NOW))) as http:
        await unlock(http)
        assert (await http.get(path)).status_code == status
        assert (await http.get(path, headers={"Origin": "https://evil.test"})).status_code == 403
