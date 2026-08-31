"""M009-M3 — MARKET FRESHNESS LAYER (directive sections 2-5, 22, 24).

Every frozen market line must carry its OBSERVATION TIMESTAMP so the
system can distinguish LIVE / STALE / MISSING and must NEVER treat a
stale differential as a live edge (sections 3, 5; validation A/E/F).

- market_timestamp persisted on checkpoint_market rows (new rows; old
  rows stay NULL = honest missing).
- market_age_seconds = checkpoint_timestamp - market_timestamp.
- market_status: LIVE (age <= threshold) | STALE | MISSING.  The
  threshold is the EXISTING system definition (dashboard.js: age <=
  300) — configurable via BLM_MARKET_STALE_SECONDS, not hard-coded.
- freshness buckets: 0-10s / 10-30s / 30-60s / 60-120s / 120-300s /
  300s+.
- edge_class: LIVE_EDGE only when LIVE; STALE -> STALE_DIFFERENTIAL
  (retained for research, excluded from live-edge statistics); missing
  -> no edge.
- blm_market_diff = BLM - market (positive = BLM higher, section 4) —
  the exact negation of M009's market_vs_fair (live - fair); both are
  exposed, never merged.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router
from blm_v4.scorecard import (
    MARKET_STALE_SECONDS,
    Scorecard,
    _edge_class,
    _freshness_bucket,
    _market_age_seconds,
    _market_status,
)
from tests.test_m009_checkpoint_market import _build, _LINES

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"


@pytest.fixture
def sc(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    # G-MIX: line carried on EVERY snapshot -> market_timestamp ==
    # checkpoint_timestamp -> age 0 -> LIVE.
    _build(dbfile, "G-MIX", lines=_LINES)
    # G-STALE: NO snapshot lines; ONE WS observation at base+30s ->
    # frozen line 180.0 with age ~330s at pct10 -> STALE.
    _build(dbfile, "G-STALE", lines=None, ws=[(0, 180.0)])
    # G-NOMKT: no market anywhere -> MISSING.
    _build(dbfile, "G-NOMKT")
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    return s


@pytest.fixture
def client(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-MIX", lines=_LINES)
    _build(dbfile, "G-STALE", lines=None, ws=[(0, 180.0)])
    _build(dbfile, "G-NOMKT")
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    os.environ["BLM_POKERBET_DB"] = str(dbfile)
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", __import__("fastapi.staticfiles",
              fromlist=["StaticFiles"]).StaticFiles(directory=str(DASH_STATIC)),
              name="test_static")
    return TestClient(app)


def _cm_rows(sc: Scorecard) -> list[dict]:
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market ORDER BY source_game_id, checkpoint_pct")]
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════

def test_freshness_helpers():
    """Age, status, buckets — boundaries at the EXISTING 300s threshold."""
    assert MARKET_STALE_SECONDS == 300          # existing system definition
    assert _market_age_seconds("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z") == 300.0
    assert _market_age_seconds(None, "2026-01-01T00:00:00Z") is None
    # negative age (clock skew) clamps to 0 — a line is never "fresher than now"
    assert _market_age_seconds("2026-01-01T00:05:00Z", "2026-01-01T00:00:00Z") == 0.0
    assert _market_status(None, "2026-01-01T00:00:00Z") == "MISSING"
    assert _market_status("2026-01-01T00:00:00Z", "2026-01-01T00:05:01Z") == "STALE"   # >300
    assert _market_status("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z") == "LIVE"    # ==300: existing def age <= 300
    assert _market_status("2026-01-01T00:00:00Z", "2026-01-01T00:04:59Z") == "LIVE"
    # configurable threshold, not hard-coded
    assert _market_status("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z",
                          stale_seconds=600) == "LIVE"
    assert _freshness_bucket(0) == "0-10s"
    assert _freshness_bucket(10) == "10-30s"
    assert _freshness_bucket(45) == "30-60s"
    assert _freshness_bucket(90) == "60-120s"
    assert _freshness_bucket(200) == "120-300s"
    assert _freshness_bucket(301) == "300s+"
    assert _freshness_bucket(None) is None


def test_edge_class():
    """LIVE EDGE only when fresh; STALE -> STALE DIFFERENTIAL, never an edge."""
    assert _edge_class("LIVE", 5.0) == "LIVE_EDGE"
    assert _edge_class("STALE", -39.5) == "STALE_DIFFERENTIAL"
    assert _edge_class("MISSING", 5.0) is None
    assert _edge_class(None, None) is None
    assert _edge_class("LIVE", None) is None


def test_record_captures_market_timestamp(sc):
    """G-MIX rows: market_timestamp == checkpoint_timestamp (age 0, LIVE).
    G-STALE rows: market_timestamp = WS observation time, age ~330s, STALE.
    G-NOMKT rows: market_timestamp NULL, MISSING."""
    rows = _cm_rows(sc)
    mix = [r for r in rows if r["source_game_id"] == "G-MIX" and r["checkpoint_pct"] == 10][0]
    assert mix["market_timestamp"] == mix["checkpoint_timestamp"]
    stale = [r for r in rows if r["source_game_id"] == "G-STALE" and r["checkpoint_pct"] == 20][0]
    assert stale["live_market_line"] == 180.0
    assert stale["market_timestamp"] is not None
    age = _market_age_seconds(stale["market_timestamp"], stale["checkpoint_timestamp"])
    assert age is not None and age > 300          # STALE (base+30s vs base+12min)
    nomkt = [r for r in rows if r["source_game_id"] == "G-NOMKT"][0]
    assert nomkt["market_timestamp"] is None


def test_stale_never_live_edge(sc):
    """Per-checkpoint aggregation: STALE rows count in n_stale, never
    n_live; the STALE game is excluded from live-edge stats (section 5).
    pct50: G-MIX (LIVE, UNDER_WIN) + G-STALE (STALE, UNDER_WIN) have
    market; G-NOMKT MISSING."""
    agg = sc.market_vs_fair()
    c50 = next(c for c in agg["checkpoints"] if c["checkpoint_pct"] == 50)
    assert c50["n"] == 2
    assert c50["n_live"] == 1
    assert c50["n_stale"] == 1
    assert c50["n_missing"] == 1
    # live-edge aggregation only counts the LIVE game's outcome
    assert c50["live_under_win"] == 1 and c50["live_under_loss"] == 0
    assert c50["live_over_win"] == 0 and c50["live_over_loss"] == 0
    # the stale game's outcome is reported separately, never as a live edge
    assert c50["stale_under_win"] == 1 and c50["stale_under_loss"] == 0


def test_blm_market_diff_sign(sc):
    """section 4: blm_market_diff = BLM - market.  Positive = BLM higher.
    G-MIX pct10: fair ~182.4 > market 172 -> positive.  pct50: fair
    ~148.4 < market 180 -> negative.  Exact negation of market_vs_fair."""
    rows = _cm_rows(sc)
    r10 = [r for r in rows if r["source_game_id"] == "G-MIX" and r["checkpoint_pct"] == 10][0]
    r50 = [r for r in rows if r["source_game_id"] == "G-MIX" and r["checkpoint_pct"] == 50][0]
    assert r10["market_vs_fair"] < 0              # live - fair
    assert round(r10["blm_fair_value"] - r10["live_market_line"], 2) > 0   # BLM higher
    assert r50["market_vs_fair"] > 0
    assert round(r50["blm_fair_value"] - r50["live_market_line"], 2) < 0   # BLM lower


def test_api_exposes_freshness(client):
    """Game detail rows carry age/status/bucket/edge_class/diff; the
    scorecard carries market_freshness (age-bucket section)."""
    d = client.get("/api/v4/game/G-MIX").json()
    rows = d["market_vs_fair"]
    assert rows and rows[0]["checkpoint_pct"] == 10
    r = rows[0]
    for key in ("market_age_seconds", "market_status", "freshness_bucket",
                "edge_class", "blm_market_diff"):
        assert key in r, f"missing {key}"
    assert r["market_status"] == "LIVE"
    assert r["market_age_seconds"] == 0
    assert r["freshness_bucket"] == "0-10s"
    assert r["edge_class"] == "LIVE_EDGE"

    d2 = client.get("/api/v4/game/G-STALE").json()
    r2 = next(x for x in d2["market_vs_fair"] if x["checkpoint_pct"] == 20)
    assert r2["market_status"] == "STALE"
    assert r2["market_age_seconds"] > 300
    assert r2["freshness_bucket"] == "300s+"
    assert r2["edge_class"] == "STALE_DIFFERENTIAL"

    sc = client.get("/api/v4/scorecard").json()
    mf = sc["market_vs_fair"]
    assert "market_freshness" in mf
    buckets = {b["bucket"]: b for b in mf["market_freshness"]}
    assert "0-10s" in buckets and "300s+" in buckets
    assert buckets["0-10s"]["n"] >= 1 and buckets["300s+"]["n"] >= 1
    c50 = next(c for c in mf["checkpoints"] if c["checkpoint_pct"] == 50)
    assert c50["n_live"] >= 1
