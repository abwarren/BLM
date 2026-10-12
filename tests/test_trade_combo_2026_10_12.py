"""THE LIVE TRADE COMBO (operator directive 2026-10-12, third revision).

One named test per requirement of the Auto-Bet gate
(docs/rag/15_EDGE_FINDINGS.md §15.7):

  * progress band 75-92            -> TRADE_MIN/MAX_PROGRESS_PCT
  * pace ratio STRICTLY > 1.04     -> TRADE_MIN_PACE_RATIO
  * scored points >= 70            -> TRADE_MIN_SCORED_POINTS
  * shortfall margin >= 0.0        -> SHORTFALL_MARGIN
  * under price >= 1.60 (flat)     -> TRADE_MIN_UNDER_PRICE / trade_price_reason
  * the ALERT is UNCHANGED         -> under_alert.REQUIRED_MARGIN 1.04 and
                                      under_alert.ALERT_PROGRESS_PCT 75, and
                                      the frozen analysis (fingerprint_stats)
                                      still reads the EXEC_* constants.

Measured evidence (corrected cohort, 40 days, in-sample): 72.9% win, n=2,476,
61.9 bets/day.  Nearby rungs of the same frontier: 71.2% @ 70.7/day
(price>=1.60, pace>1.00); 67.5% @ 91.9/day (no pace filter); 74.6% @ 51.4/day
(pace>1.08); 78.1% @ 27.8/day (price>=1.90, pace>1.08, shortfall>=0.25, band
78-90 — the previous revision).  Caveats: an in-sample maximum over ~240 combos
(overfitting risk); corrected finals; modelled entry prices with no slippage;
EV depends on the REALISED price, not the floor.
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
                                 TRADE_MIN_SCORED_POINTS, TRADE_MIN_UNDER_PRICE,
                                 exec_alert_reason, exec_shortfall_reason,
                                 execution_window_reason, price_reason,
                                 score_reason, trade_price_reason)

# Clears the FLAT trade floor (1.60) but sits BELOW the frozen per-band
# break-even (1.85) — so a passing trade at this price proves the trade floor
# moved from 1.85 to the flat 1.60.
GOOD_PRICE = 1.70
GOOD_POINTS = 150.0        # well clear of the 70 scored-points floor


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


# ── 1. the progress band 75-92 ──────────────────────────────────────────

def test_band_floor_75_is_inclusive_and_74_999_is_refused():
    assert TRADE_MIN_PROGRESS_PCT == 75.0
    assert execution_window_reason(75.0) is None       # inclusive floor
    assert execution_window_reason(74.999) == BEFORE


def test_band_ceiling_92_is_inclusive_and_92_001_is_refused():
    assert TRADE_MAX_PROGRESS_PCT == 92.0
    assert execution_window_reason(92.0) is None       # inclusive ceiling
    assert execution_window_reason(92.001) == AFTER


# ── 2. the pace ratio bar 1.04 (STRICT) ─────────────────────────────────

def test_pace_ratio_1_04_boundary_is_strict():
    assert TRADE_MIN_PACE_RATIO == 1.04
    # exactly 1.04 -> refused (STRICT); a hair above -> armed
    assert exec_alert_reason(4.16, 4.0, 80.0) == PACE_BELOW          # ratio 1.040
    assert exec_alert_reason(4.1600001, 4.0, 80.0) is None           # ratio > 1.04


# ── 3. the scored-points floor 70 ───────────────────────────────────────

def test_scored_points_70_boundary():
    assert TRADE_MIN_SCORED_POINTS == 70.0
    assert score_reason(70.0) is None
    assert score_reason(69.999) == SCORE_FLOOR


# ── 4. the shortfall margin 0.0 ─────────────────────────────────────────

def test_shortfall_margin_zero_boundary():
    assert SHORTFALL_MARGIN == 0.0
    assert exec_shortfall_reason(5.0, 5.0) is None        # gap exactly 0.0
    assert exec_shortfall_reason(5.0, 5.0001) == SHORTFALL  # gap -0.0001


# ── 5. the flat trade under-price floor 1.60 ────────────────────────────

def test_under_price_1_60_flat_floor():
    assert TRADE_MIN_UNDER_PRICE == 1.60
    assert trade_price_reason(1.60) is None
    assert trade_price_reason(1.599999) == PRICE_FLOOR
    assert trade_price_reason(1.70) is None          # above 1.60 (below 1.85)


def test_the_trade_price_floor_is_flat_and_below_the_frozen_break_even():
    """The trade reads the FLAT 1.60 floor (trade_price_reason); the frozen
    per-band break-even (1.85) is retained for the analysis, NOT the trade."""
    assert TRADE_MIN_UNDER_PRICE == 1.60
    assert price_reason(1.70, 80.0) == PRICE_FLOOR   # the frozen 1.85 floor
    assert trade_price_reason(1.70) is None          # the trade's 1.60 floor


def test_the_frozen_per_band_price_function_is_unchanged():
    """min_price_for / price_reason are the frozen operator-published floor —
    untouched by the third revision."""
    from blm_v4.trade_window import EXEC_MIN_UNDER_PRICE as FROZEN
    assert FROZEN == 1.85
    assert price_reason(1.85, 80.0) is None
    assert price_reason(1.849999, 80.0) == PRICE_FLOOR


# ── 6. THE ALERT IS UNCHANGED (the critical constraint) ─────────────────

def test_the_alert_class_bar_is_still_1_04_and_its_floor_still_75():
    assert REQUIRED_MARGIN == 1.04
    assert ALERT_PROGRESS_PCT == 75.0


def test_the_alert_still_fires_on_its_own_class_not_the_trade_combo():
    """The alert reads ITS OWN constants.  A game at 95% progress with a 1.05
    ratio is INSIDE the alert (progress >= 75, ratio > 1.04) but OUTSIDE the
    trade band (progress > 92) — the alert fires exactly as before; only the
    trade moves."""
    st = under_alert_state(4.0, 4.2, 4.0, 95.0, eligible=True)
    assert st["active"] is True                        # alert fires, unchanged
    assert execution_window_reason(95.0) == AFTER      # trade refuses the band
    # a game inside the trade band is inside the alert too — the band now
    # starts at the alert's own floor.
    st2 = under_alert_state(4.0, 4.2, 4.0, 76.0, eligible=True)
    assert st2["active"] is True
    assert execution_window_reason(76.0) is None


def test_the_alert_module_does_not_read_the_trade_or_exec_gate_constants():
    """The alert must be provably independent of the trade gate — it reads
    neither the TRADE_* nor the EXEC_* constants and calls none of the trade
    verdict functions."""
    import blm_v4.live_analytics.under_alert as ua
    src = inspect.getsource(ua)
    for forbidden in ("trade_window", "TRADE_MIN_PROGRESS_PCT",
                      "TRADE_MAX_PROGRESS_PCT", "TRADE_MIN_SCORED_POINTS",
                      "TRADE_MIN_PACE_RATIO", "TRADE_MIN_UNDER_PRICE",
                      "SHORTFALL_MARGIN", "EXEC_MIN_PROGRESS_PCT",
                      "EXEC_MAX_PROGRESS_PCT", "MIN_SCORED_POINTS",
                      "EXEC_MIN_PACE_RATIO", "EXEC_MIN_UNDER_PRICE",
                      "exec_alert_reason", "exec_shortfall_reason",
                      "execution_window_reason", "price_reason",
                      "trade_price_reason", "score_reason"):
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
    assert EXEC_MIN_UNDER_PRICE == 1.85


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
                       "TRADE_MIN_UNDER_PRICE", "SHORTFALL_MARGIN"):
        assert trade_only not in src, trade_only


def test_the_alert_source_literals_are_untouched():
    import blm_v4.live_analytics.under_alert as ua
    src = inspect.getsource(ua)
    assert "REQUIRED_MARGIN = 1.04" in src
    assert "ALERT_PROGRESS_PCT = 75.0" in src


# ── 7. the served armed verdict requires every trade leg ────────────────

def test_the_served_armed_verdict_requires_every_trade_leg():
    """The api armed expression must fold in the pace bar, the shortfall, the
    band, the score floor AND the under price — one served boolean the executor
    consumes."""
    src = inspect.getsource(api)
    armed = src.split('g["under_alert_exec_armed"] =', 1)[1][:800]
    for leg in ("exec_alert_reason(", "exec_shortfall_reason(",
                "execution_window_reason(", "score_reason(",
                "trade_price_reason("):
        assert leg in armed, leg


def test_the_execution_window_block_reports_the_trade_combo():
    from blm_v4.api import _execution_window_block
    win = _execution_window_block(85.0, 1.70, 150.0, True)
    assert win == {
        "min": 75.0, "max": 92.0, "min_price": 1.60, "min_points": 70.0,
        "min_pace_ratio": 1.04, "pace_armed": True,
        "in_window": True, "reason": None}


# ── 8. the executor gate (what is TRADED) end-to-end ────────────────────

@pytest.mark.parametrize("progress", [75.0, 80.0, 85.0, 92.0])
def test_executor_trades_inside_the_band(tmp_path, progress):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] in ("EXECUTE", "WOULD_BET"), got
    assert got["candidate"] is not None, got


@pytest.mark.parametrize("progress,reason", [(74.9, BEFORE), (92.1, AFTER)])
def test_executor_refuses_outside_the_band(tmp_path, progress, reason):
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == reason
    assert got["candidate"] is None


def test_executor_refuses_below_70_points(tmp_path):
    got = _evaluate(tmp_path, 80.0, points=69.0)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == SCORE_FLOOR


def test_executor_refuses_a_price_below_1_60(tmp_path):
    got = _evaluate(tmp_path, 80.0, price=1.59)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == PRICE_FLOOR


def test_executor_trades_a_price_between_the_trade_floor_and_the_break_even(
        tmp_path):
    """The whole point of the third revision: a price in [1.60, 1.85) is
    TRADED — the executor reads the flat 1.60 floor, not the frozen 1.85."""
    got = _evaluate(tmp_path, 80.0, price=1.70)
    assert got["decision"] in ("EXECUTE", "WOULD_BET"), got
    assert got["candidate"] is not None, got


def test_executor_refuses_an_unarmed_game(tmp_path):
    """The pace/shortfall legs are SERVED via armed; an unarmed game (any leg
    failed on the server) is refused before the executor's own checks."""
    got = _evaluate(tmp_path, 80.0, under_alert_exec_armed=False,
                    under_alert={"active": False, "checkpoint": 75,
                                 "trigger_line": 193.5})
    assert got["decision"] == "NO_BET"
    assert got["reason"] == "alert_not_active"
