"""UNDER TRADE EXECUTION WINDOW + PRICE FLOOR (operator directives 2026-10-08).

The autonomous UNDER trade is placed only when BOTH hold:
  * progress sits inside [75%, 92%], and
  * the UNDER price clears the break-even for that progress band.

The floor is 75% because that is the ALERT's own floor (progress_pct >= 75), so
the window opens the moment an alert can exist — there is no "too early"
refusal left.  Measured: the 75-78 slice runs 65.44% UNDER (n=2,208, break-even
1.528) and was 30.7% of alerted games, all previously discarded.

The ceiling is 92%: the observed unfillable attempts (EVENT_NOT_FOUND) were at
94-96%, and the 90-92 slice measures 78.48% UNDER (n=316, break-even 1.274) —
+EV, so it is traded.  It replaced the earlier game-clock "final four minutes"
stop, which refused that slice.

The PRICE FLOOR is the operator's positive-EV criterion made enforceable: a hit
rate alone licenses nothing without the price.  Break-evens come from the
rebuilt cohort — the construction that reproduces the platform's own served
cohort.

THREE surfaces, ONE definition:
  * ``blm_v4.trade_window``     — the band, the price floor, the verdicts
  * ``blm_v4.betting.executor`` — the gate that decides what is TRADED
  * ``blm_v4.api`` / dashboard  — the payload block the BETTABLE badge reads

A second copy of the numbers, or a badge that ignores either verdict, is the
failure these tests exist to prevent.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate
from blm_v4.betting.store import BettingStore
from blm_v4.trade_window import (AFTER, BEFORE, EXEC_MAX_PROGRESS_PCT,
                                 EXEC_MIN_PROGRESS_PCT, MIN_PRICE_BANDS,
                                 PRICE_FLOOR, execution_window_reason,
                                 in_execution_window, min_price_for,
                                 price_reason)

HERE = Path(__file__).resolve().parent
EXECUTOR_PY = HERE.parent / "blm_v4" / "betting" / "executor.py"
API_PY = HERE.parent / "blm_v4" / "api.py"
DASH_JS = HERE.parent / "blm_v4" / "dashboard" / "static" / "dashboard.js"
INDEX_HTML = HERE.parent / "blm_v4" / "dashboard" / "static" / "index.html"

GOOD_PRICE = 1.95          # clears every band's break-even


def _cfg(tmp_path) -> BettingConfig:
    return BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "blm_betting.db"))


def _game(progress, game_id="WINDOW-1", price=GOOD_PRICE):
    """A payload that satisfies every OTHER condition, so the only things that
    can refuse it are the window and the price floor."""
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id,
        "classification": "BETUAL_NBA",
        "live": True,
        "live_reason": None,
        "market": {"total_line": 193.5, "market_status": "LIVE",
                   "under_odds": price},
        "projector": {"progress_pct": progress, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap},
        "under_alert_eligibility": {"eligible": True,
                                    "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": 193.5},
    }


def _evaluate(tmp_path, progress, price=GOOD_PRICE, **over):
    game = _game(progress, price=price)
    game.update(over)
    return evaluate(game, cfg=_cfg(tmp_path), store=BettingStore(
        str(tmp_path / "blm_betting.db")), enabled=True, unit_price=10.0,
        stats={"verifiable": True, "bets": 0, "amount": 0.0})


# ── the window definition ───────────────────────────────────────────────

def test_the_floor_is_the_operators_75_percent():
    """75 == the ALERT's own floor, so no alert is ever refused as "too early"."""
    assert EXEC_MIN_PROGRESS_PCT == 75.0


def test_the_ceiling_is_the_top_of_the_last_quoted_band():
    assert EXEC_MAX_PROGRESS_PCT == 92.0
    assert EXEC_MIN_PROGRESS_PCT < EXEC_MAX_PROGRESS_PCT


@pytest.mark.parametrize("pct", [75.0, 78.0, 85.0, 88.0, 90.0, 91.5, 92.0])
def test_inside_the_window_has_no_refusal(pct):
    assert execution_window_reason(pct) is None
    assert in_execution_window(pct) is True


@pytest.mark.parametrize("pct", [92.01, 93.0, 94.0, 95.0, 97.5, 100.0])
def test_past_the_window_is_refused(pct):
    """94-96 is where the observed unfillable attempts landed."""
    assert execution_window_reason(pct) == AFTER
    assert in_execution_window(pct) is False


