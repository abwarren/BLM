"""BLM V4 — PokerBet pipeline tests.

Replayable fixtures for BOTH categories (CYBER_2K26 + BETUAL_NBA) proving:
  - discovery of each category
  - correct classification
  - correct source (PokerBet)
  - durable identity (source + source_game_id)
  - duplicate-game handling
  - snapshot persistence (append-only, no overwrite)
  - historical records stay separate per classification
  - CYBER_2K26 statistics ≠ BETUAL_NBA statistics (no leakage)
"""

from __future__ import annotations

import os

import pytest

from blm_v4.classifications import (
    Classification,
    canonical_competition_name,
    classify_competition,
    classify_event_url,
    parse_event_url,
)
from blm_v4.discovery import (
    discover_competitions,
    find_relevant_competitions,
)
from blm_v4.event_parser import parse_event_view
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.reconcile import reconcile_event
from blm_v4.storage import PokerBetStore

# ── Fixtures ────────────────────────────────────────────────────────

CYBER_URL = (
    "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/World/"
    "18295203/cyber-basketball-2k26-matches/30727642/"
    "oklahoma-city-thunder-cyber-san-antonio-spurs-cyber"
)
BETUAL_URL = (
    "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/"
    "Virtual%20Matches/18296756/betual-nba/30738600/"
    "sacramento-kings-virtual-miami-heat-virtual"
)

CYBER_EVENT_TEXT = """Cyber Basketball. 2K26 Matches
4th Quarter
09:46'
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
1 32 22 2 28 23 3 33 22 4 7 6 Quarter 100 73
100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46
All Match Totals Handicaps Markets
Points Handicap
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
-26.5 1.95 +26.5 1.75
-25.5 1.75 +25.5 1.95
Total Points
Over Under
216.5 1.70 2.02
217.5 1.80 1.90
218.5 1.90 1.80
Oklahoma City Thunder Cyber Total Points
Over Under
121.5 1.75 1.95
San Antonio Spurs Cyber Total Points
Over Under
95.5 1.75 1.95"""

BETUAL_EVENT_TEXT = """Betual NBA
3rd Quarter
04:15'
Sacramento Kings Virtual
Miami Heat Virtual
1 29 25 2 28 25 3 21 26 Quarter 78 76
78 : 76, (29:25), (28:25), (21:26) 04:15
4 Quarters of 12 min. Simulated Game
All Match Totals Handicaps Halves Quarters Markets
Match Winner
Sacramento Kings Virtual
Miami Heat Virtual
1.65 2.20
Points Handicap
Sacramento Kings Virtual
Miami Heat Virtual
-1.5 1.80 +1.5 2.02
Total Points
Over Under
225.5 1.55 2.40
227.5 1.85 1.96
229.5 2.20 1.65
Sacramento Kings Virtual Total Points
Over Under
111.5 1.32 3.25
Miami Heat Virtual Total Points
Over Under
114.5 1.82 2.00"""


def _row_span(comp_name: str, region: str, count: str, row_html: str) -> str:
    return f"""
    <div class="sp-sub-list-bc  active selected">
      <div class="sp-s-l-head-bc" role="button" aria-expanded="true">
        <div class="sp-s-l-h-title-content ellipsis"><p class="sp-s-l-h-title-bc ellipsis">{region}</p><p class="sp-s-l-h-title-bc ellipsis">{comp_name}</p></div>
        <span class="sp-s-l-h-count-bc">{count}</span>
      </div>
      <div class="sp-s-l-b-content-bc">{row_html}</div>
    </div>"""


def _row(home: str, hs: int, away: str, as_: int, period: str,
         spread: str, detail: str, start: str, w1: str, w2: str) -> str:
    return f"""
      <div class="market-game-section">
        <p class="market-game-team"><span class="market-game-team-name ellipsis">{home}</span><b class="market-game-odd">{hs}</b></p>
        <p class="market-game-team"><span class="market-game-team-name ellipsis">{away}</span><b class="market-game-odd">{as_}</b></p>
        <div class="market-game-part-container"><span class="market-game-part">{period}</span><b>{spread}</b></div>
        <div class="market-game-additional-info-container"><span class="market-game-additional-info">{detail}</span><time class="market-game-additional-info-time">{start}</time></div>
        <div class="market-group-holder-bc left-menu-market">
          <div class="market-group-item-bc"><div class="sgm-market-g-i-cell-bc market-bc"><span class="market-name-bc ellipsis">W1</span><div class="market-odds-container"><span class="market-odd-bc">{w1}</span></div></div></div>
          <div class="market-group-item-bc"><div class="sgm-market-g-i-cell-bc market-bc"><span class="market-name-bc ellipsis">W2</span><div class="market-odds-container"><span class="market-odd-bc">{w2}</span></div></div></div>
        </div>
      </div>"""


