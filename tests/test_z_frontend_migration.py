"""Z-SCORE FRONTEND MIGRATION — the user-facing view is now the
descriptive pace-Z surface and the old analytical presentation is gone
from the served dashboard assets.

Proves, statically against the served files (parallel-safe):
  - the primary chart is SCORE vs LIVE LINE — MARKET MOVEMENT with the
    two observed series ONLY (actual cumulative score + live O/U line);
    no trajectory/fair/residual/forecast series exist anywhere
  - the PACE Z-SCORE panel exists (readout + stat cells + own chart)
  - the Z value consumed by the UI is the authoritative API value —
    the frontend formats it (2dp) but never computes it: no z math
    (no x−μ/σ division) exists in the JS
  - Z provenance fields N / μ / σ / benchmark key are rendered from the
    authoritative payload
  - benchmark isolation vocabulary is present (provider | competition |
    period | progress) and no competition-merge logic exists
  - score_line_gap stays exact descriptive arithmetic
  - the descriptive-only firewall on the health/audit side remains
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


def test_primary_chart_series_only_score_and_line(client):
    js = _js(client)
    # the chart head + both observed series
    assert "SCORE vs LIVE LINE — MARKET MOVEMENT" in js
    assert 'label: "Actual score (combined)"' in js
    assert 'label: "Live O/U line"' in js
    # the two datasets live in the primary chart build; no third series
    assert "data.datasets[2]" not in js


def test_no_model_series_anywhere(client):
    js = _js(client)
    for banned in ("projected_final_total", "market_trajectory_residual",
                   "residData", "BLM trajectory", "fair_value",
                   "blm_fair", "win_rate", "blm_side", "z_score",
                   "momentum", "edge", "signal"):
        assert banned not in js, banned


def test_pace_z_panel_present(client):
    js = _js(client)
    html = _html(client)
    assert "PACE Z-SCORE DEVIATION" in js        # panel head
    assert "PACE Z-SCORE</h4>" in js             # panel chart head
    assert 'id="zPanelReadout"' in js
    assert 'id="zStatPace"' in js and 'id="zStatN"' in js
    assert 'id="zStatMu"' in js and 'id="zStatSigma"' in js
    assert "HISTORICAL N" in js
    assert "HISTORICAL MEAN PACE" in js
    assert "HISTORICAL STD DEV" in js
    assert "CURRENT ACTUAL PACE" in js


def test_z_consumed_authoritative_not_computed(client):
    js = _js(client)
    # the Z series + latest value come from the /pace-z payload
    assert "API_GAME_PACE_Z" in js
    assert "pz.series" in js and "s.z" in js
    assert "pz.z" in js or "lastZ" in js
    # formatting only: 2dp sign-explicit display of the API value
    assert "z = n/a" in js
    # NO z math in the browser — no x−μ/σ expression may exist
    assert "/ pz.std_pace" not in js
    assert "mean_pace) /" not in js
    assert "(z - " not in js
    assert "((x" not in js.lower()


def test_z_provenance_from_authoritative_payload(client):
    js = _js(client)
    # provenance fields are read straight from the payload, never invented
    assert "pz.n" in js
    assert "pz.mean_pace" in js
    assert "pz.std_pace" in js
    assert "pz.benchmark_key" in js
    assert "N=" in js and "μ=" in js and "σ=" in js


def test_benchmark_isolation_vocabulary(client):
    js = _js(client)
    # the four partition dimensions are named in the panel provenance
    assert "provider|competition|period|progress" in js
    # no code path merges competitions or infers them
    assert "betual-nba" not in js and "betual-tbsl" not in js


def test_score_line_gap_descriptive_arithmetic(client):
    js = _js(client)
    # the gap is displayed as SCORE − LINE = signed diff, and it is the
    # same arithmetic in the immediate pre-fetch readout (curScore−curLine)
    assert "Score − line" in js
    assert "curScore - curLine" in js
    # it never becomes anything else: no conversion vocabulary exists
    for banned in ("edge", "probab", "calibrat"):
        assert banned not in js.lower(), banned


def test_modal_state_hierarchy_present(client):
    js = _js(client)
    # required visual hierarchy panels
    assert 'modalPanel("Game State"' in js
    assert 'modalPanel("Market State"' in js
    assert 'modalPanel("Pace State"' in js
    assert "PACE Z-SCORE DEVIATION" in js
    # Game State panel carries the canonical identity rows
    assert "Provider" in js and "Competition" in js
    assert "g.provider" in js and "g.competition_slug" in js


def test_descriptive_only_firewall_still_active():
    """The descriptive-only firewall on the observational side must not
    have been touched by the migration."""
    from blm_v4.live_analytics.prospective_health import (
        FORBIDDEN_KEYS, assert_descriptive_only)
    for k in ("edge", "win_rate", "probability", "signal", "staking",
              "ev", "threshold", "betting"):
        assert k in FORBIDDEN_KEYS
    with pytest.raises(ValueError):
        assert_descriptive_only({"a": {"edge": 1}})
    assert_descriptive_only({"a": {"score_line_gap": -4.5}})
