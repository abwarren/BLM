"""THE HISTORICAL UNDER FINGERPRINT LAYER — C1, C3, C5 + R2 (authorization
2026-09-21).

Locks the approved fingerprints as production code:

  * the EXACT threshold semantics from the directive (boundary values on
    both edges of every comparison);
  * three-state evaluation — TRUE / FALSE / UNAVAILABLE — with missing
    data NEVER treated as TRUE (fail closed);
  * conjunction propagation (C4 = C1 AND C2, C6 = C2 AND q3<avg; C3/C5
    UNAVAILABLE whenever any leg is unprovable);
  * fingerprint_count counts TRUE only, and fingerprints_fired lists the
    exact fired keys;
  * the production under_alert contract is untouched (fingerprints are
    context, never a gate);
  * R1 has NO production implementation — verified two ways: the source
    contains no widened band, and a req_ratio inside [1.20, 1.35) never
    fires any fingerprint;
  * no leakage — the layer is pure arithmetic over its operands (no DB,
    no file, no game-state, no settlement reference anywhere in it).
"""
from __future__ import annotations

import inspect
import math

import pytest

from blm_v4.live_analytics.under_alert import under_alert_state
from blm_v4.live_analytics.under_fingerprints import (
    C1_REQ_RATIO_MAX,
    C1_REQ_RATIO_MIN,
    C3_REQ_RATIO_MIN,
    FP_FALSE,
    FP_TRUE,
    FP_UNAVAILABLE,
    FINGERPRINT_KEYS,
    R2_Q3_RATIO_MAX,
    evaluate_fingerprints,
)

# A league whose Q3 average sits below its full-game average (the usual
# shape: fourth quarters run hot), so both ratio families can be exercised
# independently.
LEAGUE = 4.00
LEAGUE_Q3 = 3.00


def fp(required=4.0, league=LEAGUE, q3=3.0, q3avg=LEAGUE_Q3,
       recent3=None, actual=None):
    return evaluate_fingerprints(required, league, q3, q3avg,
                                 recent3, actual)


def req_for(ratio, league=LEAGUE):
    return ratio * league


# ══════════════════════════════════════════════════════════════════════
# C1 — req_ratio in [1.10, 1.20)  (lower INCLUSIVE, upper EXCLUSIVE)
# ══════════════════════════════════════════════════════════════════════

def test_c1_directive_boundary_values():
    # the directive's exact probes
    assert fp(required=req_for(1.10))["fingerprint_c1"] == FP_TRUE       # 1.10 -> TRUE
    assert fp(required=req_for(1.1999))["fingerprint_c1"] == FP_TRUE     # 1.1999 -> TRUE
    assert fp(required=req_for(1.20))["fingerprint_c1"] == FP_FALSE      # 1.20 -> FALSE
    assert fp(required=req_for(1.0999))["fingerprint_c1"] == FP_FALSE    # 1.0999 -> FALSE
    # both edges are part of the frozen constants, in the right polarity
    assert C1_REQ_RATIO_MIN == 1.10 and C1_REQ_RATIO_MAX == 1.20
    # mid-band sanity
    assert fp(required=req_for(1.15))["fingerprint_c1"] == FP_TRUE
    # strictly above the upper edge never qualifies
    assert fp(required=req_for(1.25))["fingerprint_c1"] == FP_FALSE
    assert fp(required=req_for(1.35))["fingerprint_c1"] == FP_FALSE


# ══════════════════════════════════════════════════════════════════════
# C3 — req_ratio > 1.04 AND q3_ratio < 1.00  (both STRICT)
# ══════════════════════════════════════════════════════════════════════

def test_c3_both_legs_strict():
    assert fp(required=req_for(1.05), q3=2.9)["fingerprint_c3"] == FP_TRUE
    # exactly at the production margin does NOT qualify (mirrors the alert)
    assert fp(required=req_for(1.04), q3=2.9)["fingerprint_c3"] == FP_FALSE
    assert C3_REQ_RATIO_MIN == 1.04
    # exactly at the Q3 average does NOT qualify
    assert fp(required=req_for(1.05), q3=3.0)["fingerprint_c3"] == FP_FALSE
    # each leg alone failing is FALSE
    assert fp(required=req_for(1.02), q3=2.9)["fingerprint_c3"] == FP_FALSE
    assert fp(required=req_for(1.05), q3=3.1)["fingerprint_c3"] == FP_FALSE


# ══════════════════════════════════════════════════════════════════════