@pytest.fixture
def live_panel_html() -> str:
    cyber_rows = (
        _row("Oklahoma City Thunder Cyber", 100, "San Antonio Spurs Cyber", 73,
             "4th Quarter", "+12", "100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46",
             "15:45", "1.25", "3.57")
        + _row("Toronto Raptors Cyber", 7, "Miami Heat Cyber", 5,
               "1st Quarter", "+41", "7 : 5, (7:5) 09:35",
               "16:30", "1.80", "1.95")
    )
    betual_rows = (
        _row("Sacramento Kings Virtual", 78, "Miami Heat Virtual", 76,
             "3rd Quarter", "+37", "78 : 76, (29:25), (28:25), (21:26) 04:15",
             "16:02", "1.65", "2.20")
        + _row("Memphis Grizzlies Virtual", 80, "San Antonio Spurs Virtual", 90,
               "3rd Quarter", "+36", "80 : 90, (28:38), (39:35), (13:17) 05:15",
               "16:02", "8.00", "1.05")
    )
    return f"""
    <div class="pp-sport-list-holder-bc"><div class="left-menu-scroll">
      <div class="sp-sub-list-bc  active selected">
        <div class="sp-s-l-head-bc"><div class="sp-s-l-h-title-content ellipsis"><p class="sp-s-l-h-title-bc ellipsis">Basketball</p></div></div>
        <div class="sp-s-l-b-content-wrp verticalNavigationContent">
          {_row_span("Cyber Basketball. 2K26 Matches", "World", "2", cyber_rows)}
          {_row_span("Betual NBA", "Virtual Matches", "2", betual_rows)}
        </div>
      </div>
    </div></div>"""


@pytest.fixture
def store(tmp_path):
    return PokerBetStore(tmp_path / "pokerbet_test.db")


# ── Classification ─────────────────────────────────────────────────

def test_classify_cyber_by_name_and_slug():
    assert classify_competition(display_name="Cyber Basketball. 2K26 Matches") == Classification.CYBER_2K26
    assert classify_competition(competition_slug="cyber-basketball-2k26-matches") == Classification.CYBER_2K26
    assert classify_event_url(CYBER_URL) == Classification.CYBER_2K26


def test_classify_betual_by_name_and_slug():
    assert classify_competition(display_name="Betual NBA") == Classification.BETUAL_NBA
    assert classify_competition(competition_slug="betual-nba") == Classification.BETUAL_NBA
    assert classify_event_url(BETUAL_URL) == Classification.BETUAL_NBA


def test_canonical_names():
    assert canonical_competition_name(Classification.CYBER_2K26) == "Cyber Basketball 2K26"
    assert canonical_competition_name(Classification.BETUAL_NBA) == "Betual NBA"


def test_url_taxonomy_parses():
    tax = parse_event_url(BETUAL_URL)
    assert tax is not None
    assert tax["game_id"] == "30738600"
    assert tax["competition_id"] == "18296756"
    assert tax["competition_slug"] == "betual-nba"
    assert tax["region"] == "Virtual%20Matches"


# ── Discovery ──────────────────────────────────────────────────────

def test_discovery_finds_both_categories(live_panel_html):
    comps = discover_competitions(live_panel_html)
    by_cls = {c.classification: c for c in comps}
    assert Classification.CYBER_2K26 in by_cls
    assert Classification.BETUAL_NBA in by_cls
    assert len(by_cls[Classification.CYBER_2K26].games) == 2
    assert len(by_cls[Classification.BETUAL_NBA].games) == 2


def test_discovery_assigns_correct_source_taxonomy(live_panel_html):
    comps = find_relevant_competitions(live_panel_html)
    cyber = next(c for c in comps if c.classification == Classification.CYBER_2K26)
    betual = next(c for c in comps if c.classification == Classification.BETUAL_NBA)
    assert cyber.region == "World"
    assert cyber.sport == "Basketball"
    assert betual.region == "Virtual Matches"
    g = betual.games[0]
    assert g.home_team == "Sacramento Kings Virtual"
    assert g.home_score == 78 and g.away_score == 76
    assert g.period_label == "3rd Quarter"
    assert g.clock == "04:15"
    assert g.w1_odds == 1.65 and g.w2_odds == 2.20


