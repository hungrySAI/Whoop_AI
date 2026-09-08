"""Synthetic schema-mapping tests; actual WHOOP-export interoperability is not_run."""

import copy
import csv
import json
import stat
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from whoop_copilot import exports
from whoop_copilot.contracts import timestamp
from whoop_copilot.exports import inspect_export, parse_export

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/fixtures/export_sample.csv"
EXPORTED_AT = "2026-09-05T08:00:00-07:00"


@pytest.fixture
def profile():
    return json.loads((ROOT / "examples/whoop-export-mapping.example.json").read_text())


@pytest.fixture
def minimal_profile():
    return {
        "version": 1,
        "resource": "physiological_cycles",
        "member": None,
        "identity_columns": ["Synthetic ID"],
        "start": {"column": "Synthetic start", "format": "iso8601"},
        "end": {"column": "Synthetic end", "format": "iso8601"},
        "timezone": "from_timestamp",
        "metrics": [{"column": "Synthetic HRV", "metric": "whoop.hrv_rmssd", "unit": "ms"}],
    }


@pytest.fixture
def row():
    return {
        "Synthetic ID": "synth-1",
        "Synthetic start": "2026-09-01T06:00:00-07:00",
        "Synthetic end": "2026-09-02T06:00:00-07:00",
        "Synthetic HRV": "47",
    }


