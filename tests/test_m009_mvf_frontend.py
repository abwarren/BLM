"""M009-M2 (REFINED, post Z-migration) — the Market-vs-Fair scorecard and
MODEL-vs-MARKET diagnostic surfaces have been REMOVED from the frontend
by the Z-score migration.  These static tests pin that removal: no fair-
value, win-rate, or diagnostic scorecard presentation remains in the
served dashboard assets (the backend endpoints that feed them are
untouched and out of scope here).

Served-asset contract (the RUNNING server's dashboard.js is the contract):
  - 'MARKET VS FAIR VALUE' absent (never renamed, never hidden — gone)
  - 'GAME-LEVEL SCORECARD' absent
  - MODEL vs MARKET / O/U PERFORMANCE diagnostics absent
  - the live view carries the descriptive pace-Z surface instead
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


def test_mvf_primary_section_absent(client):
    js = _js(client)
    assert "MARKET VS FAIR VALUE" not in js
    assert "Avg M-F" not in js
    assert "Under Value %" not in js
    assert "GAME-LEVEL SCORECARD" not in js
    assert "mvf-game" not in js
    assert "market_vs_fair" not in js


def test_model_vs_market_diagnostics_absent(client):
    js = _js(client)
    assert "MODEL vs MARKET — DIAGNOSTIC" not in js
    assert "O/U PERFORMANCE — DIAGNOSTIC" not in js
    assert "MODEL SCORECARD" not in js
    # the descriptive replacement surface is present
    assert "PACE Z-SCORE" in js
    assert "SCORE vs LIVE LINE — MARKET MOVEMENT" in js
