"""Regression tests for the post-final replay guard.

Confirmed defect (2026-09-05): finished BETUAL_NBA games 30802376 /
30802377 (game_ids 4859 / 4860) were re-adopted as live when the source
re-listed the finished events with an EARLIER frozen frame (123-118 /
102-107 @ 01:30, blank period label, same start_time) — repeated
byte-identical payloads for 30+ minutes after the finals (123-121 /
102-109) were recorded.  `_detect_event_reset` could not classify the
frame: the replayed total (241) is within the score_drop threshold (not
< 50% of 244) and the blank period makes clock_regression unparseable.

The fix adds a third positive-evidence signal — `post_final_replay`: the
fixture ALREADY has a recorded final (game_results.final_total) and the
observed total is strictly BELOW that final.  A live game can never dip
below its own recorded final, so the frame must be an earlier replay of
the finished fixture.  The existing score_drop / clock_regression
signals are unchanged, and legitimate static states (halftime, short
same-score stretches, final-equal frames) must NOT be classified as
replays.
"""

import json
from datetime import datetime, timedelta, timezone

from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector
from blm_v4.discovery import RowGame
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_store(tmp_path) -> PokerBetStore:
    return PokerBetStore(tmp_path / "blm.db")


def _add_game(st: PokerBetStore, gid: str, home: str = "Toronto Raptors Virtual",
              away: str = "Philadelphia 76ers Virtual",
              status: str = "ended") -> int:
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team=home, away_team=away,
        game_slug="home-virtual-away-virtual",
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=_iso(datetime.now(timezone.utc) - timedelta(hours=2)),
        last_seen_at=_iso(datetime.now(timezone.utc)),
    )
    return st.upsert_game(game)


def _snap(st: PokerBetStore, gid_db: int, gid: str, t: datetime,
          hs: int, as_: int, q: int | None, clock: str,
          period: str | None = None) -> None:
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=_iso(t), home_team="H", away_team="A",
        home_score=hs, away_score=as_,
        period_label=period if period is not None else (
            f"{q}th Quarter" if q is not None else ""),
        quarter=q, clock=clock,
        game_status="live", total_line=None,
        markets_json=json.dumps({}),
    )
    st.insert_snapshot(gid_db, obs, force=True)


def _record_final(st: PokerBetStore, gid: str, home: int, away: int,
                  status: str = "OK") -> None:
    """Insert a game_results row (schema mirrors scorecard.py)."""
    with st._lock:
        conn = st._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_game_id TEXT,
                    classification TEXT,
                    final_home INTEGER,
                    final_away INTEGER,
                    final_total INTEGER,
                    result_at TEXT,
                    final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN'
                )
            """)
            conn.execute("""
                INSERT INTO game_results
                (source_game_id, classification, final_home, final_away,
                 final_total, result_at, final_result_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (gid, "BETUAL_NBA", home, away, home + away,
                  _iso(datetime.now(timezone.utc)), status))
            conn.commit()
        finally:
            conn.close()


def _game(gid: str, status: str = "ended") -> PokerBetGame:
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Toronto Raptors Virtual", away_team="Philadelphia 76ers Virtual",
        game_slug="t-p", source_url=f"https://x/{gid}", status=status,
    )


def _collector(st: PokerBetStore) -> PokerBetCollector:
    return PokerBetCollector(store=st)


def test_post_final_replay_detected_confirmed_defect(tmp_path):
    """The exact 4859/30802376 case: finished 123-121, replay frame
    123-118 @ 01:30 with a blank period label must be a replay."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802376", status="ended")
    _snap(st, gid_db, "30802376",
          datetime.now(timezone.utc) - timedelta(hours=1), 123, 121, 4, "00:15")
    _record_final(st, "30802376", 123, 121)  # final_total 244, OK
    c = _collector(st)
    # fails against the old implementation (None), passes with the guard
    assert c._detect_event_reset(_game("30802376"), 123, 118, "", "01:30") \
        == "post_final_replay"


def test_finished_game_replay_both_games_and_routing(tmp_path):
    """Structural case for both observed games (4859/4860) plus the
    existing-architecture routing: split to a fresh #i1, base stays ended."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802376", status="live")
    _snap(st, gid_db, "30802376",
          datetime.now(timezone.utc) - timedelta(hours=1), 123, 121, 4, "00:15")
    _record_final(st, "30802376", 123, 121)
    gid_db2 = _add_game(st, "30802377",
                        home="Orlando Magic Virtual", away="Los Angeles Lakers Virtual",
                        status="live")
    _snap(st, gid_db2, "30802377",
          datetime.now(timezone.utc) - timedelta(hours=1), 102, 109, 4, "00:15")
    _record_final(st, "30802377", 102, 109)
    c = _collector(st)
    assert c._detect_event_reset(_game("30802376"), 123, 118, "", "01:30") \
        == "post_final_replay"
    assert c._detect_event_reset(_game("30802377"), 102, 107, "", "01:30") \
        == "post_final_replay"
    # list-path detection drives the split
    row = RowGame(home_team="Toronto Raptors Virtual",
                  away_team="Philadelphia 76ers Virtual",
                  home_score=123, away_score=118,
                  period_label="", clock="01:30",
                  w1_odds=None, w2_odds=None, spread_indicator=None)
    game = _game("30802376", status="live")
    sig = c._detect_instance_reset(game, row)
    assert sig == "post_final_replay"
    new_game = c._split_instance(game, row, Classification.BETUAL_NBA,
                                 signal=sig, path="list")
    assert new_game.source_game_id == "30802376#i1"
    assert game.status == "ended"
    assert c._instances["30802376"] == "30802376#i1"


