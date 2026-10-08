"""UNDER TRADE EXECUTION WINDOW (operator directives 2026-10-07 / 2026-10-08).

The autonomous UNDER trade is placed ONLY inside a progress band.  Outside it
the alert may be statistically live yet unbettable: live probing (2026-10-07)
shows PokerBet stops quoting the game total near the end — at 95-97.5% the
event view renders with no market grid or redirects to another live game — so
an execution arriving there is never filled.

Two edges, two KINDS of rule:

  * the FLOOR is a progress percent — 78% (operator directive 2026-10-07,
    revised down from 85% to restore the volume the narrower floor discarded:
    the measured edge is positive across the whole of Q4);
  * the CEILING is a GAME-CLOCK stop — no placement inside the FINAL FOUR
    MINUTES of the match (operator directive 2026-10-08).  Expressed per
    classification, never as a flat percent, because the same final minutes
    are a different percentage in each league:

        BETUAL_NBA (10-min quarters, 40-min game) -> (40-4)/40 = 90.00%
        CYBER_2K26 (12-min quarters, 48-min game) -> (48-4)/48 = 91.67%

    The old flat 92% ceiling was the trap this documents: 92% of a 40-minute
    game is 36.8 minutes, i.e. still inside the final four minutes.

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
from blm_v4.trade_window import (AFTER, BEFORE, EXEC_LAST_GAME_MINUTES,
                                 EXEC_MAX_PROGRESS_PCT_FALLBACK,
                                 EXEC_MIN_PROGRESS_PCT, exec_max_progress_pct,
                                 execution_window_reason, in_execution_window)

NBA = "BETUAL_NBA"       # 40-minute game -> ceiling 90.00%
CYBER = "CYBER_2K26"     # 48-minute game -> ceiling 91.67%

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


def _game(progress, game_id="WINDOW-1", classification=NBA):
    """A payload that satisfies every OTHER condition, so the only thing
    that can refuse it is the execution window."""
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id,
        "classification": classification,
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


def _evaluate(tmp_path, progress, classification=NBA, **over):
    game = _game(progress, classification=classification)
    game.update(over)
    return evaluate(game, cfg=_cfg(tmp_path), store=BettingStore(
        str(tmp_path / "blm_betting.db")), enabled=True, unit_price=10.0,
        stats={"verifiable": True, "bets": 0, "amount": 0.0})


# ── the definition ──────────────────────────────────────────────────────

def test_the_floor_is_the_operators_78_percent():
    assert EXEC_MIN_PROGRESS_PCT == 78.0


def test_the_ceiling_is_the_final_four_minutes_of_the_game():
    """A GAME-CLOCK rule: the ceiling is `full - 4` minutes, per league."""
    assert EXEC_LAST_GAME_MINUTES == 4.0
    assert exec_max_progress_pct(NBA) == pytest.approx(100 * (40 - 4) / 40)
    assert exec_max_progress_pct(CYBER) == pytest.approx(100 * (48 - 4) / 48)


def test_the_two_leagues_do_not_share_one_percent():
    """The directive's whole point: the same final four minutes are a
    different percentage in each classification, so one flat ceiling is
    simply wrong for at least one league."""
    assert exec_max_progress_pct(NBA) != exec_max_progress_pct(CYBER)
    assert exec_max_progress_pct(CYBER) > exec_max_progress_pct(NBA)


def test_an_unknown_length_is_never_the_permissive_ceiling():
    """Fail closed: an unknowable game length takes the TIGHTER ceiling."""
    for cls in (None, "", "NOT_A_LEAGUE"):
        assert exec_max_progress_pct(cls) == pytest.approx(
            exec_max_progress_pct(NBA))
        assert exec_max_progress_pct(cls) <= EXEC_MAX_PROGRESS_PCT_FALLBACK


def test_the_band_edges_are_ordered_for_every_league():
    for cls in (NBA, CYBER, None):
        assert EXEC_MIN_PROGRESS_PCT < exec_max_progress_pct(cls)


@pytest.mark.parametrize("pct", [78.0, 80.0, 85.0, 88.0, 89.9, 90.0])
def test_inside_the_window_has_no_refusal(pct):
    assert execution_window_reason(pct, NBA) is None
    assert in_execution_window(pct, NBA) is True


@pytest.mark.parametrize("pct", [90.01, 91.0, 92.0, 95.0, 97.5, 100.0])
def test_the_final_four_minutes_are_refused(pct):
    """Every progress past 36:00 of a 40-minute game — the operator's hard
    stop, and the regression this directive fixes (92% used to be allowed)."""
    assert execution_window_reason(pct, NBA) == AFTER
    assert in_execution_window(pct, NBA) is False


@pytest.mark.parametrize("pct", [91.67, 92.0, 95.0, 100.0])
def test_the_cyber_ceiling_is_later_than_the_nba_one(pct):
    """44:00 of a 48-minute game — the same rule, a different percentage."""
    assert execution_window_reason(pct, CYBER) == AFTER


@pytest.mark.parametrize("pct", [90.5, 91.0, 91.6])
def test_the_same_percent_is_inside_for_cyber_and_outside_for_nba(pct):
    """The concrete proof the ceiling is per classification, not flat."""
    assert execution_window_reason(pct, NBA) == AFTER
    assert execution_window_reason(pct, CYBER) is None


@pytest.mark.parametrize("pct", [0.0, 50.0, 74.9, 77.9, 77.999])
def test_before_the_window_is_refused(pct):
    assert execution_window_reason(pct, NBA) == BEFORE


@pytest.mark.parametrize("pct", [None, "", "abc", float("nan"),
                                 float("inf"), float("-inf")])
def test_unprovable_progress_fails_closed(pct):
    assert execution_window_reason(pct, NBA) == BEFORE
    assert in_execution_window(pct, NBA) is False


# ── the executor gate (what is TRADED) ──────────────────────────────────

@pytest.mark.parametrize("progress", [78.0, 85.0, 88.0, 89.5, 90.0])
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


@pytest.mark.parametrize("progress", [90.5, 91.0, 92.0, 95.0, 97.5, 100.0])
def test_the_last_four_minutes_are_refused_by_the_executor(tmp_path, progress):
    """No NBA trade inside the final four minutes, at any progress past it."""
    got = _evaluate(tmp_path, progress)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == AFTER
    assert got["candidate"] is None


def test_the_executor_reads_the_ceiling_per_classification(tmp_path):
    """End-to-end: 91% is refused for a 40-minute game and traded for a
    48-minute one — the executor must pass the classification through."""
    assert _evaluate(tmp_path, 91.0, classification=NBA)["reason"] == AFTER
    cyber = _evaluate(tmp_path, 91.0, classification=CYBER)
    assert cyber["decision"] in ("EXECUTE", "WOULD_BET"), cyber


def test_the_92_regression_is_closed(tmp_path):
    """Operator directive 2026-10-08: 92% of a 40-minute game is 36.8 min —
    inside the final four minutes — so it must be refused, as it was NOT
    under the old flat 92% ceiling."""
    got = _evaluate(tmp_path, 92.0)
    assert got["decision"] == "NO_BET"
    assert got["reason"] == AFTER


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
    assert "EXEC_LAST_GAME_MINUTES =" not in src


# ── the served payload + the BETTABLE badge (what is SHOWN) ─────────────

def test_api_publishes_the_window_on_both_branches():
    """The badge reads a SERVED verdict; the payload must carry it on the
    normal path AND the fail-closed path (never an absent block), and every
    path must pass the classification so the ceiling can be per league."""
    src = API_PY.read_text(encoding="utf-8")
    assert "under_alert_execution_window" in src
    assert src.count("_execution_window_block(") == 3   # def + 2 call sites
    assert 'None, g.get("classification"))' in src       # fail-closed path
    assert src.count('g.get("classification"))') >= 2    # both live paths


def test_payload_block_shape():
    from blm_v4.api import _execution_window_block
    assert _execution_window_block(88.0, NBA) == {
        "min": 78.0, "max": 90.0, "in_window": True, "reason": None}
    cyber = _execution_window_block(91.0, CYBER)
    assert cyber["in_window"] is True
    assert cyber["max"] == pytest.approx(100 * 44 / 48)
    late = _execution_window_block(90.5, NBA)
    assert late["in_window"] is False and late["reason"] == AFTER
    early = _execution_window_block(70.0, NBA)
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
    assert "/static/dashboard.js?v=6855974.5" in html
