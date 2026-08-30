"""WS market feed tests: parser, storage, API fallback, prediction freeze.

The eu-swarm WebSocket feed is the independent market path that works
when the PokerBet event-view route is dead — these tests pin the parser
against a REAL captured frame and prove the fallback end-to-end.
"""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from blm_v4 import ws_market
from blm_v4.storage import PokerBetStore
from blm_v4.projection import project

# A REAL eu-swarm frame captured live (2026-08-31, Betual NBA / LNBP
# basketball): game 30734614 carries a MatchTotal market with base + prices.
REAL_FRAME = json.dumps({
    "code": 0,
    "rid": "0",
    "data": {
        "1234567890123456789": {
            "game": {
                "30734614": {
                    "id": 30734614,
                    "markets_count": 42,
                    "is_blocked": 0,
                    "team1_name": "Dorados de Chihuahua",
                    "team2_name": "Mineros de Zacatecas",
                    "info": {
                        "current_game_state": "set3",
                        "current_game_time": "06:48",
                        "score1": "44",
                        "score2": "53",
                        "additional_data": {"quarter": 3},
                    },
                    "stats": {"foul": {"team1_value": 2, "team2_value": 0}},
                    "market": {
                        "2396632236": {
                            "type": "P1P2",
                            "name": "Match Winner",
                            "id": 2396632236,
                            "name_template": "Match Winner",
                            "event": {
                                "7098713413": {"id": 7098713413, "price": 4.42,
                                               "type_1": "W1", "name": "W1", "order": 0},
                                "7098713414": {"id": 7098713414, "price": 1.17,
                                               "type_1": "W2", "name": "W2", "order": 1},
                            },
                        },
                        "2397413971": {
                            "type": "MatchTotal",
                            "name": "Total Points",
                            "id": 2397413971,
                            "base": 204.5,
                            "name_template": "Total Points",
                            "event": {
                                "7100815504": {"id": 7100815504, "price": 1.87,
                                               "type_1": "Under", "name": "Under", "base": 204.5},
                                "7100815505": {"id": 7100815505, "price": 1.94,
                                               "type_1": "Over", "name": "Over", "base": 204.5},
                            },
                        },
                    },
                }
            }
        }
    }
})


def test_ws_parser_extracts_game_and_total_market():
    payloads = ws_market.parse_market_frame(REAL_FRAME)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["game_id"] == "30734614"
    assert p["home_score"] == 44 and p["away_score"] == 53
    assert p["period_label"] == "3rd Quarter"      # from additional_data.quarter
    assert p["clock"] == "06:48"
    types = {m["type"] for m in p["markets"]}
    assert "MatchTotal" in types and "P1P2" in types


def test_ws_normalize_total_points():
    payloads = ws_market.parse_market_frame(REAL_FRAME)
    obs = ws_market.normalize_observations(payloads, "2026-08-31T00:00:00.000Z")
    totals = [o for o in obs if o["market_type"] == "MatchTotal"]
    assert len(totals) == 1
    t = totals[0]
    assert t["line_value"] == 204.5
    assert t["over_price"] == 1.94 and t["under_price"] == 1.87
    assert t["home_score"] == 44 and t["away_score"] == 53
    assert t["period_label"] == "3rd Quarter"


def test_ws_parser_ignores_non_market_frames():
    assert ws_market.parse_market_frame("") == []
    assert ws_market.parse_market_frame("not json") == []
    assert ws_market.parse_market_frame('{"code":0,"data":{"sport":{"1":{}}}}') == []


def test_ws_market_store_roundtrip_and_dedupe(tmp_path):
    store = PokerBetStore(tmp_path / "t.db")
    payloads = ws_market.parse_market_frame(REAL_FRAME)
    obs = ws_market.normalize_observations(payloads, "2026-08-31T00:00:00.000Z")
    for o in obs:
        store.upsert_market_observation(o)
    # same frame again → deduped (UNIQUE constraint)
    for o in ws_market.normalize_observations(payloads, "2026-08-31T00:00:00.000Z"):
        store.upsert_market_observation(o)
    latest = store.latest_market_observation("30734614", "MatchTotal")
    assert latest is not None
    assert latest["line_value"] == 204.5
    assert latest["over_price"] == 1.94


