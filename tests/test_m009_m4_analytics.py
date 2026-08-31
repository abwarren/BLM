"""M009-M4 — CHECKPOINT MARKET-LINE ANALYTICS (directive: strengthen
market-line analytics; time-of-day; false-momentum; large-edge
investigation; duplicate protection; contamination exclusion).

Builds on M009-M3 exactly (LIVE/STALE/MISSING + freshness semantics
untouched).  New behavior:

- checkpoint_market rows capture momentum_state / momentum_strength /
  false_momentum / false_momentum_confidence AT THE CHECKPOINT — computed
  from snapshots AT-OR-BEFORE the checkpoint (no look-ahead), sharing the
  API's single momentum/signal definition.
- time_of_day aggregation: per hour-of-day + configurable bands (env
  BLM_TOD_BANDS, default 0-6,6-12,12-18,18-24), with N, over/under/push,
  BLM win rate, market win rate (the market's side of the line), and
  average BLM-market differential.  Hypotheses measured, never hard-coded.
- edge_buckets aggregation: |BLM-market| magnitude buckets (0-2 / 2-5 /
  5-10 / 10-15 / 15-20 / 20+) split by direction (BLM_OVER = fair >
  market, BLM_UNDER = fair < market), each with n / win / loss / push /
  win_rate / avg_age — large apparent edges stay attributable to
  freshness (a big stale differential is visible as such).
- duplicate protection: checkpoint_market never double-counts a
  (game, checkpoint) — UNIQUE + INSERT OR IGNORE.
- contamination exclusion: INVALID games never enter checkpoint_market.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router
from blm_v4.scorecard import Scorecard
from blm_v4.trends import analytics_tz
from tests.test_m009_checkpoint_market import _build, _LINES

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"


@pytest.fixture
def sc(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    # fresh carried line, both disparity directions
    _build(dbfile, "G-MIX", lines=_LINES)
    # no snapshot lines; one WS line at base+30s -> STALE, static line ->
    # score burst with no line response -> FALSE MOMENTUM active
    _build(dbfile, "G-STALE", lines=None, ws=[(0, 180.0)])
    # no market anywhere
    _build(dbfile, "G-NOMKT")
    # contaminated (score regression) -> INVALID -> must be excluded
    _build(dbfile, "G-BAD", lines=_LINES, dip=True)
    # deterministic start times for time-of-day segmentation
    _build(dbfile, "G-AM", lines=_LINES,
           start=datetime(2026, 1, 1, 3, 15, tzinfo=timezone.utc))
    _build(dbfile, "G-PM", lines=_LINES,
           start=datetime(2026, 1, 1, 15, 45, tzinfo=timezone.utc))
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


def _rows(sc: Scorecard) -> list[dict]:
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market ORDER BY source_game_id, checkpoint_pct")]
    finally:
        conn.close()


def _snap_prefix(sc: Scorecard, gid: str, idx: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM snapshots s JOIN games g ON g.id = s.game_id "
            "WHERE g.source_game_id=? ORDER BY s.captured_at LIMIT ?",
            (gid, idx + 1))]
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════

def test_momentum_captured_at_checkpoint_no_lookahead(sc):
    """Stored momentum == _momentum(snapshots up to the checkpoint) —
    recomputing from the PREFIX only reproduces the stored value, so no
    later snapshot leaked in."""
    from blm_v4.api import _momentum
    rows = _rows(sc)
    # G-MIX pct10 is snapshot idx 1 (closest to 10% of 40 min)
    r10 = next(r for r in rows if r["source_game_id"] == "G-MIX"
               and r["checkpoint_pct"] == 10)
    for key in ("momentum_state", "momentum_strength", "false_momentum",
                "false_momentum_confidence"):
        assert key in r10, f"missing {key}"
    prefix = _snap_prefix(sc, "G-MIX", 1)
    mom = _momentum(prefix)
    expected_state = {"up": "RISING", "down": "FALLING", "flat": "FLAT"}[mom["direction"]]
    assert r10["momentum_state"] == expected_state
    assert r10["momentum_strength"] == mom["strength"]
    # G-MIX has fast early scoring BUT the line moves +1/snapshot ->
    # the burst is answered by the line -> NOT false momentum
    assert r10["false_momentum"] == 0
    assert r10["false_momentum_confidence"] == 0.0


def test_false_momentum_captured_when_line_does_not_follow(sc):
    """G-STALE: fast scoring with a STATIC (WS) line -> false momentum
    fires at early checkpoints (line never answers the burst)."""
    rows = _rows(sc)
    r10 = next(r for r in rows if r["source_game_id"] == "G-STALE"
               and r["checkpoint_pct"] == 10)
    assert r10["false_momentum"] == 1
    assert 0.0 < r10["false_momentum_confidence"] <= 0.9
    assert r10["momentum_state"] in ("RISING", "FALLING", "FLAT")


def test_time_of_day_segmentation(sc):
    """hours + bands from game start (first_seen_at, analytics tz);
    G-AM lands in the 0-6 band, G-PM in 12-18.  Bands configurable."""
    agg = sc.market_vs_fair()
    tod = agg["time_of_day"]
    tz = ZoneInfo(analytics_tz())
    am_hour = datetime(2026, 1, 1, 3, 15, tzinfo=timezone.utc).astimezone(tz).hour
    pm_hour = datetime(2026, 1, 1, 15, 45, tzinfo=timezone.utc).astimezone(tz).hour
    hours = {h["hour"]: h for h in tod["hours"]}
    assert hours[am_hour]["n"] >= 10          # G-AM's 10 checkpoints
    assert hours[pm_hour]["n"] >= 10          # G-PM's 10 checkpoints
    for h in hours.values():
        for key in ("n", "over_n", "under_n", "push_n", "blm_win_rate",
                    "market_win_rate", "avg_diff"):
            assert key in h
    bands = {b["band"]: b for b in tod["bands"]}
    band_labels = [b["band"] for b in tod["bands"]]
    am_band = next(b for b in band_labels
                   if am_hour >= int(b.split("-")[0]) and am_hour < int(b.split("-")[1]))
    pm_band = next(b for b in band_labels
                   if pm_hour >= int(b.split("-")[0]) and pm_hour < int(b.split("-")[1]))
    assert bands[am_band]["n"] >= 10
    assert bands[pm_band]["n"] >= 10
    assert tod["band_def"]  # the configurable band definition is reported


def test_edge_buckets_direction_and_large_edge_freshness(sc):
    """Magnitude buckets split by direction; the large-edge investigation
    stays attributable: the 20+ BLM_UNDER bucket holds G-MIX pct50
    (diff -31.6, FRESH, WIN) AND G-STALE late rows (diff ~-37, STALE) —
    avg_age reveals the stale contamination instead of hiding it.  A
    fresh-only bucket (10-15 BLM_OVER, only G-MIX pct10 +12.4) shows
    avg_age ~0.  A stale-only bucket (2-5 BLM_OVER, G-STALE pct10 +2.4)
    shows avg_age > 300."""
    agg = sc.market_vs_fair()
    eb = agg["edge_buckets"]
    key = lambda b, d: next((x for x in eb if x["bucket"] == b and x["direction"] == d), None)
    # G-MIX pct10: diff +12.4 -> BLM_OVER / 10-15; actual 143 < 170 -> LOSS
    o10 = key("10-15", "BLM_OVER")
    assert o10 is not None and o10["n"] >= 1 and o10["loss"] >= 1
    assert o10["avg_age"] is not None and o10["avg_age"] < 5     # fresh only
    # G-MIX pct50 (-31.6, FRESH, WIN) + G-STALE late (~-37, STALE, WIN)
    u20 = key("20+", "BLM_UNDER")
    assert u20 is not None and u20["n"] >= 2 and u20["win"] >= 1
    assert u20["avg_age"] is not None and u20["avg_age"] > 300   # stale rows visible
    # G-STALE pct10: diff +2.4 -> BLM_OVER / 2-5, STALE
    s25 = key("2-5", "BLM_OVER")
    assert s25 is not None and s25["n"] >= 1 and s25["avg_age"] is not None
    assert s25["avg_age"] > 300                                  # stale visible


def test_duplicate_protection(sc):
    """(game, checkpoint) is unique — fragments can never inflate N."""
    rows = _rows(sc)
    keys = [(r["source_game_id"], r["checkpoint_pct"]) for r in rows]
    assert len(keys) == len(set(keys))
    # re-run the recorder: still no duplicates (immutable INSERT OR IGNORE)
    sc.record_checkpoint_market()
    rows2 = _rows(sc)
    assert len(rows2) == len(rows)


def test_contamination_exclusion(sc):
    """G-BAD (score regression -> INVALID) never enters checkpoint_market."""
    rows = _rows(sc)
    assert all(r["source_game_id"] != "G-BAD" for r in rows)
    assert any(r["source_game_id"] == "G-MIX" for r in rows)


def test_api_exposes_m4_fields(client):
    """Game detail rows carry momentum; scorecard carries time_of_day +
    edge_buckets."""
    d = client.get("/api/v4/game/G-MIX").json()
    r10 = next(x for x in d["market_vs_fair"] if x["checkpoint_pct"] == 10)
    for key in ("momentum_state", "momentum_strength", "false_momentum",
                "false_momentum_confidence", "market_status"):
        assert key in r10, f"missing {key}"
    scd = client.get("/api/v4/scorecard").json()["market_vs_fair"]
    assert "time_of_day" in scd and "edge_buckets" in scd
    assert scd["edge_buckets"]  # non-empty
