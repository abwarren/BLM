"""Auto-Bet RUNG rule — canonical formulation + automated cases (2026-10-03).

Directive: "Add the canonical PokerBet rung definition to the Auto-Bet RAG
documentation."  Normative spec: docs/rag/13_AUTOBET_RUNG.md.

This module is the REFERENCE IMPLEMENTATION of the rung rule and pins it with
one named test per required case (0 rungs, +1, +multiple, -1, -2, ambiguous),
plus two requirement pins: the rule is expressed in RUNGS (not a fixed point
difference) and the manual and autonomous paths share ONE calculation.

It does NOT touch production.  Wiring the gate into
``blm_v4/betting/executor.py::evaluate`` is a production behaviour change that
requires its own authorization (see 13_AUTOBET_RUNG.md Rule 13.7); the operands
it would use already exist there (``market["total_line"]`` vs ``ua["trigger_line"]``).

Pure functions only — no DB, no network, no config, no provider.
"""
from __future__ import annotations

import math
from typing import Optional

import pytest

#: The inclusive boundary, expressed in RUNGS.  NOT a point difference.
RUNG_ALLOW_MIN = -1.0
#: Float tolerance so an intended exact -1 rung is not rejected by binary rounding.
EPS = 1e-9


def _f(value) -> Optional[float]:
    """Finite float, else None (booleans excluded) — the codebase's guard shape."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def infer_rung_size(observed_lines) -> Optional[float]:
    """The market's rung (tick) size DERIVED from observed line values.

    Never a global constant.  Fail closed (None) whenever the size is not
    provable from the data:

      * fewer than 3 distinct values (< 2 increments)  -> None (AMBIGUOUS)
      * fewer than 2 positive increments               -> None (AMBIGUOUS)
      * increments not exact multiples of the tick     -> None (INCONSISTENT)
      * tick <= 0                                      -> None

    The tick is the smallest positive increment; it is accepted only when every
    observed increment is an exact integer multiple of it.
    """
    vals = sorted({round(float(v), 6) for v in (observed_lines or ())
                   if _f(v) is not None})
    if len(vals) < 3:
        return None
    steps = [round(vals[i + 1] - vals[i], 6) for i in range(len(vals) - 1)]
    steps = [s for s in steps if s > 0]
    if len(steps) < 2:
        return None
    tick = min(steps)
    if tick <= 0:
        return None
    for s in steps:
        k = round(s / tick)
        if k < 1 or abs(s - k * tick) > 1e-6:
            return None
    return tick


def rungs_moved(current_line, trigger_line, rung_size) -> Optional[float]:
    """Signed count of rungs the live line has moved from the frozen trigger.

        rungs_moved = (current_line - trigger_line) / rung_size

    None whenever any operand is unprovable (fail closed) — never a guess.
    """
    cur, trig, rung = _f(current_line), _f(trigger_line), _f(rung_size)
    if cur is None or trig is None or rung is None or rung <= 0:
        return None
    moved = (cur - trig) / rung
    return moved if math.isfinite(moved) else None


def under_rung_decision(current_line, trigger_line, rung_size) -> dict:
    """The canonical UNDER execution decision, expressed in RUNGS.

        rungs_moved >= -1  -> ALLOW
        rungs_moved <  -1  -> REJECT

    Ambiguous / unprovable rung size fails CLOSED (REJECT).  Upward movement is
    unrestricted; zero movement is allowed; exactly one rung downward is allowed.

    Returns ``{"decision": "ALLOW"|"REJECT", "reason": str, "rungs_moved": float|None}``.
    """
    moved = rungs_moved(current_line, trigger_line, rung_size)
    if moved is None:
        return {"decision": "REJECT", "reason": "rung_ambiguous",
                "rungs_moved": None}
    if moved >= RUNG_ALLOW_MIN - EPS:
        return {"decision": "ALLOW", "reason": "within_one_rung_down",
                "rungs_moved": round(moved, 6)}
    return {"decision": "REJECT", "reason": "more_than_one_rung_down",
            "rungs_moved": round(moved, 6)}


# ── manual vs autonomous: ONE calculation, two callers ────────────────────
# The directive requires the SAME rung calculation for manual and autonomous
# execution.  Both wrappers delegate to ``under_rung_decision`` — the single
# canonical function — so neither can drift.
def manual_execution_decision(current_line, trigger_line, rung_size) -> dict:
    return under_rung_decision(current_line, trigger_line, rung_size)


def autonomous_execution_decision(current_line, trigger_line, rung_size) -> dict:
    return under_rung_decision(current_line, trigger_line, rung_size)


# ── required cases (one named test each) ──────────────────────────────────
TRIGGER = 205.5
RUNG = 0.5   # EXAMPLE ONLY — not universal.


def test_zero_rungs_allow():
    r = under_rung_decision(TRIGGER, TRIGGER, RUNG)
    assert r["rungs_moved"] == 0.0
    assert r["decision"] == "ALLOW"


def test_plus_one_rung_allow():
    r = under_rung_decision(TRIGGER + 1 * RUNG, TRIGGER, RUNG)
    assert r["rungs_moved"] == 1.0
    assert r["decision"] == "ALLOW"


def test_plus_multiple_rungs_allow():
    r = under_rung_decision(TRIGGER + 3 * RUNG, TRIGGER, RUNG)
    assert r["rungs_moved"] == 3.0
    assert r["decision"] == "ALLOW"
    # upward movement is unrestricted
    r10 = under_rung_decision(TRIGGER + 10 * RUNG, TRIGGER, RUNG)
    assert r10["decision"] == "ALLOW"


def test_minus_one_rung_allow():
    r = under_rung_decision(TRIGGER - 1 * RUNG, TRIGGER, RUNG)
    assert r["rungs_moved"] == -1.0
    assert r["decision"] == "ALLOW"     # inclusive boundary


def test_minus_two_rungs_reject():
    r = under_rung_decision(TRIGGER - 2 * RUNG, TRIGGER, RUNG)
    assert r["rungs_moved"] == -2.0
    assert r["decision"] == "REJECT"
    assert r["reason"] == "more_than_one_rung_down"


@pytest.mark.parametrize("bad_rung", [None, 0, 0.0, -0.5, float("nan"),
                                      float("inf"), float("-inf")])
def test_invalid_or_ambiguous_rung_size_rejects(bad_rung):
    r = under_rung_decision(TRIGGER, TRIGGER, bad_rung)
    assert r["decision"] == "REJECT"
    assert r["reason"] == "rung_ambiguous"
    assert r["rungs_moved"] is None


def test_just_beyond_one_rung_down_rejects_and_exactly_one_allows():
    # -1.5 rungs is more than one rung down -> REJECT
    r = under_rung_decision(TRIGGER - 1.5 * RUNG, TRIGGER, RUNG)
    assert r["rungs_moved"] == -1.5
    assert r["decision"] == "REJECT"
    # a hair beyond -1.0 (beyond float tolerance) -> REJECT
    r2 = under_rung_decision(TRIGGER - RUNG - 1e-6, TRIGGER, RUNG)
    assert r2["decision"] == "REJECT"


# ── requirement pin: expressed in RUNGS, not a fixed point difference ─────
def test_rule_is_expressed_in_rungs_not_points():
    """The SAME point move must give DIFFERENT decisions under different rung
    sizes — proving the rule is not a fixed point threshold like -1.0."""
    diff = -1.5                      # identical move away from the trigger, in points
    coarse = under_rung_decision(TRIGGER + diff, TRIGGER, 0.5)   # -3.0 rungs
    fine = under_rung_decision(TRIGGER + diff, TRIGGER, 2.0)     # -0.75 rungs
    assert coarse["rungs_moved"] == -3.0 and coarse["decision"] == "REJECT"
    assert fine["rungs_moved"] == -0.75 and fine["decision"] == "ALLOW"
    assert coarse["decision"] != fine["decision"]


# ── requirement pin: manual == autonomous share ONE calculation ───────────
@pytest.mark.parametrize("current", [TRIGGER, TRIGGER + RUNG, TRIGGER + 3 * RUNG,
                                     TRIGGER - RUNG, TRIGGER - 2 * RUNG])
def test_manual_and_autonomous_use_the_same_calculation(current):
    assert (manual_execution_decision(current, TRIGGER, RUNG)
            == autonomous_execution_decision(current, TRIGGER, RUNG)
            == under_rung_decision(current, TRIGGER, RUNG))


# ── rung-size derivation from the live market ─────────────────────────────
def test_infer_rung_size_from_market_tick():
    assert infer_rung_size([205.5, 205.0, 204.5]) == 0.5
    assert infer_rung_size([206, 205, 204, 202]) == 1.0
    assert infer_rung_size([205.5, 205.25, 205.0]) == 0.25


@pytest.mark.parametrize("series", [
    [], [205.5], [205.5, 205.5],        # < 3 distinct values -> AMBIGUOUS
    [205.5, 208.0],                      # single increment -> AMBIGUOUS
    [205.5, 208.0, 210.0],               # steps 2.5 & 2.0 -> INCONSISTENT
])
def test_infer_rung_size_ambiguous_fails_closed(series):
    assert infer_rung_size(series) is None


def test_ambiguous_market_rung_blocks_execution():
    """End-to-end: an unprovable rung size REJECTs even at zero movement."""
    rung = infer_rung_size([205.5, 208.0])          # ambiguous -> None
    r = under_rung_decision(TRIGGER, TRIGGER, rung)
    assert rung is None
    assert r["decision"] == "REJECT"
    assert r["reason"] == "rung_ambiguous"


def test_example_0_5_is_not_hard_coded():
    """A market whose tick is 1.0 must be judged on 1.0, not the 0.5 example."""
    # one rung down at tick 1.0 -> ALLOW
    assert under_rung_decision(204.5, 205.5, 1.0)["decision"] == "ALLOW"
    # the same absolute move at tick 0.5 is two rungs down -> REJECT
    assert under_rung_decision(204.5, 205.5, 0.5)["decision"] == "REJECT"