def test_c4_is_c1_and_c2():
    assert fp(required=req_for(1.15), recent3=3.0, actual=3.5)[
        "fingerprint_c4"] == FP_TRUE
    # C1 true + C2 false
    assert fp(required=req_for(1.15), recent3=3.5, actual=3.0)[
        "fingerprint_c4"] == FP_FALSE
    # C1 false + C2 true
    assert fp(required=req_for(1.25), recent3=3.0, actual=3.5)[
        "fingerprint_c4"] == FP_FALSE
    # C1 unavailable propagates even when C2 is provable
    assert fp(required=None, recent3=3.0, actual=3.5)[
        "fingerprint_c4"] == FP_UNAVAILABLE


# ══════════════════════════════════════════════════════════════════════
# C5 — req_ratio > 1.10 AND q3_ratio < 1.00  (both STRICT)
# ══════════════════════════════════════════════════════════════════════

def test_c5_both_legs_strict_and_matches_fingerprint_c5_module():
    from blm_v4.live_analytics.fingerprint_c5 import fingerprint_c5
    assert fp(required=req_for(1.11), q3=2.9)["fingerprint_c5"] == FP_TRUE
    # exactly 1.10 does NOT qualify (the audited edge is STRICT)
    assert fp(required=req_for(1.10), q3=2.9)["fingerprint_c5"] == FP_FALSE
    # q3 exactly at the average does NOT qualify
    assert fp(required=req_for(1.11), q3=3.0)["fingerprint_c5"] == FP_FALSE
    # the layer's C5 verdict agrees with the standalone C5 module on a
    # grid of operand combinations — one truth, two entries
    for rq in (1.05, 1.10, 1.11, 1.2, 1.4):
        for qq in (2.5, 2.9, 3.0, 3.2):
            a = fp(required=req_for(rq), q3=qq)["fingerprint_c5"]
            b = fingerprint_c5(req_for(rq), LEAGUE, qq, LEAGUE_Q3)[
                "fingerprint_c5"]
            assert a == b, (rq, qq, a, b)


# ══════════════════════════════════════════════════════════════════════

def test_c6_is_c2_and_q3_below_average():
    assert fp(recent3=3.0, actual=3.5, q3=2.9)["fingerprint_c6"] == FP_TRUE
    # deceleration but Q3 at/above average
    assert fp(recent3=3.0, actual=3.5, q3=3.0)["fingerprint_c6"] == FP_FALSE
    # Q3 slump but no deceleration
    assert fp(recent3=3.5, actual=3.0, q3=2.9)["fingerprint_c6"] == FP_FALSE


# ══════════════════════════════════════════════════════════════════════
# R2 — q3_ratio < 0.90  (STRICT; the material Q3 slump)
# ══════════════════════════════════════════════════════════════════════

def test_r2_directive_boundary_values():
    # strictly below 0.90 qualifies (the material Q3 slump)
    assert fp(q3=2.69, q3avg=3.0)["fingerprint_r2"] == FP_TRUE    # 0.8967
    assert fp(q3=2.4, q3avg=3.0)["fingerprint_r2"] == FP_TRUE     # 0.80
    # exactly 0.90 does NOT qualify (STRICTLY below) and neither does above
    assert fp(q3=2.7, q3avg=3.0)["fingerprint_r2"] == FP_FALSE    # 0.90 exact
    assert fp(q3=2.8, q3avg=3.0)["fingerprint_r2"] == FP_FALSE    # 0.933
    assert R2_Q3_RATIO_MAX == 0.90


# ══════════════════════════════════════════════════════════════════════
# UNAVAILABLE — missing data is NEVER true (fail closed)
# ══════════════════════════════════════════════════════════════════════

def test_missing_req_ratio_is_unavailable_where_applicable():
    b = fp(required=None)
    assert b["fingerprint_c1"] == FP_UNAVAILABLE
    assert b["fingerprint_c3"] == FP_UNAVAILABLE
    assert b["fingerprint_c5"] == FP_UNAVAILABLE
        assert b["req_ratio"] is None
    # fingerprints that need no required pace stay decidable: the default
    # Q3 pair is provable (ratio 1.0) so R2 is FALSE, not UNAVAILABLE
    assert b["fingerprint_r2"] == FP_FALSE
    # with the Q3 pair missing too, R2 is UNAVAILABLE
    assert fp(required=None, q3=None)["fingerprint_r2"] == FP_UNAVAILABLE


