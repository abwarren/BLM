"""THE LIVE TRADE COMBO (operator directive 2026-10-12, second revision).

One named test per requirement of the new Auto-Bet gate
(docs/rag/15_EDGE_FINDINGS.md §15.6):

  * progress band 78-90            -> TRADE_MIN/MAX_PROGRESS_PCT
  * pace ratio STRICTLY > 1.12     -> TRADE_MIN_PACE_RATIO
  * scored points >= 90            -> TRADE_MIN_SCORED_POINTS
  * shortfall margin >= 0.25       -> SHORTFALL_MARGIN
  * under price >= 1.85            -> min_price_for / EXEC_MIN_UNDER_PRICE
  * the ALERT is UNCHANGED         -> under_alert.REQUIRED_MARGIN 1.04 and
                                      under_alert.ALERT_PROGRESS_PCT 75, and
                                      the frozen analysis (fingerprint_stats)
                                      still reads the EXEC_* constants.

Measured evidence (corrected cohort, 40 days, in-sample): 782W/193L = 80.2%
(n=975, 24.4 bets/day).  Caveats: an in-sample maximum over ~4,500 combos
(overfitting risk); 95% CI ~= 77.7-82.5% at n=975; modelled entry prices with no
slippage.
"""
from datetime import datetime, timedelta, timezone
import inspect

import pytest

import blm_v4.api as api
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate
from blm_v4.betting.store import BettingStore
from blm_v4.live_analytics.under_alert import (ALERT_PROGRESS_PCT,
                                               REQUIRED_MARGIN,
                                               under_alert_state)
from blm_v4.trade_window import (AFTER, BEFORE, EXEC_MIN_UNDER_PRICE,
                                 PACE_BELOW, PRICE_FLOOR, SCORE_FLOOR,
                                 SHORTFALL, SHORTFALL_MARGIN,
                                 TRADE_MAX_PROGRESS_PCT,
                                 TRADE_MIN_PACE_RATIO,
                                 TRADE_MIN_PROGRESS_PCT,
                                 TRADE_MIN_SCORED_POINTS, exec_alert_reason,
                                 exec_shortfall_reason,
                                 execution_window_reason, price_reason,
                                 score_reason)

GOOD_PRICE = 1.95          # clears the 1.85 floor
GOOD_POINTS = 150.0        # well clear of the 90 scored-points floor


def _cfg(tmp_path) -> BettingConfig:
    return BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "blm_betting.db"))


def _game(progress, game_id="COMBO-1", price=GOOD_PRICE, points=GOOD_POINTS):
    """A payload that satisfies every OTHER condition, so the only things that
    can refuse it are the trade band, the score floor and the price floor."""
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
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "current_total_points": points},
        "under_alert_eligibility": {"eligible": True,
                                    "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": 193.5},
        "under_alert_exec_armed": True,
    }


def _evaluate(tmp_path, progress, price=GOOD_PRICE, points=GOOD_POINTS, **over):
    game = _game(progress, price=price, points=points)
    game.update(over)
    return evaluate(game, cfg=_cfg(tmp_path), store=BettingStore(
        str(tmp_path / "blm_betting.db")), enabled=True, unit_price=10.0,
        stats={"verifiable": True, "bets": 0, "amount": 0.0})


# ── 1. the progress band 78-90 (the dominant lever) ─────────────────────

def test_band_floor_78_is_inclusive_and_77_999_is_refused():
    assert TRADE_MIN_PROGRESS_PCT == 78.0
    assert execution_window_reason(78.0) is None       # inclusive floor
    assert execution_window_reason(77.999) == BEFORE


def test_band_ceiling_90_is_inclusive_and_90_001_is_refused():
    assert TRADE_MAX_PROGRESS_PCT == 90.0
    assert execution_window_reason(90.0) is None       # inclusive ceiling
    assert execution_window_reason(90.001) == AFTER


