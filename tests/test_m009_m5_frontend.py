"""Frontend contract after the Z-SCORE migration — the served dashboard
assets carry NO old-model research/analytical presentation.

Served-asset contract (the RUNNING server's dashboard.js / index.html is
the contract):
  - the old research surfaces are GONE from the frontend assets:
    DISPARITY BANDS, EVENT DATASET, TIME-OF-DAY scorecards, MODEL
    SCORECARD, MARKET VS FAIR, deviation research — no /scorecard,
    /scorecard/events or /deviation endpoint literals remain in the JS
  - freshness literals LIVE/STALE/MISSING still render as-is (observed
    market state) — NO coercion of STALE (or MISSING) to LIVE
  - NULL/absent fields still render '–' (en dash), never fabricated
  - nothing claims a 'winning strategy' anywhere in the assets

Static-asset string tests only (parallel-safe: the backend may be
mid-change while this runs).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"


@pytest.fixture
def client():
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


# ── the old-model surfaces are REMOVED from the frontend assets ──────

def test_disparity_bands_absent(client):
    js = _js(client)
    assert "DISPARITY BANDS" not in js
    assert "BLM position win rate" not in js
    assert "win rate" not in js
    assert "edge_bucket_min_sample" not in js
    assert "reliable" not in js


def test_old_model_vocabulary_absent(client):
    js = _js(client)
    html = _html(client)
    for asset in (js, html):
        assert "BLM_OVER" not in asset
        assert "BLM_UNDER" not in asset
        assert "market_vs_fair" not in asset
        assert "fair" not in asset.lower()
        assert "EVENT DATASET" not in asset
        assert "TIME-OF-DAY" not in asset
        assert "MODEL SCORECARD" not in asset
        assert "PREDICTIVE CHECKPOINTS" not in asset
        assert "SETTLEMENT / TERMINAL" not in asset
        assert "winning strategy" not in asset


def test_old_research_endpoint_literals_absent(client):
    js = _js(client)
    assert "/api/v4/scorecard" not in js
    assert "/api/v4/scorecard/events" not in js
    assert "/api/v4/trends" not in js
    assert "/api/v4/deviation" not in js
    assert "/deviation/benchmarks" not in js
    assert "/deviation/validation" not in js
    assert "evLarge" not in js
    assert "eventsToggle" not in js
    assert "blm_fair" not in js
    assert "blm_side" not in js
    assert "blm_won" not in js
    assert "momentum_state" not in js
    assert "false_momentum" not in js


def test_null_renders_en_dash(client):
    js = _js(client)
    # NULL/absent fields render '–', never fabricated values
    assert "–" in js
    assert '?? "–"' in js


def test_market_status_literals_no_stale_to_live_coercion(client):
    js = _js(client)
    # freshness literals must be displayed as-is — never substituted
    assert "LIVE" in js
    assert "STALE" in js
    assert "MISSING" in js
    assert "market_status" in js or "mstatus" in js
    # no coercion of STALE/MISSING to LIVE anywhere in the render path
    assert '"STALE" ? "LIVE"' not in js
    assert '"MISSING" ? "LIVE"' not in js
    assert '.replace("STALE"' not in js


def test_no_old_section_markup_in_html(client):
    html = _html(client)
    assert "HISTORICAL / RESEARCH" not in html
    assert "scorecardToggle" not in html
    assert "devToggle" not in html
    assert "gsToggle" not in html
    assert "auditToggle" not in html
    # the live view + modal chrome remain
    assert "LIVE GAMES" in html
    assert "modalBackdrop" in html
