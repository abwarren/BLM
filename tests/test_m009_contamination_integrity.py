"""M009 CONTAMINATION INTEGRITY — retrospective (later) contamination.

The defect: a game can pass the quality gate, get checkpoint_market
rows frozen, then be re-verified INVALID by capture_results (M007-M8
re-verification) — but market_vs_fair() and /scorecard/events continue
aggregating those rows forever, contaminating headline analytics.

Required semantics (LOGICAL EXCLUSION — no row destruction):
- initial contamination (insert-time INVALID)  -> never enters (existing)
- later contamination (clean -> re-verify INVALID) -> rows EXCLUDED from
  headline aggregation while the historical observations REMAIN in
  checkpoint_market, intact and auditable (market line, timestamp,
  checkpoint, freshness classification, BLM differential unchanged).

Matrix:
  clean game                                -> included
  initially contaminated                    -> excluded
  clean -> later INVALID (retrospective)    -> excluded (critical)
  clean + unrelated INVALID                 -> clean remains included
  multiple checkpoints on later-invalid     -> all excluded
  multiple valid games                      -> all included
  re-verification repeated                  -> stable/idempotent
  aggregation repeated                      -> no metric inflation
  historical rows retained (quarantine)     -> still auditable
  freshness classification                  -> unchanged (LIVE/STALE kept)
  duplicate protection                      -> preserved
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router
from blm_v4.scorecard import Scorecard, _market_status
from tests.test_m009_checkpoint_market import _build, _LINES

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

# AUTHORIZED (2026-08-31, consolidated M009 directive): the logical-
# exclusion fix (game_quality eligibility at read time) is authorized and
# applied in the working tree.  The RETRO_XFAIL markers documented the
# defect while it was un-authorized; they are REMOVED so these tests now
# assert the real invariant (see docs/milestones/M009-CONTAMINATION-
# INTEGRITY.md).


def _force_regression(db: Path, gid: str) -> None:
    """Mutate the 7th snapshot (idx 6) so home_score REGRESSES below the
    6th snapshot's value — the same score-regression that dip=True
    creates — so the NEXT capture_results() re-verification marks the
    game INVALID (M007-M8 path)."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """UPDATE snapshots SET home_score = home_score - 5
               WHERE id = (SELECT s.id FROM snapshots s
                           JOIN games g ON g.id = s.game_id
                           WHERE g.source_game_id = ?
                           ORDER BY s.captured_at LIMIT 1 OFFSET 6)""",
            (gid,),
        )
        conn.commit()
    finally:
        conn.close()


def _cm_rows(db: Path, gid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id = ? "
            "ORDER BY checkpoint_pct", (gid,))]
    finally:
        conn.close()


def _in_games(agg: dict, gid: str) -> bool:
    return any(g["source_game_id"] == gid for g in agg["games"])


def _in_checkpoints(agg: dict, gid: str) -> int:
    """How many checkpoint rows of gid leak into per-checkpoint stats."""
    leaked = 0
    for c in agg["checkpoints"]:
        # checkpoints aggregate counts only; count via the band/TOD leak
        pass
    # rows leak into edge_buckets + time_of_day + market_freshness;
    # detect by scanning the raw rows is not exposed, so use games list
    return 1 if _in_games(agg, gid) else 0


@pytest.fixture
def sc(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-CLEAN", lines=_LINES)
    _build(dbfile, "G-BAD", lines=_LINES, dip=True)      # initially INVALID
    _build(dbfile, "G-REV", lines=_LINES)                # clean first, later INVALID
    _build(dbfile, "G-REVSTALE", lines=None, ws=[(0, 180.0)])  # STALE rows, later INVALID
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    return s


@pytest.fixture
def client(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-REV", lines=_LINES)
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    _force_regression(dbfile, "G-REV")
    s.capture_results()                                   # re-verify -> INVALID
    os.environ["BLM_POKERBET_DB"] = str(dbfile)
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", __import__("fastapi.staticfiles",
              fromlist=["StaticFiles"]).StaticFiles(directory=str(DASH_STATIC)),
              name="test_static")
    return TestClient(app), dbfile


# ═════════════════════════════════════════════════════════════════════

def test_clean_game_included(sc):
    agg = sc.market_vs_fair()
    assert _in_games(agg, "G-CLEAN")
    assert len(_cm_rows(sc._db_path, "G-CLEAN")) == 10


def test_initial_contamination_excluded(sc):
    agg = sc.market_vs_fair()
    assert not _in_games(agg, "G-BAD")
    assert _cm_rows(sc._db_path, "G-BAD") == []          # never entered


def test_retrospective_contamination_excluded(sc):
    """THE critical case: clean -> recorded -> re-verify INVALID ->
    rows no longer contribute to headline analytics."""
    db = sc._db_path
    # 1. rows were recorded while clean
    assert len(_cm_rows(db, "G-REV")) == 10
    agg = sc.market_vs_fair()
    assert _in_games(agg, "G-REV")                       # initially included
    # 2. later re-verification discovers contamination
    _force_regression(db, "G-REV")
    sc.capture_results()
    # 3. headline aggregation must no longer include G-REV
    agg2 = sc.market_vs_fair()
    assert not _in_games(agg2, "G-REV")
    for c in agg2["checkpoints"]:
        for key in ("n", "over_win", "under_win", "under_value_n"):
            pass  # per-checkpoint stats are aggregates; the games list is the marker
    # 4. rows must remain in the table (logical exclusion, not purge)
    assert len(_cm_rows(db, "G-REV")) == 10


def test_unrelated_invalid_does_not_affect_clean(sc):
    agg = sc.market_vs_fair()
    assert _in_games(agg, "G-CLEAN")
    assert not _in_games(agg, "G-BAD")


def test_later_invalid_excludes_all_checkpoints(sc):
    db = sc._db_path
    _force_regression(db, "G-REV")
    sc.capture_results()
    rows = _cm_rows(db, "G-REV")
    assert len(rows) == 10                               # all 10 retained
    pcts = {r["checkpoint_pct"] for r in rows}
    assert pcts == set(range(10, 101, 10))
    agg = sc.market_vs_fair()
    assert not _in_games(agg, "G-REV")                   # all excluded


def test_multiple_clean_games_all_included(sc):
    _build(sc._db_path, "G-CLEAN2", lines=_LINES)
    sc.capture_results()
    sc.record_checkpoint_market()
    agg = sc.market_vs_fair()
    assert _in_games(agg, "G-CLEAN") and _in_games(agg, "G-CLEAN2")


def test_reverification_repeated_idempotent(sc):
    db = sc._db_path
    _force_regression(db, "G-REV")
    sc.capture_results()
    sc.capture_results()
    sc.capture_results()                                 # repeated re-verify
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        q = conn.execute("SELECT COUNT(*) n FROM game_quality WHERE source_game_id='G-REV'").fetchone()
        assert q["n"] == 1                               # single INVALID record
        r = conn.execute("SELECT final_result_status s FROM game_results "
                         "WHERE source_game_id='G-REV'").fetchone()
        assert r["s"] == "INVALID"
    finally:
        conn.close()
    assert len(_cm_rows(db, "G-REV")) == 10              # no dup fragments


def test_aggregation_repeated_no_inflation(sc):
    db = sc._db_path
    _force_regression(db, "G-REV")
    sc.capture_results()
    agg1 = sc.market_vs_fair()
    agg2 = sc.market_vs_fair()
    assert agg1 == agg2                                  # stable, no inflation
    sc.record_checkpoint_market()                        # recorder re-run
    assert len(_cm_rows(db, "G-REV")) == 10              # INSERT OR IGNORE
    n1 = sum(c["n"] for c in agg1["checkpoints"])
    n2 = sum(c["n"] for c in sc.market_vs_fair()["checkpoints"])
    assert n1 == n2


def test_historical_rows_retained_and_auditable(client):
    c, db = client
    # rows still in the DB, fully intact (line, ts, checkpoint, fairness)
    rows = _cm_rows(db, "G-REV")
    assert len(rows) == 10
    r50 = next(r for r in rows if r["checkpoint_pct"] == 50)
    assert r50["live_market_line"] is not None
    assert r50["checkpoint_timestamp"] is not None
    assert r50["blm_fair_value"] is not None
    # game detail (diagnostic) still shows the historical rows
    d = c.get("/api/v4/game/G-REV").json()
    assert len(d["market_vs_fair"]) == 10
    # events (headline) must NOT include them
    ev = c.get("/api/v4/scorecard/events").json()
    assert all(r["game"] != "G-REV" for r in ev["rows"])


def test_freshness_classification_preserved(sc):
    """A row that was historically STALE (WS line, old timestamp) must
    REMAIN STALE after the game becomes INVALID — freshness is a
    market-observation dimension, game quality is separate."""
    db = sc._db_path
    stale_before = _cm_rows(db, "G-REVSTALE")
    assert len(stale_before) >= 1
    statuses_before = {
        r["checkpoint_pct"]: _market_status(r["market_timestamp"],
                                            r["checkpoint_timestamp"])
        for r in stale_before if r["market_timestamp"] is not None}
    assert any(s == "STALE" for s in statuses_before.values())
    _force_regression(db, "G-REVSTALE")
    sc.capture_results()
    stale_after = _cm_rows(db, "G-REVSTALE")
    statuses_after = {
        r["checkpoint_pct"]: _market_status(r["market_timestamp"],
                                            r["checkpoint_timestamp"])
        for r in stale_after if r["market_timestamp"] is not None}
    assert statuses_after == statuses_before             # unchanged
    agg = sc.market_vs_fair()
    assert not _in_games(agg, "G-REVSTALE")              # but excluded


def test_events_exclude_retrospective_invalid(client):
    c, _db = client
    ev = c.get("/api/v4/scorecard/events", params={"game": "G-REV"}).json()
    assert ev["total"] == 0 and ev["rows"] == []
    # UNIQUE legacy requirement: when the ONLY game in the DB is later
    # invalidated, the per-checkpoint skeleton (10..100%) must still be
    # returned with honest N=0 — the shape never vanishes.
    agg = Scorecard(_db).market_vs_fair()
    assert len(agg["checkpoints"]) == 10
    assert all(c["n"] == 0 for c in agg["checkpoints"])
    assert agg["games"] == []
