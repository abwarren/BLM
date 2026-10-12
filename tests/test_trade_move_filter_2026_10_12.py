"""LINE-MOVEMENT VOLATILITY FILTER — the new Auto-Bet trade leg.

One named test per requirement (operator directive 2026-10-12;
docs/rag/15_EDGE_FINDINGS.md §15.8):

  * the boundary at EXACTLY 2.0% (in vs out)      -> test_move_boundary_*
  * fail CLOSED (missing open line / league avg)  -> test_missing_* / unprovable
  * the toggle on/off, default ON                 -> test_*toggle*
  * the min-history variant EXCLUDED at 0         -> test_min_history_*
  * the FROZEN constants/functions + the alert
    are BYTE-UNCHANGED                            -> test_the_frozen_* / alert
  * the leg is ANDed into the SERVED
    ``under_alert_exec_armed`` the executor reads  -> test_the_served_armed_*
  * open_line + league_avg_line carried, and the
    executor refuses a volatile move               -> test_served_payload_*/e2e

The leg's evidence (OOS walk-forward, 38 clean days, entry-date attribution):
flat <2% -> 78.8% UNDER [76.5,80.9] n=1363 EV +0.672 vs moved >=2% -> 64.7%
[61.6,67.7] n=949 EV +0.228; gap +14.1 pp, p~6e-14; plateau across 1-4%, holds
in all six leagues and all price bands.
"""
from __future__ import annotations

import inspect
import sqlite3
import subprocess
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
from blm_v4.live_analytics.line_reference import (build_reference, first_lines,
                                                  game_slug_map)
from blm_v4.live_analytics.line_reference_worker import LineReferenceWorker
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore
from blm_v4.trade_window import (MOVE_UNPROVABLE, MOVE_VOLATILE,
                                 TRADE_MOVE_MAX, TRADE_MOVE_MIN_HISTORY_S,
                                 trade_move_reason)

REPO = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════════
# 1. THE BOUNDARY AT EXACTLY 2.0%
# ══════════════════════════════════════════════════════════════════════

def test_move_boundary_is_strict_at_two_percent():
    """2.0% exactly is REFUSED; a hair under is FLAT (the rule is '<')."""
    assert TRADE_MOVE_MAX == 0.02
    # |entry - open| / league == 2.00% exactly -> moved (STRICT)
    assert trade_move_reason(102.0, 100.0, 100.0) == MOVE_VOLATILE
    # a hair under 2% -> flat
    assert trade_move_reason(101.999, 100.0, 100.0) is None
    # the move is ABSOLUTE: -2.00% is refused, -1.999% is flat
    assert trade_move_reason(98.0, 100.0, 100.0) == MOVE_VOLATILE
    assert trade_move_reason(98.001, 100.0, 100.0) is None


def test_move_is_normalised_by_the_league_average():
    """The SAME absolute move flips verdict with the league's average line."""
    # 5 pts on a 300-pt league = 1.67% -> flat; on a 200-pt league = 2.5% -> moved
    assert trade_move_reason(105.0, 100.0, 300.0) is None
    assert trade_move_reason(105.0, 100.0, 200.0) == MOVE_VOLATILE


# ══════════════════════════════════════════════════════════════════════
# 2. FAIL CLOSED — unprovable open line / league average
# ══════════════════════════════════════════════════════════════════════

def test_missing_open_line_fails_closed():
    assert trade_move_reason(200.0, None, 200.0) == MOVE_UNPROVABLE


def test_missing_league_average_fails_closed():
    assert trade_move_reason(200.0, 200.0, None) == MOVE_UNPROVABLE


def test_unprovable_inputs_fail_closed():
    assert trade_move_reason(200.0, 200.0, 0.0) == MOVE_UNPROVABLE   # avg <= 0
    assert trade_move_reason(200.0, 200.0, -5.0) == MOVE_UNPROVABLE
    assert trade_move_reason("x", 200.0, 200.0) == MOVE_UNPROVABLE
    assert trade_move_reason(200.0, "x", 200.0) == MOVE_UNPROVABLE
    assert trade_move_reason(float("nan"), 200.0, 200.0) == MOVE_UNPROVABLE
    assert trade_move_reason(200.0, float("inf"), 200.0) == MOVE_UNPROVABLE


# ══════════════════════════════════════════════════════════════════════
# 3. THE TOGGLE — default ON, false disables
# ══════════════════════════════════════════════════════════════════════