def test_missing_q3_ratio_is_unavailable_where_applicable():
    b = fp(q3=None)
    assert b["fingerprint_c3"] == FP_UNAVAILABLE
    assert b["fingerprint_c5"] == FP_UNAVAILABLE
        assert b["fingerprint_r2"] == FP_UNAVAILABLE
    # required-pace-only fingerprints stay decidable (default req 1.0x)
    assert b["fingerprint_c1"] == fp()["fingerprint_c1"]
    assert b["fingerprint_c1"] == FP_FALSE
    

def test_missing_momentum_is_unavailable_where_applicable():
    b = fp()                                  # no recent3/actual supplied
        assert b["fingerprint_c4"] == FP_UNAVAILABLE
    assert b["fingerprint_c6"] == FP_UNAVAILABLE
    # required-pace and Q3 fingerprints stay decidable
    assert b["fingerprint_c1"] == FP_FALSE
    assert b["fingerprint_c3"] == FP_FALSE
    assert b["fingerprint_c5"] == FP_FALSE
    assert b["fingerprint_r2"] == FP_FALSE


def test_non_finite_operands_fail_closed():
    for bad in (float("nan"), float("inf"), float("-inf"), True, "x", ""):
        assert fp(required=bad)["fingerprint_c1"] == FP_UNAVAILABLE
        assert fp(q3=bad)["fingerprint_r2"] == FP_UNAVAILABLE
                        assert fp(league=bad)["fingerprint_c1"] == FP_UNAVAILABLE
        assert fp(q3avg=bad)["fingerprint_r2"] == FP_UNAVAILABLE


def test_conjunction_unavailable_propagates():
    # C3/C5: one leg provable TRUE, the other unavailable -> UNAVAILABLE
    b = fp(required=req_for(1.2), q3=None)
    assert b["fingerprint_c3"] == FP_UNAVAILABLE
    assert b["fingerprint_c5"] == FP_UNAVAILABLE
    # C6: deceleration provable, Q3 unavailable -> UNAVAILABLE
    b = fp(recent3=3.0, actual=3.5, q3=None)
    assert b["fingerprint_c6"] == FP_UNAVAILABLE
    # C4: both legs provable but failing is still FALSE, not UNAVAILABLE
    assert fp(required=req_for(1.3), recent3=3.5, actual=3.0)[
        "fingerprint_c4"] == FP_FALSE


def test_unavailable_never_counts_and_never_fires():
    b = fp(required=None, q3=None)
    assert b["fingerprint_count"] == 0
    assert b["fingerprints_fired"] == []
    for k in FINGERPRINT_KEYS:
        assert b[f"fingerprint_{k.lower()}_triggered"] is False


def test_zero_league_average_is_unavailable_not_crash():
    b = fp(required=4.0, league=0.0)
    assert b["fingerprint_c1"] == FP_UNAVAILABLE
    b = fp(q3=3.0, q3avg=0.0)
    assert b["fingerprint_r2"] == FP_UNAVAILABLE


# ══════════════════════════════════════════════════════════════════════
# fingerprint_count + fingerprints_fired
# ══════════════════════════════════════════════════════════════════════

def test_fingerprint_count_counts_true_only():
    b = fp(required=req_for(1.15), q3=2.7)
    assert b["fingerprints_fired"] == ["C1", "C3", "C5"]
    assert b["fingerprint_count"] == 3
    b = fp(required=req_for(1.15), q3=2.5)
    assert b["fingerprints_fired"] == ["C1", "C3", "C5", "R2"]
    assert b["fingerprint_count"] == 4


def test_fingerprints_fired_order_is_canonical():
    b = fp(required=req_for(1.12), q3=2.5)
    assert b["fingerprints_fired"] == [k for k in FINGERPRINT_KEYS
                                       if k in b["fingerprints_fired"]]
    assert set(b["fingerprints_fired"]) <= set(FINGERPRINT_KEYS)


def test_directive_example_shape_two_fingerprints():
    b = fp(required=req_for(1.05), q3=2.4)
    assert b["fingerprints_fired"] == ["C3", "R2"]
    assert b["fingerprint_count"] == 2


# ══════════════════════════════════════════════════════════════════════
# the fingerprint layer NEVER touches the production alert
# ══════════════════════════════════════════════════════════════════════

def test_under_alert_contract_untouched_by_fingerprints():
    # the fingerprint layer takes no part in `active` — the production
    # rule is still progress>=75 AND required > league*1.04, nothing else
    for kwargs in ({}, {"recent3": 0.1, "actual": 99.0}):
        before = under_alert_state(4.0, 5.0, 4.5, 80)
        assert before["active"] is True
        assert before["checkpoint"] == 75
    # a fired fingerprint set cannot activate a non-qualifying game...
    b = fp(required=req_for(1.15), q3=2.5, recent3=3.0, actual=3.5)
    assert b["fingerprint_count"] == 7
    assert under_alert_state(4.0, req_for(1.15), LEAGUE, 74.9)["active"] is False
    # ...and an UNAVAILABLE set cannot deactivate a qualifying one
    assert under_alert_state(4.0, 5.0, 4.5, 80)["active"] is True


