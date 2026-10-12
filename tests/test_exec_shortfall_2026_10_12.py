"""The SHORTFALL execution leg (2026-10-12) — docs/rag/15_EDGE_FINDINGS.md.

The second Auto-Bet execution leg: the required rate must exceed the actual rate
by AT LEAST the SHORTFALL_MARGIN (0.25) —
``required_pts_per_min - actual_pts_per_min >= 0.25`` (the 2026-10-12 combo; the
earlier leg only required ``actual < required``).  One named test per requirement.
"""
import inspect

from blm_v4.trade_window import SHORTFALL, SHORTFALL_MARGIN, exec_shortfall_reason


def test_the_shortfall_margin_is_the_operators_0_25():
    assert SHORTFALL_MARGIN == 0.25


def test_below_required_by_the_margin_clears_the_leg():
    """The edge case: a game UNDER-shooting its required rate by >= 0.25 is
    allowed."""
    assert exec_shortfall_reason(5.0, 4.0) is None
    assert exec_shortfall_reason(5.0, 0.0) is None


def test_the_margin_boundary_0_25():
    """Exactly the 0.25 margin clears; 0.249 does not."""
    assert exec_shortfall_reason(5.0, 4.75) is None              # gap 0.250
    assert exec_shortfall_reason(5.0, 4.751) == SHORTFALL        # gap 0.249
    assert exec_shortfall_reason(5.0, 4.9) == SHORTFALL          # gap 0.100


def test_at_or_above_required_is_refused():
    """A game at or outpacing its required rate has no shortfall at all."""
    assert exec_shortfall_reason(5.0, 5.0) == SHORTFALL          # gap 0.000
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
    armed = src.split('g["under_alert_exec_armed"] =', 1)[1][:600]
    assert "exec_shortfall_reason(" in armed


def test_the_primary_rule_is_shortfall_and_the_operator_price_floor():
    """The operator's 2026-10-12 PRIMARY rule = shortfall AND under_price >= 1.85."""
    from blm_v4.trade_window import EXEC_MIN_UNDER_PRICE, PRICE_FLOOR, price_reason
    assert EXEC_MIN_UNDER_PRICE == 1.85
    # both legs clear -> the trade is eligible
    assert exec_shortfall_reason(5.0, 4.0) is None
    assert price_reason(1.90, 80.0) is None
    # the price leg alone refuses below the floor
    assert price_reason(1.84, 80.0) == PRICE_FLOOR
    # the shortfall leg alone refuses at/above the required rate
    assert exec_shortfall_reason(5.0, 5.0) == SHORTFALL