# ── 2. the pace ratio bar 1.12 (STRICT) ─────────────────────────────────

def test_pace_ratio_1_12_boundary_is_strict():
    assert TRADE_MIN_PACE_RATIO == 1.12
    # exactly 1.12 -> refused (STRICT); a hair above -> armed
    assert exec_alert_reason(4.48, 4.0, 80.0) == PACE_BELOW        # ratio 1.120
    assert exec_alert_reason(4.4800001, 4.0, 80.0) is None         # ratio > 1.12


# ── 3. the scored-points floor 90 ───────────────────────────────────────

def test_scored_points_90_boundary():
    assert TRADE_MIN_SCORED_POINTS == 90.0
    assert score_reason(90.0) is None
    assert score_reason(89.999) == SCORE_FLOOR


# ── 4. the shortfall margin 0.25 ────────────────────────────────────────

def test_shortfall_margin_0_25_boundary():
    assert SHORTFALL_MARGIN == 0.25
    assert exec_shortfall_reason(5.0, 4.75) is None       # gap exactly 0.25
    assert exec_shortfall_reason(5.0, 4.7501) == SHORTFALL  # gap 0.2499


# ── 5. the under price floor 1.85 ───────────────────────────────────────

def test_under_price_1_85_boundary():
    assert EXEC_MIN_UNDER_PRICE == 1.85
    assert price_reason(1.85, 80.0) is None
    assert price_reason(1.849999, 80.0) == PRICE_FLOOR


# ── 6. THE ALERT IS UNCHANGED (the critical constraint) ─────────────────

def test_the_alert_class_bar_is_still_1_04_and_its_floor_still_75():
    assert REQUIRED_MARGIN == 1.04
    assert ALERT_PROGRESS_PCT == 75.0


def test_the_alert_still_fires_on_its_own_1_04_class_not_the_trade_combo():
    """The alert reads ITS OWN constants.  A game at 76% progress with a 1.05
    ratio is INSIDE the alert (progress >= 75, ratio > 1.04) but OUTSIDE the
    live trade band — the alert fires exactly as before; only the trade moves."""
    st = under_alert_state(4.0, 4.2, 4.0, 76.0, eligible=True)
    assert st["active"] is True                        # alert fires, unchanged
    assert execution_window_reason(76.0) == BEFORE     # trade refuses the band
    # a ratio between the two bars: alert active (1.05 > 1.04), trade refused
    st2 = under_alert_state(4.0, 4.2, 4.0, 80.0, eligible=True)
    assert st2["active"] is True
    assert exec_alert_reason(4.2, 4.0, 80.0) == PACE_BELOW   # <= 1.12 trade bar


def test_the_alert_module_does_not_read_the_trade_or_exec_gate_constants():
    """The alert must be provably independent of the trade gate — it reads
    neither the TRADE_* nor the EXEC_* constants and calls none of the trade
    verdict functions."""
    import blm_v4.live_analytics.under_alert as ua
    src = inspect.getsource(ua)
    for forbidden in ("trade_window", "TRADE_MIN_PROGRESS_PCT",
                      "TRADE_MAX_PROGRESS_PCT", "TRADE_MIN_SCORED_POINTS",
                      "TRADE_MIN_PACE_RATIO", "SHORTFALL_MARGIN",
                      "EXEC_MIN_PROGRESS_PCT", "EXEC_MAX_PROGRESS_PCT",
                      "MIN_SCORED_POINTS", "EXEC_MIN_PACE_RATIO",
                      "exec_alert_reason", "exec_shortfall_reason",
                      "execution_window_reason", "price_reason",
                      "score_reason"):
        assert forbidden not in src, forbidden


