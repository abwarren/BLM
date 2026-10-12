"""The SHORTFALL execution leg (2026-10-12) — docs/rag/15_EDGE_FINDINGS.md.

The second Auto-Bet execution leg: the required rate must be AT LEAST the
SHORTFALL_MARGIN above the actual rate —
``required_pts_per_min - actual_pts_per_min >= SHORTFALL_MARGIN``.  The THIRD
revision (2026-10-12) sets the margin to 0.0, so the leg now means "the game is
at or below its required rate" (the original intent, without the 0.25 cushion).
One named test per requirement.
"""
import inspect

from blm_v4.trade_window import SHORTFALL, SHORTFALL_MARGIN, exec_shortfall_reason


def test_the_shortfall_margin_is_now_zero():
    """Third revision (2026-10-12): the margin tightened to 0.0."""
    assert SHORTFALL_MARGIN == 0.0


def test_below_required_by_the_margin_clears_the_leg():
    """A game UNDER-shooting its required rate is allowed."""
    assert exec_shortfall_reason(5.0, 4.0) is None
    assert exec_shortfall_reason(5.0, 0.0) is None


def test_the_margin_boundary_zero():
    """Exactly at the required rate clears (gap 0.0 >= 0.0); above it refuses."""
    assert exec_shortfall_reason(5.0, 5.0) is None               # gap 0.0000
    assert exec_shortfall_reason(5.0, 5.0001) == SHORTFALL       # gap -0.0001
    assert exec_shortfall_reason(5.0, 5.5) == SHORTFALL          # gap -0.5000


def test_above_required_is_refused():
    """A game OUTPACING its required rate has no shortfall at all."""
    assert exec_shortfall_reason(5.0, 6.0) == SHORTFALL          # gap -1.000


def test_missing_or_unprovable_input_fails_closed():
    """An unprovable pace is REFUSED — a payload that cannot show the shortfall
    can only ever be stricter, never looser."""
    assert exec_shortfall_reason(None, 4.0) == SHORTFALL
    assert exec_shortfall_reason(5.0, None) == SHORTFALL
    assert exec_shortfall_reason("n/a", 4.0) == SHORTFALL
    assert exec_shortfall_reason(5.0, "n/a") == SHORTFALL
    assert exec_shortfall_reason(float("nan"), 4.0) == SHORTFALL
    assert exec_shortfall_reason(float("inf"), 4.0) == SHORTFALL


def test_served_armed_verdict_includes_the_leg():
    """The SERVED verdict (api) must AND this leg in, so the engine and the
    dashboard badge cannot disagree about it — the executor consumes the boolean
    and never re-derives it."""
    import blm_v4.api as api
    src = inspect.getsource(api)
    armed = src.split('g["under_alert_exec_armed"] =', 1)[1][:800]
    assert "exec_shortfall_reason(" in armed


def test_the_primary_rule_is_shortfall_and_the_flat_trade_price_floor():
    """The third-revision trade rule = shortfall (>= 0.0) AND the FLAT trade
    price floor (TRADE_MIN_UNDER_PRICE, 1.60).  The frozen operator floor
    (EXEC_MIN_UNDER_PRICE, 1.85) is retained for the analysis, unchanged."""
    from blm_v4.trade_window import (EXEC_MIN_UNDER_PRICE, PRICE_FLOOR,
                                     TRADE_MIN_UNDER_PRICE, price_reason,
                                     trade_price_reason)
    assert EXEC_MIN_UNDER_PRICE == 1.85                 # frozen, unchanged
    assert TRADE_MIN_UNDER_PRICE == 1.60                # the trade floor
    # both legs clear -> the trade is eligible
    assert exec_shortfall_reason(5.0, 4.0) is None
    assert trade_price_reason(1.70) is None
    # the trade price leg alone refuses below the flat 1.60 floor
    assert trade_price_reason(1.59) == PRICE_FLOOR
    # the frozen per-band floor is untouched and still refuses a 1.70 price
    assert price_reason(1.70, 80.0) == PRICE_FLOOR
    # the shortfall leg alone refuses a game outpacing its required rate
    assert exec_shortfall_reason(5.0, 6.0) == SHORTFALL
