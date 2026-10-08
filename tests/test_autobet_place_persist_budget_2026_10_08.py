"""REQUIREMENT (2026-10-08, operator directive): "set the engine to
persistently try for 30-60 seconds before aborting".

Before this change the placement loop was bounded by a fixed ATTEMPT COUNT
(``place_attempts=5``), so how long it actually persisted depended on how slow
each attempt happened to be — measured LIVE on the same five attempts: 16 s in
one case and 141 s in another.  The engine's persistence was therefore
inconsistent and usually far shorter than intended.

Now the bound is WALL CLOCK (``EXECUTION_PLACE_BUDGET_S``, default 45 s, inside
the requested 30-60 s window), with ``place_attempts`` retained as a MINIMUM so
a budget of 0 reproduces the old behaviour exactly.

The clock is faked: these tests prove the loop's cadence and bound, and must not
spend 45 real seconds doing it.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import FakeBrowserAdapter  # noqa: E402

import blm_v4.execution.browser_bridge as B  # noqa: E402

EVENT = "E1"
GID = "31159816"
CMD = {"game_id": GID, "event": EVENT, "market": "TOTAL", "selection": "UNDER"}
FAILED = "FAILED"


class _FakeClock:
    """A monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self._t = 1_000.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self._t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self._t += float(s)

    def elapsed(self) -> float:
        return self._t - 1_000.0


def _failing_adapter() -> FakeBrowserAdapter:
    """Every attempt fails fast at RESOLVE (the position is not offered)."""
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.missing_positions.add(f"{EVENT}|UNDER")
    return a


def _install_clock(monkeypatch) -> _FakeClock:
    c = _FakeClock()
    monkeypatch.setattr(time, "monotonic", c.monotonic)
    monkeypatch.setattr(time, "sleep", c.sleep)
    return c


