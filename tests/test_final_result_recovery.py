"""NO FINAL result recovery — directive 2026-09-22.

Two production defects were diagnosed (investigation 2026-09-22):

1. The collector's LAST look at a game can be a degenerate SPA row
   (home_score/away_score/period all NULL) or a mid-quarter scored row —
   because the list-page DOM renders blank / the game vanishes from the
   source panel and ENDED_GRACE_S=60 untracks it before any terminal
   state is captured.  The scorecard then grades `rows[-1]`: a NULL-score
   row → UNKNOWN with NULL finals, and no new data ever arrives.  The
   dashboard honestly renders NO FINAL.  (12 of 57 ended games broken on
   2026-09-21/22; 9/12 ended on a blank row, all 12 with game_status
   'live'; backlog 5,441 UNKNOWN + 686 INVALID game_results rows.)

2. Grading reads ONLY rows[-1]; an earlier fully-scored terminal row is
   ignored even though it is authoritative stored data.

The fix under test:

  scorecard — grade from the LAST FULLY-SCORED row when rows[-1] is
  degenerate (the verdict rules themselves untouched: a mid-quarter
  scored tail still stays UNKNOWN); `result_at` points at the graded
  row; the incremental settle gate compares against the last SCORED
  row's timestamp (blanks cannot change the verdict, so a scored-row
  verdict whose history only grew blank rows stays settled — while an
  UNKNOWN game that later receives scored data is re-graded, which is
  how the existing backlog self-heals).

  collector — a game at grace expiry whose tail cannot prove a final
  gets ONE bounded final-capture window (BLM_FINAL_CAPTURE_GRACE_S,
  default 120s): it stays tracked, is queued at the FRONT of the slow
  event-view rotation (bypassing the freshness gate), and the proven
  verified event view — which returns real scores when the list page
  renders blanks — captures real/terminal state through the existing
  verified path.  When the window expires (or the tail already carries
  a Q4/final-labeled scored row) the normal ended path runs unchanged.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector
from blm_v4.models import MarketObservation, PokerBetGame, utcnow_iso
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore


# ── helpers ────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _store(tmp_path) -> PokerBetStore:
    return PokerBetStore(tmp_path / "blm_pokerbet.db")


def _add_game(st: PokerBetStore, gid: str, status: str = "ended") -> int:
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-betual", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=f"{gid} Home",
        away_team=f"{gid} Away", game_slug=f"{gid.lower()}-game",
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=_iso(base), last_seen_at=_iso(base + timedelta(hours=1)),
    )
    return st.upsert_game(game)


def _snap(st: PokerBetStore, gid: str, gid_db: int, mins_ago: float,
          home, away, label: str = "", quarter=None, clock: str = "") -> str:
    """Insert one snapshot; returns its captured_at (for exact asserts)."""
    ts = _iso(datetime.now(timezone.utc) - timedelta(minutes=mins_ago))
    st.insert_snapshot(gid_db, MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=ts, home_team=f"{gid} Home", away_team=f"{gid} Away",
        home_score=home, away_score=away,
        period_label=label, quarter=quarter, clock=clock,
        game_status="live", total_line=180.5,
        markets_json="{}",
    ), force=True)
    return ts


def _result(tmp_path, gid: str) -> sqlite3.Row:
    conn = sqlite3.connect(f"file:{tmp_path / 'blm_pokerbet.db'}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM game_results WHERE source_game_id=?",
                            (gid,)).fetchone()
    finally:
        conn.close()


# BETUAL_NBA: quarter=10min, full=40.  A "4th Quarter" 01:00 row has
# elapsed 3*10 + (10-1) = 39 >= 40-2 → _final_result grades OK.
# A "3rd Quarter" 01:00 row (elapsed 30 < 38) stays UNKNOWN.  nsnaps must
# be >= 5 for any OK, so fixtures carry five rows.


# ════════════════════════════════════════════════════════════════════
# 1. scorecard — degenerate-tail grading rule
# ════════════════════════════════════════════════════════════════════

def test_degenerate_tail_grades_from_last_scored_terminal_row(tmp_path):
    """Blank SPA row last + scored Q4 row before it → OK from that row.

    The dominant real-world shape (9 of 12 broken games): the collector's
    final look is blank, but the game's Q4 state was captured minutes
    earlier.  Pre-fix this graded UNKNOWN/NULL forever → NO FINAL."""
    st = _store(tmp_path)
    gid = "9001"
    gid_db = _add_game(st, gid)
    _snap(st, gid, gid_db, 44, 10, 9, "1st Quarter", 1, "08:00")
    _snap(st, gid, gid_db, 42, 30, 28, "2nd Quarter", 2, "06:00")
    _snap(st, gid, gid_db, 40, 50, 48, "3rd Quarter", 3, "01:00")
    scored_ts = _snap(st, gid, gid_db, 30, 60, 55, "4th Quarter", 4, "01:00")
    _snap(st, gid, gid_db, 28, None, None)          # the degenerate tail

    s = Scorecard(tmp_path / "blm_pokerbet.db")
    s.capture_results()

    r = _result(tmp_path, gid)
    assert r is not None
    assert r["final_result_status"] == "OK"
    assert r["final_home"] == 60 and r["final_away"] == 55
    assert r["final_total"] == 115
    # result_at points at the GRADED (scored) row, not the blank one
    assert r["result_at"] == scored_ts


def test_degenerate_tail_with_midgame_scored_row_stays_unknown(tmp_path):
    """Blank tail + only a MID-QUARTER scored row → still UNKNOWN.

    The rule must never guess: a 3rd-quarter score is not a final.  But
    result_at moves to the graded (scored) row, not the blank one."""
    st = _store(tmp_path)
    gid = "9002"
    gid_db = _add_game(st, gid)
    _snap(st, gid, gid_db, 44, 10, 8, "1st Quarter", 1, "08:00")
    _snap(st, gid, gid_db, 42, 40, 30, "2nd Quarter", 2, "06:00")
    scored_ts = _snap(st, gid, gid_db, 35, 75, 54, "3rd Quarter", 3, "01:00")
    _snap(st, gid, gid_db, 30, None, None)
    _snap(st, gid, gid_db, 29, None, None)

    s = Scorecard(tmp_path / "blm_pokerbet.db")
    s.capture_results()

    r = _result(tmp_path, gid)
    assert r["final_result_status"] == "UNKNOWN"
    assert r["final_home"] is None and r["final_away"] is None
    assert r["final_total"] is None
    assert r["result_at"] == scored_ts


def test_scored_tail_grading_unchanged(tmp_path):
    """Pin: a history ending on a scored Q4 row grades OK exactly as
    before (the rule only changes WHICH row grades a degenerate tail)."""
    st = _store(tmp_path)
    gid = "9003"
    gid_db = _add_game(st, gid)
    _snap(st, gid, gid_db, 44, 10, 9, "1st Quarter", 1, "08:00")
    _snap(st, gid, gid_db, 42, 30, 28, "2nd Quarter", 2, "06:00")
    _snap(st, gid, gid_db, 40, 50, 48, "3rd Quarter", 3, "01:00")
    _snap(st, gid, gid_db, 30, 60, 55, "4th Quarter", 4, "01:00")
    last_ts = _snap(st, gid, gid_db, 29, 62, 56, "4th Quarter", 4, "00:30")

    s = Scorecard(tmp_path / "blm_pokerbet.db")
    s.capture_results()

    r = _result(tmp_path, gid)
    assert r["final_result_status"] == "OK"
    assert r["final_total"] == 118
    assert r["result_at"] == last_ts


def test_invalid_history_still_wins_over_degenerate_rule(tmp_path):
    """Pin: contamination INVALIDates the whole game regardless of any
    scored terminal row — the rule must not resurrect a poisoned game."""
    st = _store(tmp_path)
    gid = "9004"
    gid_db = _add_game(st, gid)
    _snap(st, gid, gid_db, 44, 10, 9, "1st Quarter", 1, "08:00")
    _snap(st, gid, gid_db, 42, 30, 28, "2nd Quarter", 2, "06:00")
    _snap(st, gid, gid_db, 40, 50, 48, "3rd Quarter", 3, "01:00")
    _snap(st, gid, gid_db, 39, 160, 155, "4th Quarter", 4, "01:00")  # +217/60s
    _snap(st, gid, gid_db, 30, None, None)

    s = Scorecard(tmp_path / "blm_pokerbet.db")
    s.capture_results()

    r = _result(tmp_path, gid)
    assert r["final_result_status"] == "INVALID"


def test_settle_gate_ignores_trailing_blank_rows(tmp_path):
    """A scored-row verdict whose history only GREW blank rows is settled:
    the gate's exact-input test compares against the last SCORED row —
    trailing degenerate rows cannot change the verdict, so the second
    pass must skip the game instead of re-grading forever."""
    st = _store(tmp_path)
    gid = "9005"
    gid_db = _add_game(st, gid)
    _snap(st, gid, gid_db, 44, 10, 9, "1st Quarter", 1, "08:00")
    _snap(st, gid, gid_db, 42, 30, 28, "2nd Quarter", 2, "06:00")
    _snap(st, gid, gid_db, 40, 50, 48, "3rd Quarter", 3, "01:00")
    _snap(st, gid, gid_db, 30, 60, 55, "4th Quarter", 4, "01:00")
    scored_ts = _snap(st, gid, gid_db, 29, 62, 56, "4th Quarter", 4, "00:30")

    s = Scorecard(tmp_path / "blm_pokerbet.db")
    # arm the incremental settle gate (default OFF until a full pass
    # stamps the revision): the gate's skip behavior is what we assert
    s._store_revision()
    stats1 = s.capture_results()
    assert stats1["ok"] == 1

    # history grows by a degenerate row only (the collector's blank tail)
    _snap(st, gid, gid_db, 28, None, None)
    stats2 = s.capture_results()
    assert stats2["skipped_settled"] == 1, (
        "a verdict graded from the scored row must stay settled when only "
        "blank rows arrive — otherwise every pass re-reads every snapshot")

    r = _result(tmp_path, gid)
    assert r["final_total"] == 118          # verdict unchanged
    assert r["result_at"] == scored_ts


def test_unknown_regrades_when_scored_data_arrives(tmp_path):
    """The backlog self-heal: an UNKNOWN game (degenerate-only tail at
    grading time) is NOT settled once a scored row arrives — the next
    pass re-grades from it (UNKNOWN → OK when the row is terminal)."""
    st = _store(tmp_path)
    gid = "9006"
    gid_db = _add_game(st, gid)
    _snap(st, gid, gid_db, 44, 10, 9, "1st Quarter", 1, "08:00")
    _snap(st, gid, gid_db, 42, 30, 28, "2nd Quarter", 2, "06:00")
    _snap(st, gid, gid_db, 31, None, None)          # blank row
    _snap(st, gid, gid_db, 30, None, None)          # blank tail at grade 1

    s = Scorecard(tmp_path / "blm_pokerbet.db")
    s._store_revision()
    stats1 = s.capture_results()
    assert stats1["unknown"] == 1

    # later: the collector's final-capture window lands the terminal state
    scored_ts = _snap(st, gid, gid_db, 29, 62, 56, "4th Quarter", 4, "00:30")
    time.sleep(0.02)   # the gate's grace cut must exclude this new row
    stats2 = s.capture_results()
    assert stats2["ok"] == 1, "scored data must re-open grading"

    r = _result(tmp_path, gid)
    assert r["final_result_status"] == "OK"
    assert r["final_total"] == 118


# ════════════════════════════════════════════════════════════════════
# 2. collector — final event-view capture before untracking
# ════════════════════════════════════════════════════════════════════

def _tracked_collector(tmp_path, tail: str):
    """A collector with ONE tracked live game one tick from grace expiry.

    tail: "blank" (degenerate newest row), "q4" (scored Q4 tail — provable
    final), or "q3" (scored mid-game tail)."""
    st = _store(tmp_path)
    c = PokerBetCollector(store=st)
    gid = "8001"
    gid_db = _add_game(st, gid, status="live")
    if tail == "q4":
        _snap(st, gid, gid_db, 30, 60, 55, "4th Quarter", 4, "01:00")
    elif tail == "q3":
        _snap(st, gid, gid_db, 30, 50, 48, "3rd Quarter", 3, "01:00")
    else:
        _snap(st, gid, gid_db, 30, None, None)
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-betual", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team="8001 Home",
        away_team="8001 Away", game_slug="8001-game",
        source_url=f"https://x/{gid}", status="live",
        first_seen_at=utcnow_iso(), last_seen_at=utcnow_iso(),
    )
    cls = Classification.BETUAL_NBA.value
    key = f"{game.home_team}|{game.away_team}"
    c._tracked[cls][key] = game
    c._unseen_ticks[cls][key] = c._ended_grace_ticks - 1
    return c, st, gid, gid_db, game


def test_mark_ended_blank_tail_keeps_tracking_and_queues_capture(tmp_path):
    """At grace expiry with a blank tail the game is NOT untracked: it
    stays live, is queued at the FRONT of the slow rotation, and the
    final-capture window is armed."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="blank")
    c._market_queue = ["9999", gid]
    c._mark_ended({})          # one more unseen tick → grace reached

    cls = Classification.BETUAL_NBA.value
    key = "8001 Home|8001 Away"
    assert key in c._tracked[cls], "blank-tail game must stay tracked"
    assert game.status != "ended"
    assert gid in c._final_capture_gids
    assert gid in c._final_capture_until
    assert gid in c._market_queue
    assert c._market_queue[0] == gid, "final capture must be PRIORITIZED"
    assert c.stats["final_capture_retries"] == 1
    # the store row is still live (no premature 'ended')
    assert st.get_game(gid)["status"] == "live"