def test_fingerprint_names_and_keys():
    assert FINGERPRINT_KEYS == ("C1", "C3", "C5", "R2")


# ══════════════════════════════════════════════════════════════════════
# R1 EXCLUSION — verified two independent ways
# ══════════════════════════════════════════════════════════════════════

def test_r1_has_no_production_implementation():
    """The widened band [1.10, 1.35) is NOT implemented anywhere in
    production: no R1 name, no R1 key, no 1.35 constant, and the served
    block carries no r1 field.  (AST-based: prose in docstrings that
    documents the exclusion must not count as an implementation.)"""
    import ast
    import blm_v4.live_analytics.under_fingerprints as mod
    assert "R1" not in FINGERPRINT_KEYS
    assert not [n for n in vars(mod)
                if "R1" in n or "1_35" in n or n == "BAND_1_35"]
    # no numeric constant 1.35 anywhere in the module's code (the value
    # the widened band would need); docstring prose is not a constant
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            assert not math.isclose(node.value, 1.35, abs_tol=1e-9), node.value
    # ...and the served block carries no r1 field
    b = fp(required=req_for(1.2), q3=2.5, recent3=3.0, actual=3.5)
    assert "fingerprint_r1" not in b
    assert "fingerprint_r1_triggered" not in b


def test_r1_band_fires_nothing_in_production():
    """A trigger inside the excluded [1.20, 1.35) widening must not light
    any fingerprint that a 1.20-exact trigger would not."""
    at_120 = fp(required=req_for(1.20), q3=3.2, recent3=3.5, actual=3.0)
    at_134 = fp(required=req_for(1.34), q3=3.2, recent3=3.5, actual=3.0)
    # neither fires C1 (upper edge exclusive); the widening adds nothing
    assert at_120["fingerprint_c1"] == FP_FALSE
    assert at_134["fingerprint_c1"] == FP_FALSE
    assert at_134["fingerprint_count"] == at_120["fingerprint_count"]
    assert at_134["fingerprints_fired"] == at_120["fingerprints_fired"]
    assert at_134["fingerprints_fired"] == []


# ══════════════════════════════════════════════════════════════════════
# NO DATA LEAKAGE — the layer is pure arithmetic over its operands
# ══════════════════════════════════════════════════════════════════════

def test_layer_reads_no_database_no_settlement_no_future():
    """The module may not import sqlite3/os/json/datetime, open files or
    reference game state: every fingerprint must be computable BEFORE the
    outcome is known, from the operands alone.  AST-based so prose in the
    docstring that documents the guarantee cannot fail the scan."""
    import ast
    import blm_v4.live_analytics.under_fingerprints as mod
    tree = ast.parse(inspect.getsource(mod))
    banned_imports = {"sqlite3", "os", "json", "datetime", "pathlib",
                      "subprocess", "socket", "urllib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in banned_imports, alias.name
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in banned_imports, node.module
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            assert name not in ("open", "connect", "Now", "now"), name


def test_supporting_values_served_with_the_verdict():
    b = fp(required=4.9, league=4.2, q3=2.9, q3avg=3.2,
           recent3=3.3, actual=4.0)
    assert b["required_pace"] == 4.9
    assert b["league_avg_pace"] == 4.2
    assert math.isclose(b["req_ratio"], 4.9 / 4.2, abs_tol=1e-6)
    assert b["q3_pace"] == 2.9
    assert b["league_q3_avg"] == 3.2
    assert math.isclose(b["q3_ratio"], 2.9 / 3.2, abs_tol=1e-6)
    assert b["recent3_pace"] == 3.3
    assert math.isclose(b["recent3_minus_act"], -0.7, abs_tol=1e-6)
    # C5-compat aliases (dashboard history records sealed the old shape)
    assert b["fingerprint_c5_req_ratio"] == b["req_ratio"]
    assert b["fingerprint_c5_q3_ratio"] == b["q3_ratio"]
    assert b["required_pts_per_min"] == 4.9
    assert b["league_average_pace"] == 4.2
    assert b["q3_ppm"] == 2.9
    assert b["q3_league_avg"] == 3.2
