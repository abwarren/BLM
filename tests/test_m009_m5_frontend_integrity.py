"""M009-M5 FOLLOW-UP — FRONTEND DATA-INTEGRITY FIX regression tests.

Covers the authorized frontend-integrity slice (separate from the
contamination-lifecycle suite):

1. /api/v4/live + /api/v4/game/{id} carry the AUTHORITATIVE backend
   game_quality state (quality_status / quality_reason) — the frontend
   must never re-derive validity in the browser.
2. An INVALID game renders EXCLUDED, never as a normal eligible card,
   and its model panel (momentum/signals/projections/edge) is gated —
   while historical diagnostics (lines, MVF rows) stay visible.
3. Time-of-day labels say "first-observed hour, local" (never falsely
   "start hour") until a real fixture-start timestamp exists.
4. Missing odds never render as a genuine 50/50 — "— (no odds captured)".
5. Exact market age is rendered (fmtAgeExact) with LIVE/STALE/MISSING
   status preserved (M3 semantics untouched).
6. False-momentum distinction: live-window heuristic chip is labelled
   "(live)", frozen per-checkpoint false_momentum is exposed in the
   modal MARKET VS FAIR table.
7. Metric labels: Market-implied win prob / Model data confidence /
   BLM position win rate / mkt proximity / SNAPSHOTS (window).

Static-asset string tests + API payload tests (parallel-safe: backend
may be mid-change while these run).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router
from blm_v4.scorecard import Scorecard
from tests.test_m009_checkpoint_market import _build, _LINES

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    _build(dbfile, "G-CLEAN", lines=_LINES)          # valid, clean
    _build(dbfile, "G-BAD", lines=_LINES, dip=True)  # initially INVALID
    sc = Scorecard(dbfile)
    sc.capture_results()
    sc.record_checkpoint_market()
    return dbfile


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")
    return TestClient(app)


def _js(client) -> str:
    resp = client.get("/static/dashboard.js")
    assert resp.status_code == 200
    return resp.text


def _html(client) -> str:
    resp = client.get("/static/index.html")
    assert resp.status_code == 200
    return resp.text


# ── API: authoritative quality state on the payload ──────────────────────

def test_live_payload_carries_authoritative_quality(client):
    body = client.get("/api/v4/live").json()
    games = {g["game_id"]: g for g in body["games"]}
    assert games["G-CLEAN"]["quality_status"] == "OK"
    assert games["G-CLEAN"]["quality_reason"] is None
    assert games["G-BAD"]["quality_status"] == "INVALID"
    assert games["G-BAD"]["quality_reason"]


def test_detail_payload_carries_quality(client):
    d = client.get("/api/v4/game/G-BAD").json()
    assert d["quality_status"] == "INVALID"
    assert d["quality_reason"]
    c = client.get("/api/v4/game/G-CLEAN").json()
    assert c["quality_status"] == "OK"


def test_invalid_game_absent_from_headline_aggregates(client, db):
    agg = Scorecard(db).market_vs_fair()
    gids = {g["source_game_id"] for g in agg["games"]}
    assert "G-CLEAN" in gids
    assert "G-BAD" not in gids


# ── Static: INVALID presentation + model-panel gating ────────────────────

def test_invalid_game_marked_excluded_not_normal(client):
    js = _js(client)
    assert "EXCLUDED" in js
    assert "INVALID — EXCLUDED FROM ANALYTICS" in js
    assert "gatedNoteHTML" in js
    assert "chip-excluded" in js
    # the invalid branch replaces the model panel with the gated note
    assert "invalid ? gatedNoteHTML(g)" in js
    assert "momentumHTML(g)}${signalsHTML(g)}${projHTML(g)}" in js


def test_model_panel_gated_in_modal(client):
    js = _js(client)
    assert "model panel unavailable — game excluded from analytics" in js
    assert "momentum unavailable — game excluded from analytics" in js
    assert "renderModalCharts(invalid ? null : g)" in js


# ── Static: time-of-day label honesty ────────────────────────────────────

def test_tod_label_first_observed_not_start(client):
    js = _js(client)
    assert "first-observed hour, local" in js
    assert "start hour, local" not in js
    assert "TIME-OF-DAY (first-observed hour, local)" in js


# ── Static: missing odds never 50/50 ─────────────────────────────────────

def test_missing_odds_renders_unavailable(client):
    js = _js(client)
    assert "no odds captured" in js
    assert "unavailable — no odds captured" in js
    assert "if (!hasOdds)" in js
    assert "MARKET-IMPLIED" in js


# ── Static: exact market age with LIVE/STALE/MISSING ─────────────────────

def test_market_age_exact_and_status_preserved(client):
    js = _js(client)
    assert "fmtAgeExact" in js
    assert 'mstatus === "MISSING" ? "—" : fmtAgeExact(age)' in js
    # M3 freshness literals untouched (event dataset chips + filters)
    assert "st-live" in js and "st-stale" in js and "st-missing" in js
    # the card/modal freshness word comes from the M3 threshold helper
    assert "mktStatusWord" in js
    assert "age <= 300" in js


# ── Static: false-momentum distinction ───────────────────────────────────

def test_false_momentum_live_heuristic_labelled(client):
    js = _js(client)
    assert "False Mom (live)" in js
    assert "live-window heuristic" in js
    assert "MARKET VS FAIR — FROZEN PER-CHECKPOINT" in js
    assert "False-momentum here is the RECORDED checkpoint value" in js


# ── Static: metric labels ────────────────────────────────────────────────

def test_metric_labels_explicit(client):
    js = _js(client)
    assert "Model data confidence" in js
    assert "Model Confidence" not in js
    assert "Market-implied win prob (home)" in js
    assert "Win probability (home)" not in js
    assert "BLM position win rate" in js
    assert "mkt proximity" in js
    assert '"eff "' not in js
    html = _html(client)
    assert "SNAPSHOTS (window)" in html
    assert "served games only" in html
