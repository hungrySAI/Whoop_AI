import csv
import json
from pathlib import Path

import pytest

from whoop_copilot.adapters import parse_csv, parse_whoop
from whoop_copilot.contracts import timestamp

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def bundle():
    return json.loads((FIXTURES / "synthetic_whoop.json").read_text())


def write_bundle(tmp_path, bundle):
    path = tmp_path / "whoop.json"
    path.write_text(json.dumps(bundle))
    return path


@pytest.fixture
def body_row():
    return {
        "synthetic": "true",
        "external_id": "synthetic-one",
        "metric": "body.weight",
        "measured_at": "2026-08-01T06:00:00-07:00",
        "updated_at": "2026-08-01T06:01:00-07:00",
        "value": "150",
        "unit": "lb",
    }


def write_csv(tmp_path, rows):
    path = tmp_path / "body.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_whoop_preserves_official_scores_and_cycle_evidence(bundle):
    records = parse_whoop(FIXTURES / "synthetic_whoop.json")
    assert len(records) == 14
    first = records[0]
    by_metric = {observation.metric: observation for observation in first.observations}
    assert by_metric["whoop.hrv_rmssd"].value == 40
    assert by_metric["whoop.recovery_score"].value == 60
    assert by_metric["whoop.resting_heart_rate"].value == 62
    assert by_metric["whoop.hrv_rmssd"].unit == "ms"
    assert by_metric["whoop.recovery_score"].unit == "%"
    assert by_metric["whoop.resting_heart_rate"].unit == "bpm"
    assert by_metric["whoop.spo2"].unit == "%"
    assert by_metric["whoop.skin_temp"].unit == "degC"
    assert all(observation.official for observation in first.observations)
    assert first.provider == "whoop"
    assert first.resource == "recovery"
    assert first.external_id == str(bundle["recoveries"][0]["cycle_id"])
    assert first.metadata == {
        "synthetic": True,
        "whoop_user_id": "999999",
        "source_version_parts": {
            "recovery": timestamp(bundle["recoveries"][0]["updated_at"]),
            "cycle": timestamp(bundle["cycles"][0]["updated_at"]),
        },
    }
    assert first.payload["recovery"] == bundle["recoveries"][0]
    assert first.payload["cycle"] == bundle["cycles"][0]
    assert first.activities[0].kind == "whoop_cycle"
    assert first.activities[0].timezone_offset == "-07:00"


def test_measurement_uses_cycle_interval_not_recovery_update(tmp_path, bundle):
    bundle["recoveries"][0]["updated_at"] = "2026-09-01T00:00:00Z"
    record = parse_whoop(write_bundle(tmp_path, bundle))[0]
    observation = record.observations[0]
    assert observation.start_at == timestamp(bundle["cycles"][0]["start"])
    assert observation.end_at == timestamp(bundle["cycles"][0]["end"])
    assert observation.time_precision == "interval"
    assert record.source_updated_at == timestamp("2026-09-01T00:00:00Z")


def test_linked_cycle_revision_changes_payload_and_source_time(tmp_path, bundle):
    before = parse_whoop(write_bundle(tmp_path, bundle))[0]
    bundle["cycles"][0]["timezone_offset"] = "+08:00"
    bundle["cycles"][0]["updated_at"] = "2026-09-02T00:00:00Z"
    after = parse_whoop(write_bundle(tmp_path, bundle))[0]
    assert after.payload != before.payload
    assert after.source_updated_at > before.source_updated_at
    assert after.activities[0].timezone_offset == "+08:00"


@pytest.mark.parametrize("state", ["PENDING_SCORE", "UNSCORABLE"])
def test_unscored_recovery_does_not_produce_observations(tmp_path, bundle, state):
    bundle["recoveries"][0]["score_state"] = state
    records = parse_whoop(write_bundle(tmp_path, bundle))
    assert records[0].observations == ()
    assert records[0].activities
    assert records[0].payload["recovery"]["score_state"] == state


def test_calibrating_observations_are_marked_for_default_exclusion(tmp_path, bundle):
    bundle["recoveries"][0]["score"]["user_calibrating"] = True
    records = parse_whoop(write_bundle(tmp_path, bundle))
    assert records[0].observations
    assert all(observation.quality == "calibrating" for observation in records[0].observations)


def test_null_scores_are_not_zero_and_official_zero_is_preserved(tmp_path, bundle):
    bundle["recoveries"][0]["score"]["hrv_rmssd_milli"] = None
    bundle["recoveries"][0]["score"]["spo2_percentage"] = None
    bundle["recoveries"][0]["score"]["recovery_score"] = 0
    bundle["recoveries"][1]["score"] = None
    records = parse_whoop(write_bundle(tmp_path, bundle))
    by_metric = {observation.metric: observation for observation in records[0].observations}
    assert "whoop.hrv_rmssd" not in by_metric
    assert "whoop.spo2" not in by_metric
    assert by_metric["whoop.recovery_score"].value == 0
    assert records[1].observations == ()


def test_open_cycle_retains_missing_end_without_inventing_a_duration(tmp_path, bundle):
    bundle["cycles"][0]["end"] = None
    record = parse_whoop(write_bundle(tmp_path, bundle))[0]
    assert record.activities[0].end_at is None
    assert all(observation.end_at is None for observation in record.observations)


@pytest.mark.parametrize("value", [None, False, "true", 1])
def test_whoop_requires_explicit_synthetic_boolean(tmp_path, bundle, value):
    bundle["synthetic"] = value
    with pytest.raises(ValueError, match="synthetic"):
        parse_whoop(write_bundle(tmp_path, bundle))


