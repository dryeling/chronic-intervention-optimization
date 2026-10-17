"""Synthetic fixtures are test-only and never contribute to business reports."""

import hashlib

import pandas as pd
import pytest

from scripts import analyze


@pytest.fixture
def loaded_sample(tmp_path):
    source = tmp_path / "test_only_interventions.csv"
    pd.DataFrame({
        "intervention_id": ["test-negative", "test-zero", "test-15min", "test-30min"],
        "created_at": ["2026-01-01T08:30:00+08:00"] * 4,
        "device_synced_at": [
            "2026-01-01T00:31:00Z", "2026-01-01T08:30:00+08:00",
            "2026-01-01T00:15:00Z", "2026-01-01T00:00:00Z",
        ],
        "center_id": ["test-center"] * 4,
        "plan_type": ["test-plan"] * 4,
        "buffer_days": [3] * 4,
        "msg_count": [6] * 4,
        "result_ok": ["0", "1", "", " "],
    }, columns=analyze.EXPECTED_COLUMNS).to_csv(source, index=False)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    raw, data, parsed = analyze.load_data(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    return raw, data, parsed


@pytest.mark.parametrize("values, expected", [
    (["0", "1", pd.NA], (1, 1, 1, 0, 2)),
    ([pd.NA, pd.NA], (0, 0, 2, 0, 0)),
    (["0", "1", "bad", "2", pd.NA], (1, 1, 1, 2, 2)),
])
def test_failure_rate_denominator_uses_only_zero_and_one(values, expected):
    outcomes = pd.DataFrame({"result_ok": pd.Series(values, dtype="string")})
    assert analyze.outcome_counts(outcomes) == expected
    metrics = analyze.evaluate_rule("test-only", pd.Series(True, index=outcomes.index), outcomes)
    assert metrics["hit_returned"] == expected[4]
    assert metrics["hit_failed"] == expected[1]
    assert metrics["hit_missing"] == expected[2]
    assert metrics["hit_invalid"] == expected[3]
    if expected[4]:
        assert metrics["hit_rate"] == pytest.approx(expected[1] / expected[4])
    else:
        assert pd.isna(metrics["hit_rate"])
    assert metrics["verification_cost"] == len(values) * 10


def test_loading_preserves_missing_outcomes(loaded_sample):
    raw, data, parsed = loaded_sample
    assert len(raw) == len(data) == len(parsed) == 4
    assert data["result_ok"].isna().sum() == parsed["result_ok"].isna().sum() == 2
    assert analyze.outcome_counts(data) == (1, 1, 2, 0, 2)


def test_grouped_rates_exclude_missing_and_conserve_counts():
    index = [10, 20, 30, 40, 50]
    buckets = pd.DataFrame({"group": ["A", "A", "A", "B", "B"]}, index=index)
    outcomes = pd.Series([0, 1, pd.NA, 0, 1], index=index, dtype="Float64")
    summary = analyze.summarize_groups(buckets, outcomes, ["group"]).set_index("group")
    assert summary["total"].sum() == 5
    assert summary["returned"].sum() == 4
    assert summary["missing"].sum() == 1
    assert summary.loc["A", ["total", "returned", "failed", "missing"]].tolist() == [3, 2, 1, 1]
    assert summary.loc["B", ["total", "returned", "failed", "missing"]].tolist() == [2, 2, 1, 0]
    assert summary["failure_rate"].tolist() == pytest.approx([0.5, 0.5])


def test_rule_input_whitelist_excludes_outcome():
    assert set(analyze.RULE_INPUT_COLUMNS) == {
        "created_at", "device_synced_at", "center_id", "plan_type", "buffer_days", "msg_count",
    }
    assert "result_ok" not in analyze.RULE_INPUT_COLUMNS


@pytest.mark.parametrize("rule, arguments", [
    (analyze.rule_candidates, ()),
    (analyze.budget_rule_mask, (3, 15)),
])
@pytest.mark.parametrize("replace_feature", [False, True])
def test_rules_reject_outcome_input(loaded_sample, rule, arguments, replace_feature):
    _, _, parsed = loaded_sample
    features = parsed[analyze.RULE_INPUT_COLUMNS].copy()
    if replace_feature:
        features = features.drop(columns="msg_count")
    features["result_ok"] = parsed["result_ok"]
    with pytest.raises(ValueError, match="only the six allowed"):
        rule(features, *arguments)


def test_rule_hits_do_not_change_when_outcomes_change(loaded_sample):
    _, _, parsed = loaded_sample
    changed = parsed.copy()
    changed["result_ok"] = [1, 0, 1, 0]
    original_hits = analyze.rule_candidates(parsed[analyze.RULE_INPUT_COLUMNS])
    changed_hits = analyze.rule_candidates(changed[analyze.RULE_INPUT_COLUMNS])
    for name in original_hits:
        pd.testing.assert_series_equal(original_hits[name], changed_hits[name])
    pd.testing.assert_series_equal(
        analyze.budget_rule_mask(parsed[analyze.RULE_INPUT_COLUMNS], 3, 15),
        analyze.budget_rule_mask(changed[analyze.RULE_INPUT_COLUMNS], 3, 15),
    )


def test_negative_lag_is_retained_and_flagged(loaded_sample):
    _, data, parsed = loaded_sample
    assert parsed["data_lag_hours"].tolist() == pytest.approx([-1 / 60, 0, 0.25, 0.5])
    buckets = analyze.bucket_features(parsed[["data_lag_hours", "buffer_days", "msg_count"]])
    assert buckets.loc[0, "lag_bucket"] == "negative"
    assert list(analyze.minute_lag_buckets(parsed["data_lag_hours"]))[0] == "negative"
    summary = analyze.summarize_groups(buckets, parsed["result_ok"], ["lag_bucket"])
    negative = summary.loc[summary["lag_bucket"].eq("negative")].iloc[0]
    assert negative["total"] == negative["returned"] == negative["failed"] == 1
    assert summary["total"].sum() == len(data)
    rules = analyze.rule_candidates(parsed[analyze.RULE_INPUT_COLUMNS])
    assert not rules["prior_combination"].iloc[0]
    assert rules["buffer_2_3"].iloc[0]
    assert analyze.budget_rule_mask(parsed[analyze.RULE_INPUT_COLUMNS], 3, 0).tolist() == [False, True, True, True]


@pytest.mark.parametrize("lag, expected", [
    (-1 / 60, "negative"), (0, "0-5min"), (5 / 60, "5-15min"),
    (15 / 60, "15-30min"), (0.5, "30-60min"), (1, "1-2h"),
    (2, "2-6h"), (6, "6-24h"), (24, ">=24h"), (float("nan"), "missing"),
])
def test_minute_lag_boundaries(lag, expected):
    assert list(analyze.minute_lag_buckets(pd.Series([lag]))) == [expected]


def test_budget_selector_adds_no_work_without_known_failures():
    candidates = pd.DataFrame([
        {"rule": "none", "kind": "none", "hits": 0, "hit_returned": 0, "hit_failed": 0, "failure_coverage": float("nan")},
        {"rule": "test-threshold", "kind": "threshold", "hits": 31, "hit_returned": 30, "hit_failed": 0, "failure_coverage": float("nan")},
        {"rule": "all", "kind": "all", "hits": 1000, "hit_returned": 900, "hit_failed": 0, "failure_coverage": float("nan")},
    ])
    selected = analyze.select_budget_plans(candidates, 1000)
    assert selected["plan"].tolist() == ["A", "B", "C"]
    assert selected["hits"].eq(0).all()
