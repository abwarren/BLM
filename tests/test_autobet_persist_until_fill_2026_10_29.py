"""REQUIREMENT (2026-10-29, operator directive): the engine PERSISTS until the
fill succeeds — five minutes is the ceiling, not three attempts.

    "if the market is stale or locked the engine must persist until the fill is
     successful ... dont reclick on no receipt — try to fill the order, wait for
     the market to return and try again"

Two halves, and they must not blur together:

  * a PROVABLE no-money failure (submit control unavailable, slip not updated,
    position not found) may be retried for as long as the market is open — that
    is the persist-until-fill half.  No double-stake risk: the book never took
    the bet.
  * a submit with NO receipt is AMBIGUOUS and must never be re-clicked; that is
    guarded by test_autobet_place_persist_budget_2026_10_08 and re-asserted here
    against the new defaults, because raising the budget to 5 minutes is exactly
    the change that could turn a safe guard into a 5-minute loop of double bets.

The clock is faked so a 5-minute budget costs no real time.
"""
from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import FakeBrowserAdapter  # noqa: E402

import blm_v4.execution.browser_bridge as B  # noqa: E402
from blm_v4.execution.config import ExecutionConfig  # noqa: E402

EVENT = "E1"
CMD = {"game_id": "31159816", "event": EVENT, "market": "TOTAL",
       "selection": "UNDER"}


class _FakeClock:
    def __init__(self) -> None:
        self._t = 1_000.0

    def monotonic(self) -> float:
        return self._t

    def sleep(self, s: float) -> None:
        self._t += float(s)

    def elapsed(self) -> float:
        return self._t - 1_000.0


def _install_clock(monkeypatch) -> _FakeClock:
    c = _FakeClock()
    monkeypatch.setattr(time, "monotonic", c.monotonic)
    monkeypatch.setattr(time, "sleep", c.sleep)
    return c


def _failing_adapter() -> FakeBrowserAdapter:
    """Every submit comes back FAILED — provably no money moved, so retrying is
    safe: this is the stale/locked-market case the directive is about."""
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.place_status = "FAILED"
    a.place_reject_reason = "SUBMIT_CONTROL_UNAVAILABLE"
    # the bridge's Remove-All before each attempt — without it every retry dies
    # on a duplicate leg and never reaches the submit step
    a.on_before_click = lambda: a.slip.clear()
    return a


def test_defaults_are_five_minutes_with_a_five_second_wait():
    d = inspect.signature(B.ResolverBrowserBridge.__init__).parameters
    assert d["place_budget_s"].default == 300.0, "directive: five minutes"
    assert d["retry_delay_s"].default == 5.0, (
        "the pause is the wait for a suspended market to return")
    # and the deployment path uses the same ceiling, not the old 45 s
    cfg_default = ExecutionConfig.__dataclass_fields__["place_budget_s"].default
    assert cfg_default == 300.0


def test_provable_failures_keep_retrying_far_past_three_attempts(monkeypatch):
    """Three attempts was the old contract; a locked market needed more."""
    c = _install_clock(monkeypatch)
    a = _failing_adapter()
    b = B.ResolverBrowserBridge(a, place_budget_s=300.0, retry_delay_s=5.0)

    out = b.place(command=CMD, stake_amount=40.0)

    assert out["status"] == "FAILED"
    attempts = len(a.placements)
    assert attempts > 3, f"must persist past three tries, got {attempts}"
    assert c.elapsed() >= 300.0, f"must hold the full budget, got {c.elapsed()}s"


def test_no_receipt_after_a_submit_is_still_never_reclicked(monkeypatch):
    """THE DOUBLE-STAKE GUARD, re-asserted at the new 5-minute budget."""
    c = _install_clock(monkeypatch)
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.place_status = "SUBMITTED"
    a.confirmation = None                      # no receipt at all
    b = B.ResolverBrowserBridge(a, place_budget_s=300.0, retry_delay_s=5.0)

    out = b.place(command=CMD, stake_amount=40.0)

    assert len(a.placements) == 1, "a second submit could double the stake"
    assert c.elapsed() == 0.0, "an ambiguous submit must not burn the budget"
    assert out["status"] in ("SUBMITTED", "ACCEPTED")
