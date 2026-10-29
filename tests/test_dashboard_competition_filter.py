"""Dashboard competition filter — canonical competition identifier tests.

The filter selects a COMPETITION (league), not a provider classification, and
filtering must operate on the canonical competition identifier carried by the
live payload (games.competition_slug), never a display alias — so "NBA" maps to
"betual-nba", "CYBER 2K26" to "cyber-basketball-2k26-matches", and so on.

2026-10-29: league became a DROPDOWN inside LIVE GAMES.  The destination spec
(tabs = dataset segments; league/period/signal/sort = dropdowns) removed the
button pills precisely so the selected league has ONE control instead of a pill
row that duplicated the same state.  These tests pin the same property on the
new control: canonical slugs as option VALUES, human labels as option TEXT, the
predicate still comparing competition_slug, and no second copy of the state.
"""
from __future__ import annotations

import re
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


def _league_select_options(html: str) -> dict:
    """{value: label} for every <option> inside #leagueFilter."""
    m = re.search(r'<select id="leagueFilter".*?</select>', html, re.S)
    assert m, "the LEAGUE dropdown (#leagueFilter) must exist"
    out = {}
    for value, label in re.findall(
            r'<option value="([^"]*)"[^>]*>([^<]*)</option>', m.group(0)):
        out[value] = label.strip()
    return out


def test_league_dropdown_uses_canonical_competition_ids(client):
    options = _league_select_options(_html())
    assert options.get("") == "ALL"                      # ALL = no filter
    for cid, label in CANONICAL.items():
        assert options.get(cid) == label, cid


def test_no_classification_aliases_in_filter_keys(client):
    """The old classification-based filter keys must be gone."""
    html = _html()
    assert 'data-filter="CYBER_2K26"' not in html
    assert 'data-filter="BETUAL_NBA"' not in html


def test_league_state_has_one_control(client):
    """No duplicate league state: the tabs are dataset segments, not leagues."""
    html = _html()
    assert 'data-filter="betual-nba"' not in html        # no button pill copy
    segments = re.search(r'<div class="ds-tabs" id="liveSegments".*?</div>', html, re.S)
    assert segments, "the dataset segment tabs must exist"
    for value in ("", "UNDER", "OVER", "WATCH"):
        assert f'data-segment="{value}"' in segments.group(0)


def test_js_filters_on_competition_slug(client):
    js = client.get("/static/dashboard.js").text
    assert "g.competition_slug === state.filter" in js
    # the classification-based predicate must not remain
    assert "g.classification === state.filter" not in js
    # the filter value now comes from the SELECT's value
    assert 'state.filter = ev.target.value || ""' in js
    assert "btn.dataset.filter" not in js


def test_all_is_the_unfiltered_default(client):
    js = client.get("/static/dashboard.js").text
    # empty filter string => no filtering (ALL)
    assert "!state.filter" in js