def test_place_persists_for_the_wall_clock_budget_not_a_fixed_attempt_count(
        monkeypatch):
    """It keeps trying for the whole budget — far past the old 5 attempts."""
    c = _install_clock(monkeypatch)
    b = B.ResolverBrowserBridge(_failing_adapter(), place_budget_s=45.0,
                                retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert out["status"] == FAILED            # nothing was ever placed
    # it persisted the FULL budget, not 5 quick attempts (~7.5 s)
    assert c.elapsed() >= 45.0
    # and therefore made far more than the old fixed five attempts
    assert c.sleeps > 5, f"only {c.sleeps} attempts — the old fixed bound"


def test_a_zero_budget_reproduces_the_legacy_attempt_count(monkeypatch):
    """place_attempts stays a MINIMUM: budget 0 ⇒ exactly today's behaviour."""
    c = _install_clock(monkeypatch)
    b = B.ResolverBrowserBridge(_failing_adapter(), place_budget_s=0.0,
                                place_attempts=5, retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert out["status"] == FAILED
    assert c.sleeps == 5, f"expected the legacy 5 attempts, got {c.sleeps}"


def test_the_budget_never_shortens_the_attempt_floor(monkeypatch):
    """A tiny budget still honours place_attempts — never FEWER tries."""
    c = _install_clock(monkeypatch)
    b = B.ResolverBrowserBridge(_failing_adapter(), place_budget_s=0.001,
                                place_attempts=5, retry_delay_s=1.5)

    b.place(command=CMD, stake_amount=10.0)

    assert c.sleeps == 5


def test_a_verified_leg_is_still_placed_exactly_once(monkeypatch):
    """No regression: a healthy attempt places once and stops immediately."""
    c = _install_clock(monkeypatch)
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)     # resolvable and clickable
    b = B.ResolverBrowserBridge(a, place_budget_s=45.0, retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert out["status"] in ("ACCEPTED", "SUBMITTED"), out
    assert len(a.placements) == 1              # exactly one, never a second
    assert c.elapsed() == 0.0                  # and no burning of the budget


# ══════════════════════════════════════════════════════════════════════════
# DOM FEEDBACK (operator directive 2026-10-08): read the DOM straight away to
# learn whether the bet was accepted; on a provable non-acceptance, clear the
# stale market and select it again.
# ══════════════════════════════════════════════════════════════════════════

def test_a_receipt_means_accepted_and_stops_immediately(monkeypatch):
    """The bookmaker's OWN receipt is proof — accept it and stop."""
    _install_clock(monkeypatch)
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.place_status = "SUBMITTED"
    a.confirmation = {"reference": "PB-ABC123"}   # the receipt
    b = B.ResolverBrowserBridge(a, place_budget_s=45.0, retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert out["status"] == "SUBMITTED"
    assert out["provider_ref"] == "PB-ABC123"    # the receipt is carried back
    assert len(a.placements) == 1                # placed once, no retry


def test_an_unconfirmed_submission_is_never_retried(monkeypatch):
    """THE DOUBLE-STAKE GUARD.

    A slow/absent receipt is indistinguishable from a slow acceptance, so the
    engine must NOT re-select and re-submit on it — that could stake twice.
    Exactly one placement, and the outcome stays UNKNOWN.
    """
    c = _install_clock(monkeypatch)
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.place_status = "SUBMITTED"
    a.confirmation = None                        # no receipt at all
    b = B.ResolverBrowserBridge(a, place_budget_s=45.0, retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert len(a.placements) == 1, "a second submit could double the stake"
    # the adapter's own status is carried back UNCHANGED and with NO receipt.
    # Mapping an unconfirmed submission to UNKNOWN is the EXECUTOR's job (see
    # test_timeout_is_never_success) — place() must not invent it.
    assert out["status"] == "SUBMITTED"
    assert out["provider_ref"] is None
    assert c.elapsed() <= 1.5                    # and it did not churn


def test_a_rejection_clears_the_slip_and_selects_the_market_again(monkeypatch):
    """A provable non-acceptance ⇒ Remove-All + re-select, then it succeeds.

    The bookmaker refusing outright is the case the DOM CAN prove, so the loop
    must clear the stale market and select it again — visible here as a SECOND
    resolve/click and a second placement, ending accepted.
    """
    _install_clock(monkeypatch)
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.place_status = "REJECTED"                  # refused on the first attempt
    a.place_reject_reason = "market suspended"

    seen = {"clicks": 0}

    def _accept_on_retry():
        seen["clicks"] += 1
        if seen["clicks"] >= 2:
            a.place_status = "ACCEPTED"          # takes it after the re-select

    # the REAL adapter exposes clear_betslip — the Remove-All step the bridge
    # calls before every attempt (dom.py).  This shared double does not, so the
    # rejected leg would still be in the slip on the retry and read as a
    # duplicate.  Supply it here so the test drives the contract the engine
    # actually drives.
    a.clear_betslip = lambda: (a.slip.clear() or 0)

    a.on_before_click = _accept_on_retry
    b = B.ResolverBrowserBridge(a, place_budget_s=45.0, retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert out["status"] == "ACCEPTED"
    assert len(a.clicks) == 2, "the market must be SELECTED AGAIN after clearing"
    assert len(a.placements) == 2, "one refused submit, then the accepted one"


def test_a_pre_submit_failure_also_clears_and_selects_again(monkeypatch):
    """THE TRANSPORT-MISS FIX.

    A FAILED place means the adapter did NOT submit — that is its contract for
    every pre-submit problem (the submit control never appeared, the stake
    mismatched, the slip was unreadable).  No money is at stake, so these must
    clear the stale market and select the market again rather than burning the
    game, which is what the live SUBMIT_CONTROL_UNAVAILABLE misses were doing.
    """
    _install_clock(monkeypatch)
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 193.5, 1.95, 1.90)
    a.place_status = "FAILED"                    # e.g. submit control absent
    a.place_reject_reason = "submit_control_unavailable"

    seen = {"clicks": 0}

    def _ok_on_retry():
        seen["clicks"] += 1
        if seen["clicks"] >= 2:
            a.place_status = "ACCEPTED"

    a.clear_betslip = lambda: (a.slip.clear() or 0)
    a.on_before_click = _ok_on_retry
    b = B.ResolverBrowserBridge(a, place_budget_s=45.0, retry_delay_s=1.5)

    out = b.place(command=CMD, stake_amount=10.0)

    assert out["status"] == "ACCEPTED", out
    assert len(a.clicks) == 2, "the market must be selected again, not abandoned"
