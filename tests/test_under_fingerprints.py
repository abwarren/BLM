"""Historical UNDER fingerprint tests — C1, C3, C5 + R2.

C2 and its dependent composites C4/C6 are deliberately absent from the
live fingerprint layer. Fingerprints remain enrichment only and never
create or gate production alerts.
"""
from __future__ import annotations

import inspect
import math

from blm_v4.live_analytics.under_alert import under_alert_state
from blm_v4.live_analytics.under_fingerprints import (
    C1_REQ_RATIO_MAX, C1_REQ_RATIO_MIN, C3_REQ_RATIO_MIN,
    FP_FALSE, FP_TRUE, FP_UNAVAILABLE, FINGERPRINT_KEYS,
    R2_Q3_RATIO_MAX, evaluate_fingerprints,
)
from blm_v4.live_analytics.fingerprint_c5 import fingerprint_c5

LEAGUE = 4.00
LEAGUE_Q3 = 3.00


def fp(required=4.0, league=LEAGUE, q3=3.0, q3avg=LEAGUE_Q3,
       recent3=None, actual=None):
    # Legacy momentum arguments remain accepted by the function but are
    # intentionally ignored after C2 removal.
    return evaluate_fingerprints(required, league, q3, q3avg,
                                 recent3, actual)


def req_for(ratio, league=LEAGUE):
    return ratio * league


def test_c1_directive_boundary_values():
    assert fp(required=req_for(1.10))["fingerprint_c1"] == FP_TRUE
    assert fp(required=req_for(1.1999))["fingerprint_c1"] == FP_TRUE
    assert fp(required=req_for(1.20))["fingerprint_c1"] == FP_FALSE
    assert fp(required=req_for(1.0999))["fingerprint_c1"] == FP_FALSE
    assert C1_REQ_RATIO_MIN == 1.10
    assert C1_REQ_RATIO_MAX == 1.20


def test_c3_both_legs_strict():
    assert fp(required=req_for(1.05), q3=2.9)["fingerprint_c3"] == FP_TRUE
    assert fp(required=req_for(1.04), q3=2.9)["fingerprint_c3"] == FP_FALSE
    assert C3_REQ_RATIO_MIN == 1.04
    assert fp(required=req_for(1.05), q3=3.0)["fingerprint_c3"] == FP_FALSE


def test_c5_matches_standalone_module():
    for rq in (1.05, 1.10, 1.11, 1.2, 1.4):
        for qq in (2.5, 2.9, 3.0, 3.2):
            a = fp(required=req_for(rq), q3=qq)["fingerprint_c5"]
            b = fingerprint_c5(req_for(rq), LEAGUE, qq, LEAGUE_Q3)["fingerprint_c5"]
            assert a == b


def test_r2_directive_boundary_values():
    assert fp(q3=2.69)["fingerprint_r2"] == FP_TRUE
    assert fp(q3=2.4)["fingerprint_r2"] == FP_TRUE
    assert fp(q3=2.7)["fingerprint_r2"] == FP_FALSE
    assert fp(q3=2.8)["fingerprint_r2"] == FP_FALSE
    assert R2_Q3_RATIO_MAX == 0.90


def test_missing_data_fails_closed():
    b = fp(required=None)
    assert b["fingerprint_c1"] == FP_UNAVAILABLE
    assert b["fingerprint_c3"] == FP_UNAVAILABLE
    assert b["fingerprint_c5"] == FP_UNAVAILABLE
    assert b["fingerprint_r2"] == FP_FALSE

    b = fp(q3=None)
    assert b["fingerprint_c3"] == FP_UNAVAILABLE
    assert b["fingerprint_c5"] == FP_UNAVAILABLE
    assert b["fingerprint_r2"] == FP_UNAVAILABLE

    b = fp(required=None, q3=None)
    assert b["fingerprint_count"] == 0
    assert b["fingerprints_fired"] == []