@pytest.mark.parametrize("collection", ["cycles", "recoveries"])
def test_whoop_rejects_duplicate_source_ids(tmp_path, bundle, collection):
    bundle[collection].append(bundle[collection][0].copy())
    with pytest.raises(ValueError, match="Duplicate"):
        parse_whoop(write_bundle(tmp_path, bundle))


def test_whoop_rejects_missing_cycle(tmp_path, bundle):
    bundle["cycles"].pop(0)
    with pytest.raises(ValueError, match="missing WHOOP cycle"):
        parse_whoop(write_bundle(tmp_path, bundle))


def test_whoop_rejects_mixed_users_even_when_each_pair_matches(tmp_path, bundle):
    bundle["cycles"][0]["user_id"] = 111111
    bundle["recoveries"][0]["user_id"] = 111111
    with pytest.raises(ValueError, match="mix multiple external users"):
        parse_whoop(write_bundle(tmp_path, bundle))


def test_whoop_rejects_recovery_cycle_user_mismatch(tmp_path, bundle):
    bundle["recoveries"][0]["user_id"] = 111111
    with pytest.raises(ValueError, match="different WHOOP users"):
        parse_whoop(write_bundle(tmp_path, bundle))


@pytest.mark.parametrize(
    "collection,field",
    [
        ("cycles", "start"),
        ("cycles", "end"),
        ("cycles", "updated_at"),
        ("recoveries", "updated_at"),
        ("recoveries", "created_at"),
    ],
)
def test_whoop_rejects_timestamps_without_timezone(tmp_path, bundle, collection, field):
    bundle[collection][0][field] = "2026-08-01T06:00:00"
    with pytest.raises(ValueError, match="timezone offset"):
        parse_whoop(write_bundle(tmp_path, bundle))


@pytest.mark.parametrize("offset", ["UTC", "+99:00", "-07:99", "+14:01", None])
def test_whoop_rejects_invalid_timezone_offset(tmp_path, bundle, offset):
    bundle["cycles"][0]["timezone_offset"] = offset
    with pytest.raises(ValueError, match="timezone_offset"):
        parse_whoop(write_bundle(tmp_path, bundle))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "43"])
def test_whoop_rejects_invalid_observation_values(tmp_path, bundle, value):
    bundle["recoveries"][0]["score"]["hrv_rmssd_milli"] = value
    with pytest.raises(ValueError, match="numeric|finite"):
        parse_whoop(write_bundle(tmp_path, bundle))


def test_whoop_rejects_nonfinite_values_in_unnormalized_source_fields(tmp_path, bundle):
    bundle["cycles"][0]["score"]["strain"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        parse_whoop(write_bundle(tmp_path, bundle))


def test_whoop_rejects_reversed_cycle_interval(tmp_path, bundle):
    bundle["cycles"][0]["end"] = bundle["cycles"][0]["start"]
    with pytest.raises(ValueError, match="after cycle.start"):
        parse_whoop(write_bundle(tmp_path, bundle))


def test_body_fixture_has_14_days_and_converts_to_kg():
    records = parse_csv(FIXTURES / "synthetic_body.csv")
    assert len(records) == 14
    assert records[0].observations[0].value == 75
    assert records[1].observations[0].value == pytest.approx(74.9)
    assert records[-1].observations[0].value == pytest.approx(73.7)
    assert all(
        record.provider == "manual" and record.resource == "body_metric" for record in records
    )


def test_body_unit_conversion_preserves_original_value_and_provenance(tmp_path, body_row):
    record = parse_csv(write_csv(tmp_path, [body_row]))[0]
    observation = record.observations[0]
    assert observation.metric == "body.weight"
    assert observation.value == pytest.approx(68.0388555)
    assert observation.unit == "kg"
    assert observation.original_value == 150
    assert observation.original_unit == "lb"
    assert observation.start_at == timestamp(body_row["measured_at"])
    assert observation.time_precision == "instant"
    assert observation.official is False
    assert record.payload == {"synthetic": True, "row": body_row}
    assert record.metadata == {"synthetic": True}


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("synthetic", "false", "synthetic"),
        ("synthetic", "", "synthetic"),
        ("external_id", "", "external_id"),
        ("metric", "body.height", "metric"),
        ("unit", "stone", "unit"),
        ("unit", "", "unit"),
        ("value", "nan", "finite"),
        ("value", "inf", "finite"),
        ("value", "-inf", "finite"),
        ("value", "0", "positive"),
        ("value", "-10", "positive"),
        ("measured_at", "2026-08-01T06:00:00", "timezone offset"),
        ("updated_at", "2026-08-01", "timezone offset"),
    ],
)
def test_body_csv_rejects_invalid_values(tmp_path, body_row, field, value, error):
    body_row[field] = value
    with pytest.raises(ValueError, match=error):
        parse_csv(write_csv(tmp_path, [body_row]))


def test_body_csv_rejects_duplicate_identifiers(tmp_path, body_row):
    with pytest.raises(ValueError, match="duplicate external_id"):
        parse_csv(write_csv(tmp_path, [body_row, body_row]))


def test_body_csv_rejects_missing_columns(tmp_path, body_row):
    del body_row["synthetic"]
    with pytest.raises(ValueError, match="requires columns"):
        parse_csv(write_csv(tmp_path, [body_row]))
