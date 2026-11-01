"""EXECUTION price floor (operator directive 2026-11-02).

"I do not want to play anything with a price of 1.69 or lower … only place bets
of 1.7 or larger.  There will normally be more than 1 rung on the market."

So the SIGNAL keeps its 1.60 floor — the alert, the board and the cohort label
still show a game trading there — and a SECOND, higher bar gates only what can
arm, place or re-validate a bet.  One requirement per test:

  1. unconfigured deployment is unchanged (exec floor == signal floor)
  2. at the live floor (1.70) a 1.65 price is refused for EXECUTION while the
     signal still passes it — the trade is restricted, the signal is not
  3. the boundary is exact: 1.70 passes, 1.69 is refused
  4. an ABSENT price is another rule's concern (unchanged contract); an
     unprovable one fails closed
  5. the executor refuses to place below the floor and names PRICE_FLOOR
"""
from __future__ import annotations

import pytest

from blm_v4 import trade_window as tw


def test_unconfigured_execution_floor_equals_the_signal_floor():
    assert tw.TRADE_MIN_UNDER_PRICE == 1.60          # the SIGNAL floor, untouched
    assert tw.TRADE_EXEC_MIN_PRICE == tw.TRADE_MIN_UNDER_PRICE, (
        "an unconfigured deployment must behave exactly as before")


def test_at_the_live_floor_only_the_execution_bar_moves(monkeypatch):
    monkeypatch.setattr(tw, "TRADE_EXEC_MIN_PRICE", 1.70)
    assert tw.trade_price_reason(1.65) is None, "the SIGNAL still passes 1.65"
    assert tw.exec_price_reason(1.65) == tw.PRICE_FLOOR, "the TRADE must not"


def test_boundary_is_exact_at_170(monkeypatch):
    monkeypatch.setattr(tw, "TRADE_EXEC_MIN_PRICE", 1.70)
    assert tw.exec_price_reason(1.70) is None
    assert tw.exec_price_reason(1.69) == tw.PRICE_FLOOR
    assert tw.exec_price_reason(1.60) == tw.PRICE_FLOOR


def test_absent_price_is_not_this_rules_business(monkeypatch):
    """Same contract as trade_price_reason: None is market_missing's concern."""
    monkeypatch.setattr(tw, "TRADE_EXEC_MIN_PRICE", 1.70)
    assert tw.exec_price_reason(None) is None


@pytest.mark.parametrize("bad", ["", "n/a", float("nan"), float("inf")])
def test_unprovable_price_fails_closed(bad, monkeypatch):
    monkeypatch.setattr(tw, "TRADE_EXEC_MIN_PRICE", 1.70)
    assert tw.exec_price_reason(bad) == tw.PRICE_FLOOR


def test_executor_gate_uses_the_execution_floor(monkeypatch):
    """The placement gate must read the EXECUTION bar, not the signal bar."""
    from blm_v4.betting import executor as ex
    monkeypatch.setattr(tw, "TRADE_EXEC_MIN_PRICE", 1.70)
    assert ex.exec_price_reason is tw.exec_price_reason
    assert ex.exec_price_reason(1.65) == tw.PRICE_FLOOR
    assert ex.exec_price_reason(1.72) is None
