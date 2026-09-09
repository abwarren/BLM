"""M009-M5 FOLLOW-UP — FRONTEND DATA-INTEGRITY regression tests
(d e s c r i p t i v e - o n l y contract).

Covers the authorized frontend-integrity slice (separate from the
contamination-lifecycle suite).  The live view is LIVE + CLEAN +
DESCRIPTIVE + COMPACT:

1. /api/v4/live + /api/v4/game/{id} carry the AUTHORITATIVE backend
   game_quality state (quality_status / quality_reason) — the frontend
   must never re-derive validity in the browser.
2. An INVALID game renders EXCLUDED, never as a normal eligible card;
   live cards render descriptive panels only (no model panels anywhere).
3. Time-of-day labels say "first-observed hour, local" (never falsely
   "start hour") until a real fixture-start timestamp exists.
4. No probability UI at all — odds are only raw observed values under
   modal Details.
5. Exact market age is rendered (fmtAgeExact) with LIVE/STALE/MISSING
   status preserved (M3 semantics untouched); the frozen checkpoint
   market history (observed lines only) lives under modal Details.
6. No trap/signal/projection/confidence UI in the live view; model
   diagnostics remain behind the collapsed HISTORICAL / RESEARCH
   top-level section (and its inner collapsed sections).
7. Metric labels: Pts/Min / Pace gap / mkt proximity / BLM position win
   rate (audit only) / SNAPSHOTS (window).
8. Collapsible sections are frontend-only: HISTORICAL / RESEARCH
   collapses as a whole (default collapsed, localStorage-persisted),
   inner sections keep independent toggles, game DETAILS default
   collapsed, CHARTS remain available.  No data deletion and no
   prediction logic added.

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
    # the invalid branch keeps the gated note on the descriptive card
    assert "invalid ? gatedNoteHTML(g)" in js
    # live cards render descriptive panels, never model panels
    assert "paceStripHTML(g)" in js
    assert "liveMarketHTML(g)" in js
    assert "gameStateHTML(g)" in js


def test_live_view_has_no_model_panels(client):
    """The live card + modal render path is descriptive-only: no model
    panel, no win probability, no signals/traps, no projections, no
    confidence anywhere in the live view.  The invalid gate still
    suppresses charts for excluded games."""
    js = _js(client)
    assert "renderModalCharts(invalid ? null : g)" in js
    assert "model panel unavailable — game excluded from analytics" not in js
    assert "momentum unavailable — game excluded from analytics" not in js
    assert "Model data confidence" not in js
    assert "Market-implied win prob" not in js
    assert "WIN PROBABILITY" not in js
    assert "no odds captured" not in js
    assert "bull_trap" not in js
    assert "Signals / Traps" not in js
    assert "MARKET VS FAIR — FROZEN PER-CHECKPOINT" not in js
    assert "winprobHTML" not in js
    assert "signalsHTML" not in js
    assert "projHTML" not in js
    assert "divergenceHTML" not in js


# ── Static: market age exact + no trap/signal/dead UI (Z migration) ─────

def test_market_age_exact_and_status_preserved(client):
    js = _js(client)
    assert "fmtAgeExact" in js
    assert 'mstatus === "MISSING" ? "—" : fmtAgeExact(age)' in js
    # M3 freshness literals untouched (market-state badges)
    assert "st-live" in js and "st-stale" in js and "st-missing" in js
    # the card/modal freshness word comes from the M3 threshold helper
    assert "mktStatusWord" in js
    assert "age <= 300" in js


def test_no_trap_signal_ui_in_live_view(client):
    """Trap/signal renderers are absent from the live view entirely, and
    the Z migration removed the checkpoint/settlement presentation from
    the modal as well (terminal semantics remain backend-only)."""
    js = _js(client)
    assert "bull_trap" not in js
    assert "trap_meter" not in js
    assert "False Mom" not in js
    assert "PREDICTIVE CHECKPOINTS" not in js
    assert "SETTLEMENT / TERMINAL" not in js
    assert "Market @CP" not in js
    assert "market_at_checkpoint" not in js
    assert "momentum" not in js


# ── Static: metric labels ────────────────────────────────────────────────

def test_metric_labels_explicit(client):
    js = _js(client)
    html = _html(client)
    # descriptive measurements are named as measurements
    assert "Pts/Min" in js
    assert "Pace gap" in js
    assert "LIVE TOTAL" in js
    assert "PACE Z-SCORE" in js
    # old-model labels are gone from the frontend entirely
    assert "mkt proximity" not in js
    assert "BLM" not in js
    assert "Model data confidence" not in js
    assert "Market-implied win prob" not in js
    assert '"eff "' not in js
    assert "HISTORICAL / RESEARCH" not in html
    assert "SNAPSHOTS (window)" in html
    assert "served games only" in html
    assert "LIVE GAMES" in html


def test_collapsible_sections_and_details(client):
    """Collapsible-sections directive (post-migration): game DETAILS
    default collapsed, CHARTS stay available, and section state persists
    to localStorage.  The HISTORICAL / RESEARCH block is gone — the live
    view carries no research/audit sub-sections."""
    html = _html(client)
    js = _js(client)
    # the HISTORICAL / RESEARCH wrapper no longer exists
    assert 'id="auditToggle"' not in html
    assert "HISTORICAL / RESEARCH" not in html
    for sid in ("scorecardSection", "trendsSection", "eventsSection",
                "gsSection", "devSection"):
        assert sid not in html
    # localStorage preference keys with the required defaults
    assert "pz.gameDetailsCollapsed" in js
    assert "pz.chartsCollapsed" in js
    assert "prefGet(PREF.GAME_DETAILS, true)" in js  # collapsed default
    assert "prefGet(PREF.CHARTS, false)" in js       # visible default
    # cards + detail modal expose CHARTS (open) and DETAILS (collapsed)
    assert "details.card-charts" in js
    assert "details.card-details" in js
    assert 'data-label="CHARTS"' in js
    assert 'data-label="DETAILS"' in js
    # descriptive defaults stay in the live view
    assert "LIVE GAMES" in html
    assert "Pts/Min" in js
    assert "LIVE TOTAL" in js


def test_deviation_research_frontend_absent(client):
    """The Phase 2/3 research surface (deviation charts, VALIDATION) is
    REMOVED from the frontend by the Z migration — the live view is the
    descriptive pace-Z view and no research/audit block remains."""
    html = _html(client)
    js = _js(client)
    assert 'id="devSection"' not in html
    assert 'id="devGame"' not in html
    assert 'id="devChartA"' not in html and 'id="devChartC"' not in html
    assert 'id="valDetails"' not in html
    assert "VALIDATION — WHAT HAPPENED NEXT" not in html
    assert "API_GAME_DEV" not in js
    assert "API_VALIDATION" not in js
    assert "/deviation/validation" not in js
    assert "auditBody" not in html


def test_checkpoint_research_surface_absent(client):
    """The checkpoint/residual research table (projected trajectory,
    residual, benchmark Z-of-residual, maturity) is REMOVED from the
    frontend by the Z migration.  The modal presents the descriptive
    Z view only; no residual or trajectory field appears in the assets."""
    html = _html(client)
    js = _js(client)
    assert 'id="devCheckpointsTable"' not in html
    assert 'id="devCheckpointsBody"' not in html
    assert 'id="cpDetails"' not in html and 'id="mtDetails"' not in html
    assert 'id="dzDetails"' not in html
    for label in ("CHECKPOINTS — ALL CLEAN OBSERVATIONS",
                  "MARKET ↔ TRAJECTORY", "DEVIATION / Z-SCORE"):
        assert label not in html
    assert "devRenderCheckpoints" not in js
    assert "market_trajectory_residual" not in js
    assert "projected_final_total" not in js
    assert "z_score" not in js
    assert "residData" not in js