def test_the_leg_toggle_defaults_on(monkeypatch):
    monkeypatch.delenv("TRADE_MOVE_FILTER_ENABLED", raising=False)
    assert tw._env_flag("TRADE_MOVE_FILTER_ENABLED", True) is True
    assert tw.TRADE_MOVE_FILTER_ENABLED is True
    assert tw._env_flag("TRADE_MOVE_FILTER_ENABLED", True) is tw._env_flag(
        "TRADE_MOVE_FILTER_ENABLED", True)


def test_toggle_off_disables_the_leg(monkeypatch):
    """false disables: the leg imposes NO requirement for ANY input."""
    monkeypatch.setattr(tw, "TRADE_MOVE_FILTER_ENABLED", False)
    assert trade_move_reason(150.0, 100.0, 100.0) is None   # wildly volatile
    assert trade_move_reason(None, None, None) is None      # unprovable
    monkeypatch.setattr(tw, "TRADE_MOVE_FILTER_ENABLED", True)
    assert trade_move_reason(150.0, 100.0, 100.0) == MOVE_VOLATILE
    assert trade_move_reason(None, None, None) == MOVE_UNPROVABLE


def test_the_toggle_env_parsing():
    assert tw._env_flag("NOPE_UNSET", True) is True
    assert tw._env_flag("NOPE_UNSET", False) is False
    import os
    os.environ["BLM_TEST_FLAG_X"] = "0"
    try:
        assert tw._env_flag("BLM_TEST_FLAG_X", True) is False
        os.environ["BLM_TEST_FLAG_X"] = "false"
        assert tw._env_flag("BLM_TEST_FLAG_X", True) is False
        os.environ["BLM_TEST_FLAG_X"] = "1"
        assert tw._env_flag("BLM_TEST_FLAG_X", True) is True
    finally:
        os.environ.pop("BLM_TEST_FLAG_X", None)


# ══════════════════════════════════════════════════════════════════════
# 4. THE MIN-HISTORY VARIANT — EXCLUDED at the default 0
# ══════════════════════════════════════════════════════════════════════

def test_min_history_variant_is_excluded_at_default_zero(monkeypatch):
    monkeypatch.setattr(tw, "TRADE_MOVE_MIN_HISTORY_S", 0.0)
    assert TRADE_MOVE_MIN_HISTORY_S == 0.0
    # at 0 the history leg is a NO-OP: a flat line passes with ANY history
    assert trade_move_reason(100.0, 100.0, 100.0, history_s=1.0) is None
    assert trade_move_reason(100.0, 100.0, 100.0, history_s=None) is None
    # and a volatile line is still refused (the move rule is untouched)
    assert trade_move_reason(110.0, 100.0, 100.0, history_s=1.0) == MOVE_VOLATILE


def test_min_history_variant_stricter_when_enabled(monkeypatch):
    monkeypatch.setattr(tw, "TRADE_MOVE_MIN_HISTORY_S", 900.0)
    # flat BUT too little captured history -> refused (fail closed)
    assert trade_move_reason(100.0, 100.0, 100.0,
                             history_s=60.0) == MOVE_UNPROVABLE
    assert trade_move_reason(100.0, 100.0, 100.0,
                             history_s=None) == MOVE_UNPROVABLE
    # enough history -> the move rule decides again
    assert trade_move_reason(100.0, 100.0, 100.0, history_s=1200.0) is None
    assert trade_move_reason(110.0, 100.0, 100.0,
                             history_s=1200.0) == MOVE_VOLATILE


# ══════════════════════════════════════════════════════════════════════
# 5. THE FROZEN BLOCK + THE ALERT ARE BYTE-UNCHANGED
# ══════════════════════════════════════════════════════════════════════

