"""Regression tests for the halftime-continuity guard + instance-chain bridge.

Defect 30964771 (2026-09-20): a game re-resolved by the collector at its
halftime boundary re-keyed to a #i1 sibling.  The half-end sentinel
(Q2-end 24 game-min elapsed vs a fresh "Half End"/12:00 frame reading 12)
misread as `clock_regression`, the base series died at halftime, and the
final lived only in the sibling — the base game could never settle and
every alert on it rendered NO FINAL.

Two fixes, both pinned here:
  1. `_detect_event_reset` — a frame at/after the half boundary whose
     total is >= the stored last total is CONTINUATION, never a replay;
     only a strict total DROP with an earlier clock is clock_regression.
  2. `settle_once` — a base game with a non-OK verdict whose `base#i*`
     chain completes the game settles OK from the chained series, written
     to the BASE game_results row.  Chained-INVALID falls back to the
     base verdict; OK base rows are never re-read.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from blm_v4.collector import PokerBetCollector
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.settle_worker import settle_once
from blm_v4.storage import PokerBetStore

_NBA = "BETUAL_NBA"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _game(gid: str = "30964771", status: str = "ended") -> PokerBetGame:
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification=_NBA, sport="basketball",
        home_team="Lakers Virtual", away_team="Celtics Virtual",
        game_slug="lakers-virtual-celtics-virtual",
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=_iso(datetime.now(timezone.utc) - timedelta(hours=2)),
        last_seen_at=_iso(datetime.now(timezone.utc)),
    )


def _make_store(tmp_path) -> PokerBetStore:
    return PokerBetStore(tmp_path / "blm.db")


def _snap(st: PokerBetStore, gid_db: int, gid: str, t: datetime,
          hs: int, as_: int, q: int | None, clock: str,
          period: str | None = None) -> None:
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid, classification=_NBA,
        captured_at=_iso(t), home_team="H", away_team="A",
        home_score=hs, away_score=as_,
        period_label=period if period is not None else (
            f"{q}th Quarter" if q is not None else ""),
        quarter=q, clock=clock,
        game_status="live", total_line=None,
        markets_json=json.dumps({}),
    )
    st.insert_snapshot(gid_db, obs, force=True)


# ══════════════════════════════════════════════════════════════════════
# 1. the halftime-continuity guard
# ══════════════════════════════════════════════════════════════════════

def _collector_with_last_row(tmp_path, last_row: dict):
    """A collector whose store returns `last_row` as the game's newest
    snapshot (the state the reset detector compares against)."""
    st = _make_store(tmp_path)
    gid_db = st.upsert_game(_game())
    _snap(st, gid_db, "30964771",
          datetime(2026, 9, 20, 11, 40, 38, tzinfo=timezone.utc),
          last_row["home_score"], last_row["away_score"],
          last_row.get("quarter"), last_row.get("clock", ""),
          last_row.get("period_label"))
    collector = PokerBetCollector(store=st)
    return collector


def test_halftime_continuation_is_not_a_replay(tmp_path):
    """The 30964771 shape: Q2-end (24 min, 47-61) then a fresh frame at the
    half-end sentinel with the SAME score.  Continuation — no split."""
    collector = _collector_with_last_row(tmp_path, {
        "quarter": 2, "clock": "00:00", "period_label": "2nd Quarter",
        "home_score": 47, "away_score": 61,
    })
    sig = collector._detect_event_reset(_game(), 47, 61, "Half End", "12:00")
    assert sig is None


def test_q3_first_points_are_continuation(tmp_path):
    """Q3 under way, scores ADDED (49-63): still the same game."""
    collector = _collector_with_last_row(tmp_path, {
        "quarter": 2, "clock": "00:00", "period_label": "2nd Quarter",
        "home_score": 47, "away_score": 61,
    })
    sig = collector._detect_event_reset(_game(), 49, 63, "3rd Quarter", "11:00")
    assert sig is None


def test_score_drop_still_splits(tmp_path):
    """A genuine replay restart (total collapses below 50%) still splits."""
    collector = _collector_with_last_row(tmp_path, {
        "quarter": 4, "clock": "01:00", "period_label": "4th Quarter",
        "home_score": 55, "away_score": 60,
    })
    sig = collector._detect_event_reset(_game(), 28, 28, "1st Quarter", "10:00")
    assert sig == "score_drop"


def test_post_final_replay_still_splits(tmp_path):
    """The 2026-09-05 signal-3 case is untouched by the continuity guard."""
    from tests.test_replay_post_final import _record_final
    st = _make_store(tmp_path)
    gid_db = st.upsert_game(_game("30802376", status="ended"))
    _snap(st, gid_db, "30802376",
          datetime.now(timezone.utc) - timedelta(hours=1), 123, 121, 4, "00:15")
    _record_final(st, "30802376", 123, 121)
    collector = PokerBetCollector(store=st)
    assert collector._detect_event_reset(
        _game("30802376"), 123, 118, "", "01:30") == "post_final_replay"


# ══════════════════════════════════════════════════════════════════════
# 2. the instance-chain settlement bridge
# ══════════════════════════════════════════════════════════════════════

def _pipeline(tmp_path):
    """Real storage DB + ids for base/#i1 games, and a raw sqlite handle
    for the worker (which opens the DB by path)."""
    db = tmp_path / "blm_pokerbet.db"
    st = PokerBetStore(db)
    base = st.upsert_game(_game("30964771", status="ended"))
    sib = st.upsert_game(_game("30964771#i1", status="ended"))
    return st, base, sib


def _run_worker(tmp_path, st):
    stats = settle_once(tmp_path / "blm_pokerbet.db")
    conn = sqlite3.connect(str(tmp_path / "blm_pokerbet.db"))
    conn.row_factory = sqlite3.Row
    return stats, conn


def test_bridge_settles_base_from_split_final(tmp_path):
    """Base dies at halftime; #i1 carries Q4 00:00.  The base row settles
    OK with the SIBLING's final — the 30964771 defect, end to end."""
    st, base, sib = _pipeline(tmp_path)
    t = datetime(2026, 9, 20, 10, 45, tzinfo=timezone.utc)

    def snap(gid, gid_num, q, clock, label, hs, as_):
        nonlocal t
        t += timedelta(seconds=20)
        _snap(st, gid_num, gid, t, hs, as_, q, clock, label)

    hs = as_ = 0
    for i in range(24):
        if i % 2 == 0:
            hs += 2
        else:
            as_ += 2
        q, clock, label = (1, f"{12 - (i % 12):02d}:00", "1st Quarter") \
            if i < 12 else (2, f"{12 - (i % 12):02d}:00", "2nd Quarter")
        snap("30964771", base, q, clock, label, hs, as_)
    snap("30964771", base, 2, "00:00", "2nd Quarter", 47, 61)
    # sibling: halftime rows then Q3/Q4 to a verified finish
    snap("30964771#i1", sib, None, "12:00", "Half End", 47, 61)
    hs, as_ = 47, 61
    for i in range(24):
        hs += 1 if i % 3 else 2
        as_ += 2
        q, clock, label = (3, f"{12 - (i % 12):02d}:00", "3rd Quarter") \
            if i < 12 else (4, f"{12 - (i % 12):02d}:00", "4th Quarter")
        snap("30964771#i1", sib, q, clock, label, hs, as_)
    snap("30964771#i1", sib, 4, "00:00", "4th Quarter", hs, as_)

    stats, conn = _run_worker(tmp_path, st)
    # the base settles via the bridge; the sibling also has a valid history
    # ending at the same final, so it settles OK on its own — the defect's
    # point is the BASE row, so assert on that row below
    assert stats["settled"] >= 1
    row = conn.execute(
        "SELECT final_total, final_result_status FROM game_results "
        "WHERE source_game_id='30964771'").fetchone()
    assert row["final_result_status"] == "OK"
    assert row["final_total"] == hs + as_      # the SIBLING's final, on the BASE row
    conn.close()


