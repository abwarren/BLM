"""The NAMED trade cohorts (TC-*) — one named test per cohort + the resolver.

Operator directive 2026-10-23: every alert carries the name of the cohort it
belongs to, and the name must never drift from the legs it claims.  These tests
pin both: the registry values and the resolve/refuse behaviour of the one
resolver every consumer uses.
"""
import math

import pytest

from blm_v4 import trade_cohorts as tc
from blm_v4.trade_window import (TRADE_MAX_PROGRESS_PCT, TRADE_MIN_PROGRESS_PCT,
                                 TRADE_MIN_SCORED_POINTS)

BASE = dict(progress_pct=80.0, total_points=140.0, avg_pace=2.0, actual_pace=2.0)


def legs(**over):
    """A game that satisfies TC-W by default; `over` moves one input."""
    d = dict(BASE)
    d.update(over)
    return tc.resolve_cohort(
        progress_pct=d["progress_pct"], total_points=d["total_points"],
        required_pace=d["required_pace"], avg_pace=d["avg_pace"],
        actual_pace=d["actual_pace"], under_price=d.get("under_price", 1.70),
        competition_slug=d.get("competition_slug"))


# ── one test per cohort: the legs are what the name claims ──────────────
def test_tc_w_wide_is_the_live_gate():
    assert tc.COHORTS["TC-W"]["pace"] == 1.00
    assert tc.COHORTS["TC-W"]["shortfall"] == -0.50
    assert tc.COHORTS["TC-W"]["price"] == 1.60
    assert tc.COHORTS["TC-W"]["name"] == "WIDE"


def test_tc_c_core_is_the_104_pace_bar():
    assert tc.COHORTS["TC-C"]["pace"] == 1.04
    assert tc.COHORTS["TC-C"]["name"] == "CORE"


def test_tc_s_sharp_is_the_108_pace_bar():
    assert tc.COHORTS["TC-S"]["pace"] == 1.08
    assert tc.COHORTS["TC-S"]["name"] == "SHARP"


def test_tc_e_edge_is_the_112_pace_bar():
    assert tc.COHORTS["TC-E"]["pace"] == 1.12
    assert tc.COHORTS["TC-E"]["name"] == "EDGE"


def test_tc_x_elite_is_the_116_pace_bar():
    assert tc.COHORTS["TC-X"]["pace"] == 1.16
    assert tc.COHORTS["TC-X"]["name"] == "ELITE"


def test_tc_d_deep_is_the_shortfall_route():
    assert tc.COHORTS["TC-D"]["pace"] == 1.00
    assert tc.COHORTS["TC-D"]["shortfall"] == 0.50
    assert tc.COHORTS["TC-D"]["name"] == "DEEP"


def test_tc_c_plus_is_pace_shortfall_and_price():
    c = tc.COHORTS["TC-C+"]
    assert (c["pace"], c["shortfall"], c["price"]) == (1.04, 0.25, 1.80)
    assert c["name"] == "CORE+"


# ── shared legs come from the gate, never retyped ───────────────────────
def test_shared_legs_are_the_trade_window_constants():
    for code in tc.COHORT_ORDER:
        lg = tc.cohort_legs(code)
        assert lg["progress_min"] == TRADE_MIN_PROGRESS_PCT
        assert lg["progress_max"] == TRADE_MAX_PROGRESS_PCT
        assert lg["total_points_min"] == TRADE_MIN_SCORED_POINTS


def test_registry_order_matches_the_nesting():
    # every code in COHORTS appears exactly once in COHORT_ORDER
    assert sorted(tc.COHORT_ORDER) == sorted(tc.COHORTS)


# ── the resolver ────────────────────────────────────────────────────────
def test_tightest_cohort_wins_and_all_holding_are_listed():
    # req 2.5 / avg 2.0 = 1.25 ratio -> clears TC-W..TC-E (1.16) but not 1.20+
    r = legs(required_pace=2.5, actual_pace=2.0)
    assert r["code"] == "TC-X"
    assert r["name"] == "ELITE"
    assert r["codes"][0] == "TC-X" and "TC-W" in r["codes"]


def test_only_the_widest_holds_at_the_floor():
    r = legs(required_pace=2.02)          # 1.01 ratio -> TC-W only
    assert r["code"] == "TC-W"
    assert r["codes"] == ["TC-W"]


def test_no_cohort_when_pace_is_at_or_below_one():
    assert legs(required_pace=2.00)["code"] is None      # exactly 1.00 is refused
    assert legs(required_pace=1.90)["code"] is None


def test_no_cohort_outside_the_progress_band():
    assert tc.resolve_cohort(progress_pct=74.9, total_points=140.0, required_pace=3.0,
                             avg_pace=2.0, actual_pace=2.0, under_price=1.9)["code"] is None
    assert tc.resolve_cohort(progress_pct=92.1, total_points=140.0, required_pace=3.0,
                             avg_pace=2.0, actual_pace=2.0, under_price=1.9)["code"] is None


def test_no_cohort_below_the_points_floor():
    r = tc.resolve_cohort(progress_pct=80.0, total_points=69.0, required_pace=3.0,
                          avg_pace=2.0, actual_pace=2.0, under_price=1.9)
    assert r["code"] is None


def test_price_floor_refuses_a_short_price():
    assert legs(required_pace=3.0, under_price=1.59)["code"] is None
    assert legs(required_pace=3.0, under_price=1.60)["code"] == "TC-X"


def test_excluded_competition_is_never_named():
    r = legs(required_pace=3.0, competition_slug="cyber-basketball-2k26-matches")
    assert r["code"] is None and r["excluded"] is True


def test_missing_inputs_fail_closed():
    for field in ("progress_pct", "total_points", "required_pace", "actual_pace"):
        r = tc.resolve_cohort(progress_pct=None if field == "progress_pct" else 80.0,
                              total_points=None if field == "total_points" else 140.0,
                              required_pace=None if field == "required_pace" else 3.0,
                              avg_pace=2.0,
                              actual_pace=None if field == "actual_pace" else 2.0,
                              under_price=1.9)
        assert r["code"] is None, f"{field} absent must not be named"
    assert tc.resolve_cohort(progress_pct=80.0, total_points=140.0, required_pace=3.0,
                             avg_pace=None, actual_pace=2.0, under_price=1.9)["code"] is None
    assert tc.resolve_cohort(progress_pct=80.0, total_points=140.0, required_pace=3.0,
                             avg_pace=0.0, actual_pace=2.0, under_price=1.9)["code"] is None
    assert tc.resolve_cohort(progress_pct=float("nan"), total_points=140.0,
                             required_pace=3.0, avg_pace=2.0, actual_pace=2.0,
                             under_price=1.9)["code"] is None


def test_tc_d_can_hold_without_any_pace_lead():
    # shortfall route: ratio 1.01 (TC-W only on pace) but req-act = 1.0 -> TC-D too
    r = legs(required_pace=2.02, actual_pace=1.02)
    assert "TC-D" in r["codes"]
    assert r["code"] == "TC-W"          # tighter-by-order pace cohort first


def test_label_helper():
    assert tc.label("TC-X") == "TC-X \u00b7 ELITE"
    assert tc.label(None) == "\u2014"
    assert tc.label("nope") == "\u2014"
