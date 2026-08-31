"""M009 section 18 — PERMANENT REGRESSION TEST: fixture integrity.

The Karsiyaka/Denizli-vs-Karsiyaka/Korfez incident (2026-08-31, M007-M6
audit) must never regress.  A user report of "157.0 - 186.5 = -29.5"
conflated TWO DIFFERENT fixtures that share a home team (Pinar
Karsiyaka) but have different opponents, event ids, status, and market
lines:

  30741194  Pinar Karsiyaka vs DENIZLI   ENDED    single line 157.5
  30741844  Pinar Karsiyaka vs KORFEZ    LIVE     WS batch 182.5/184.5/186.5

Live today (verified 2026-08-31): 30741194 still serves only 157.5
(ended), 30741844 serves only its own WS line (now 191.5 — the market
moved; the isolation holds).

This file encodes the data-layer invariants that make cross-fixture
conflation impossible:
  - line helpers (_first_verified_line / _last_verified_line /
    _frozen_market_line) are scoped per source_game_id — a foreign
    fixture's market observations must NEVER leak in
  - the "main line" of a WS batch (same captured_at, multiple O/U
    variants) is the LOWEST — 182.5, never 186.5
  - the API detail for each game serves only that game's own line

The fixture mirrors the incident with the incident-era values: A (ended,
Denizli) carries only 157.5; B (live, Korfez) carries 182.5/184.5/186.5
in one WS batch.  Both are modeled WS-only so the per-fixture WS filter
is genuinely under test (snapshot lines are inherently scoped by the
caller's row list).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import (
    _first_verified_line,
    _frozen_market_line,
    _last_verified_line,
)
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_game(db: Path, gid: str, home: str, away: str, status: str,
                ws_lines: list[float]) -> None:
    """Insert one Karsiyaka-style game: NO snapshot market lines (WS-only),
    one WS MatchTotal batch per line set (same captured_at), monotonic
    scores across 5 snapshots (Q1 -> Q2)."""
    st = PokerBetStore(db)
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-1", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=home, away_team=away,
        game_slug="pinar-karsiyaka-virtual", source_url=f"https://x/{gid}",
        status=status, first_seen_at=_iso(base),
        last_seen_at=_iso(base + timedelta(minutes=12)),
    )
    gid_db = st.upsert_game(game)
    for i in range(5):
        t = base + timedelta(minutes=3 * i)
        q = i // 5 + 1
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
            captured_at=_iso(t), home_team=home, away_team=away,
            home_score=10 * i, away_score=8 * i,
            period_label=f"{q}th Quarter", quarter=q, clock="08:00",
            game_status=status, total_line=None, spread=None,
            w1_odds=None, w2_odds=None, markets_json="{}",
        )
        st.insert_snapshot(gid_db, obs, force=True)
    # ONE WS batch: all lines share the same captured_at (the eu-swarm
    # feed pushes 3 O/U variants per capture).
    for line in ws_lines:
        st.upsert_market_observation({
            "game_id": gid_db, "source_game_id": gid,
            "captured_at": _iso(base + timedelta(minutes=6)),
            "market_type": "MatchTotal", "market_name": "Match Total",
            "line_value": line, "over_price": 1.9, "under_price": 1.9,
            "home_score": 20, "away_score": 16,
            "period_label": "", "clock": "", "raw_json": "{}",
        })


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    # A: the ENDED fixture — Denizli opponent, one line: 157.5.
    _build_game(dbfile, "30741194", "Pinar Karsiyaka Virtual",
                "Denizli Basket SK Virtual", "ended", [157.5])
    # B: the LIVE fixture — Korfez opponent, incident-era WS batch.
    _build_game(dbfile, "30741844", "Pinar Karsiyaka Virtual",
                "Korfez Basket Virtual", "live", [182.5, 184.5, 186.5])
    return dbfile


@pytest.fixture
def conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _rows(conn, gid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM snapshots WHERE source_game_id=? ORDER BY captured_at",
        (gid,))]


def test_karsiyaka_fixtures_are_distinct_events(db):
    """The two incident games are DIFFERENT events: distinct ids, SAME
    home team, DIFFERENT opponent, DIFFERENT status."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        a = dict(conn.execute("SELECT * FROM games WHERE source_game_id='30741194'").fetchone())
        b = dict(conn.execute("SELECT * FROM games WHERE source_game_id='30741844'").fetchone())
    finally:
        conn.close()
    assert a["home_team"] == b["home_team"] == "Pinar Karsiyaka Virtual"
    assert a["away_team"] == "Denizli Basket SK Virtual"
    assert b["away_team"] == "Korfez Basket Virtual"
    assert a["status"] == "ended" and b["status"] == "live"
    assert a["source_game_id"] != b["source_game_id"]


