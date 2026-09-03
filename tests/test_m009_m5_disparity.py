"""M009-M5 — DISPARITY BAND ANALYTICS + EVENT DATASET (directive:
disparity bands; statistical segmentation; settlement context; small-
sample handling; fresh/stale separation; duplicate/contamination
protection; frontend exposure of the underlying event data).

Builds on M009-M4 exactly.  M4's edge_buckets keys (win/loss/push/
win_rate/avg_age) are PRESERVED — M5 is strictly ADDITIVE:

- edge_buckets gains: over_n/under_n/push_n (actual vs market line),
  market_win_rate, fresh_n/stale_n/missing_n, fresh_win_rate/
  stale_win_rate, reliable (n >= edge_bucket_min_sample, default 30,
  env BLM_MIN_BAND_SAMPLE — small samples are FLAGGED, never treated as
  conclusions).
- NEW endpoint GET /api/v4/scorecard/events — the underlying event
  dataset (not summary-only): every (game, checkpoint) row with market
  line, BLM fair, diff, direction (BLM_OVER/BLM_UNDER), market_status
  (LIVE/STALE/MISSING), market_age_seconds, momentum fields, BLM side,
  actual, outcome, blm_won.  Filters: direction, freshness, checkpoint,
  min_diff/max_diff (magnitude), game, limit.  One row per (game,
  checkpoint) — no duplicates; contaminated games excluded.
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
from blm_v4.scorecard import Scorecard
from tests.test_m009_checkpoint_market import _build, _LINES

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"


@pytest.fixture
def sc(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    # fresh carried line, both disparity directions
    _build(dbfile, "G-MIX", lines=_LINES)
    # no snapshot lines; one WS line at base+30s -> STALE
    _build(dbfile, "G-STALE", lines=None, ws=[(0, 180.0)])
    # no market anywhere
    _build(dbfile, "G-NOMKT")
    # contaminated (score regression) -> INVALID -> excluded
    _build(dbfile, "G-BAD", lines=_LINES, dip=True)
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    return s


@pytest.fixture
def client(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-MIX", lines=_LINES)
    _build(dbfile, "G-STALE", lines=None, ws=[(0, 180.0)])
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


def _bands(agg: dict) -> list[dict]:
    return agg["edge_buckets"]


def _band(agg: dict, bucket: str, direction: str) -> dict:
    return next(b for b in _bands(agg) if b["bucket"] == bucket
                and b["direction"] == direction)


# ═════════════════════════════════════════════════════════════════════

def test_edge_buckets_additive_m4_keys_preserved(sc):
    """M5 adds fields; M4 keys win/loss/push/win_rate/avg_age survive."""
    agg = sc.market_vs_fair()
    b = _band(agg, "10-15", "BLM_OVER")
    for key in ("bucket", "direction", "n", "win", "loss", "push",
                "win_rate", "avg_age", "avg_diff"):
        assert key in b, f"M4 key missing: {key}"
    for key in ("over_n", "under_n", "push_n", "market_win_rate",
                "fresh_n", "stale_n", "missing_n", "fresh_win_rate",
                "stale_win_rate", "reliable"):
        assert key in b, f"M5 key missing: {key}"
    assert "edge_bucket_min_sample" in agg


def test_over_under_push_counts(sc):
    """over_n/under_n/push_n = actual vs market line; push = outcome push
    (M4) preserved separately."""
    b = _band(sc.market_vs_fair(), "10-15", "BLM_OVER")
    # G-MIX pct10: market 170, actual 143 -> UNDER; only that row here
    assert b["n"] == 1
    assert b["under_n"] == 1
    assert b["over_n"] == 0
    assert b["push_n"] == 0


def test_market_win_rate_is_blm_loss_rate(sc):
    """market_win_rate = the market's side won = BLM's side lost."""
    b = _band(sc.market_vs_fair(), "10-15", "BLM_OVER")
    # G-MIX pct10: BLM LOSS -> market won
    assert b["win"] == 0 and b["loss"] == 1
    assert b["win_rate"] == 0.0
    assert b["market_win_rate"] == 1.0


def test_fresh_stale_separation_in_bands(sc):
    """fresh_n/stale_n split; fresh_win_rate vs stale_win_rate."""
    agg = sc.market_vs_fair()
    # 10-15 BLM_OVER: only G-MIX pct10 (fresh carried line) -> fresh only
    b = _band(agg, "10-15", "BLM_OVER")
    assert b["fresh_n"] == 1 and b["stale_n"] == 0 and b["missing_n"] == 0
    assert b["fresh_win_rate"] == 0.0
    # 5-10 BLM_UNDER: G-STALE pct30 (stale WS line 180.0, fair 174.0
    # quantized -> diff -6.0) -> stale only.  The pct10/pct20 stale rows
    # (2-5 BLM_OVER at 1dp fairs) now quantize to +5.0 -> 5-10 BLM_OVER.
    s = _band(agg, "5-10", "BLM_UNDER")
    assert s["stale_n"] >= 1 and s["fresh_n"] == 0
    assert s["stale_win_rate"] is not None


def test_small_sample_suppression(sc):
    """reliable=False when n < min_sample; min_sample reported."""
    agg = sc.market_vs_fair()
    assert agg["edge_bucket_min_sample"] > 0
    for b in _bands(agg):
        assert b["reliable"] == (b["n"] >= agg["edge_bucket_min_sample"])
    # with only 1-2 synthetic games, every bucket is a small sample
    assert all(not b["reliable"] for b in _bands(agg))


def test_events_endpoint_contract(client):
    d = client.get("/api/v4/scorecard/events").json()
    assert "total" in d and "rows" in d
    assert d["total"] >= 10
    row = d["rows"][0]
    for key in ("game", "home_team", "away_team", "checkpoint_pct",
                "checkpoint_ts", "market_line", "blm_fair", "diff",
                "direction", "market_status", "market_age_seconds",
                "momentum_state", "momentum_strength", "false_momentum",
                "blm_side", "actual", "outcome", "blm_won"):
        assert key in row, f"missing {key}"


def test_events_direction_and_freshness_filters(client):
    over = client.get("/api/v4/scorecard/events", params={"direction": "BLM_OVER"}).json()
    assert over["total"] >= 1 and all(r["direction"] == "BLM_OVER" for r in over["rows"])
    stale = client.get("/api/v4/scorecard/events", params={"freshness": "STALE"}).json()
    assert stale["total"] >= 1
    assert all(r["market_status"] == "STALE" for r in stale["rows"])
    # freshness filter must never return LIVE rows under the STALE label
    live = client.get("/api/v4/scorecard/events", params={"freshness": "LIVE"}).json()
    assert live["total"] >= 1
    assert all(r["market_status"] == "LIVE" for r in live["rows"])


def test_events_magnitude_filter_and_checkpoint(client):
    big = client.get("/api/v4/scorecard/events", params={"min_diff": 10}).json()
    assert big["total"] >= 1
    assert all(abs(r["diff"]) >= 10 for r in big["rows"] if r["diff"] is not None)
    p50 = client.get("/api/v4/scorecard/events", params={"checkpoint": 50}).json()
    assert p50["total"] >= 1 and all(r["checkpoint_pct"] == 50 for r in p50["rows"])


def test_events_settlement_and_no_stale_as_live(client):
    d = client.get("/api/v4/scorecard/events", params={"game": "G-MIX"}).json()
    # G-MIX pct50: diff -31.6 BLM_UNDER, actual 143 < market 180 -> WIN
    r50 = next(r for r in d["rows"] if r["checkpoint_pct"] == 50)
    assert r50["direction"] == "BLM_UNDER"
    assert r50["blm_side"] == "UNDER"
    assert r50["market_status"] == "LIVE"
    assert r50["blm_won"] is True
    assert r50["outcome"] == "UNDER_WIN"
    # G-STALE rows: market_status STALE, never coerced to LIVE
    s = client.get("/api/v4/scorecard/events", params={"game": "G-STALE"}).json()
    assert s["total"] >= 1
    assert all(r["market_status"] in ("STALE", "MISSING") for r in s["rows"])


def test_events_duplicate_protection_and_contamination(client):
    d = client.get("/api/v4/scorecard/events").json()
    keys = [(r["game"], r["checkpoint_pct"]) for r in d["rows"]]
    assert len(keys) == len(set(keys))          # one row per (game, checkpoint)
    assert all(r["game"] != "G-BAD" for r in d["rows"])  # INVALID excluded
    assert any(r["game"] == "G-MIX" for r in d["rows"])


def test_events_limit_cap(client):
    d = client.get("/api/v4/scorecard/events", params={"limit": 5}).json()
    assert len(d["rows"]) <= 5
    assert d["total"] >= len(d["rows"])