@pytest.mark.parametrize("pct", [0.0, 50.0, 70.0, 74.0, 74.9, 74.999])
def test_before_the_window_is_refused(pct):
    assert execution_window_reason(pct) == BEFORE


def test_the_90_92_slice_is_traded_again():
    """The game-clock stop refused these; the recomputed evidence says the band
    is +EV, so it is inside the window once more."""
    for pct in (90.0, 90.5, 91.0, 91.9, 92.0):
        assert execution_window_reason(pct) is None


@pytest.mark.parametrize("pct", [None, "", "abc", float("nan"),
                                 float("inf"), float("-inf")])
def test_unprovable_progress_fails_closed(pct):
    assert execution_window_reason(pct) == BEFORE
    assert in_execution_window(pct) is False


# ── the price floor ─────────────────────────────────────────────────────

def test_the_bands_are_ordered_and_open_ended():
    bounds = [hi for hi, _ in MIN_PRICE_BANDS]
    bounded = [b for b in bounds if b is not None]
    assert bounded == sorted(bounded)
    assert bounds[-1] is None                  # the last band must be open-ended
    for pct in (75.0, 80.0, 85.0, 92.0):
        assert min_price_for(pct) is not None


@pytest.mark.parametrize("pct,expected", [
    (75.0, 1.51), (79.9, 1.51),      # 75-80: 66.39% UNDER -> 1.506
    (80.0, 1.37), (84.9, 1.37),      # 80-85: 73.17% UNDER -> 1.367
    (85.0, 1.28), (92.0, 1.28),      # 85-92: 78.43% UNDER -> 1.275
])
def test_the_floor_matches_the_measured_break_even(pct, expected):
    assert min_price_for(pct) == pytest.approx(expected)


def test_a_price_below_the_band_break_even_is_refused():
    assert price_reason(1.50, 76.0) == PRICE_FLOOR     # needs 1.51
    assert price_reason(1.30, 82.0) == PRICE_FLOOR     # needs 1.37
    assert price_reason(1.27, 88.0) == PRICE_FLOOR     # needs 1.28


def test_a_price_at_or_above_the_break_even_passes():
    assert price_reason(1.51, 76.0) is None
    assert price_reason(1.95, 76.0) is None
    assert price_reason(1.28, 88.0) is None


def test_a_later_band_needs_a_lower_price():
    """The same 1.40 is below the floor early and above it late — the floor is
    per band, not one global number."""
    assert price_reason(1.40, 76.0) == PRICE_FLOOR
    assert price_reason(1.40, 88.0) is None


@pytest.mark.parametrize("bad", ["", "abc", float("nan"), float("inf")])
def test_an_unprovable_price_fails_closed(bad):
    """A present value that cannot be shown to clear the floor is refused.
    An ABSENT price (None) is not gated — see the test below."""
    assert price_reason(bad, 80.0) == PRICE_FLOOR


def test_an_absent_price_is_not_gated_here():
    """Line and price come from the same market row, so absence is already the
    executor's market_missing concern — this gate only refuses a KNOWN-low
    price, and therefore keeps every existing caller's contract."""
    assert price_reason(None, 80.0) is None


def test_no_price_claim_outside_the_window():
    """The window refuses these first; the price floor makes no claim."""
    for pct in (70.0, 95.0, None):
        assert min_price_for(pct) is None
        assert price_reason(1.0, pct) is None


# ── the executor gate (what is TRADED) ──────────────────────────────────

@pytest.mark.parametrize("progress", [75.0, 78.0, 85.0, 90.0, 92.0])
def test_inside_the_window_is_traded(tmp_path, progress):
    """Both edges inclusive: a qualifying game inside the band trades."""
    got = _evaluate(tmp_path, progress)
    assert got["decision"] in ("EXECUTE", "WOULD_BET"), got
    assert got["candidate"] is not None, got


@pytest.mark.parametrize("progress", [0.0, 70.0, 74.0, 74.9, 74.999])
def test_before_the_window_is_refused_by_the_executor(tmp_path, progress):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == BEFORE
    assert got["candidate"] is None


