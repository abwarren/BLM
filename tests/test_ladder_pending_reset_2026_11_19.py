"""The bet ladder's PENDING rule (operator directive 2026-11-19).

A placed bet whose result has not landed before the next eligible game must
not pass its stake on: that next stake goes back to rung 1, the increase is
deferred until the outstanding result is confirmed, and the progression then
resumes for the game after it.

One test per numbered requirement.  Nothing here touches a live database.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blm_v4.betting import ladder  # noqa: E402
from blm_v4.betting import settlement  # noqa: E402

RESULTS_SCHEMA = """CREATE TABLE game_results (
    source_game_id TEXT, final_total INTEGER, final_result_status TEXT,
    result_at TEXT)"""
EXEC_SCHEMA = """CREATE TABLE bet_executions (
    id INTEGER PRIMARY KEY, execution_id TEXT, game_id TEXT, market TEXT, selection TEXT,
    triggered_line REAL, price REAL, stake_amount REAL, unit_price REAL,
    status TEXT, submitted_at_utc TEXT, requested_at_utc TEXT)"""


@pytest.fixture(autouse=True)
def _factor_env(monkeypatch):
    for key in ("BET_LADDER_FACTOR", "BET_LADDER_MODE", "BET_LADDER_STEP"):
        monkeypatch.delenv(key, raising=False)


def _ledger(tmp_path, bets, results):
    """bets: (id, game, line, status, submitted) · results: (game, total, status, at)"""
    betting = tmp_path / "bet.db"
    prod = tmp_path / "prod.db"
    b = sqlite3.connect(betting)
    b.execute(EXEC_SCHEMA)
    b.executemany(
        "INSERT INTO bet_executions (id, execution_id, game_id, market, selection,"
        " triggered_line, price, stake_amount, unit_price, status,"
        " submitted_at_utc, requested_at_utc)"
        " VALUES (?, ?, ?, 'TOTAL', 'TOTAL UNDER', ?, 1.9, NULL, 1.0, ?, ?, NULL)",
        [(i, f"exec-{g}", g, line, status, submitted)
         for (i, g, line, status, submitted) in bets])
    b.commit()
    b.close()
    p = sqlite3.connect(prod)
    p.execute(RESULTS_SCHEMA)
    p.executemany("INSERT INTO game_results VALUES (?,?,?,?)", results)
    p.commit()
    p.close()
    return str(betting), str(prod)


# 1 ── an unconfirmed result resets the next stake to rung 1
def test_pending_bet_resets_the_next_rung_to_one():
    assert ladder.rung_from_replay(["WON", "WON", None], 4) == 1
    assert ladder.rung_from_replay(["WON", None], 4) == 1
    assert ladder.rung_from_replay([None], 4) == 1


# 2 ── the progression resumes after the outstanding result is confirmed
def test_progression_resumes_after_the_pending_result_is_confirmed():
    # pending bet confirmed as a WIN -> the game after it can advance
    assert ladder.rung_from_replay(["WON", "WON", None, "WON"], 4) == 2
    # ... and a loss in the deferred position behaves normally
    assert ladder.rung_from_replay(["WON", "WON", None, "LOST"], 4) == 1


# 3 ── confirmed history is unchanged (the 2026-10-30 contract stands)
def test_confirmed_history_replays_exactly_as_before():
    assert ladder.rung_from_history(["WON"], 4) == 2
    assert ladder.rung_from_history(["WON", "WON"], 4) == 3
    assert ladder.rung_from_history(["WON", "WON", "WON", "WON"], 4) == 1  # banked
    assert ladder.rung_from_history(["WON", "PUSH", "WON"], 4) == 3


# 4 ── a ledger whose newest bet has no final -> rung 1 (the measured case)
def test_current_rung_is_one_while_the_newest_bet_is_unsettled(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-19T01:34:26Z"),
         (2, "g2", 253.5, "SUBMITTED", "2026-11-19T02:13:50Z"),
         (3, "g3", 190.5, "SUBMITTED", "2026-11-19T02:47:22Z"),
         (4, "g4", 186.5, "SUBMITTED", "2026-11-19T02:54:23Z")],
        [("g1", 196, "OK", "2026-11-19T01:49:43Z"),
         ("g2", 241, "OK", "2026-11-19T02:24:47Z"),
         ("g3", 188, "OK", "2026-11-19T03:00:25Z"),   # lands AFTER bet 4 was placed
         ])                                            # g4: no row yet
    # before g3's result: two confirmed wins then a bet in flight -> rung 1,
    # NOT the rung 3 the old skip-the-pending replay produced
    assert settlement.current_rung(betting, "4", prod) == 1


# 5 ── once that result lands the replay is complete and progresses again
def test_current_rung_recovers_once_the_pending_result_lands(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-19T01:34:26Z"),
         (2, "g2", 253.5, "SUBMITTED", "2026-11-19T02:13:50Z"),
         (3, "g3", 190.5, "SUBMITTED", "2026-11-19T02:47:22Z")],
        [("g1", 196, "OK", "2026-11-19T01:49:43Z"),
         ("g2", 241, "OK", "2026-11-19T02:24:47Z"),
         ("g3", 188, "OK", "2026-11-19T03:00:25Z")])
    # every result confirmed: LOST (196 over 180.5), then two wins -> rung 3
    assert settlement.outcome_sequence(betting, prod) == ["LOST", "WON", "WON"]
    assert settlement.current_rung(betting, "4", prod) == 3


# 6 ── a final stamped BEFORE the bet is an earlier instance, not a result
def test_stale_final_counts_as_unconfirmed(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-15T07:56:17Z")],
        [("g1", 163, "OK", "2026-11-15T07:53:06Z")])       # 3 min BEFORE the bet
    assert settlement.outcome_sequence(betting, prod) == [None]
    assert settlement.current_rung(betting, "4", prod) == 1


# 7 ── not-OK verdicts and failed submissions still never move the ladder
def test_unproven_verdicts_and_failed_rows_are_pending_or_absent(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-01T00:00:00Z"),
         (2, "g2", 180.5, "FAILED", "2026-11-01T01:00:00Z"),
         (3, "g3", 180.5, "SUBMITTED", "2026-11-01T02:00:00Z")],
        [("g1", 170, "OK", "2026-11-01T00:40:00Z"),
         ("g2", 170, "OK", "2026-11-01T01:40:00Z"),
         ("g3", 181, "INVALID", "2026-11-01T02:40:00Z")])
    # g2 FAILED is not a position (absent); g3 INVALID is a bet in flight
    assert settlement.outcome_sequence(betting, prod) == ["WON", None]
    assert settlement.current_rung(betting, "4", prod) == 1


# 8 ── ladder OFF keeps the plain unit regardless of the ledger
def test_depth_zero_still_reads_rung_one(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-01T00:00:00Z")],
        [("g1", None, "UNKNOWN", None)])
    assert settlement.current_rung(betting, "0", prod) == 1


# 10 ── the pending flag: status says WHY the next stake is one unit
def test_pending_bets_lists_the_unconfirmed_ones(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-19T01:34:26Z"),
         (2, "g2", 253.5, "SUBMITTED", "2026-11-19T02:13:50Z"),
         (3, "g3", 190.5, "SUBMITTED", "2026-11-19T02:47:22Z")],
        [("g1", 196, "OK", "2026-11-19T01:49:43Z"),
         ("g2", 241, "OK", "2026-11-19T02:24:47Z")])       # g3 never resulted
    pend = settlement.pending_bets(betting, prod)
    assert [p["game_id"] for p in pend] == ["g3"]
    assert pend[0]["reason"] == "no_result_row"
    assert pend[0]["submitted_at_utc"] == "2026-11-19T02:47:22Z"
    assert pend[0]["execution_id"] is not None


def test_pending_flag_reaches_the_status_payload(monkeypatch):
    from blm_v4.betting import api as betting_api
    from blm_v4.betting import settlement as real_settlement

    class _Store:
        db_path = "/nonexistent/bet.db"

        def get_config(self, key, default=None):
            return "4" if key == "ladder_wins" else default

        def get_unit_price(self):
            return 100.0

    class _Cfg:
        stake_units = 1.0

    monkeypatch.setattr(real_settlement, "current_rung", lambda *a, **k: 1)
    monkeypatch.setattr(real_settlement, "pending_bets", lambda *a, **k: [
        {"execution_id": "x", "game_id": "g", "submitted_at_utc": "2026-11-19T02:47:22Z",
         "reason": "no_result_row"},
        {"execution_id": "y", "game_id": "h", "submitted_at_utc": "2026-10-08T05:59:50Z",
         "reason": "final_invalid"}])
    state = betting_api._ladder_live_state(_Store(), _Cfg())
    assert state["ladder_pending"] is True                  # one bet still in flight
    assert state["ladder_pending_bets"] == 1                # the unprovable one excluded
    assert state["ladder_pending_oldest_at_utc"] == "2026-11-19T02:47:22Z"
    assert state["ladder_unprovable_bets"] == 1
    assert state["ladder_rung"] == 1
    assert state["ladder_next_stake"] == 100.0


def test_an_unprovable_final_alone_does_not_read_as_pending(monkeypatch):
    from blm_v4.betting import api as betting_api
    from blm_v4.betting import settlement as real_settlement

    class _Store:
        db_path = "/nonexistent/bet.db"

        def get_config(self, key, default=None):
            return "4" if key == "ladder_wins" else default

        def get_unit_price(self):
            return 100.0

    class _Cfg:
        stake_units = 1.0

    monkeypatch.setattr(real_settlement, "current_rung", lambda *a, **k: 1)
    monkeypatch.setattr(real_settlement, "pending_bets", lambda *a, **k: [
        {"execution_id": "y", "game_id": "h", "submitted_at_utc": "2026-10-08T05:59:50Z",
         "reason": "final_invalid"}])
    state = betting_api._ladder_live_state(_Store(), _Cfg())
    assert state["ladder_pending"] is False                 # nothing is in flight
    assert state["ladder_pending_bets"] == 0
    assert state["ladder_unprovable_bets"] == 1


def test_pending_flag_is_absent_when_nothing_is_in_flight(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-01T00:00:00Z")],
        [("g1", 170, "OK", "2026-11-01T00:40:00Z")])
    assert settlement.pending_bets(betting, prod) == []


# 9 ── settled_outcomes keeps its old contract: confirmed results only
def test_settled_outcomes_still_returns_only_confirmed_results(tmp_path):
    betting, prod = _ledger(
        tmp_path,
        [(1, "g1", 180.5, "SUBMITTED", "2026-11-01T00:00:00Z"),
         (2, "g2", 180.5, "SUBMITTED", "2026-11-01T01:00:00Z")],
        [("g1", 170, "OK", "2026-11-01T00:40:00Z"),
         ("g2", 195, "OK", "2026-11-01T01:40:00Z")])
    assert settlement.settled_outcomes(betting, prod) == ["WON", "LOST"]