def write_csv(tmp_path, rows, *, name="sample.csv", encoding="utf-8-sig"):
    path = tmp_path / name
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_zip(tmp_path, members, *, compression=zipfile.ZIP_STORED, name="sample.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for member, content in members:
            archive.writestr(member, content)
    return path


def test_example_preserves_raw_and_explicit_version_basis(profile):
    records = parse_export(SAMPLE, profile, EXPORTED_AT, synthetic=True)
    assert len(records) == 2
    first = records[0]
    assert first.provider == "whoop_export"
    assert first.resource == "physiological_cycles"
    assert first.source_updated_at == timestamp(EXPORTED_AT)
    assert first.metadata["version_time_basis"] == "exported_at_not_provider_modified"
    assert first.payload["synthetic"] is True
    assert first.payload["row"]["Synthetic extra detail"].startswith("Invented headers")
    assert first.payload["export"]["mapping"]["version"] == 1
    assert "member" not in first.payload["export"]["mapping"]
    assert first.activities[0].timezone_offset == "-07:00"
    assert first.activities[0].start_at == timestamp("2026-09-01T13:00:00Z")
    assert {ob.metric: ob.value for ob in first.observations}["whoop.hrv_rmssd"] == 47
    assert all(ob.official for ob in first.observations)
    assert "whoop.hrv_rmssd" not in {ob.metric for ob in records[1].observations}


def test_real_is_default_and_must_be_isolated_by_caller(profile):
    record = parse_export(SAMPLE, profile, EXPORTED_AT)[0]
    assert record.payload["synthetic"] is False
    assert record.metadata["synthetic"] is False
    assert record.provider != "whoop"


def test_inspection_returns_structure_and_no_row_contents(profile):
    result = inspect_export(SAMPLE)
    serialized = json.dumps(result)
    assert "Invented headers" not in serialized
    assert "synthetic-cycle-1" not in serialized
    assert "2026-09-01" not in serialized
    assert result["members"][0]["row_count"] == 2
    assert "Synthetic HRV ms" in result["members"][0]["columns"]
    assert result["members"][0]["recognized_datetime_columns"][0]["needs_timezone"] is False
    assert result["mapping_required"] is True


def test_naive_inspection_diagnoses_need_without_exposing_values(tmp_path, row):
    row["Synthetic start"] = "2026-09-01T06:00:00"
    result = inspect_export(write_csv(tmp_path, [row]))
    columns = {item["column"]: item for item in result["members"][0]["recognized_datetime_columns"]}
    assert columns["Synthetic start"]["needs_timezone"] is True
    assert columns["Synthetic end"]["needs_timezone"] is False


def test_utf8_bom_and_multiline_raw_columns(tmp_path, minimal_profile, row):
    row["Synthetic unmodeled detail"] = "合成内容\n保留全部原文"
    record = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert record.payload["row"]["Synthetic unmodeled detail"] == "合成内容\n保留全部原文"


def test_csv_zip_and_filename_changes_have_identical_content(tmp_path, profile):
    csv_record = parse_export(SAMPLE, profile, EXPORTED_AT, True)
    archive = write_zip(tmp_path, [("first.csv", SAMPLE.read_bytes()), ("README.txt", "Synthetic")])
    profile["member"] = "first.csv"
    first = parse_export(archive, profile, EXPORTED_AT, True)
    assert [asdict(record) for record in first] == [asdict(record) for record in csv_record]
    renamed_archive = write_zip(
        tmp_path, [("renamed.csv", SAMPLE.read_bytes())], name="renamed.zip"
    )
    profile["member"] = "renamed.csv"
    second = parse_export(renamed_archive, profile, EXPORTED_AT, True)
    assert [asdict(record) for record in second] == [asdict(record) for record in first]
    inspected = inspect_export(archive)
    assert inspected["members"][0]["member"] == "first.csv"
    assert inspected["members"][1] == {"member": "README.txt", "format": "unsupported"}


def test_metric_mapping_order_is_not_a_new_revision(profile):
    first = parse_export(SAMPLE, profile, EXPORTED_AT, True)
    profile["metrics"].reverse()
    second = parse_export(SAMPLE, profile, EXPORTED_AT, True)
    assert first == second


def test_same_identity_source_value_correction_is_a_revision(tmp_path, minimal_profile, row):
    path = write_csv(tmp_path, [row])
    old = parse_export(path, minimal_profile, EXPORTED_AT, True)[0]
    row["Synthetic HRV"] = "52"
    new = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert old.external_id == new.external_id
    assert old.payload != new.payload


def test_duplicate_start_with_different_key_is_allowed(tmp_path, minimal_profile, row):
    second = {**row, "Synthetic ID": "synthetic-other"}
    records = parse_export(write_csv(tmp_path, [row, second]), minimal_profile, EXPORTED_AT, True)
    assert records[0].external_id != records[1].external_id


def test_timestamp_correction_preserves_explicit_source_identity(tmp_path, minimal_profile, row):
    first_path = write_csv(tmp_path, [row])
    before = parse_export(first_path, minimal_profile, "2026-09-03T00:00:00Z", synthetic=True)[0]
    row["Synthetic start"] = "2026-09-01T07:00:00-07:00"
    after = parse_export(
        write_csv(tmp_path, [row]), minimal_profile, "2026-09-04T00:00:00Z", synthetic=True
    )[0]
    assert before.external_id == after.external_id
    assert before.observations[0].start_at != after.observations[0].start_at


@pytest.mark.parametrize("resource", ["sleep", "workout"])
def test_sleep_workout_preserve_intervals_and_complex_columns(
    tmp_path, minimal_profile, row, resource
):
    minimal_profile.update(resource=resource, metrics=[])
    row["Synthetic complex stages"] = '{"stage": ["unmodeled", 12]}'
    record = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert record.activities[0].kind == f"whoop_{resource}"
    assert record.activities[0].end_at == timestamp(row["Synthetic end"])
    assert record.payload["row"]["Synthetic complex stages"] == row["Synthetic complex stages"]
    assert not record.observations


def test_journal_is_raw_evidence_only(tmp_path, minimal_profile, row):
    minimal_profile.update(resource="journal", metrics=[], end=None)
    row["Synthetic journal answer"] = "yes"
    record = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert not record.observations
    assert not record.activities
    assert record.payload["row"]["Synthetic journal answer"] == "yes"


@pytest.mark.parametrize(
    "zone,expected",
    [
        ("UTC", "2026-09-01T06:00:00Z"),
        ("+08:00", "2026-08-31T22:00:00Z"),
        ("America/Los_Angeles", "2026-09-01T13:00:00Z"),
    ],
)
def test_explicit_timezone_for_naive_timestamps(tmp_path, minimal_profile, row, zone, expected):
    minimal_profile["timezone"] = zone
    minimal_profile["end"] = None
    row["Synthetic start"] = "2026-09-01T06:00:00"
    record = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert record.observations[0].start_at == timestamp(expected)


@pytest.mark.parametrize(
    "local,message", [("2026-11-01T01:30:00", "Ambiguous"), ("2026-03-08T02:30:00", "Nonexistent")]
)
def test_dst_ambiguity_and_gaps_require_offsets(tmp_path, minimal_profile, row, local, message):
    minimal_profile.update(timezone="America/Los_Angeles", end=None)
    row["Synthetic start"] = local
    with pytest.raises(ValueError, match=message):
        parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)