# ── Event parsing ──────────────────────────────────────────────────

def test_parse_cyber_event():
    p = parse_event_view(CYBER_EVENT_TEXT)
    assert p["home_score"] == 100 and p["away_score"] == 73
    assert p["quarter"] == 4 and p["period_label"] == "4th Quarter"
    assert p["clock"] == "09:46"
    assert p["total"]["first_line"] == 216.5
    assert p["handicap"]["first_home_line"] == -26.5
    assert p["team_totals"]["Oklahoma City Thunder Cyber"]["line"] == 121.5
    assert len(p["quarter_scores"]) == 4


def test_parse_betual_event():
    p = parse_event_view(BETUAL_EVENT_TEXT)
    assert p["home_score"] == 78 and p["away_score"] == 76
    assert p["quarter"] == 3
    assert p["total"]["first_line"] == 225.5
    assert p["match_winner"]["home_odds"] == 1.65
    assert p["simulated_note"] is True  # Betual's own market structure marker


# ── Identity & dedup ───────────────────────────────────────────────

def test_durable_identity_source_game_id():
    g = PokerBetGame(source_game_id="30727642", classification="CYBER_2K26",
                     competition="Cyber Basketball 2K26", game_family="cyber",
                     home_team="Oklahoma City Thunder Cyber",
                     away_team="San Antonio Spurs Cyber", source_url=CYBER_URL)
    assert g.identity_key() == "PokerBet:30727642"
    assert g.source == "PokerBet"
    assert g.classification == "CYBER_2K26"


def test_duplicate_game_handled(store):
    g1 = PokerBetGame(source_game_id="30727642", classification="CYBER_2K26",
                      competition="Cyber Basketball 2K26", game_family="cyber",
                      home_team="Oklahoma City Thunder Cyber",
                      away_team="San Antonio Spurs Cyber", source_url=CYBER_URL)
    g2 = PokerBetGame(source_game_id="30727642", classification="CYBER_2K26",
                      competition="Cyber Basketball 2K26", game_family="cyber",
                      home_team="Oklahoma City Thunder Cyber",
                      away_team="San Antonio Spurs Cyber", source_url=CYBER_URL)
    id1 = store.upsert_game(g1)
    id2 = store.upsert_game(g2)
    assert id1 == id2  # same (source, source_game_id) → one row
    assert len(store.list_games()) == 1


# ── Snapshots: append-only, never overwritten ─────────────────────

def test_snapshots_persisted_append_only(store):
    g = PokerBetGame(source_game_id="30738600", classification="BETUAL_NBA",
                     competition="Betual NBA", game_family="betual",
                     home_team="Sacramento Kings Virtual",
                     away_team="Miami Heat Virtual", source_url=BETUAL_URL)
    gid = store.upsert_game(g)

    s1 = MarketObservation(source_game_id="30738600", classification="BETUAL_NBA",
                           home_score=78, away_score=76, period_label="3rd Quarter",
                           clock="04:15", total_line=225.5)
    s2 = MarketObservation(source_game_id="30738600", classification="BETUAL_NBA",
                           home_score=80, away_score=78, period_label="3rd Quarter",
                           clock="02:30", total_line=226.5)
    assert store.insert_snapshot(gid, s1)
    assert store.insert_snapshot(gid, s2)

    snaps = store.get_snapshots("30738600", ascending=True)
    assert len(snaps) == 2
    assert snaps[0]["home_score"] == 78  # historical observation preserved
    assert snaps[1]["home_score"] == 80
    assert snaps[0]["captured_at"] != snaps[1]["captured_at"]


def test_exact_duplicate_snapshot_suppressed(store):
    g = PokerBetGame(source_game_id="30738600", classification="BETUAL_NBA",
                     competition="Betual NBA", game_family="betual",
                     home_team="Sacramento Kings Virtual",
                     away_team="Miami Heat Virtual", source_url=BETUAL_URL)
    gid = store.upsert_game(g)
    obs = MarketObservation(source_game_id="30738600", classification="BETUAL_NBA",
                            home_score=78, away_score=76)
    assert store.insert_snapshot(gid, obs) is not None
    dup = MarketObservation(source_game_id="30738600", classification="BETUAL_NBA",
                            home_score=78, away_score=76)
    dup.captured_at = obs.captured_at
    assert store.insert_snapshot(gid, dup) is None  # suppressed


