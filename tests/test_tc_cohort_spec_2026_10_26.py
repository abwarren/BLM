"""TC cohort spec + evaluator — boundary matrix (strategy doc §3, §8.1).

One named test per requirement, all pure (no DB, no clock, no network).

Run:  python -m pytest tests/test_tc_cohort_spec_2026_10_26.py -q
"""
from __future__ import annotations

import math
import random

import pytest

from blm_v4.cohorts import evaluator as ev
from blm_v4.cohorts import spec


def inputs(**over):
    """A fully-valid baseline that qualifies EVERY cohort (ratio 1.25, sf +1.0)."""
    base = dict(
        progress_pct=80.0,
        scored_points=150,
        competition_slug="betual-euroleague",
        required_pts_per_min=5.0,
        actual_pts_per_min=4.0,
        league_avg_pts_per_min=4.0,
        under_price=1.80,
        line_move_enabled=False,
    )
    base.update(over)
    return base


# ── spec integrity ────────────────────────────────────────────────────────

def test_spec_has_the_seven_frozen_cohorts():
    assert spec.all_codes() == ("TC-X", "TC-E", "TC-S", "TC-C", "TC-W", "TC-D", "TC-C+")
    assert spec.PRECEDENCE == ("TC-X", "TC-E", "TC-S", "TC-C+", "TC-C", "TC-D", "TC-W")
    assert spec.RULE_VERSION == "tc-1.0.0"


def test_manifest_is_complete_and_stable():
    m = spec.spec_manifest()
    assert m["rule_version"] == spec.RULE_VERSION
    assert [c["code"] for c in m["cohorts"]] == list(spec.QUALIFYING_ORDER)
    assert m["common_gates"]["min_progress_pct"] == 75.0
    assert m["common_gates"]["max_progress_pct"] == 92.0
    assert m["common_gates"]["min_scored_points"] == 70
    assert m["common_gates"]["line_move_required"] is False


def test_unknown_rule_version_is_refused():
    with pytest.raises(ValueError):
        ev.evaluate(inputs(), rule_version="tc-9.9.9")


# ── baseline + all-membership behaviour ───────────────────────────────────

def test_baseline_qualifies_all_seven_and_reports_tightest():
    e = ev.evaluate(inputs())
    assert e.status == ev.STATUS_QUALIFIED
    assert set(e.qualifying_cohorts) == set(spec.all_codes())
    assert e.tightest_cohort == "TC-X"
    assert e.rule_version == spec.RULE_VERSION
    assert e.pace_ratio == pytest.approx(1.25)
    assert e.shortfall == pytest.approx(1.0)


def test_qualifying_order_is_documented_order_not_dict_order():
    e = ev.evaluate(inputs())
    assert list(e.qualifying_cohorts) == list(spec.QUALIFYING_ORDER)


def test_evaluation_does_not_depend_on_input_mapping_order():
    base = inputs()
    items = list(base.items())
    first = ev.evaluate(dict(items)).as_dict()
    for seed in range(5):
        random.Random(seed).shuffle(items)
        assert ev.evaluate(dict(items)).as_dict() == first


# ── per-cohort leg boundaries (one parametrized matrix) ──────────────────

@pytest.mark.parametrize("code", list(spec.COHORT_RULES))
def test_pace_ratio_exactly_at_threshold_fails(code):
    rule = spec.COHORT_RULES[code]
    # league = 1.0 makes the ratio exactly the float threshold (no division drift)
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0,
                           required_pts_per_min=rule.pace_ratio,
                           actual_pts_per_min=0.0,
                           under_price=2.0))
    assert code not in e.qualifying_cohorts


@pytest.mark.parametrize("code", list(spec.COHORT_RULES))
def test_pace_ratio_just_above_threshold_passes(code):
    rule = spec.COHORT_RULES[code]
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0,
                           required_pts_per_min=rule.pace_ratio + 1e-9,
                           actual_pts_per_min=0.0,
                           under_price=2.0))
    assert code in e.qualifying_cohorts