def test_dst_explicit_offset_is_accepted(tmp_path, minimal_profile, row):
    minimal_profile.update(timezone="America/Los_Angeles", end=None)
    row["Synthetic start"] = "2026-11-01T01:30:00-08:00"
    record = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert record.observations[0].start_at == timestamp("2026-11-01T09:30:00Z")


def test_explicit_strptime_format(tmp_path, minimal_profile, row):
    minimal_profile.update(timezone="+08:00", end=None)
    minimal_profile["start"]["format"] = "%Y/%m/%d %H:%M"
    row["Synthetic start"] = "2026/09/01 06:00"
    record = parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)[0]
    assert record.observations[0].start_at == timestamp("2026-08-31T22:00:00Z")


@pytest.mark.parametrize("bad", [None, "", "Mars/Unknown", "+15:00", "+12:90", 1])
def test_missing_or_invalid_timezone_rejected(profile, bad):
    profile["timezone"] = bad
    with pytest.raises(ValueError, match="[Tt]imezone|offset"):
        parse_export(SAMPLE, profile, EXPORTED_AT, True)


def test_naive_timestamp_does_not_become_utc(tmp_path, minimal_profile, row):
    row["Synthetic start"] = "2026-09-01T06:00:00"
    with pytest.raises(ValueError, match="no offset"):
        parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)


@pytest.mark.parametrize("bad", ["2026-09-05T08:00:00", "not-a-time", None])
def test_exported_at_must_be_explicit(profile, bad):
    with pytest.raises(ValueError, match="exported_at"):
        parse_export(SAMPLE, profile, bad, True)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"version": 2}, "version"),
        ({"version": True}, "version"),
        ({"resource": "unknown"}, "resource"),
        ({"identity_columns": []}, "identity"),
        ({"identity_columns": ["x", "x"]}, "identity"),
        ({"start": {"column": "Synthetic start"}}, "start"),
        ({"start": {"column": "Synthetic start", "format": "%m/%d"}}, "year"),
        ({"guess_headers": True}, "unknown"),
    ],
)
def test_incomplete_and_unknown_mapping_rejected(profile, change, message):
    profile.update(change)
    with pytest.raises(ValueError, match=message):
        parse_export(SAMPLE, profile, EXPORTED_AT, True)


@pytest.mark.parametrize(
    "metric,unit",
    [("whoop.hrv_rmssd", "unknown"), ("whoop.hrv_rmssd", "s"), ("whoop.invented_score", "%")],
)
def test_unknown_metrics_and_units_are_not_guessed(profile, metric, unit):
    profile["metrics"][0].update(metric=metric, unit=unit)
    with pytest.raises(ValueError, match="unit|unsupported"):
        parse_export(SAMPLE, profile, EXPORTED_AT, True)


def test_duplicate_metric_rejected(profile):
    profile["metrics"].append(copy.deepcopy(profile["metrics"][0]))
    with pytest.raises(ValueError, match="duplicate metric"):
        parse_export(SAMPLE, profile, EXPORTED_AT, True)


def test_wrong_member_schema_is_not_silently_accepted(tmp_path, profile):
    archive = write_zip(tmp_path, [("wrong.csv", "unrelated,columns\n1,2\n")])
    profile["member"] = "wrong.csv"
    with pytest.raises(ValueError, match="mapped columns"):
        parse_export(archive, profile, EXPORTED_AT, True)


@pytest.mark.parametrize("member", [None, "missing.csv", "README.txt"])
def test_zip_requires_exact_csv_member(tmp_path, profile, member):
    archive = write_zip(tmp_path, [("actual.csv", SAMPLE.read_bytes()), ("README.txt", "sample")])
    profile["member"] = member
    with pytest.raises(ValueError, match="member"):
        parse_export(archive, profile, EXPORTED_AT, True)


def test_single_csv_rejects_zip_member(profile):
    profile["member"] = "any.csv"
    with pytest.raises(ValueError, match="single CSV"):
        parse_export(SAMPLE, profile, EXPORTED_AT, True)


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "1e9999", "unknown"])
def test_nonfinite_and_nonnumeric_metrics_rejected(tmp_path, minimal_profile, row, bad):
    row["Synthetic HRV"] = bad
    with pytest.raises(ValueError, match="finite|numeric"):
        parse_export(write_csv(tmp_path, [row]), minimal_profile, EXPORTED_AT, True)