# ── Reconciliation ─────────────────────────────────────────────────

def test_reconcile_matched():
    rec = reconcile_event(BETUAL_URL, BETUAL_EVENT_TEXT, {
        "source_game_id": "30738600", "classification": "BETUAL_NBA",
        "competition": "Betual NBA",
        "home_team": "Sacramento Kings Virtual", "away_team": "Miami Heat Virtual",
    })
    assert rec["result"] == "matched"
    assert rec["bc_event_id"] == "30738600"
    assert rec["bc_competition_id"] == "18296756"


def test_reconcile_mismatch_detected():
    rec = reconcile_event(BETUAL_URL, BETUAL_EVENT_TEXT, {
        "source_game_id": "99999999", "classification": "CYBER_2K26",
        "competition": "Cyber Basketball 2K26",
        "home_team": "Oklahoma City Thunder Cyber", "away_team": "San Antonio Spurs Cyber",
    })
    assert rec["result"] == "mismatch"
    assert rec["failures"]  # investigated, not silently accepted


# ── Absolute separation: CYBER ≠ BETUAL ───────────────────────────

def test_statistics_never_mix(store):
    """CYBER_2K26 statistics ≠ BETUAL_NBA statistics."""
    for cls, gid, home, away, url, scores in [
        (Classification.CYBER_2K26, "30727642", "Oklahoma City Thunder Cyber",
         "San Antonio Spurs Cyber", CYBER_URL, [(100, 73), (102, 75)]),
        (Classification.BETUAL_NBA, "30738600", "Sacramento Kings Virtual",
         "Miami Heat Virtual", BETUAL_URL, [(78, 76), (80, 78)]),
    ]:
        g = PokerBetGame(source_game_id=gid, classification=cls.value,
                         competition=canonical_competition_name(cls),
                         game_family=cls.game_family.value,
                         home_team=home, away_team=away, source_url=url)
        dbid = store.upsert_game(g)
        for hs, as_ in scores:
            store.insert_snapshot(dbid, MarketObservation(
                source_game_id=gid, classification=cls.value,
                home_score=hs, away_score=as_,
            ))

    cyber_totals = sorted(
        s["home_score"] + s["away_score"] for s in store.get_snapshots("30727642")
    )
    betual_totals = sorted(
        s["home_score"] + s["away_score"] for s in store.get_snapshots("30738600")
    )
    assert cyber_totals == [173, 177]
    assert betual_totals == [154, 158]
    assert cyber_totals != betual_totals
    assert sum(cyber_totals) / len(cyber_totals) != sum(betual_totals) / len(betual_totals)

    # classification-scoped counts stay separate
    assert store.count_snapshots("CYBER_2K26") == 2
    assert store.count_snapshots("BETUAL_NBA") == 2
    stats = store.stats()
    assert stats["games"]["CYBER_2K26"] == 1
    assert stats["games"]["BETUAL_NBA"] == 1
    assert stats["snapshots"]["CYBER_2K26"] == 2
    assert stats["snapshots"]["BETUAL_NBA"] == 2


def test_source_metadata_complete(store):
    """Every production record retains full source provenance."""
    g = PokerBetGame(source_game_id="30727642", classification="CYBER_2K26",
                     competition="Cyber Basketball 2K26", game_family="cyber",
                     home_team="Oklahoma City Thunder Cyber",
                     away_team="San Antonio Spurs Cyber", source_url=CYBER_URL)
    store.upsert_game(g)
    store.insert_snapshot(1, MarketObservation(
        source_game_id="30727642", classification="CYBER_2K26",
        home_score=100, away_score=73, source_url=CYBER_URL,
    ))
    game = store.get_game("30727642")
    assert game["source"] == "PokerBet"
    assert game["source_game_id"] == "30727642"
    assert game["classification"] == "CYBER_2K26"
    assert game["competition"] == "Cyber Basketball 2K26"
    assert game["source_url"] == CYBER_URL
    snap = store.get_snapshots("30727642")[0]
    assert snap["captured_at"]
    assert snap["classification"] == "CYBER_2K26"
    assert snap["source"] == "PokerBet"
