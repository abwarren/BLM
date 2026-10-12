"""COMPETITION EXCLUSION — the new Auto-Bet trade leg.

One named test per requirement (operator directive 2026-10-13;
docs/rag/15_EDGE_FINDINGS.md §15.9):

  * the excluded slug REFUSES            -> test_the_excluded_slug_refuses
  * another competition is ALLOWED       -> test_another_competition_is_allowed
  * the env list parses (comma-separated,
    multiple slugs, empty = nothing)      -> test_the_env_list_parses*
  * the DEFAULT excludes exactly the
    NBA 2K26 slug                         -> test_the_default_excludes*
  * an absent / unknown slug behaves as
    DOCUMENTED (NOT excluded, surfaced)   -> test_an_absent_slug_is_not_excluded
  * the FROZEN block + the alert are
    BYTE-UNCHANGED                        -> test_the_frozen_* / alert
  * the leg is ANDed into the SERVED
    ``under_alert_exec_armed``            -> test_the_served_armed_* / e2e

Evidence (the live 5-leg cohort, 38 clean days): INCLUDING the NBA 2K26
simulated feed ('cyber-basketball-2k26-matches') 2,638 bets at 72.8%;
EXCLUDING it 1,742 bets at 78.4%; the excluded block itself 896 bets at
60.6%.  Last 96h: 151 at 67.5% including vs 51 at 76.3% excluding.  A
DILUTION decision, not a broken-class fix (the block is still marginally
+EV at ~60.6%); in-sample.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import blm_v4.api as api
import blm_v4.trade_window as tw
from blm_v4.api import router as v4_router
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate
from blm_v4.betting.store import BettingStore
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore
from blm_v4.trade_window import (COMPETITION_EXCLUDED,
                                 TRADE_EXCLUDED_COMPETITIONS,
                                 TRADE_EXCLUDED_COMPETITIONS_DEFAULT,
                                 trade_competition_reason)

REPO = Path(__file__).resolve().parent.parent
CYBER = "cyber-basketball-2k26-matches"
NBA = "betual-nba"


# ══════════════════════════════════════════════════════════════════════
# 1. THE EXCLUDED SLUG REFUSES / ANOTHER COMPETITION IS ALLOWED
# ══════════════════════════════════════════════════════════════════════

def test_the_excluded_slug_refuses():
    """A positively-known slug IN the list returns the refusal reason."""
    assert CYBER in TRADE_EXCLUDED_COMPETITIONS          # the code default
    assert trade_competition_reason(CYBER) == COMPETITION_EXCLUDED


def test_another_competition_is_allowed():
    """A positively-known slug NOT in the list returns None (allowed)."""
    assert trade_competition_reason(NBA) is None
    assert trade_competition_reason("betual-euroleague") is None


def test_the_match_is_exact_not_a_substring():
    """The comparison is on the WHOLE slug — a lookalike is not excluded."""
    assert trade_competition_reason("cyber-basketball-2k26") is None
    assert trade_competition_reason("cyber-basketball-2k26-matches-x") is None
    # surrounding whitespace is stripped before the comparison
    assert trade_competition_reason(f"  {CYBER}  ") == COMPETITION_EXCLUDED


# ══════════════════════════════════════════════════════════════════════
# 2. AN ABSENT / UNKNOWN SLUG IS NOT EXCLUDED (the documented fail direction)
# ══════════════════════════════════════════════════════════════════════

def test_an_absent_slug_is_not_excluded():
    """DOCUMENTED fail direction: this is an EXCLUSION list, so an absent /
    unknown slug is NOT a refusal.  Refusing it would silently kill ALL
    trading the moment the payload lost ``competition_slug``.  The trade
    refuses ONLY on POSITIVE evidence (a known slug in the list); the gap is
    surfaced instead of blocked."""
    assert trade_competition_reason(None) is None
    assert trade_competition_reason("") is None
    assert trade_competition_reason("   ") is None
    # a non-string that stringifies to a non-member -> allowed
    assert trade_competition_reason(0) is None


def test_an_absent_slug_does_not_disarm_the_trade():
    """The counterpart at the supplied-boolean level: a game with NO
    competition_slug still ARMS (the leg imposes no requirement on absence),
    so a payload regression cannot silently stop all trading."""
    assert trade_competition_reason(None) is None   # leg passes -> not a blocker


# ══════════════════════════════════════════════════════════════════════
# 3. THE ENV LIST PARSES
# ══════════════════════════════════════════════════════════════════════

def _fresh_excluded(env_value):
    """The parsed set as a FRESH interpreter reads it, with
    TRADE_EXCLUDED_COMPETITIONS either UNSET (env_value is None) or set to
    ``env_value``.  A fresh process is the honest test of the code default —
    the in-process constant is baked at import time."""
    env = {k: v for k, v in os.environ.items()
           if k != "TRADE_EXCLUDED_COMPETITIONS"}
    if env_value is not None:
        env["TRADE_EXCLUDED_COMPETITIONS"] = env_value
    out = subprocess.run(
        [sys.executable, "-c",
         "import blm_v4.trade_window as t;"
         " print(sorted(t.TRADE_EXCLUDED_COMPETITIONS))"],
        cwd=REPO, capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_default_excludes_exactly_the_nba_2k26_slug():
    """With TRADE_EXCLUDED_COMPETITIONS UNSET the default is EXACTLY the one
    slug the evidence names — nothing more, nothing less."""
    assert TRADE_EXCLUDED_COMPETITIONS_DEFAULT == CYBER
    assert TRADE_EXCLUDED_COMPETITIONS == frozenset({CYBER})
    assert _fresh_excluded(None) == repr([CYBER])


def test_the_env_list_parses_comma_separated_multiple_slugs():
    """A comma-separated value yields every slug, whitespace-trimmed and
    de-duplicated; the default is REPLACED, not appended."""
    assert _fresh_excluded("a,b") == repr(["a", "b"])
    assert _fresh_excluded(" a , b ,c ") == repr(["a", "b", "c"])
    assert _fresh_excluded("x,x,y") == repr(["x", "y"])   # deduped


def test_an_empty_env_list_excludes_nothing():
    """An EMPTY string is an explicit 'exclude nothing' opt-out: the parsed
    set is empty and the leg refuses nothing."""
    assert _fresh_excluded("") == "[]"
    assert _fresh_excluded("  ") == "[]"
    assert _fresh_excluded(",,") == "[]"


def test_the_parse_ignores_blank_entries():
    """Empty entries between commas are dropped, not kept as '' members."""
    assert _fresh_excluded("a,,b") == repr(["a", "b"])
    assert tw._env_list("NOPE_UNSET_LIST_VAR", "d1,d2") == frozenset(
        {"d1", "d2"})


def test_an_empty_configured_list_allows_every_slug(monkeypatch):
    """With the set emptied, the leg refuses nothing — even the 2K26 slug."""
    monkeypatch.setattr(tw, "TRADE_EXCLUDED_COMPETITIONS", frozenset())
    assert trade_competition_reason(CYBER) is None
    assert trade_competition_reason(None) is None


def test_a_configured_list_refuses_only_its_members(monkeypatch):
    monkeypatch.setattr(tw, "TRADE_EXCLUDED_COMPETITIONS",
                        frozenset({"a", "b"}))
    assert trade_competition_reason("a") == COMPETITION_EXCLUDED
    assert trade_competition_reason("b") == COMPETITION_EXCLUDED
    assert trade_competition_reason(CYBER) is None     # not in this list
    assert trade_competition_reason(None) is None


# ══════════════════════════════════════════════════════════════════════
# 4. THE FROZEN BLOCK + THE ALERT ARE BYTE-UNCHANGED
# ══════════════════════════════════════════════════════════════════════

def _git_show(path, rev="HEAD"):
    try:
        out = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=REPO,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def test_the_frozen_trade_window_prefix_is_byte_unchanged():
    """The leg was APPENDED to trade_window.py, so the file must still BEGIN
    with the byte-exact content at HEAD — every frozen line (EXEC_*,
    MIN_PRICE_BANDS, min_price_for, price_reason, the trade verdicts) AND the
    already-landed line-movement leg is byte-identical, and the new block
    lives ONLY in the tail after that prefix."""
    frozen = _git_show("blm_v4/trade_window.py")
    if frozen is None:
        pytest.skip("git unavailable")
    cur = (REPO / "blm_v4" / "trade_window.py").read_text()
    assert cur.startswith(frozen), "the frozen trade_window prefix changed"
    assert len(cur) > len(frozen), "the new leg must be an appended tail"


def test_the_alert_file_is_byte_unchanged():
    head = _git_show("blm_v4/live_analytics/under_alert.py")
    cur = (REPO / "blm_v4" / "live_analytics" / "under_alert.py").read_text()
    if head is not None:
        assert cur == head, "under_alert.py changed"
    assert "REQUIRED_MARGIN = 1.04" in cur
    assert "ALERT_PROGRESS_PCT = 75.0" in cur


def test_the_frozen_exec_constants_and_functions_are_unchanged():
    assert tw.EXEC_MIN_PROGRESS_PCT == 75.0
    assert tw.EXEC_MAX_PROGRESS_PCT == 92.0
    assert tw.MIN_SCORED_POINTS == 70.0
    assert tw.EXEC_MIN_PACE_RATIO == 1.04
    assert tw.EXEC_MIN_UNDER_PRICE == 1.85
    assert tw.MIN_PRICE_BANDS == ((80.0, 1.48), (85.0, 1.39), (None, 1.28))
    # the TRADE_* defaults are untouched by this change
    assert tw.TRADE_MIN_PROGRESS_PCT == 75.0
    assert tw.TRADE_MAX_PROGRESS_PCT == 92.0
    assert tw.TRADE_MIN_SCORED_POINTS == 70.0
    assert tw.TRADE_MIN_PACE_RATIO == 1.04
    assert tw.SHORTFALL_MARGIN == 0.0
    assert tw.TRADE_MIN_UNDER_PRICE == 1.60
    # the frozen floors behave exactly as before
    assert tw.min_price_for(80.0) == 1.85
    assert tw.price_reason(1.84, 80.0) == tw.PRICE_FLOOR
    assert tw.price_reason(1.85, 80.0) is None
    # the new leg lives ONLY in the appended block — no FROZEN function
    # references it
    for fn in (tw.min_price_for, tw.price_reason, tw.execution_window_reason,
               tw.score_reason, tw.exec_alert_reason,
               tw.exec_shortfall_reason, tw.trade_price_reason,
               tw.in_execution_window):
        assert "trade_competition" not in inspect.getsource(fn), fn.__name__


# ══════════════════════════════════════════════════════════════════════
# 5. THE SERVED ARMED EXPRESSION FOLDS IN THE LEG
# ══════════════════════════════════════════════════════════════════════

def test_the_served_armed_expression_folds_in_the_competition_leg():
    src = inspect.getsource(api)
    armed = src.split('g["under_alert_exec_armed"] =', 1)[1][:800]
    # the new leg AND the five earlier legs + the move leg share ONE boolean
    for leg in ("trade_competition_reason(",
                "exec_alert_reason(", "exec_shortfall_reason(",
                "execution_window_reason(", "score_reason(",
                "trade_price_reason(", "trade_move_reason("):
        assert leg in armed, leg
    # ... and it reads the ALREADY-SERVED competition_slug, not a new query
    assert 'g.get("competition_slug")' in armed


def test_the_payload_serves_the_competition_block_and_the_slug():
    src = inspect.getsource(api)
    assert "under_alert_competition" in src
    for key in ('"slug"', '"known"', '"reason"'):
        assert key in src, key
    # the slug is the SAME field the game dict already carries (line-builder)
    assert '"competition_slug": game.get("competition_slug") or None' in src


# ══════════════════════════════════════════════════════════════════════
# 6. END-TO-END — the SERVED boolean excludes the 2K26 slug and the
#    executor (consuming the SAME boolean) refuses it
# ══════════════════════════════════════════════════════════════════════

ENTRY_LINE = 200.0


def _cfg(tmp_path):
    return BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=300.0,
        db_path=str(tmp_path / "blm_betting.db"))


def _served_for_slug(tmp_path, monkeypatch, slug):
    """Drive the real router with a game in ``slug`` satisfying every OTHER
    trade leg, and return the SERVED game dict."""
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(api, "STATE_FILE", tmp_path / "collector_state.json")
    # the SERVED slug is ``slug or None`` (the detail builder normalises an
    # empty games-row slug to None), so key the pace reference the same way.
    _ref_key = slug or None
    monkeypatch.setattr(api, "_pace_reference",
                        lambda conn: {_ref_key: {"avg_pace": 4.0,
                                                 "games": 100}})
    monkeypatch.setattr(api, "_q3_pace_reference", lambda conn=None: {})
    monkeypatch.setattr(api, "_line_reference",
                        lambda conn=None: {"avg": {}, "open": {}})
    monkeypatch.setattr(api, "_LINE_REF_WORKER", None)
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")          # noqa: E731
    gid = "COMP-1"
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid, competition_id="comp-c",
        competition_slug=slug, competition="Test Comp",
        region="Virtual Matches", game_family="betual",
        classification="BETUAL_NBA", sport="basketball",
        home_team="Alpha Virtual", away_team="Beta Virtual",
        game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
        status="live", first_seen_at=iso(now - timedelta(minutes=40)),
        last_seen_at=iso(now))
    st.upsert_game(game)
    for mins, hs, as_ in ((30, 20, 18), (10, 60, 58), (0, 80, 78)):
        st.insert_snapshot(
            st.upsert_game(game),
            MarketObservation(
                source="PokerBet", source_game_id=gid,
                classification="BETUAL_NBA",
                captured_at=iso(now - timedelta(minutes=mins)),
                home_team="Alpha Virtual", away_team="Beta Virtual",
                home_score=hs, away_score=as_, period_label="3rd Quarter",
                quarter=3, clock="06:00", game_status="live",
                total_line=ENTRY_LINE, spread=None, w1_odds=None,
                w2_odds=None, markets_json="{}"),
            force=True)

    def projector(source_game_id):
        return {"source_game_id": source_game_id,
                "classification": "BETUAL_NBA",
                "captured_at": iso(datetime.now(timezone.utc)),
                "period_label": "3rd Quarter", "clock": "06:00",
                "elapsed_game_minutes": 30.0,
                "remaining_game_minutes": 10.0, "progress_pct": 85.0,
                "current_total_points": 158.0,
                "live_total_line": ENTRY_LINE, "market_status": "LIVE",
                "market_age_seconds": 5, "actual_pts_per_min": 4.0,
                "required_pts_per_min": 5.0, "pace_gap": -1.0,
                "status": "VALID"}
    monkeypatch.setattr(api, "_pace_projector_for", projector)
    app = FastAPI()
    app.include_router(v4_router)
    body = TestClient(app).get("/api/v4/live").json()
    return next(g for g in body["games"] if g["game_id"] == gid)


def test_the_served_payload_excludes_the_2k26_slug_and_disarms(tmp_path,
                                                               monkeypatch):
    """The 2K26 game satisfies EVERY other leg, so the ONLY thing that can
    disarm it is the new competition leg — proven by the nba twin arming."""
    g = _served_for_slug(tmp_path, monkeypatch, CYBER)
    assert g["competition_slug"] == CYBER
    assert g["under_alert_competition"] == {
        "slug": CYBER, "known": True, "reason": COMPETITION_EXCLUDED}
    assert g["under_alert_exec_armed"] is False
    got = evaluate(g, cfg=_cfg(tmp_path),
                   store=BettingStore(str(tmp_path / "b.db")),
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 0, "amount": 0.0})
    assert got["decision"] == "NO_BET"
    assert got["reason"] == "alert_not_active"


def test_the_served_payload_arms_a_non_excluded_competition(tmp_path,
                                                            monkeypatch):
    """The SAME fixture under the NBA slug ARMS — so the refusal above is the
    new leg, not a broken fixture."""
    g = _served_for_slug(tmp_path, monkeypatch, NBA)
    assert g["competition_slug"] == NBA
    assert g["under_alert_competition"] == {
        "slug": NBA, "known": True, "reason": None}
    assert g["under_alert_exec_armed"] is True


def test_the_served_payload_arms_when_the_exclusion_list_is_empty(
        tmp_path, monkeypatch):
    """An EMPTY exclusion list dis-arms nothing: the 2K26 game ARMS."""
    monkeypatch.setattr(tw, "TRADE_EXCLUDED_COMPETITIONS", frozenset())
    g = _served_for_slug(tmp_path, monkeypatch, CYBER)
    assert g["under_alert_competition"]["reason"] is None
    assert g["under_alert_exec_armed"] is True


def test_the_served_payload_surfaces_an_absent_slug_without_disarming(
        tmp_path, monkeypatch):
    """A game whose slug is absent is NOT excluded (documented fail
    direction): it ARMS and the gap is surfaced as known: False."""
    g = _served_for_slug(tmp_path, monkeypatch, "")   # empty games-row slug
    assert g["competition_slug"] is None
    assert g["under_alert_competition"] == {
        "slug": None, "known": False, "reason": None}
    assert g["under_alert_exec_armed"] is True