def test_mark_ended_midgame_scored_tail_also_gets_window(tmp_path):
    """A scored but mid-game tail (the 30981756 shape: Q3 01:00) cannot
    prove a final either — it gets the same bounded window."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="q3")
    c._mark_ended({})
    cls = Classification.BETUAL_NBA.value
    assert "8001 Home|8001 Away" in c._tracked[cls]
    assert gid in c._final_capture_gids
    assert game.status != "ended"


def test_mark_ended_provable_tail_ends_normally(tmp_path):
    """Pin: a Q4/final-labeled scored tail ends exactly as before —
    untracked, ended, no window."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="q4")
    c._mark_ended({})

    cls = Classification.BETUAL_NBA.value
    assert "8001 Home|8001 Away" not in c._tracked[cls]
    assert game.status == "ended"
    assert st.get_game(gid)["status"] == "ended"
    assert gid not in c._final_capture_gids


def test_final_capture_window_expiry_ends_normally(tmp_path):
    """When the bounded window expires the normal ended path runs."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="blank")
    c._mark_ended({})                       # arms the window
    c._final_capture_until[gid] = time.monotonic() - 1.0   # expired
    c._mark_ended({})

    cls = Classification.BETUAL_NBA.value
    assert "8001 Home|8001 Away" not in c._tracked[cls]
    assert game.status == "ended"
    assert gid not in c._final_capture_gids


def test_final_capture_window_stays_open_until_expiry(tmp_path):
    """While the window runs the game is kept tracked on every tick."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="blank")
    c._mark_ended({})                       # arms
    c._mark_ended({})                       # still inside the window
    c._mark_ended({})
    cls = Classification.BETUAL_NBA.value
    assert "8001 Home|8001 Away" in c._tracked[cls]
    assert game.status != "ended"
    assert c.stats["final_capture_retries"] == 1, "arm must happen ONCE"