def test_bridge_does_not_touch_ok_base(tmp_path):
    """A base game that settles OK on its own is never re-read."""
    st, base, sib = _pipeline(tmp_path)
    t = datetime(2026, 9, 20, 10, 45, tzinfo=timezone.utc)

    def snap(gid, gid_num, q, clock, label, hs, as_):
        nonlocal t
        t += timedelta(seconds=20)
        _snap(st, gid_num, gid, t, hs, as_, q, clock, label)

    for i in range(6):
        snap("30964771", base, 4, "00:00" if i == 5 else "01:00",
             "4th Quarter", 50 + i, 55 + i)
    # a DIFFERENT game's sibling carrying a wildly different final
    snap("30964771#i1", sib, 4, "00:00", "4th Quarter", 99, 120)

    _run_worker(tmp_path, st)
    conn = sqlite3.connect(str(tmp_path / "blm_pokerbet.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT final_total FROM game_results "
        "WHERE source_game_id='30964771'").fetchone()
    assert row["final_total"] == 50 + 5 + 55 + 5     # base's own final
    conn.close()


def test_bridge_chained_invalid_falls_back_to_base_verdict(tmp_path):
    """A poisoned chained history (non-monotonic scores) must not invent an
    OK verdict — the base UNKNOWN stands."""
    st, base, sib = _pipeline(tmp_path)
    t = datetime(2026, 9, 20, 10, 45, tzinfo=timezone.utc)

    def snap(gid, gid_num, q, clock, label, hs, as_):
        nonlocal t
        t += timedelta(seconds=20)
        _snap(st, gid_num, gid, t, hs, as_, q, clock, label)

    for i in range(6):
        snap("30964771", base, 2, "00:00" if i == 5 else "06:00",
             "2nd Quarter", 20 + i, 25 + i)
    # sibling ends at a final-looking state but its history is poisoned
    # (score regression mid-series) -> chained gate INVALID -> no bridge
    snap("30964771#i1", sib, 4, "00:00", "4th Quarter", 60, 70)
    snap("30964771#i1", sib, 4, "00:00", "4th Quarter", 30, 35)   # poison
    snap("30964771#i1", sib, 4, "00:00", "4th Quarter", 60, 70)

    _run_worker(tmp_path, st)
    conn = sqlite3.connect(str(tmp_path / "blm_pokerbet.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT final_result_status FROM game_results "
        "WHERE source_game_id='30964771'").fetchone()
    assert row["final_result_status"] == "UNKNOWN"
    conn.close()


def test_bridge_requires_final_label(tmp_path):
    """A sibling that also dies mid-game proves nothing — UNKNOWN stands,
    and no OK row is fabricated from a mid-Q4 clock."""
    st, base, sib = _pipeline(tmp_path)
    t = datetime(2026, 9, 20, 10, 45, tzinfo=timezone.utc)

    def snap(gid, gid_num, q, clock, label, hs, as_):
        nonlocal t
        t += timedelta(seconds=20)
        _snap(st, gid_num, gid, t, hs, as_, q, clock, label)

    for i in range(6):
        snap("30964771", base, 2, "00:00" if i == 5 else "06:00",
             "2nd Quarter", 20 + i, 25 + i)
    snap("30964771#i1", sib, 3, "05:00", "3rd Quarter", 40, 45)

    _run_worker(tmp_path, st)
    conn = sqlite3.connect(str(tmp_path / "blm_pokerbet.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT final_result_status FROM game_results "
        "WHERE source_game_id='30964771'").fetchone()
    assert row["final_result_status"] == "UNKNOWN"
    conn.close()