def _git_show(path):
    try:
        out = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=REPO,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def test_the_frozen_trade_window_prefix_is_byte_unchanged():
    """The new leg is APPENDED: the file must still begin with HEAD's bytes,
    so every frozen line (EXEC_*, MIN_PRICE_BANDS, min_price_for,
    price_reason, the trade verdicts) is byte-identical."""
    head = _git_show("blm_v4/trade_window.py")
    if head is None:
        pytest.skip("git unavailable")
    cur = (REPO / "blm_v4" / "trade_window.py").read_text()
    assert cur.startswith(head), "the frozen trade_window content changed"
    # the addition is exactly the appended tail, nothing above moved
    assert len(cur) > len(head)


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
    # the frozen floors behave exactly as before
    assert tw.min_price_for(80.0) == 1.85
    assert tw.min_price_for(85.0) == 1.85
    assert tw.price_reason(1.84, 80.0) == tw.PRICE_FLOOR
    assert tw.price_reason(1.85, 80.0) is None
    # the new leg lives ONLY in the appended block — no FROZEN function
    # references it
    for fn in (tw.min_price_for, tw.price_reason, tw.execution_window_reason,
               tw.score_reason, tw.exec_alert_reason,
               tw.exec_shortfall_reason, tw.trade_price_reason,
               tw.in_execution_window):
        assert "trade_move" not in inspect.getsource(fn), fn.__name__


# ══════════════════════════════════════════════════════════════════════
# 6. THE SERVED ARMED EXPRESSION FOLDS IN THE NEW LEG
# ══════════════════════════════════════════════════════════════════════

def test_the_served_armed_expression_folds_in_the_line_move_leg():
    src = inspect.getsource(api)
    armed = src.split('g["under_alert_exec_armed"] =', 1)[1][:800]
    # all five earlier legs AND the new one live in the SAME served boolean
    for leg in ("exec_alert_reason(", "exec_shortfall_reason(",
                "execution_window_reason(", "score_reason(",
                "trade_price_reason(", "trade_move_reason("):
        assert leg in armed, leg


def test_the_payload_carries_open_line_and_league_avg_line():
    src = inspect.getsource(api)
    assert "under_alert_line_move" in src
    for key in ('"entry_line"', '"open_line"', '"league_avg_line"',
                '"move_norm"', '"reason"'):
        assert key in src, key
    # read from the ONE line-reference authority the worker publishes
    assert 'line_reference' in src and '_line_reference(' in src


# ══════════════════════════════════════════════════════════════════════
# 7. THE LINE-REFERENCE ENGINE (first lines + per-competition average)
# ══════════════════════════════════════════════════════════════════════

def _clean_db(tmp_path):
    m = tmp_path / "blm_metrics_clean.db"
    con = sqlite3.connect(m)
    con.executescript(
        "CREATE TABLE clean_observations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, source_game_id TEXT,"
        " captured_at TEXT, live_total_line REAL);")
    con.executemany(
        "INSERT INTO clean_observations (source_game_id, captured_at,"
        " live_total_line) VALUES (?,?,?)",
        [("G1", "2026-10-12T10:00:00.000Z", None),      # no line yet
         ("G1", "2026-10-12T10:00:05.000Z", 210.0),     # FIRST captured line
         ("G1", "2026-10-12T10:05:00.000Z", 200.0),     # later move
         ("G2", "2026-10-12T09:00:00.000Z", 190.0),     # G2's first
         ("G3", "2026-10-12T10:00:00.000Z", None)])     # never a line
    con.commit()
    con.close()
    return m


def _games_db(tmp_path):
    g = tmp_path / "blm_pokerbet.db"
    con = sqlite3.connect(g)
    con.executescript("CREATE TABLE games (id INTEGER PRIMARY KEY,"
                      " source_game_id TEXT, competition_slug TEXT);")
    con.executemany(
        "INSERT INTO games (source_game_id, competition_slug) VALUES (?,?)",
        [("G1", "betual-nba"), ("G2", "betual-nba"), ("G3", "betual-nba")])
    con.commit()
    con.close()
    return g


def test_first_lines_picks_the_first_non_null_capture(tmp_path):
    m = _clean_db(tmp_path)
    con = sqlite3.connect(f"file:{m}?mode=ro", uri=True)
    try:
        fl = first_lines(con)
    finally:
        con.close()
    assert fl["G1"] == (210.0, "2026-10-12T10:00:05.000Z")   # not the later 200
    assert fl["G2"] == (190.0, "2026-10-12T09:00:00.000Z")
    assert "G3" not in fl                                    # no line ever


def test_build_reference_averages_first_lines_per_competition(tmp_path):
    m, g = _clean_db(tmp_path), _games_db(tmp_path)
    mcon = sqlite3.connect(f"file:{m}?mode=ro", uri=True)
    gcon = sqlite3.connect(f"file:{g}?mode=ro", uri=True)
    try:
        ref = build_reference(mcon, game_slug_map(gcon))
    finally:
        mcon.close()
        gcon.close()
    # mean of the per-game FIRST-captured lines: (210 + 190) / 2
    assert ref["avg"]["betual-nba"] == {"avg_line": 200.0, "games": 2}
    assert ref["open"]["G1"] == {"line": 210.0,
                                 "at": "2026-10-12T10:00:05.000Z"}