def test_end_game_clears_final_capture_state(tmp_path):
    """A verified final via the slow path (end=True) must clean up the
    final-capture bookkeeping — the game is gone."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="blank")
    c._mark_ended({})                       # arms window + queues front
    c._end_game(game)
    assert gid not in c._final_capture_gids
    assert gid not in c._market_queue


def test_final_capture_grace_zero_disables_window(tmp_path, monkeypatch):
    """BLM_FINAL_CAPTURE_GRACE_S=0 restores the old behavior exactly."""
    monkeypatch.setenv("BLM_FINAL_CAPTURE_GRACE_S", "0")
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="blank")
    c._mark_ended({})
    cls = Classification.BETUAL_NBA.value
    assert "8001 Home|8001 Away" not in c._tracked[cls]
    assert game.status == "ended"


def test_slow_market_priority_visits_final_capture_gid(tmp_path):
    """A fresh `_last_market_at` would normally skip a game (freshness
    gate); a final-capture gid must be visited anyway."""
    c, st, gid, gid_db, game = _tracked_collector(tmp_path, tail="blank")
    c._mark_ended({})                       # arms window + queues front
    # a fresh market stamp would make a NORMAL game skip as fresh
    c._last_market_at[gid] = utcnow_iso()
    c._slow_page = object()                 # non-None → the round runs
    c._ensure_comp_lobby = lambda page, cls: False   # fail AFTER the gates

    c._capture_slow_market()

    assert c._market_stats.get("attempts", 0) >= 1, (
        "final-capture gid must bypass the freshness gate")
    assert c._market_stats.get("skipped_fresh", 0) == 0
