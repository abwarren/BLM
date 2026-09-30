"""Event-view selected-section extraction (2026-09-30).

Regression pins for the audit finding that reproduced a fresh capture:
the Betual event view renders the live sidebar's WHOLE game list, and
the legacy whole-page heuristics

  * attributed the FIRST "N : M, (...)" scoreboard on the page — an
    unrelated WNBA game (Aces–Fever 83:90) — to every fixture, and
  * depended on a standalone clock line in the first 30 text lines,
    which today's DOM never produces (6,870 unverified skips on
    2026-09-29 alone).

parse_event_view now takes identity + scoreboard from the SELECTED
game section ("market-game-section active" + selected-game-indicator)
and falls back to the legacy heuristics only when that section is
absent.  These tests pin: correct extraction on the REAL captured
fixture, decoy resistance on a synthetic sidebar, and the legacy
fallback for old deploys.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from blm_v4.event_parser import parse_event_view

FIXTURE = Path(__file__).parent / "fixtures" / "event_view" / \
    "betual_eventview_31067951.html"

DECOY_SIDEBAR = """
<html><body><div>
  <div class="market-game-section ">
    <div class="selected-game-indicator"></div>
    <p class="market-game-team"><span class="market-game-team-name ellipsis">Las Vegas Aces</span><b class="market-game-odd">83</b></p>
    <p class="market-game-team"><span class="market-game-team-name ellipsis">Indiana Fever</span><b class="market-game-odd">90</b></p>
    <div class="market-game-part-container"><span class="market-game-part">4th Quarter</span></div>
    <div class="market-game-additional-info-container"><span class="market-game-additional-info">83 : 90, (26:18), (17:30), (17:24), (23:18) 03:31</span></div>
  </div>
  <div class="market-game-section active">
    <div class="selected-game-indicator"></div>
    <p class="market-game-team"><span class="market-game-team-name ellipsis">Olympiacos Piraeus BC Virtual</span><b class="market-game-odd">10</b></p>
    <p class="market-game-team"><span class="market-game-team-name ellipsis">Valencia Basket Virtual</span><b class="market-game-odd">12</b></p>
    <div class="market-game-part-container"><span class="market-game-part">1st Quarter</span></div>
    <div class="market-game-additional-info-container"><span class="market-game-additional-info">10 : 12, (10:12) 03:31</span></div>
  </div>
</div></body></html>
"""

LEGACY_TEXT = """
Cyber Basketball. 2K26 Matches
4th Quarter
09:46'
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
1 32 22  2 28 23  3 33 22  4 7 6  Quarter 100 73
100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46
All Match Totals Handicaps Markets
Points Handicap
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
-26.5 1.95  +26.5 1.75
Total Points
Over Under
216.5 1.70 2.02   217.5 1.80 1.90
"""


@pytest.mark.skipif(not FIXTURE.exists(), reason="captured fixture missing")
def test_real_fixture_teams_come_from_selected_section():
    """On the real 2026-09-30 capture the parser must return the
    SELECTED game's identity (Olympiacos/Valencia, live 10–12 1st
    quarter) — never the sidebar decoy (Aces/Fever, 83:90)."""
    ev = parse_event_view(FIXTURE.read_text())
    assert ev["home_team"] == "Olympiacos Piraeus BC Virtual"
    assert ev["away_team"] == "Valencia Basket Virtual"
    assert (ev["home_score"], ev["away_score"]) == (10, 12), (
        "score came from another game's sidebar scoreboard")
    assert ev["period_label"] == "1st Quarter"
    assert ev["quarter_scores"] == [(10, 12)]


def test_decoy_sections_never_win():
    """A synthetic sidebar whose FIRST (unselected) section carries a
    different game's full scoreboard must still yield the selected
    section's identity and score."""
    ev = parse_event_view(DECOY_SIDEBAR)
    assert ev["home_team"] == "Olympiacos Piraeus BC Virtual"
    assert ev["away_team"] == "Valencia Basket Virtual"
    assert (ev["home_score"], ev["away_score"]) == (10, 12)
    assert ev["period_label"] == "1st Quarter"


def test_legacy_clock_line_fallback_still_works():
    """Old deploys (2026-08-30 layout: no market-game-section markup)
    keep working through the clock-line heuristic."""
    ev = parse_event_view(LEGACY_TEXT)
    assert ev["home_team"] == "Oklahoma City Thunder Cyber"
    assert ev["away_team"] == "San Antonio Spurs Cyber"
    assert (ev["home_score"], ev["away_score"]) == (100, 73)
    assert ev["period_label"] == "4th Quarter"


def test_no_identity_yields_empty_teams():
    """Neither selected section nor clock line → empty teams (the
    collector's verification guard then refuses the page downstream)."""
    ev = parse_event_view("<html><body><p>Total Points</p></body></html>")
    assert ev["home_team"] == "" and ev["away_team"] == ""
