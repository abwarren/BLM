"""HARD FREEZE ON PREDICTION GENERATION — directive tests.

The live pipeline must NOT create new predictive records during the
clean-data accumulation phase (no new predictions / no fixed-% rows / no
O/U selections / no scoring), existing historical prediction records must
stay byte-identical, and the descriptive + settlement + market layers
must keep working.

These tests run with BLM_PREDICTION_FREEZE=1 (frozen) — the production
default.  tests/conftest.py unfreezes the machinery by default for the
rest of the suite (legacy / re-authorizable path), so each test here
re-enables the freeze explicitly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    """These tests exercise the FROZEN default (production behavior)."""
    monkeypatch.setenv("BLM_PREDICTION_FREEZE", "1")


@pytest.fixture
def store(tmp_path) -> PokerBetStore:
    return PokerBetStore(tmp_path / "blm.db")


def _add_game(st: PokerBetStore, gid: str, status: str = "live") -> int:
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp", competition_slug="betual_nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Home Virtual", away_team="Away Virtual",
        game_slug="home-away", source_url=f"https://x/{gid}",
        status=status,
        first_seen_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=30)),
        last_seen_at=_iso(datetime.now(timezone.utc)),
    )
    return st.upsert_game(game)


def _snap(st: PokerBetStore, gid_db: int, gid: str, t: datetime,
          hs: int, as_: int, q: int | None, clock: str,
          total: float | None = None, period: str | None = None) -> None:
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=_iso(t), home_team="Home Virtual", away_team="Away Virtual",
        home_score=hs, away_score=as_,
        period_label=period if period is not None
        else (f"{q}th Quarter" if q is not None else ""),
        quarter=q, clock=clock,
        game_status="live", total_line=total,
        markets_json=json.dumps({"total": {"first_line": total}}) if total else "{}",
    )
    st.insert_snapshot(gid_db, obs, force=True)


def _full_clean_game(st: PokerBetStore, gid: str = "F1") -> None:
    """A clean, complete Q1..Q4 game (finishes 96-88 = 184 total)."""
    gid_db = _add_game(st, gid, status="ended")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=20)
    snaps = [
        (0, 0, 1, "09:00"), (8, 6, 1, "06:00"), (16, 12, 1, "03:00"),
        (24, 20, 1, "00:00"), (30, 26, 2, "09:00"), (40, 34, 2, "06:00"),
        (52, 42, 2, "03:00"), (60, 50, 2, "00:00"), (66, 58, 3, "09:00"),
        (76, 66, 3, "06:00"), (82, 72, 3, "03:00"), (86, 78, 3, "00:00"),
        (88, 80, 4, "09:00"), (92, 84, 4, "06:00"), (96, 88, 4, "00:00"),
    ]
    for i, (hs, as_, q, clock) in enumerate(snaps):
        _snap(st, gid_db, gid, t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, 190.0 if i % 3 == 0 else None)


def _table_rows(sc: Scorecard, table: str) -> list[tuple]:
    conn = sc._connect()
    try:
        return [tuple(r) for r in conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
    finally:
        conn.close()


def test_frozen_run_creates_no_prediction_records(tmp_path):
    """§11.1/2/12: a live observation + full scorecard run while frozen
    creates NO prediction rows, NO fixed-% rows, and NO prediction_scores
    rows (no background process generates predictions)."""
    st = PokerBetStore(tmp_path / "blm.db")
    _full_clean_game(st)
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.run()
    assert stats["prediction_freeze"] is True
    assert stats["recorded"]["frozen"] is True
    assert stats["fixed"]["frozen"] is True
    assert stats["scored"]["frozen"] is True
    assert stats["recorded"]["recorded"] == 0
    assert stats["fixed"]["recorded"] == 0
    assert stats["scored"]["scored"] == 0
    conn = sc._connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM prediction_scores").fetchone()["c"] == 0
    finally:
        conn.close()


def test_settlement_and_descriptive_layers_continue(tmp_path):
    """§11.9 + freeze scope: while classic prediction generation is frozen,
    final settlement (game_results), market_history and the frozen
    per-checkpoint market-vs-fair observation rows continue to be written
    for a completed clean game."""
    st = PokerBetStore(tmp_path / "blm.db")
    _full_clean_game(st)
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.run()
    assert stats["results"]["ok"] == 1            # settlement continues
    assert stats["market"]["recorded"] == 1       # OLV/CLV outcome rows continue
    assert stats["checkpoint_market"]["recorded"] >= 1  # MVF observation rows
    conn = sc._connect()
    try:
        assert conn.execute(
            "SELECT final_result_status FROM game_results "
            "WHERE source_game_id='F1'").fetchone()["final_result_status"] == "OK"
        cm = conn.execute(
            "SELECT COUNT(*) c FROM checkpoint_market "
            "WHERE source_game_id='F1'").fetchone()["c"]
        assert cm >= 1
    finally:
        conn.close()


def test_historical_prediction_records_untouched_by_frozen_run(tmp_path):
    """§11.10/5: a frozen run never rewrites or rescores existing
    historical prediction rows — they remain byte-identical (no rebase,
    no backfill, no recalculation), even when the current code would
    compute different values."""
    st = PokerBetStore(tmp_path / "blm.db")
    _full_clean_game(st, gid="LEG1")
    sc = Scorecard(tmp_path / "blm.db")
    conn = sc._connect()
    # seed a historical prediction + score row (legacy-era content that the
    # current code would rebase differently if it ran unfrozen)
    conn.execute(
        "INSERT INTO predictions (source_game_id, classification, model_version,"
        " checkpoint, quarter, predicted_at, source_snapshot_at, elapsed_minutes,"
        " progress, home_score, away_score, combined,"
        " projected_home, projected_away, projected_total, market_total, valid)"
        " VALUES ('LEG1','BETUAL_NBA','v4-pace-1','final',4,"
        " '2026-09-01T00:00:00Z','2026-09-01T00:00:00Z',38.0,0.95,"
        " 96,88,184, 58.0,85.1,143.2,190.0,1)")
    conn.execute(
        "INSERT INTO prediction_scores (prediction_id, source_game_id,"
        " classification, model_version, home_error, away_error, total_error,"
        " abs_home_error, abs_away_error, abs_total_error, total_pct_error,"
        " model_total, market_total, actual_total, market_error,"
        " model_beat_market, ou_prediction, ou_result, ou_correct,"
        " scored_at, fragment)"
        " VALUES (1,'LEG1','BETUAL_NBA','v4-pace-1', -38,-3,-41, 38,3,41,"
        " 22.28, 143.2,190.0,184, 6, 0, -1,-1,1,"
        " '2026-09-01T00:00:00Z', 0)")
    conn.commit()
    conn.close()
    before_p = _table_rows(sc, "predictions")
    before_ps = _table_rows(sc, "prediction_scores")
    assert len(before_p) == 1 and len(before_ps) == 1

    stats = sc.run()  # BLM_PREDICTION_FREEZE=1 (autouse fixture)
    assert stats["recorded"]["frozen"] is True
    assert stats["scored"]["frozen"] is True

    after_p = _table_rows(sc, "predictions")
    after_ps = _table_rows(sc, "prediction_scores")
    # byte-identical: no rebase, no re-score, no recalculation of the legacy row
    assert after_p == before_p
    assert after_ps == before_ps


def test_live_pipeline_writes_no_predictions(tmp_path):
    """§11.1/2: the collector/store observation path (a live snapshot with
    a market line) never creates prediction records — predictions only
    ever existed through the scorecard phases, which are frozen."""
    st = PokerBetStore(tmp_path / "blm.db")
    _full_clean_game(st, gid="LIVE1")
    gid_db = _add_game(st, "LIVE2")  # a live game currently collecting
    t = datetime.now(timezone.utc) - timedelta(seconds=30)
    _snap(st, gid_db, "LIVE2", t, 20, 18, 1, "08:00", 205.5)
    sc = Scorecard(tmp_path / "blm.db")
    conn = sc._connect()
    try:
        before = conn.execute(
            "SELECT COUNT(*) c FROM predictions").fetchone()["c"]
    finally:
        conn.close()
    assert before == 0
    sc.run()
    conn = sc._connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM predictions").fetchone()["c"] == 0
    finally:
        conn.close()
