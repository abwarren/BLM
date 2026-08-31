"""M009-M2 (REFINED) — frontend: MARKET VS FAIR is the PRIMARY scorecard
section; generic Model MAE / Market MAE / model-beat-market is demoted to
a labelled DIAGNOSTIC (population + reference line named).

Served-asset contract (the RUNNING server's dashboard.js is the contract):
  - 'MARKET VS FAIR VALUE' present (primary section header)
  - per-checkpoint table markers (Avg M-F signed, Under/Over Value %)
  - 'GAME-LEVEL SCORECARD' present
  - MODEL vs MARKET block now labelled DIAGNOSTIC with its line type
  - O/U PERFORMANCE labelled DIAGNOSTIC
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


def test_mvf_primary_section_markers(client):
    js = _js(client)
    assert "MARKET VS FAIR VALUE" in js
    assert "Avg M-F" in js
    assert "Under Value %" in js
    assert "GAME-LEVEL SCORECARD" in js


def test_model_vs_market_relabelled_diagnostic(client):
    js = _js(client)
    # The generic market comparison must be demoted, not primary.
    assert "MODEL vs MARKET — DIAGNOSTIC" in js
    assert "O/U PERFORMANCE — DIAGNOSTIC" in js
    assert "checkpoint_market" in js                 # the line-type literal
    assert "population: prediction_scores fragment=0" in js
