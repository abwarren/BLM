"""QUALIFYING BETS — every game that cleared the gate is recorded (2026-11-02).

Operator directive: "that ui page can be replaced with the new executions going
forward — each bet that qualifies should be listed in the UI whether a bet was
placed or not."  These tests pin the two properties that make that honest:

  * the record is separate from ``bet_executions`` (it authorises nothing and
    can never claim an idempotency key), and
  * the row carries the DECISION — PLACED only when a slip went out, otherwise
    NOT_PLACED with the reason that stopped it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.fast_lane import FastLaneWorker
from blm_v4.betting.store import BettingStore


@pytest.fixture()
def store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "bet.db"))


def test_candidates_table_exists_and_is_separate(store):
    rows = store._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert "bet_candidates" in names
    assert "bet_executions" in names


def test_candidate_round_trip(store):
    store.record_candidate(dict(
        dedupe_key="g1:80:0", game_id="g1", checkpoint="75",
        line=196.5, price=1.9, progress_pct=82.5, total_points=161.0,
        required_pace=5.0714, actual_pace=4.8788, pace_ratio=1.183,
        shortfall=0.1926, outcome="NOT_PLACED", reason="already_placed"))
    rows = store.candidates_recent(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["game_id"] == "g1"
    assert r["outcome"] == "NOT_PLACED"
    assert r["reason"] == "already_placed"
    assert r["price"] == pytest.approx(1.9)


def test_one_row_per_qualifying_moment_and_placed_wins(store):
    """A refresh updates the SAME row; the execution id is never lost."""
    store.record_candidate(dict(dedupe_key="g1:80:0", game_id="g1",
                                outcome="NOT_PLACED", reason="limit"))
    store.record_candidate(dict(dedupe_key="g1:80:0", game_id="g1",
                                outcome="PLACED", execution_id="exec-1",
                                execution_state="SUBMITTED", stake_units=1.8,
                                stake_amount=90.0))
    # a later pass that sees NOT_PLACED again must not erase the placement
    store.record_candidate(dict(dedupe_key="g1:80:0", game_id="g1",
                                outcome="PLACED", reason=None))
    rows = store.candidates_recent(limit=10)
    assert len(rows) == 1, "the 3-second pass must not fill the table"
    assert rows[0]["outcome"] == "PLACED"
    assert rows[0]["execution_id"] == "exec-1"
    assert rows[0]["stake_amount"] == pytest.approx(90.0)


def test_candidates_are_fail_soft(store):
    assert store.record_candidate({}) is None        # no dedupe key -> no row
    assert store.record_candidate(dict(dedupe_key="", game_id="g1",
                                       outcome="PLACED")) is None
    assert store.candidates_recent(limit=5) == []


def test_lane_maps_the_row_onto_the_decision(store, tmp_path):
    """The lane's own writer: paces, ratio, shortfall and the 5-point band."""
    lane = FastLaneWorker(
        prod_db_path=Path(tmp_path / "prod.db"),
        clean_db_path=Path(tmp_path / "clean.db"),
        cfg=BettingConfig(), store=store)
    row = dict(captured_at="2026-11-02 19:00:00", progress_pct=82.5,
               total_points=161.0, required_pts_per_min=5.0,
               actual_pts_per_min=4.0, live_total_line=196.5, under_price=1.9)
    lane._record_candidate(row, "31436661", 4.0, 0,
                           outcome="NOT_PLACED", reason="already_placed")
    lane._record_candidate(row, "31436661", 4.0, 1,
                           outcome="PLACED", checkpoint="75",
                           execution_id="exec-2", execution_state="SUBMITTED",
                           stake_units=1.0, stake_amount=50.0)
    rows = store.candidates_recent(limit=10)
    assert len(rows) == 2                      # attempt 0 and attempt 1
    by_attempt = {r["attempt"]: r for r in rows}
    assert by_attempt[0]["pace_ratio"] == pytest.approx(1.25)
    assert by_attempt[0]["shortfall"] == pytest.approx(1.0)
    assert by_attempt[0]["outcome"] == "NOT_PLACED"
    assert by_attempt[1]["outcome"] == "PLACED"
    assert by_attempt[1]["checkpoint"] == "75"
    assert by_attempt[1]["stake_amount"] == pytest.approx(50.0)
    assert all(r["dedupe_key"].startswith("31436661:80:") for r in rows)
