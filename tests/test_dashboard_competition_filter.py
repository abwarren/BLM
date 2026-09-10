"""Dashboard competition filter — canonical competition identifier tests.

The top-pane filter now selects a COMPETITION (league), not a provider
classification.  Filtering must operate on the canonical competition
identifier carried by the live payload (games.competition_slug), never a
display alias — so a label like "NBA" maps to the exact canonical slug
"betual-nba", "CYBER 2K26" to "cyber-basketball-2k26-matches", and so on.

Static contract tests only (served assets, parallel-safe): they pin the
button set, the canonical keys, the human labels, and the fact that the
JS predicate compares competition_slug (and no longer classification).
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

# canonical (competition_slug -> human label) as required by the directive
CANONICAL = {
    "betual-nba": "NBA",
    "betual-kbl": "KBL",
    "betual-cba": "CBA",
    "betual-tbsl": "TBSL",
    "betual-euroleague": "EUROLEAGUE",
    "cyber-basketball-2k26-matches": "CYBER 2K26",
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")
    return TestClient(app)


def _html() -> str:
    return (DASH_STATIC / "index.html").read_text(encoding="utf-8")


def test_filter_buttons_use_canonical_competition_ids(client):
    html = _html()
    assert 'id="filters"' in html
    assert 'data-filter=""' in html                       # ALL = no filter
    for cid, label in CANONICAL.items():
        assert f'data-filter="{cid}"' in html, cid
        assert f'>{label}</button>' in html, label


def test_no_classification_aliases_in_filter_keys(client):
    """The old classification-based filter keys must be gone."""
    html = _html()
    assert 'data-filter="CYBER_2K26"' not in html
    assert 'data-filter="BETUAL_NBA"' not in html


def test_js_filters_on_competition_slug(client):
    js = client.get("/static/dashboard.js").text
    assert "g.competition_slug === state.filter" in js
    # the classification-based predicate must not remain
    assert "g.classification === state.filter" not in js
    # the filter value comes from the button's data-filter attribute
    assert "btn.dataset.filter" in js


def test_all_is_the_unfiltered_default(client):
    js = client.get("/static/dashboard.js").text
    # empty filter string => no filtering (ALL)
    assert "!state.filter" in js
