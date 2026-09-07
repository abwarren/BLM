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


# ── Static: time-of-day label honesty ────────────────────────────────────

def test_tod_label_first_observed_not_start(client):
    js = _js(client)
    assert "first-observed hour, local" in js
    assert "start hour, local" not in js
    assert "TIME-OF-DAY (first-observed hour, local)" in js


# ── Static: missing odds never 50/50 ─────────────────────────────────────

def test_observations_only_no_probability_ui(client):
    """The live view never renders any probability surface — odds are
    only exposed as raw observed values under modal Details."""
    js = _js(client)
    assert "WIN PROBABILITY" not in js
    assert "win_probability" not in js
    assert "MARKET-IMPLIED" not in js


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

def test_no_trap_signal_ui_in_live_view(client):
    """Trap/signal renderers are absent from the live view entirely;
    the frozen checkpoint market history (observed lines only) remains
    under modal Details ▾."""
    js = _js(client)
    assert "bull_trap" not in js
    assert "trap_meter" not in js
    assert "False Mom" not in js
    assert "CHECKPOINTS — frozen market at each checkpoint" in js
    assert "Market @CP" in js
    assert "market_at_checkpoint" in js


# ── Static: metric labels ────────────────────────────────────────────────

def test_metric_labels_explicit(client):
    js = _js(client)
    # descriptive measurements are named as measurements
    assert "Pts/Min" in js
    assert "Pace gap" in js
    assert "Recent pace 1/2/3/5 min" in js
    assert "LIVE TOTAL" in js
    assert "mkt proximity" in js
    assert "BLM position win rate" in js   # audit scorecard only
    # model-interpretation labels are gone from the live view
    assert "Model data confidence" not in js
    assert "Market-implied win prob" not in js
    assert '"eff "' not in js
    html = _html(client)
    assert "SNAPSHOTS (window)" in html
    assert "served games only" in html
    # HISTORICAL / RESEARCH area present (top-level collapse, per the
    # collapsible-historical directive) and separated from the live view
    assert "HISTORICAL / RESEARCH" in html
    assert "LIVE GAMES" in html


def test_collapsible_historical_and_details(client):
    """Collapsible-sections directive: HISTORICAL / RESEARCH collapses as a
    whole (collapsed by default), the research sub-sections sit inside it
    with independent toggles, game DETAILS default collapsed, CHARTS stay
    available, and section state persists to localStorage.  Frontend-only:
    no deletion, no new prediction logic."""
    html = _html(client)
    js = _js(client)
    # top-level HISTORICAL / RESEARCH toggle wraps the research sections
    assert 'id="auditToggle"' in html
    assert "HISTORICAL / RESEARCH" in html
    assert 'id="auditBody" hidden' in html          # collapsed by default
    # the four research/audit sections live inside the collapsed wrapper
    for sid in ("scorecardSection", "trendsSection", "eventsSection", "gsSection"):
        assert sid in html
    # localStorage preference keys with the required defaults
    assert "blm.historicalResearchCollapsed" in js
    assert "blm.gameDetailsCollapsed" in js
    assert "blm.chartsCollapsed" in js
    assert "prefGet(PREF.HISTORICAL, true)" in js    # collapsed default
    assert "prefGet(PREF.GAME_DETAILS, true)" in js  # collapsed default
    assert "prefGet(PREF.CHARTS, false)" in js       # visible default
    # cards + detail modal expose CHARTS (open) and DETAILS (collapsed)
    assert "details.card-charts" in js
    assert "details.card-details" in js
    assert "data-label=\"CHARTS\"" in js
    assert "data-label=\"DETAILS\"" in js
    # descriptive defaults stay in the live view
    assert "LIVE GAMES" in html
    assert "Pts/Min" in js
    assert "LIVE TOTAL" in js


def test_deviation_research_frontend_present(client):
    """Phase 2/3 research surface: the deviation charts + the VALIDATION
    (WHAT HAPPENED NEXT) block exist inside the collapsed research area
    (never the live predictive view), wired to the research endpoints."""
    html = _html(client)
    js = _js(client)
    assert 'id="devSection"' in html
    assert 'id="devGame"' in html
    assert 'id="devChartA"' in html and 'id="devChartC"' in html
    assert 'id="valDetails"' in html
    assert "VALIDATION — WHAT HAPPENED NEXT" in html
    assert "API_GAME_DEV" in js
    assert "API_VALIDATION" in js
    assert "/deviation/validation" in js
    # research section lives inside the collapsed HISTORICAL / RESEARCH area
    assert "auditBody" in html


def test_checkpoint_results_present_and_collapsible(client):
    """UI-correction directive: checkpoint results are RESTORED, not removed.
    Every clean checkpoint's full field set (clock, period, score, observed /
    required pace, pace gap, live line, projected trajectory, residual,
    benchmark N/mean/std, Z-score, maturity) must exist in the DOM; research
    sub-sections are collapsible and collapsing never deletes the data (rows
    are rendered into the table body even while the section is hidden)."""
    html = _html(client)
    js = _js(client)
    # CHECKPOINTS table exists with every required research field as a header
    assert 'id="devCheckpointsTable"' in html
    assert 'id="devCheckpointsBody"' in html
    for header in ("ELAPSED", "CLOCK", "PERIOD", "SCORE", "LIVE O/U LINE",
                   "OBS PACE", "REQ PACE", "PACE GAP", "PROJECTED TOTAL",
                   "RESIDUAL", "N", "μ", "σ", "Z", "STATUS"):
        assert f">{header}<" in html
    # the table lives inside a collapsible section (CHECKPOINTS), with the
    # two chart groups and VALIDATION as sibling collapsibles
    assert 'id="cpDetails"' in html and 'id="mtDetails"' in html
    assert 'id="dzDetails"' in html and 'id="valDetails"' in html
    for label in ("CHECKPOINTS — ALL CLEAN OBSERVATIONS",
                  "MARKET ↔ TRAJECTORY", "DEVIATION / Z-SCORE",
                  "VALIDATION — WHAT HAPPENED NEXT"):
        assert label in html
    # rendering is additive: rows go into the tbody whether or not the
    # section is open — collapse only hides, never deletes
    assert "devRenderCheckpoints" in js
    assert "devCheckpointsBody" in js
    assert "collapsing only hides" in js or "collapse only hides" in js
    # per-subsection localStorage prefs with the required defaults
    for key in ("blm.checkpointsCollapsed", "blm.marketTrajectoryCollapsed",
                "blm.deviationCollapsed", "blm.validationCollapsed"):
        assert key in js
    assert '"cpDetails", PREF.CHECKPOINTS, true' in js   # checkpoints collapsed default
    assert '"mtDetails", PREF.MARKET_TRAJ, false' in js  # charts visible default
    assert '"dzDetails", PREF.DEVIATION_Z, false' in js
    assert '"valDetails", PREF.VALIDATION, false' in js
    # every checkpoint row carries residual + Z + benchmark maturity
    assert "market_trajectory_residual" in js
    assert "benchmark_n" in js and "benchmark_std" in js
    assert "benchmark_status" in js
    assert "z_score" in js