@pytest.mark.parametrize("progress", [92.01, 94.0, 95.0, 100.0])
def test_past_the_window_is_refused_by_the_executor(tmp_path, progress):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == AFTER
    assert got["candidate"] is None


def test_the_executor_refuses_a_price_below_the_break_even(tmp_path):
    """End-to-end: an in-window game at a sub-break-even price is NOT traded —
    the operator's criterion is positive EV, not volume."""
    got = _evaluate(tmp_path, 76.0, price=1.50)          # needs 1.51
    assert got["decision"] == "NO_BET"
    assert got["reason"] == PRICE_FLOOR
    assert got["candidate"] is None
    ok = _evaluate(tmp_path, 76.0, price=1.60)
    assert ok["decision"] in ("EXECUTE", "WOULD_BET"), ok


def test_a_missing_price_is_not_gated_by_this_rule(tmp_path):
    """An absent price keeps the pre-existing contract: the gate exists to
    refuse a KNOWN-low price, not to invent a refusal for a payload that omits
    the field (the line and price share one market row)."""
    got = _evaluate(tmp_path, 80.0, price=None)
    assert got["reason"] != PRICE_FLOOR
    assert got["decision"] in ("EXECUTE", "WOULD_BET"), got


def test_out_of_window_never_becomes_a_candidate(tmp_path):
    """No candidate ⇒ the worker never calls the provider: the point of the
    gate is that a doomed attempt is not spent at all."""
    for progress in (74.0, 95.0):
        assert _evaluate(tmp_path, progress)["candidate"] is None


@pytest.mark.parametrize("progress", [88.0, 95.0])
def test_window_never_overrides_a_missing_alert(tmp_path, progress):
    """The window can only refuse: a game with no active alert is refused
    for the alert reason, inside the band or out of it."""
    got = _evaluate(tmp_path, progress,
                    under_alert={"active": False, "checkpoint": 75,
                                 "trigger_line": 193.5})
    assert got["decision"] == "NO_BET"
    assert got["reason"] == "alert_not_active"


def test_executor_has_no_second_copy_of_the_band():
    """The executor must CONSUME the shared definition, never re-declare the
    numbers — a second copy is the drift this guards."""
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    assert ("from blm_v4.trade_window import execution_window_reason, "
            "price_reason") in src
    for name in ("EXEC_MIN_PROGRESS_PCT =", "EXEC_MAX_PROGRESS_PCT =",
                 "MIN_PRICE_BANDS =", "PRICE_FLOOR ="):
        assert name not in src, name


# ── the served payload + the BETTABLE badge (what is SHOWN) ─────────────

def test_api_publishes_the_window_on_both_branches():
    """The badge reads a SERVED verdict; the payload must carry it on the
    normal path AND the fail-closed path (never an absent block)."""
    src = API_PY.read_text(encoding="utf-8")
    assert "under_alert_execution_window" in src
    assert src.count("_execution_window_block(") == 3   # def + 2 call sites
    assert '"min_price": min_price_for(progress_pct)' in src


def test_payload_block_shape():
    from blm_v4.api import _execution_window_block
    assert _execution_window_block(88.0, 1.95) == {
        "min": 75.0, "max": 92.0, "min_price": 1.28,
        "in_window": True, "reason": None}
    cheap = _execution_window_block(88.0, 1.20)
    assert cheap["in_window"] is False and cheap["reason"] == PRICE_FLOOR
    late = _execution_window_block(93.0, 1.95)
    assert late["in_window"] is False and late["reason"] == AFTER
    early = _execution_window_block(70.0, 1.95)
    assert early["in_window"] is False and early["reason"] == BEFORE


def test_badge_requires_the_window():
    """The dashboard badge mirrors the executor: BETTABLE only with the
    window verdict, and the reason surfaces when the window is the blocker."""
    js = DASH_JS.read_text(encoding="utf-8")
    assert "g.under_alert_execution_window" in js
    assert "win.in_window === true" in js
    assert "ua.active === true && elig.eligible === true && inWindow" in js
    assert "outside the execution window" in js[
        js.index("blmAlertStateRowHTML"):]


def test_the_changed_asset_is_cache_busted():
    """dashboard.js is served from disk; a stale browser copy would keep the
    old badge, so the ?v= must have moved with the file."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "/static/dashboard.js?v=6855974.7" in html
