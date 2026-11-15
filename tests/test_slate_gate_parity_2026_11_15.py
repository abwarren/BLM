"""Slate ratio ≡ gate pace leg (2026-11-15).

The slate publishes ``ratio``; the auto-bet PACE leg decides on the same
quantity.  They must be the SAME definition — ``required_pts_per_min ÷
league_avg_pace`` — or the board can disagree with what the engine enforces.

``trade_pace_ratio`` is that single definition (the gate's frozen rule computes
it inline; this pins the equivalence over a grid).  Actual pace is deliberately
absent: it belongs to Gap / the (now disabled) shortfall leg, and to the
display-only Act÷Avg column.

One test per requirement.
"""
import inspect

import pytest

from blm_v4 import trade_window as TW
from blm_v4.trade_window import (PACE_BELOW, exec_alert_reason,
                                 trade_pace_ratio)


RATIOS = [None, 0.0, 0.5, 0.9999, 1.0, 1.04, 1.0799, 1.08, 1.0801,
          1.12, 1.16, 1.5, 2.0, 5.0]
PROGRESS = [None, 0.0, 69.9, 70.0, 75.0, 80.0, 92.0, 99.0]


def test_ratio_is_required_over_league_average():
    """The ratio is required ÷ league-average — never actual ÷ anything."""
    assert trade_pace_ratio(4.32, 4.0) == pytest.approx(1.08)
    assert trade_pace_ratio(5.0, 4.0) == pytest.approx(1.25)
    # actual pace is not an input at all
    assert "actual" not in inspect.signature(trade_pace_ratio).parameters


def test_unprovable_inputs_yield_no_ratio():
    """Missing / zero / non-positive / non-finite inputs are unprovable."""
    assert trade_pace_ratio(None, 4.0) is None
    assert trade_pace_ratio(4.0, None) is None
    assert trade_pace_ratio("n/a", 4.0) is None
    assert trade_pace_ratio(4.0, 0) is None
    assert trade_pace_ratio(4.0, -1) is None
    assert trade_pace_ratio(float("nan"), 4.0) is None
    assert trade_pace_ratio(float("inf"), 4.0) is None


def test_the_gate_clears_exactly_when_the_ratio_clears_it():
    """Grid equivalence: PACE leg passes ⇔ ratio > threshold (strict) AND the
    progress floor is met.  No value may round-trip differently in the two."""
    avg = 4.0
    for r in RATIOS:
        req = None if r is None else r * avg
        for pct in PROGRESS:
            leg = exec_alert_reason(req, avg, pct)
            ratio = trade_pace_ratio(req, avg)
            expected_pass = (ratio is not None
                             and ratio > TW.TRADE_MIN_PACE_RATIO
                             and pct is not None
                             and pct >= TW.TRADE_MIN_PROGRESS_PCT)
            assert (leg is None) is expected_pass, (r, pct, ratio, leg)
            if not expected_pass:
                assert leg == PACE_BELOW


def test_the_strict_boundary_is_shared():
    """Exactly at the threshold is a REFUSAL for both sides."""
    avg = 4.0
    boundary = TW.TRADE_MIN_PACE_RATIO * avg
    assert trade_pace_ratio(boundary, avg) == pytest.approx(
        TW.TRADE_MIN_PACE_RATIO)
    assert exec_alert_reason(boundary, avg, 80.0) == PACE_BELOW
    assert exec_alert_reason(boundary + 1e-6, avg, 80.0) is None


def test_actual_pace_cannot_change_the_ratio():
    """A game running far above or below its required rate gets the SAME ratio;
    only Gap/shortfall see actual pace.  This is the confusion the slate labels."""
    req, avg = 4.5, 4.0
    assert trade_pace_ratio(req, avg) == pytest.approx(1.125)
    # the ratio never receives actual pace, so no actual value can move it
    assert list(inspect.signature(trade_pace_ratio).parameters) == [
        "required_pace", "avg_pace"]


def test_the_slate_reads_the_gate_ratio_not_its_own_formula():
    """slate.build_slate must consume the shared definition."""
    import blm_v4.betting.slate as slate
    src = inspect.getsource(slate.build_slate)
    assert "TW.trade_pace_ratio(" in src
    assert "float(req) / float(avg)" not in src


def test_the_published_gate_threshold_is_the_enforced_one():
    """The slate's PACE chip and the leg read the same constant, not a copy."""
    import blm_v4.betting.slate as slate
    gate_src = inspect.getsource(slate.build_slate)
    assert '"pace_ratio": TW.TRADE_MIN_PACE_RATIO' in gate_src
