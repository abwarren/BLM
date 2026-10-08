"""UNDER TRADE EXECUTION WINDOW (operator directive 2026-10-07).

The autonomous UNDER trade is placed ONLY inside a progress band (default
[85%, 92%]).  Outside it the alert may be statistically live yet
unbettable: live probing (2026-10-07) shows PokerBet stops quoting the game
total near the end — at 95-97.5% the event view renders with no market grid
or redirects to another live game — so an execution arriving there is never
filled.  The lower edge is the operator's Q4 four-minute mark, the earliest
point of the measured edge (+16.9% ROI over 661 settled entries, market
still open).

THREE surfaces, ONE definition:

  * ``blm_v4.trade_window``     — the band and the verdict (the definition)
  * ``blm_v4.betting.executor`` — the gate that decides what is TRADED
  * ``blm_v4.api`` / dashboard  — the payload block the BETTABLE badge reads

A second copy of the numbers, or a badge that ignores the window, is the
failure these tests exist to prevent.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate
from blm_v4.betting.store import BettingStore
from blm_v4.trade_window import (AFTER, BEFORE, EXEC_MAX_PROGRESS_PCT,
                                 EXEC_MIN_PROGRESS_PCT,
                                 execution_window_reason, in_execution_window)

HERE = Path(__file__).resolve().parent
EXECUTOR_PY = HERE.parent / "blm_v4" / "betting" / "executor.py"
API_PY = HERE.parent / "blm_v4" / "api.py"
DASH_JS = HERE.parent / "blm_v4" / "dashboard" / "static" / "dashboard.js"
INDEX_HTML = HERE.parent / "blm_v4" / "dashboard" / "static" / "index.html"


def _cfg(tmp_path) -> BettingConfig:
    return BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "blm_betting.db"))


def _game(progress, game_id="WINDOW-1"):
    """A payload that satisfies every OTHER condition, so the only thing
    that can refuse it is the execution window."""
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id,
        "live": True,
        "live_reason": None,
        "market": {"total_line": 193.5, "market_status": "LIVE"},
        "projector": {"progress_pct": progress, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap},
        "under_alert_eligibility": {"eligible": True,
                                    "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": 193.5},
    }


def _evaluate(tmp_path, progress, **over):
    game = _game(progress)
    game.update(over)
    return evaluate(game, cfg=_cfg(tmp_path), store=BettingStore(
        str(tmp_path / "blm_betting.db")), enabled=True, unit_price=10.0,
        stats={"verifiable": True, "bets": 0, "amount": 0.0})


# ── the definition ──────────────────────────────────────────────────────

def test_window_constants_are_the_operator_directive():
    """The band is the operator's 78% floor through the 92% ceiling."""
    assert EXEC_MIN_PROGRESS_PCT == 78.0
    assert EXEC_MAX_PROGRESS_PCT == 92.0
    assert EXEC_MIN_PROGRESS_PCT < EXEC_MAX_PROGRESS_PCT


@pytest.mark.parametrize("pct", [78.0, 85.0, 88.0, 91.5, 92.0])
def test_inside_the_window_has_no_refusal(pct):
    assert execution_window_reason(pct) is None
    assert in_execution_window(pct) is True


@pytest.mark.parametrize("pct", [0.0, 50.0, 74.9, 77.9, 77.999])
def test_before_the_window_is_refused(pct):
    assert execution_window_reason(pct) == BEFORE


@pytest.mark.parametrize("pct", [92.01, 93.0, 95.0, 97.5, 100.0])
def test_after_the_window_is_refused(pct):
    assert execution_window_reason(pct) == AFTER


@pytest.mark.parametrize("pct", [None, "", "abc", float("nan"),
                                 float("inf"), float("-inf")])
def test_unprovable_progress_fails_closed(pct):
    assert execution_window_reason(pct) == BEFORE
    assert in_execution_window(pct) is False


# ── the executor gate (what is TRADED) ──────────────────────────────────

@pytest.mark.parametrize("progress", [78.0, 85.0, 88.0, 91.5, 92.0])
def test_inside_the_window_is_traded(tmp_path, progress):
    """Both edges inclusive: a qualifying game inside the band trades."""
    got = _evaluate(tmp_path, progress)
    assert got["decision"] in ("EXECUTE", "WOULD_BET"), got
    assert got["candidate"] is not None, got


@pytest.mark.parametrize("progress", [0.0, 50.0, 74.9, 75.0, 77.5, 77.999])
def test_before_the_window_is_refused_by_the_executor(tmp_path, progress):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == BEFORE
    assert got["candidate"] is None


@pytest.mark.parametrize("progress", [92.01, 93.0, 95.0, 97.5, 100.0])
def test_after_the_window_is_refused_by_the_executor(tmp_path, progress):
    """No trade later than the window — the market is gone by then."""
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == AFTER
    assert got["candidate"] is None


def test_out_of_window_never_becomes_a_candidate(tmp_path):
    """No candidate ⇒ the worker never calls the provider: the point of the
    gate is that a doomed attempt is not spent at all."""
    for progress in (75.0, 95.0):
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
    assert "from blm_v4.trade_window import execution_window_reason" in src
    assert "EXEC_MIN_PROGRESS_PCT =" not in src
    assert "EXEC_MAX_PROGRESS_PCT =" not in src


# ── the served payload + the BETTABLE badge (what is SHOWN) ─────────────

def test_api_publishes_the_window_on_both_branches():
    """The badge reads a SERVED verdict; the payload must carry it on the
    normal path AND the fail-closed path (never an absent block)."""
    src = API_PY.read_text(encoding="utf-8")
    assert "under_alert_execution_window" in src
    assert src.count("_execution_window_block(") == 3   # def + 2 call sites
    assert "_execution_window_block(None)" in src        # fail-closed path


def test_payload_block_shape():
    from blm_v4.api import _execution_window_block
    assert _execution_window_block(88.0) == {
        "min": 78.0, "max": 92.0, "in_window": True, "reason": None}
    late = _execution_window_block(97.5)
    assert late["in_window"] is False and late["reason"] == AFTER
    early = _execution_window_block(70.0)
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
    assert "/static/dashboard.js?v=6855974.4" in html