@pytest.mark.parametrize("code", list(spec.COHORT_RULES))
def test_shortfall_exactly_at_minimum_passes(code):
    rule = spec.COHORT_RULES[code]
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0,
                           required_pts_per_min=2.0,
                           actual_pts_per_min=2.0 - rule.shortfall,
                           under_price=2.0))
    assert code in e.qualifying_cohorts


@pytest.mark.parametrize("code", list(spec.COHORT_RULES))
def test_shortfall_just_below_minimum_fails(code):
    rule = spec.COHORT_RULES[code]
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0,
                           required_pts_per_min=2.0,
                           actual_pts_per_min=2.0 - rule.shortfall + 1e-9,
                           under_price=2.0))
    assert code not in e.qualifying_cohorts


@pytest.mark.parametrize("code", list(spec.COHORT_RULES))
def test_price_exactly_at_floor_passes(code):
    rule = spec.COHORT_RULES[code]
    e = ev.evaluate(inputs(under_price=rule.min_under_price))
    assert code in e.qualifying_cohorts


@pytest.mark.parametrize("code", list(spec.COHORT_RULES))
def test_price_just_below_floor_fails(code):
    rule = spec.COHORT_RULES[code]
    e = ev.evaluate(inputs(under_price=rule.min_under_price - 1e-9))
    assert code not in e.qualifying_cohorts


# ── common gates ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("progress", [75.0, 80.0, 92.0])
def test_progress_inside_band_passes(progress):
    assert ev.evaluate(inputs(progress_pct=progress)).status == ev.STATUS_QUALIFIED


@pytest.mark.parametrize("progress", [74.999999, 92.000001, 0.0, 100.0])
def test_progress_outside_band_is_not_qualified(progress):
    e = ev.evaluate(inputs(progress_pct=progress))
    assert e.status == ev.STATUS_NOT_QUALIFIED
    assert ev.R_PROGRESS_OUT_OF_BAND in e.reason_codes


def test_points_exactly_seventy_passes():
    assert ev.evaluate(inputs(scored_points=70)).status == ev.STATUS_QUALIFIED


def test_points_sixty_nine_fails():
    e = ev.evaluate(inputs(scored_points=69))
    assert e.status == ev.STATUS_NOT_QUALIFIED and ev.R_POINTS_BELOW_MIN in e.reason_codes


def test_cyber_feed_is_excluded():
    e = ev.evaluate(inputs(competition_slug="cyber-basketball-2k26-matches"))
    assert e.status == ev.STATUS_NOT_QUALIFIED
    assert ev.R_EXCLUDED_COMPETITION in e.reason_codes


def test_line_move_off_passes():
    assert ev.evaluate(inputs(line_move_enabled=False)).status == ev.STATUS_QUALIFIED


def test_line_move_on_does_not_silently_pass():
    e = ev.evaluate(inputs(line_move_enabled=True))
    assert e.status == ev.STATUS_NOT_QUALIFIED
    assert ev.R_LINE_MOVE_ENABLED in e.reason_codes


def test_line_move_unknown_is_unknown_never_a_pass():
    for bad in (None, "off", 0):
        e = ev.evaluate(inputs(line_move_enabled=bad))
        assert e.status == ev.STATUS_UNKNOWN, bad
        assert e.reason_codes == (ev.R_LINE_MOVE_UNKNOWN,)


# ── UNKNOWN vs NOT_QUALIFIED (never conflated) ───────────────────────────

@pytest.mark.parametrize("field,code", [
    ("progress_pct", ev.R_MISSING_PROGRESS),
    ("scored_points", ev.R_MISSING_POINTS),
    ("competition_slug", ev.R_MISSING_COMPETITION),
    ("required_pts_per_min", ev.R_MISSING_REQUIRED_PACE),
    ("actual_pts_per_min", ev.R_MISSING_ACTUAL_PACE),
    ("league_avg_pts_per_min", ev.R_MISSING_LEAGUE_PACE),
    ("under_price", ev.R_MISSING_PRICE),
])
def test_missing_operand_is_unknown_with_reason(field, code):
    e = ev.evaluate(inputs(**{field: None}))
    assert e.status == ev.STATUS_UNKNOWN
    assert e.reason_codes == (code,)
    assert e.qualifying_cohorts == () and e.tightest_cohort is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc", {}, []])