@pytest.mark.parametrize("kind", ["duplicate", "empty", "end_before_start", "wrong_time_format"])
def test_invalid_records_are_atomic(tmp_path, minimal_profile, row, kind):
    bad = copy.deepcopy(row)
    if kind == "empty":
        bad["Synthetic ID"] = ""
    elif kind == "end_before_start":
        bad["Synthetic end"] = "2026-08-01T00:00:00Z"
    elif kind == "wrong_time_format":
        bad["Synthetic start"] = "09/01/2026"
    with pytest.raises(ValueError):
        parse_export(write_csv(tmp_path, [row, bad]), minimal_profile, EXPORTED_AT, True)


@pytest.mark.parametrize(
    "raw,message",
    [
        (b"a,a\n1,2\n", "duplicate"),
        (b"a,\n1,2\n", "nonempty"),
        (b"a,b\n1\n", "number of columns"),
        (b"a,b\n1,2,3\n", "number of columns"),
        (b'a,b\n"unclosed,2\n', "syntax"),
        (b"a\n\x00\n", "NUL"),
        (b"\xff", "UTF-8"),
        (b"", "nonempty"),
    ],
)
def test_malformed_csv_inspection_rejected(tmp_path, raw, message):
    path = tmp_path / "bad.csv"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=message):
        inspect_export(path)


def test_maximum_row_count_is_enforced(tmp_path, minimal_profile, row):
    rows = [{**row, "Synthetic ID": f"synthetic-{i}"} for i in range(10_001)]
    path = write_csv(tmp_path, rows)
    with pytest.raises(ValueError, match="10000-row"):
        parse_export(path, minimal_profile, EXPORTED_AT, True)
    with pytest.raises(ValueError, match="10000-row"):
        inspect_export(path)


@pytest.mark.parametrize(
    "member",
    [
        "../escape.csv",
        "/absolute.csv",
        "folder/../escape.csv",
        "C:/escape.csv",
        "folder\\escape.csv",
        "folder//file.csv",
    ],
)
def test_zip_traversal_rejected_even_in_unselected_member(tmp_path, profile, member):
    archive = write_zip(tmp_path, [("safe.csv", SAMPLE.read_bytes()), (member, "x\n1\n")])
    profile["member"] = "safe.csv"
    with pytest.raises(ValueError, match="unsafe member path"):
        parse_export(archive, profile, EXPORTED_AT, True)


def test_zip_symlink_rejected(tmp_path):
    entry = zipfile.ZipInfo("link.csv")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = write_zip(tmp_path, [(entry, "/private/target")])
    with pytest.raises(ValueError, match="symlink"):
        inspect_export(archive)


def test_duplicate_zip_members_rejected(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate"):
        archive = write_zip(tmp_path, [("same.csv", "a\n1\n"), ("same.csv", "a\n2\n")])
    with pytest.raises(ValueError, match="duplicate member"):
        inspect_export(archive)


def test_zip_bomb_compression_ratio_rejected(tmp_path):
    archive = write_zip(
        tmp_path, [("compressed.csv", "a\n" + "x" * 100_000)], compression=zipfile.ZIP_DEFLATED
    )
    with pytest.raises(ValueError, match="compression ratio"):
        inspect_export(archive)


def test_zip_too_many_members_rejected(tmp_path):
    archive = write_zip(tmp_path, [(f"{i}.csv", "a\n1\n") for i in range(33)])
    with pytest.raises(ValueError, match="32 entries"):
        inspect_export(archive)


def test_raw_and_zip_uncompressed_byte_limits(tmp_path, monkeypatch):
    csv_path = tmp_path / "large.csv"
    csv_path.write_bytes(b"a\n" + b"b" * 300)
    monkeypatch.setattr(exports, "MAX_BYTES", 256)
    with pytest.raises(ValueError, match="input limit"):
        inspect_export(csv_path)
    archive = write_zip(
        tmp_path, [("large.csv", "a\n" + "b" * 300)], compression=zipfile.ZIP_DEFLATED
    )
    assert archive.stat().st_size < 256
    with pytest.raises(ValueError, match="uncompressed limit"):
        inspect_export(archive)


def test_invalid_zip_and_unsupported_format(tmp_path):
    path = tmp_path / "invalid.zip"
    path.write_text("synthetic invalid archive")
    with pytest.raises(ValueError, match="valid ZIP"):
        inspect_export(path)
    path = tmp_path / "not-csv.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="CSV or ZIP"):
        inspect_export(path)