def test_halftime_static_state_not_replay(tmp_path):
    """Legitimate halftime pause: same score/clock repeatedly, NO recorded
    final — must never be classified as a replay."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802380", status="live")
    t = datetime.now(timezone.utc) - timedelta(minutes=30)
    _snap(st, gid_db, "30802380", t, 57, 48, None, "12:00", period="Half End")
    c = _collector(st)
    game = _game("30802380", status="live")
    assert c._detect_event_reset(game, 57, 48, "Half End", "12:00") is None
    assert st.get_recorded_final("30802380") is None


def test_normal_static_live_frame_not_replay(tmp_path):
    """A legitimately live game with an unchanged score/clock for a short
    interval and no recorded final is NOT a replay."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802381", status="live")
    _snap(st, gid_db, "30802381",
          datetime.now(timezone.utc) - timedelta(minutes=5), 20, 24, 1, "00:45")
    c = _collector(st)
    game = _game("30802381", status="live")
    assert c._detect_event_reset(game, 20, 24, "1st Quarter", "00:45") is None


def test_equal_to_recorded_final_not_replay(tmp_path):
    """A frame equal to the recorded final (same total) is not
    demonstrably earlier — the guard must NOT fire on it."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802384", status="ended")
    _snap(st, gid_db, "30802384",
          datetime.now(timezone.utc) - timedelta(hours=1), 123, 121, 4, "00:15")
    _record_final(st, "30802384", 123, 121)
    c = _collector(st)
    assert c._detect_event_reset(_game("30802384"), 123, 121, "", "00:00") is None


def test_existing_score_drop_unchanged(tmp_path):
    """Existing score_drop behavior is untouched (fires before the new
    signal; continuation stays None even with a recorded final)."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802382", status="ended")
    _snap(st, gid_db, "30802382",
          datetime.now(timezone.utc) - timedelta(minutes=20), 96, 88, 4, "00:00")
    _record_final(st, "30802382", 96, 88)  # final_total 184
    c = _collector(st)
    game = _game("30802382")
    assert c._detect_event_reset(game, 31, 24, "2nd Quarter", "20:00") \
        == "score_drop"  # 55 < 92: big drop wins over the new signal
    assert c._detect_event_reset(game, 97, 89, "4th Quarter", "01:00") is None


def test_existing_clock_regression_unchanged(tmp_path):
    """Existing clock_regression behavior is untouched (mirrors
    test_collector_detects_reset_via_clock_regression)."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "30802383", status="live")
    t = datetime.now(timezone.utc) - timedelta(minutes=5)
    _snap(st, gid_db, "30802383", t, 41, 52, 4, "00:45")
    c = _collector(st)
    game = _game("30802383", status="live")
    assert c._detect_event_reset(game, 28, 28, "1st Quarter", "01:30") \
        == "clock_regression"
    assert c._detect_event_reset(game, 44, 54, "4th Quarter", "00:30") is None
    assert c._detect_event_reset(game, 50, 55, "2nd Quarter", "05:00") \
        == "clock_regression"
    # forward jumps stay legitimate (list feed lags ~7x game speed)
    _snap(st, gid_db, "30802383", datetime.now(timezone.utc), 19, 14, 1, "03:45")
    assert c._detect_event_reset(game, 62, 71, "4th Quarter", "21:00") is None
    assert c._detect_event_reset(game, 44, 54, "4th Quarter", "00:30") is None