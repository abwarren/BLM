"""BLM V4 — Betual team-name normalization + sport-guard tests.

Covers the 2026-09-12 fixes:
  - normalize_betual_team: "Virtual" marker strip (prefix + suffix
    renderings), whitespace tolerance, idempotence, no-op for Cyber /
    conventional names
  - classify_competition sport guard: non-basketball panel sections
    must not classify as basketball populations
  - discovery threading the panel sport into classification
  - collector boundary behavior is covered indirectly via the pure
    functions; reconciliation slug tolerance is covered here too.
"""

from __future__ import annotations

import pytest

from blm_v4.classifications import (
    Classification,
    classify_competition,
    classify_event_url,
    normalize_betual_team,
    slugify_team,
)
from blm_v4.discovery import discover_competitions
from blm_v4.reconcile import _strip_virtual_slug


# ── normalize_betual_team ──────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # actual live source format (suffix marker)
    ("Los Angeles Lakers Virtual", "Los Angeles Lakers"),
    ("Petkim Spor KB Virtual", "Petkim Spor KB"),
    ("Korfez Basket Virtual", "Korfez Basket"),
    # brief-mandated prefix rendering
    ("Virtual Lakers", "Lakers"),
    ("Virtual Golden State Warriors", "Golden State Warriors"),
    # harmless whitespace variations
    ("  Lakers   Virtual ", "Lakers"),
    ("  Virtual  Lakers  ", "Virtual  Lakers".replace("Virtual  ", "")),
    # case-aware marker match (source case of team part preserved)
    ("lakers virtual", "lakers"),
    ("VIRTUAL LAKERS", "LAKERS"),
    # already-canonical / no marker: unchanged (no arbitrary rewriting)
    ("Lakers", "Lakers"),
    ("Oklahoma City Thunder Cyber", "Oklahoma City Thunder Cyber"),
    ("Fluminense RJ U22", "Fluminense RJ U22"),
    ("", ""),
    (None, ""),
])
def test_normalize_betual_team(raw, expected):
    assert normalize_betual_team(raw) == expected


def test_normalize_idempotent():
    once = normalize_betual_team("Los Angeles Lakers Virtual")
    assert normalize_betual_team(once) == once


def test_normalize_strips_only_the_marker():
    # Edge markers are presentation; an INNER "Virtual" is part of the
    # team name and must survive (no arbitrary rewriting).
    assert normalize_betual_team("United Virtual Airlines Virtual") == \
        "United Virtual Airlines"
    assert normalize_betual_team("Virtual United Virtual Airlines") == \
        "United Virtual Airlines"


# ── sport guard in classification ──────────────────────────────────

def test_betual_football_is_not_betual_nba():
    # Observed live 2026-09-12: virtual football under "Virtual Matches"
    # matched the "betual" name signal and classified BETUAL_NBA.
    assert classify_competition(
        display_name="Betual England Premier League",
        region="Virtual Matches",
        sport="Football",
    ) == Classification.UNKNOWN


def test_betual_volleyball_is_not_betual_nba():
    assert classify_competition(
        display_name="Betual Sultanlar Ligi",
        region="Virtual Matches",
        sport="Volleyball",
    ) == Classification.UNKNOWN


def test_cyber_tennis_is_not_cyber_2k26():
    assert classify_competition(
        display_name="Cyber Tennis AO2 Matches - Women",
        region="Cyber Matches",
        sport="Tennis",
    ) == Classification.UNKNOWN


def test_betual_nba_basketball_still_classifies():
    assert classify_competition(
        display_name="Betual NBA",
        region="Virtual Matches",
        sport="Basketball",
    ) == Classification.BETUAL_NBA


def test_cyber_2k26_basketball_still_classifies():
    assert classify_competition(
        display_name="Cyber Basketball. 2K26 Matches",
        region="World",
        sport="Basketball",
    ) == Classification.CYBER_2K26


def test_ebasketball_sport_accepted():
    assert classify_competition(
        display_name="Cyber Basketball. 2K26 Matches",
        region="World",
        sport="E-Basketball",
    ) == Classification.CYBER_2K26


def test_sport_none_keeps_legacy_behaviour():
    # Callers without sport context (e.g. reconcile) keep legacy semantics
    assert classify_competition(
        display_name="Betual NBA") == Classification.BETUAL_NBA


def test_event_url_sport_guard():
    # A virtual-football event URL must not classify BETUAL_NBA even
    # though its competition slug contains "betual".
    url = ("https://www.pokerbet.co.za/en/sports/live/event-view/Football/"
           "Virtual%20Matches/18297000/betual-england-premier-league/"
           "30879999/manchester-united-virtual-tottenham-hotspur-virtual")
    assert classify_event_url(url) == Classification.UNKNOWN


def test_event_url_basketball_still_classifies():
    url = ("https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/"
           "Virtual%20Matches/18296756/betual-nba/30738600/"
           "sacramento-kings-virtual-miami-heat-virtual")
    assert classify_event_url(url) == Classification.BETUAL_NBA


# ── discovery threads the panel sport ──────────────────────────────

def _row(home: str, away: str) -> str:
    return f"""
      <div class="market-game-section">
        <p class="market-game-team"><span class="market-game-team-name ellipsis">{home}</span><b class="market-game-odd">10</b></p>
        <p class="market-game-team"><span class="market-game-team-name ellipsis">{away}</span><b class="market-game-odd">20</b></p>
      </div>"""


