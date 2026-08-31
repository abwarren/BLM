"""M009-M1b — expose the immutable Market-vs-Fair checkpoint history
through the game-detail API.

The game-detail endpoint (/api/v4/game/{id}) already exposes the live/
rebase-able `predictions` checkpoints.  M009's PRIMARY analytical data —
the immutable per-checkpoint MARKET vs FAIR rows (checkpoint_market) —
must be observable through the same detail route so the frontend can
render the progressive history (M009 section 13) without a second query.

Covers:
  - /api/v4/game/{id} returns market_vs_fair[] alongside checkpoints[]
  - each row: checkpoint_pct, checkpoint_timestamp, opening_line,
    live_market_line, blm_fair_value, closing_line, actual_final_total,
    market_vs_fair (signed), signal, outcome, market_move_toward_blm
  - rows ordered by checkpoint_pct ascending
  - empty when the table has no rows for that game (never fabricated)
  - honest NULLs preserved (missing market -> signal/outcome/mvf NULL)
  - the route stays lean for games without any checkpoint_market rows
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore
from tests.test_m009_checkpoint_market import _build, _iso, _now

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"

_LINES = [170 + i for i in range(20)]


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    _build(dbfile, "G-MIX", lines=_LINES)
    _build(dbfile, "G-NOMKT")           # no market -> honest NULLs
    _build(dbfile, "G-LIVE", status="live", lines=_LINES)
    return dbfile


@pytest.fixture
def app(db):
    application = FastAPI()
    application.include_router(v4_router)
    application.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
                      name="test_dashboard_static")
    return application


@pytest.fixture
def client(app, db):
    sc = Scorecard(db)
    sc.capture_results()
    sc.record_checkpoint_market()
    return TestClient(app)


def _mvf(client, gid):
    resp = client.get(f"/api/v4/game/{gid}")
    assert resp.status_code == 200
    return resp.json()


def test_detail_exposes_market_vs_fair_rows(client):
    """G-MIX: market_vs_fair[] present, ordered, full field set."""
    body = _mvf(client, "G-MIX")
    rows = body.get("market_vs_fair")
    assert rows, "market_vs_fair must be non-empty for a clean game"
    assert [r["checkpoint_pct"] for r in rows] == list(range(10, 101, 10))
    for r in rows:
        for key in ("checkpoint_pct", "checkpoint_timestamp", "opening_line",
                    "live_market_line", "blm_fair_value", "closing_line",
                    "actual_final_total", "market_vs_fair", "signal",
                    "outcome", "market_move_toward_blm"):
            assert key in r, f"missing {key}"
    # both disparity directions present in one game
    assert any(r["market_vs_fair"] < 0 for r in rows)
    assert any(r["market_vs_fair"] > 0 for r in rows)


def test_detail_market_vs_fair_honest_nulls(client):
    """G-NOMKT: rows exist with fair values but market-linked fields NULL."""
    rows = _mvf(client, "G-NOMKT")["market_vs_fair"]
    assert rows, "G-NOMKT must expose fair rows"
    for r in rows:
        assert r["live_market_line"] is None
        assert r["market_vs_fair"] is None
        assert r["signal"] is None
        assert r["outcome"] is None
        assert r["blm_fair_value"] is not None


def test_detail_live_game_no_rows(client):
    """G-LIVE: no checkpoint_market rows -> empty list, never fabricated."""
    assert _mvf(client, "G-LIVE")["market_vs_fair"] == []


def test_detail_lean_without_checkpoint_market(client):
    """A game with no rows still returns the normal detail payload."""
    body = _mvf(client, "G-LIVE")
    assert "checkpoints" in body
    assert "market_vs_fair" in body