def test_malformed_operand_is_never_a_pass(bad):
    e = ev.evaluate(inputs(under_price=bad))
    assert e.status == ev.STATUS_UNKNOWN
    assert e.reason_codes in ((ev.R_MISSING_PRICE,), (ev.R_INVALID_PRICE,))


@pytest.mark.parametrize("field,code", [
    ("required_pts_per_min", ev.R_INVALID_REQUIRED_PACE),
    ("actual_pts_per_min", ev.R_INVALID_ACTUAL_PACE),
    ("league_avg_pts_per_min", ev.R_INVALID_LEAGUE_PACE),
    ("under_price", ev.R_INVALID_PRICE),
])
def test_negative_or_zero_operand_is_unknown(field, code):
    value = 0.0 if field == "league_avg_pts_per_min" else -1.0
    e = ev.evaluate(inputs(**{field: value}))
    assert e.status == ev.STATUS_UNKNOWN
    assert code in e.reason_codes


def test_boolean_is_not_accepted_as_a_number():
    e = ev.evaluate(inputs(scored_points=True))
    assert e.status == ev.STATUS_UNKNOWN


# ── progress unit normalization ──────────────────────────────────────────

def test_progress_fraction_is_normalized_once():
    e = ev.evaluate({"progress": 0.80, **{k: v for k, v in inputs().items()
                                          if k != "progress_pct"}}, progress_units="fraction")
    assert e.status == ev.STATUS_QUALIFIED and e.progress_pct == pytest.approx(80.0)


def test_progress_units_are_never_mixed_silently():
    # 0.8 passed as PERCENT is out of band (not silently read as 80%)
    e = ev.evaluate(inputs(progress_pct=0.8))
    assert e.status == ev.STATUS_NOT_QUALIFIED
    assert ev.R_PROGRESS_OUT_OF_BAND in e.reason_codes


# ── overlap / precedence specifics ───────────────────────────────────────

def test_core_plus_outranks_core_when_both_match():
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0, required_pts_per_min=1.05,
                           actual_pts_per_min=0.75, under_price=1.85))
    # ratio 1.05 (>1.04), shortfall 0.30 (>=0.25), price 1.85 (>=1.80)
    assert e.tightest_cohort == "TC-C+"
    assert "TC-C" in e.qualifying_cohorts


def test_deep_is_a_side_route_not_a_pace_rung():
    # ratio 1.01 only clears WIDE's pace, but shortfall 0.60 clears DEEP
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0, required_pts_per_min=1.01,
                           actual_pts_per_min=0.41, under_price=1.70))
    assert e.qualifying_cohorts == ("TC-D", "TC-W")
    assert e.tightest_cohort == "TC-D"


def test_no_cohort_match_is_not_qualified_without_reasons():
    e = ev.evaluate(inputs(league_avg_pts_per_min=1.0, required_pts_per_min=0.99,
                           actual_pts_per_min=1.5, under_price=1.70))
    assert e.status == ev.STATUS_NOT_QUALIFIED
    assert e.qualifying_cohorts == () and e.reason_codes == ()
    assert set(e.cohort_checks) == set(spec.COHORT_RULES)


def test_cohort_checks_expose_each_leg():
    e = ev.evaluate(inputs())
    for code, legs in e.cohort_checks.items():
        assert set(legs) == {"pace", "shortfall", "price"}
        assert all(v is True for v in legs.values()), code


def test_metrics_are_full_precision_not_rounded():
    e = ev.evaluate(inputs(league_avg_pts_per_min=3.0, required_pts_per_min=4.123456789))
    assert e.pace_ratio == pytest.approx(4.123456789 / 3.0, rel=0, abs=0)
    assert math.isclose(e.pace_ratio, 1.3744855963, rel_tol=1e-9)