def test_line_helpers_never_cross_fixtures(conn):
    """A's helpers return ONLY 157.5; B's return ONLY its own WS batch
    (182.5 = lowest of the batch).  A foreign fixture's line never leaks:
    A never sees 182.5/184.5/186.5, B never sees 157.5."""
    rows_a = _rows(conn, "30741194")
    rows_b = _rows(conn, "30741844")
    # A: opening == closing == 157.5 (its OWN single line).  Frozen at
    # the FIRST snapshot is None (honest at-or-before: the WS obs lands
    # at +6min, not available at t=0); frozen at the LAST snapshot is
    # 157.5.
    assert _first_verified_line(conn, "30741194", rows_a) == 157.5
    assert _last_verified_line(conn, "30741194", rows_a) == 157.5
    assert _frozen_market_line(conn, "30741194", rows_a, 0) is None
    assert _frozen_market_line(conn, "30741194", rows_a, len(rows_a) - 1) == 157.5
    # B: opening == closing == 182.5 (lowest of its own batch); same
    # at-or-before semantics.
    assert _first_verified_line(conn, "30741844", rows_b) == 182.5
    assert _last_verified_line(conn, "30741844", rows_b) == 182.5
    assert _frozen_market_line(conn, "30741844", rows_b, 0) is None
    assert _frozen_market_line(conn, "30741844", rows_b, len(rows_b) - 1) == 182.5
    # Cross-check: B's lines are ABSENT from A's results and vice-versa.
    for val in (182.5, 184.5, 186.5):
        assert _first_verified_line(conn, "30741194", rows_a) != val
        assert _last_verified_line(conn, "30741194", rows_a) != val
    assert _first_verified_line(conn, "30741844", rows_b) != 157.5
    assert _last_verified_line(conn, "30741844", rows_b) != 157.5


def test_ws_batch_main_line_is_lowest(conn):
    """The eu-swarm feed pushes 3 O/U variants per capture (182.5/184.5/
    186.5 at the same captured_at); the MAIN line is the LOWEST — 182.5,
    never 186.5 (the exact '186.5 vs 182.5' confusion from the incident)."""
    rows_b = _rows(conn, "30741844")
    line = _last_verified_line(conn, "30741844", rows_b)
    assert line == 182.5
    assert line != 186.5
    assert line != 184.5


def test_api_detail_serves_only_own_line(db):
    """The running-service contract: /api/v4/game/{id} serves that game's
    OWN market line — A shows 157.5 (never B's batch), B shows 182.5
    (lowest of its own batch, never A's 157.5, never its own 186.5)."""
    application = FastAPI()
    application.include_router(v4_router)
    application.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
                      name="test_dashboard_static")
    client = TestClient(application)
    a = client.get("/api/v4/game/30741194").json()
    b = client.get("/api/v4/game/30741844").json()
    assert a["market"]["total_line"] == 157.5
    assert a["market"]["total_line"] not in (182.5, 184.5, 186.5)
    assert b["market"]["total_line"] == 182.5
    assert b["market"]["total_line"] != 157.5
    assert b["market"]["total_line"] != 186.5