def test_ws_market_freeze_uses_line_before_timestamp(tmp_path):
    """A prediction checkpoint must freeze the WS line observed at-or-before
    its snapshot — a LATER line movement must not rewrite it."""
    store = PokerBetStore(tmp_path / "t.db")
    payloads = ws_market.parse_market_frame(REAL_FRAME)
    obs = ws_market.normalize_observations(payloads, "2026-08-31T00:00:00.000Z")
    for o in obs:
        store.upsert_market_observation(o)
    # later movement: line moves 204.5 -> 210.5
    payloads[0]["markets"][1]["base"] = 210.5
    payloads[0]["markets"][1]["events"] = [
        {"type_1": "Under", "price": 1.8, "base": 210.5, "name": "Under"},
        {"type_1": "Over", "price": 1.9, "base": 210.5, "name": "Over"},
    ]
    for o in ws_market.normalize_observations(payloads, "2026-08-31T00:01:00.000Z"):
        store.upsert_market_observation(o)
    before = store.market_observations_before("30734614", "2026-08-31T00:00:30.000Z")
    assert before["line_value"] == 204.5   # frozen line, not the later 210.5
    after = store.market_observations_before("30734614", "2026-08-31T00:02:00.000Z")
    assert after["line_value"] == 210.5


def test_project_market_override_is_observed_line():
    rows = [
        {"captured_at": "2026-08-31T00:00:00Z", "quarter": None, "clock": None,
         "period_label": None, "home_score": None, "away_score": None,
         "total_line": None},
    ]
    # no snapshot line and no clock → project has no market and no pace
    p_none = project(rows)
    assert p_none["market_total"] is None
    assert p_none["pace"] == 100.0          # documented fallback, no fabrication
    # override pins the observed WS line (204.5) into the model
    p_ws = project(rows, 204.5)
    assert p_ws["market_total"] == 204.5
    assert p_ws["pace"] == 204.5
    assert p_ws["expected_total"] == 204.5


# ════════════════════════════════════════════════════════════════════
# API fallback: game with NO snapshot market line but a WS observation
# ════════════════════════════════════════════════════════════════════

def test_api_market_falls_back_to_ws(tmp_path):
    from blm_v4.api import _analyze_game
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT 'PokerBet',
            source_game_id TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'BETUAL_NBA',
            home_team TEXT, away_team TEXT, status TEXT DEFAULT 'live',
            last_seen_at TEXT, source_url TEXT,
            competition TEXT, region TEXT, sport TEXT);
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL REFERENCES games(id),
            source TEXT NOT NULL DEFAULT 'PokerBet',
            source_game_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            period_label TEXT, clock TEXT, quarter INTEGER,
            home_score INTEGER, away_score INTEGER,
            total_line REAL, total_over_odds REAL, total_under_odds REAL,
            spread REAL, spread_indicator TEXT,
            home_total_line REAL, away_total_line REAL,
            w1_odds REAL, w2_odds REAL,
            source_url TEXT, markets_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER REFERENCES games(id),
            source_game_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            market_type TEXT NOT NULL,
            market_name TEXT NOT NULL,
            line_value REAL, over_price REAL, under_price REAL,
            home_score INTEGER, away_score INTEGER,
            period_label TEXT, clock TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}');
    """)
    conn.execute("""INSERT INTO games (source_game_id, home_team, away_team, status)
        VALUES ('30734614', 'Dorados', 'Mineros', 'live')""")
    gid = conn.execute("SELECT id FROM games").fetchone()[0]
    conn.execute("""INSERT INTO snapshots (game_id, source_game_id, classification,
        captured_at, home_score, away_score, period_label, clock, quarter)
        VALUES (?, '30734614', 'BETUAL_NBA', '2026-08-31T00:00:30Z', 44, 53,
                '3rd Quarter', '06:48', 3)""", (gid,))
    conn.execute("""INSERT INTO market_observations
        (game_id, source_game_id, captured_at, market_type, market_name,
         line_value, over_price, under_price, home_score, away_score)
        VALUES (?, '30734614', '2026-08-31T00:00:10Z', 'MatchTotal', 'Total Points',
                204.5, 1.94, 1.87, 44, 53)""", (gid,))
    conn.commit()
    game = dict(conn.execute("SELECT * FROM games").fetchone())
    rows = [dict(r) for r in conn.execute("SELECT * FROM snapshots").fetchall()]
    d = _analyze_game(game, rows, datetime.now(timezone.utc), conn)
    assert d["market"]["total_line"] == 204.5
    assert d["market"]["market_source"] == "ws"
    assert d["market"]["over_odds"] == 1.94 and d["market"]["under_odds"] == 1.87
    assert d["market"]["total_line_at"] == "2026-08-31T00:00:10Z"
    # model receives the observed WS line — blended with pace, then the
    # live-score floor lifts the total to hp+ap (81.5+97.0 = 178.5)
    assert d["model"]["expected_total"] == 178.5
    conn.close()