def test_the_reference_is_failure_isolated(tmp_path):
    con = sqlite3.connect(":memory:")
    try:
        assert first_lines(con) == {}
        assert game_slug_map(con) == {}
        assert build_reference(con, {}) == {"avg": {}, "open": {}}
    finally:
        con.close()


def test_a_game_absent_from_the_games_table_borrows_no_average(tmp_path):
    """A game with no competition_slug contributes to NO league average —
    never borrowed across competitions."""
    m, g = _clean_db(tmp_path), _games_db(tmp_path)
    mcon = sqlite3.connect(f"file:{m}?mode=ro", uri=True)
    gcon = sqlite3.connect(f"file:{g}?mode=ro", uri=True)
    try:
        ref = build_reference(mcon, {"G1": "betual-nba"})   # G2 slug missing
    finally:
        mcon.close()
        gcon.close()
    assert ref["avg"]["betual-nba"]["games"] == 1           # only G1
    assert ref["avg"]["betual-nba"]["avg_line"] == 210.0


# ══════════════════════════════════════════════════════════════════════
# 8. THE WORKER (change-detector, publish, fail-soft)
# ══════════════════════════════════════════════════════════════════════

def test_worker_publishes_and_change_detects(tmp_path):
    m, g = _clean_db(tmp_path), _games_db(tmp_path)
    got = {}
    w = LineReferenceWorker(m, g, interval_s=3600,
                            consumer=lambda avg, om: got.update(avg=avg,
                                                                 open=om))
    assert w.should_scan() == (True, "first_run")
    assert w._run_once() is True
    assert got["avg"]["betual-nba"]["avg_line"] == 200.0
    assert got["open"]["G1"]["line"] == 210.0
    # nothing changed -> the scan is SKIPPED
    assert w.should_scan()[0] is False
    # a new observation moves the change key -> rescan
    con = sqlite3.connect(m)
    con.execute("INSERT INTO clean_observations"
                " (source_game_id, captured_at, live_total_line)"
                " VALUES ('G2','2026-10-12T11:00:00.000Z',190.0)")
    con.commit()
    con.close()
    assert w.should_scan()[0] is True
    snap = w.snapshot()
    assert snap["runs"] >= 1 and snap["last_error"] is None


def test_worker_fails_soft_on_a_missing_database(tmp_path):
    w = LineReferenceWorker(tmp_path / "nope_metrics.db",
                            tmp_path / "nope_games.db", interval_s=3600)
    assert w._run_once() is False                 # never raises
    assert w.snapshot()["last_error"] is not None
    assert w.snapshot()["available"] is False


def test_the_api_wrapper_fails_closed_while_warming(monkeypatch):
    """No worker wired and no metrics DB beside the pokerbet DB -> an EMPTY
    reference (the trade leg then fails closed, never licenses a trade)."""
    monkeypatch.setattr(api, "_LINE_REF_WORKER", None)
    monkeypatch.setenv("BLM_POKERBET_DB", "/nonexistent/blm_pokerbet.db")
    assert api._line_reference(None) == {"avg": {}, "open": {}}


# ══════════════════════════════════════════════════════════════════════
# 9. END-TO-END — the SERVED game carries the numbers and the executor
#    refuses a volatile move (consuming the SAME armed boolean)
# ══════════════════════════════════════════════════════════════════════

ENTRY_LINE = 200.0


