"""M009-M5 — frontend: DISPARITY BANDS (edge buckets by magnitude x
direction), EVENT DATASET (filterable scorecard events table), and
TIME-OF-DAY (hours + bands) in the served dashboard assets.

Served-asset contract (the RUNNING server's dashboard.js is the contract):
  - 'DISPARITY BANDS' present, grouped BLM_OVER / BLM_UNDER
  - observed rates carry N ('N=… | BLM win rate …'), small samples flagged
    'SMALL SAMPLE'; never 'winning strategy'
  - NULL renders '–' (en dash), never fabricated
  - event dataset: /api/v4/scorecard/events endpoint literal, filter
    controls (direction/freshness/checkpoint/min_diff/game/limit),
    market_status LIVE/STALE/MISSING rendered as-is — NO coercion of
    STALE (or MISSING) to LIVE
  - large-edges preset (min_diff=10) labelled as inspection
  - TIME-OF-DAY hours + bands tables from market_vs_fair.time_of_day

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


def test_disparity_bands_markers(client):
    js = _js(client)
    assert "DISPARITY BANDS" in js
    assert "BLM win rate" in js
    assert "Market win rate" in js
    assert "Avg Δ (signed)" in js
    assert "edge_bucket_min_sample" in js
    assert "reliable" in js


def test_disparity_bands_direction_separation(client):
    js = _js(client)
    # magnitude and direction stay separate: two direction groups, and the
    # group labels make the separation explicit (fair above/below market).
    assert "BLM_OVER" in js
    assert "BLM_UNDER" in js
    assert "fair above market" in js
    assert "fair below market" in js


def test_observed_rate_with_n_not_strategy_claim(client):
    js = _js(client)
    # analytical rule: 'N=443 | BLM win rate 70%' — never '70% winning strategy'
    assert "N=${e.n} | BLM win rate" in js
    assert "SMALL SAMPLE" in js
    assert "winning strategy" not in js


def test_null_renders_en_dash(client):
    js = _js(client)
    # NULL/absent fields render '–', never fabricated values
    assert "–" in js
    assert '?? "–"' in js


def test_event_dataset_endpoint_and_controls(client):
    js = _js(client)
    assert "/api/v4/scorecard/events" in js
    assert "min_diff" in js
    assert "max_diff" in js
    assert "checkpoint" in js
    assert "freshness" in js
    assert "game" in js
    assert "limit" in js
    assert "evLarge" in js
    assert "INSPECTION" in js


def test_event_table_columns_and_total(client):
    js = _js(client)
    assert "checkpoint_pct" in js
    assert "market_line" in js
    assert "blm_fair" in js
    assert ">CP<" in js
    assert "Market status" in js
    assert "market_age_seconds" in js
    assert "momentum_state" in js
    assert "false_momentum" in js
    assert "blm_side" in js
    assert "blm_won" in js
    assert "showing" in js
    assert "of ${total}" in js


def test_market_status_literals_no_stale_to_live_coercion(client):
    js = _js(client)
    # freshness literals must be displayed as-is — never substituted
    assert "LIVE" in js
    assert "STALE" in js
    assert "MISSING" in js
    assert "r.market_status" in js
    # no coercion of STALE/MISSING to LIVE anywhere in the render path
    assert '"STALE" ? "LIVE"' not in js
    assert '"MISSING" ? "LIVE"' not in js
    assert '.replace("STALE"' not in js


def test_time_of_day_tables(client):
    js = _js(client)
    assert "TIME-OF-DAY" in js
    assert "band_def" in js
    assert "blm_win_rate" in js
    assert "market_win_rate" in js
    assert "avg_diff" in js


def test_events_section_in_html(client):
    resp = client.get("/static/index.html")
    assert resp.status_code == 200
    html = resp.text
    assert "EVENT DATASET" in html
    assert "eventsToggle" in html
    assert "evLarge" in html
    # large-edges preset is labelled as inspection, not as profitable
    assert "LARGE EDGES" in html
    assert "NOT a profitability claim" in html
    # freshness literals offered as filters, as-is
    assert "LIVE" in html
    assert "STALE" in html
    assert "MISSING" in html