def _panel(sport: str, region: str, comp: str, rows: str) -> str:
    return f"""
    <div class="pp-sport-list-holder-bc"><div class="left-menu-scroll">
      <div class="sp-sub-list-bc  active selected">
        <div class="sp-s-l-head-bc"><div class="sp-s-l-h-title-content ellipsis"><p class="sp-s-l-h-title-bc ellipsis">{sport}</p></div></div>
        <div class="sp-s-l-b-content-wrp verticalNavigationContent">
          <div class="sp-sub-list-bc">
            <div class="sp-s-l-head-bc"><div class="sp-s-l-h-title-content ellipsis"><p class="sp-s-l-h-title-bc ellipsis">{region}</p><p class="sp-s-l-h-title-bc ellipsis">{comp}</p></div></div>
            <div class="sp-s-l-b-content-bc">{rows}</div>
          </div>
        </div>
      </div>
    </div></div>"""


def test_discovery_betual_football_unknown():
    html = _panel(
        "Football", "Virtual Matches", "Betual England Premier League",
        _row("Manchester United Virtual", "Tottenham Hotspur Virtual"))
    comps = discover_competitions(html)
    assert len(comps) == 1
    assert comps[0].classification == Classification.UNKNOWN


def test_discovery_cyber_tennis_unknown():
    html = _panel(
        "Tennis", "Cyber Matches", "Cyber Tennis AO2 Matches - Women",
        _row("Emma Raducanu Cyber", "Iga Swiatek Cyber"))
    comps = discover_competitions(html)
    assert comps[0].classification == Classification.UNKNOWN


def test_discovery_betual_nba_basketball_kept():
    html = _panel(
        "Basketball", "Virtual Matches", "Betual NBA",
        _row("Sacramento Kings Virtual", "Miami Heat Virtual"))
    comps = discover_competitions(html)
    assert comps[0].classification == Classification.BETUAL_NBA


# ── reconciliation slug tolerance ──────────────────────────────────

def test_strip_virtual_slug_suffix_format():
    # current source: marker tokens at slug EDGES are removed; inner
    # "-virtual-" tokens between the two team slugs are kept (they belong
    # to neither team's own slug) — containment still works.
    assert _strip_virtual_slug(
        "sacramento-kings-virtual-miami-heat-virtual"
    ) == "sacramento-kings-virtual-miami-heat"


def test_strip_virtual_slug_inner_token_case():
    # "korfez-basket-virtual-petkim-spor-kb-virtual": the trailing token
    # goes, the inner one survives (it separates the two team slugs).
    assert _strip_virtual_slug(
        "korfez-basket-virtual-petkim-spor-kb-virtual"
    ) == "korfez-basket-virtual-petkim-spor-kb"


def test_strip_virtual_slug_idempotent():
    once = _strip_virtual_slug("lakers-virtual-miami-heat-virtual")
    assert _strip_virtual_slug(once) == once


def test_slugify_team_on_canonical_name_matches_stripped_slug():
    # The containment check: slugify(canonical "Miami Heat") must be a
    # suffix of the marker-stripped source slug.
    stripped = _strip_virtual_slug(
        "sacramento-kings-virtual-miami-heat-virtual")
    rec_home = slugify_team("Sacramento Kings")
    rec_away = slugify_team("Miami Heat")
    assert stripped.startswith(rec_home) and stripped.endswith(rec_away)


def test_slugify_team_unchanged_for_cyber():
    assert slugify_team("Oklahoma City Thunder Cyber") == \
        "oklahoma-city-thunder-cyber"


# ── consolidated directive (2026-09-12 §2): classification-gated boundary ──

def test_canonical_teams_normalizes_betual_only():
    """§2 CRITICAL — normalization applies ONLY to Betual-origin data."""
    from blm_v4.collector import PokerBetCollector
    norm = PokerBetCollector._canonical_teams
    # Betual: both renderings normalized
    assert norm("Virtual Team A", "Team B Virtual",
                "BETUAL_NBA") == ("Team A", "Team B")
    # non-Betual: untouched, even when the name carries "Virtual"
    assert norm("Virtual Team A", "Team B Virtual",
                "CYBER_2K26") == ("Virtual Team A", "Team B Virtual")
    assert norm("Virtual Team A", "Team B Virtual",
                "CONVENTIONAL") == ("Virtual Team A", "Team B Virtual")
    # None classification: historical single-family behavior (normalize)
    assert norm("Virtual Team A", "Team B Virtual",
                None) == ("Team A", "Team B")


def test_canonical_teams_whitespace_and_idempotent_via_collector():
    from blm_v4.collector import PokerBetCollector
    norm = PokerBetCollector._canonical_teams
    # extra whitespace collapses with the marker strip
    assert norm("Virtual  Team B", "Team A   Virtual",
                "BETUAL_NBA") == ("Team B", "Team A")
    # idempotent
    once = norm("Virtual Team A", "Virtual  Team B", "BETUAL_NBA")
    assert norm(*once, "BETUAL_NBA") == once


# ── consolidated directive (2026-09-12 §3): duration/pace correctness ──

def test_betual_classification_durations():
    """BETUAL_NBA keeps its canonical regulation duration (40:00, 10'/q)."""
    from blm_v4.projection import duration_for
    assert duration_for("BETUAL_NBA") == (10.0, 40.0)
    assert duration_for("CYBER_2K26") == (12.0, 48.0)