def test_the_frozen_exec_constants_are_unchanged():
    """The EXEC_* constants are the frozen alert-era class — unchanged."""
    from blm_v4.trade_window import (EXEC_MAX_PROGRESS_PCT,
                                     EXEC_MIN_PACE_RATIO,
                                     EXEC_MIN_PROGRESS_PCT, MIN_SCORED_POINTS)
    assert EXEC_MIN_PROGRESS_PCT == 75.0
    assert EXEC_MAX_PROGRESS_PCT == 92.0
    assert MIN_SCORED_POINTS == 70.0
    assert EXEC_MIN_PACE_RATIO == 1.04


def test_the_frozen_analysis_still_reads_the_exec_constants():
    """live_analytics.fingerprint_stats (the frozen traded-cohort analysis) must
    still read the EXEC_* / alert constants, NOT the TRADE_* ones."""
    import blm_v4.live_analytics.fingerprint_stats as fs
    src = inspect.getsource(fs)
    for must in ("EXEC_MIN_PROGRESS_PCT", "EXEC_MAX_PROGRESS_PCT",
                 "MIN_SCORED_POINTS", "EXEC_MIN_PACE_RATIO",
                 "ALERT_PROGRESS_PCT", "REQUIRED_MARGIN"):
        assert must in src, must
    for trade_only in ("TRADE_MIN_PROGRESS_PCT", "TRADE_MAX_PROGRESS_PCT",
                       "TRADE_MIN_SCORED_POINTS", "TRADE_MIN_PACE_RATIO",
                       "SHORTFALL_MARGIN"):
        assert trade_only not in src, trade_only


def test_the_alert_source_literals_are_untouched():
    import blm_v4.live_analytics.under_alert as ua
    src = inspect.getsource(ua)
    assert "REQUIRED_MARGIN = 1.04" in src
    assert "ALERT_PROGRESS_PCT = 75.0" in src


# ── 7. the served armed verdict requires every trade leg ────────────────

def test_the_served_armed_verdict_requires_every_trade_leg():
    """The api armed expression must fold in the pace bar, the shortfall, the
    band AND the score floor — one served boolean the executor consumes."""
    src = inspect.getsource(api)
    armed = src.split('g["under_alert_exec_armed"] =', 1)[1][:800]
    for leg in ("exec_alert_reason(", "exec_shortfall_reason(",
                "execution_window_reason(", "score_reason("):
        assert leg in armed, leg


def test_the_execution_window_block_reports_the_trade_combo():
    from blm_v4.api import _execution_window_block
    win = _execution_window_block(85.0, 1.95, 150.0, True)
    assert win == {
        "min": 78.0, "max": 90.0, "min_price": 1.85, "min_points": 90.0,
        "min_pace_ratio": 1.12, "pace_armed": True,
        "in_window": True, "reason": None}


# ── 8. the executor gate (what is TRADED) end-to-end ────────────────────

@pytest.mark.parametrize("progress", [78.0, 80.0, 85.0, 90.0])
def test_executor_trades_inside_the_band(tmp_path, progress):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] in ("EXECUTE", "WOULD_BET"), got
    assert got["candidate"] is not None, got


@pytest.mark.parametrize("progress,reason", [(77.9, BEFORE), (90.1, AFTER)])
def test_executor_refuses_outside_the_band(tmp_path, progress, reason):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == reason
    assert got["candidate"] is None


def test_executor_refuses_below_90_points(tmp_path):
    got = _evaluate(tmp_path, 80.0, points=89.0)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == SCORE_FLOOR


def test_executor_refuses_a_price_below_1_85(tmp_path):
    got = _evaluate(tmp_path, 80.0, price=1.84)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == PRICE_FLOOR


def test_executor_refuses_an_unarmed_game(tmp_path):
    """The pace/shortfall legs are SERVED via armed; an unarmed game (any leg
    failed on the server) is refused before the executor's own checks."""
    got = _evaluate(tmp_path, 80.0, under_alert_exec_armed=False,
                    under_alert={"active": False, "checkpoint": 75,
                                 "trigger_line": 193.5})
    assert got["decision"] == "NO_BET"
    assert got["reason"] == "alert_not_active"