def _cfg(tmp_path):
    return BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=300.0,
        db_path=str(tmp_path / "blm_betting.db"))


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(api, "STATE_FILE", tmp_path / "collector_state.json")
    monkeypatch.setattr(api, "_pace_reference",
                        lambda conn: {"betual-nba": {"avg_pace": 4.0,
                                                     "games": 100}})
    monkeypatch.setattr(api, "_q3_pace_reference", lambda conn=None: {})
    monkeypatch.setattr(api, "_LINE_REF_WORKER", None)
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")               # noqa: E731
    gid = "MOVE-1"
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid, competition_id="comp-b",
        competition_slug="betual-nba", competition="Betual NBA",
        region="Virtual Matches", game_family="betual",
        classification="BETUAL_NBA", sport="basketball",
        home_team="Alpha", away_team="Beta", game_slug=f"{gid}-game",
        source_url=f"https://x/{gid}", status="live",
        first_seen_at=iso(now - timedelta(minutes=40)),
        last_seen_at=iso(now))
    st.upsert_game(game)
    for mins, hs, as_, total in ((30, 20, 18, ENTRY_LINE),
                                 (10, 60, 58, ENTRY_LINE),
                                 (0, 80, 78, ENTRY_LINE)):
        st.insert_snapshot(
            st.upsert_game(game),
            MarketObservation(
                source="PokerBet", source_game_id=gid,
                classification="BETUAL_NBA",
                captured_at=iso(now - timedelta(minutes=mins)),
                home_team="Alpha", away_team="Beta", home_score=hs,
                away_score=as_, period_label="3rd Quarter", quarter=3,
                clock="06:00", game_status="live", total_line=total,
                spread=None, w1_odds=None, w2_odds=None, markets_json="{}"),
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
    return {"db": db, "gid": gid, "tmp_path": tmp_path}


def _served(live_env):
    app = FastAPI()
    app.include_router(v4_router)
    body = TestClient(app).get("/api/v4/live").json()
    return next(g for g in body["games"] if g["game_id"] == live_env["gid"])


def _with_reference(monkeypatch, open_line, avg_line):
    monkeypatch.setattr(api, "_line_reference", lambda conn=None: {
        "avg": {"betual-nba": {"avg_line": avg_line, "games": 500}},
        "open": {"MOVE-1": {"line": open_line,
                            "at": "2026-10-12T09:00:00.000Z"}},
    })


def test_served_payload_carries_a_flat_move_and_arms(live_env, monkeypatch):
    """open == entry and the league average is large -> flat -> armed."""
    _with_reference(monkeypatch, open_line=ENTRY_LINE, avg_line=226.6)
    g = _served(live_env)
    lm = g["under_alert_line_move"]
    assert lm["entry_line"] == ENTRY_LINE
    assert lm["open_line"] == ENTRY_LINE
    assert lm["league_avg_line"] == 226.6
    assert lm["move_norm"] == 0.0
    assert lm["reason"] is None
    assert g["under_alert_exec_armed"] is True


def test_served_payload_refuses_a_volatile_move_and_disarms(live_env,
                                                           monkeypatch):
    """open far from entry -> move >= 2% -> the served boolean is FALSE and
    the executor refuses (it consumes the SAME boolean)."""
    # ENTRY 200 vs open 150 on a ~200 league -> |50|/190 = 26% (volatile)
    _with_reference(monkeypatch, open_line=150.0, avg_line=190.0)
    g = _served(live_env)
    lm = g["under_alert_line_move"]
    assert lm["move_norm"] is not None and lm["move_norm"] >= 0.02
    assert lm["reason"] == MOVE_VOLATILE
    assert g["under_alert_exec_armed"] is False
    got = evaluate(g, cfg=_cfg(live_env["tmp_path"]),
                   store=BettingStore(str(live_env["tmp_path"] / "b.db")),
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 0, "amount": 0.0})
    assert got["decision"] == "NO_BET"
    assert got["reason"] == "alert_not_active"


def test_served_payload_fails_closed_without_a_reference(live_env,
                                                         monkeypatch):
    """An EMPTY line reference (a game the worker has not seen) REFUSES —
    an unprovable move never licenses a trade."""
    monkeypatch.setattr(api, "_line_reference",
                        lambda conn=None: {"avg": {}, "open": {}})
    g = _served(live_env)
    lm = g["under_alert_line_move"]
    assert lm["open_line"] is None and lm["league_avg_line"] is None
    assert lm["reason"] == MOVE_UNPROVABLE
    assert g["under_alert_exec_armed"] is False


def test_served_payload_with_the_filter_disabled_still_arms(live_env,
                                                            monkeypatch):
    """Toggle OFF: a volatile move no longer disarms (the leg imposes no
    requirement)."""
    monkeypatch.setattr(tw, "TRADE_MOVE_FILTER_ENABLED", False)
    _with_reference(monkeypatch, open_line=150.0, avg_line=190.0)
    g = _served(live_env)
    assert g["under_alert_line_move"]["reason"] is None
    assert g["under_alert_exec_armed"] is True