def test_non_finite_operands_fail_closed():
    for bad in (float("nan"), float("inf"), float("-inf"), True, "x", ""):
        assert fp(required=bad)["fingerprint_c1"] == FP_UNAVAILABLE
        assert fp(q3=bad)["fingerprint_r2"] == FP_UNAVAILABLE
        assert fp(league=bad)["fingerprint_c1"] == FP_UNAVAILABLE
        assert fp(q3avg=bad)["fingerprint_r2"] == FP_UNAVAILABLE


def test_zero_reference_is_unavailable():
    assert fp(required=4.0, league=0.0)["fingerprint_c1"] == FP_UNAVAILABLE
    assert fp(q3=3.0, q3avg=0.0)["fingerprint_r2"] == FP_UNAVAILABLE


def test_fingerprint_count_and_order():
    b = fp(required=req_for(1.15), q3=2.7)
    assert b["fingerprints_fired"] == ["C1", "C3", "C5"]
    assert b["fingerprint_count"] == 3

    b = fp(required=req_for(1.15), q3=2.5)
    assert b["fingerprints_fired"] == ["C1", "C3", "C5", "R2"]
    assert b["fingerprint_count"] == 4


def test_directive_example_without_c2():
    b = fp(required=req_for(1.05), q3=2.4)
    assert b["fingerprints_fired"] == ["C3", "R2"]
    assert b["fingerprint_count"] == 2


def test_fingerprint_keys_exclude_c2_and_dependents():
    assert FINGERPRINT_KEYS == ("C1", "C3", "C5", "R2")
    b = fp(required=req_for(1.15), q3=2.5, recent3=3.0, actual=3.5)
    assert not any(k in b["fingerprints_fired"] for k in ("C2", "C4", "C6"))
    assert "fingerprint_c2" not in b
    assert "fingerprint_c4" not in b
    assert "fingerprint_c6" not in b


def test_legacy_momentum_arguments_do_not_change_result():
    plain = fp(required=req_for(1.15), q3=2.5)
    decel = fp(required=req_for(1.15), q3=2.5, recent3=3.0, actual=3.5)
    assert plain["fingerprints_fired"] == decel["fingerprints_fired"]
    assert plain["fingerprint_count"] == decel["fingerprint_count"]


def test_under_alert_contract_untouched():
    b = fp(required=req_for(1.15), q3=2.5, recent3=3.0, actual=3.5)
    assert b["fingerprint_count"] == 4
    assert under_alert_state(4.0, req_for(1.15), LEAGUE, 74.9)["active"] is False
    assert under_alert_state(4.0, 5.0, 4.5, 80)["active"] is True


def test_r1_has_no_production_implementation():
    import ast
    import blm_v4.live_analytics.under_fingerprints as mod
    assert "R1" not in FINGERPRINT_KEYS
    assert not [n for n in vars(mod) if "R1" in n or "1_35" in n or n == "BAND_1_35"]
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            assert not math.isclose(node.value, 1.35, abs_tol=1e-9)


def test_r1_band_fires_nothing():
    at_120 = fp(required=req_for(1.20), q3=3.2)
    at_134 = fp(required=req_for(1.34), q3=3.2)
    assert at_120["fingerprint_c1"] == FP_FALSE
    assert at_134["fingerprint_c1"] == FP_FALSE
    assert at_134["fingerprint_count"] == at_120["fingerprint_count"]


def test_layer_reads_no_database_or_settlement():
    import ast
    import blm_v4.live_analytics.under_fingerprints as mod
    tree = ast.parse(inspect.getsource(mod))
    banned = {"sqlite3", "os", "json", "datetime", "pathlib", "subprocess", "socket", "urllib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name not in ("open", "connect", "Now", "now")


def test_supporting_values_served():
    b = fp(required=4.9, league=4.2, q3=2.9, q3avg=3.2,
           recent3=3.3, actual=4.0)
    assert b["required_pace"] == 4.9
    assert b["league_avg_pace"] == 4.2
    assert math.isclose(b["req_ratio"], 4.9 / 4.2, abs_tol=1e-6)
    assert b["q3_pace"] == 2.9
    assert b["league_q3_avg"] == 3.2
    assert math.isclose(b["q3_ratio"], 2.9 / 3.2, abs_tol=1e-6)
