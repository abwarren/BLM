"""M009-M8 — collector market-capture behavior audit.

Audits the collector's market-capture contract at the DB / unit level.
No real browser is driven — Playwright ``Page`` objects are faked where
the capture path touches navigation (row click + hydration wait), and
``parse_event_view`` / ``parse_event_url`` are monkeypatched where the
test is about the collector's control flow, not the parser (the parser
itself is exercised for real in ``test_event_view_capture_sync_invariant``).

Covers:

  1. EVENT-VIEW SYNC INVARIANT — a snapshot stored via the event-view
     capture path (``_capture_event_state``) carries ``total_line`` AND
     ``markets_json['total']['first_line']`` and they AGREE.  This is
     the prod audit '0 rows with first_line in JSON but NULL column'
     as a test — exercised through the REAL parser, not a mock.
  2. LIST-STUB vs EVENT-VIEW — a list-stub snapshot (``_store_list_snapshot``,
     raw_json ~350B) never carries a market (``total_line`` NULL,
     ``markets_json`` '{}'); an event-view snapshot (raw_json ~5.9KB)
     does.  Pins the LENGTH(raw_json) heuristic (<500B = list stub,
     >5000B = event view) documented in the M006 milestone.
  3. MARKET_REFRESH_S (=480) per-game refresh window — a game captured
     within the window is skipped by the round-robin queue scan;
     outside it, captured.  Boundary pinned exactly: age < 480 skip,
     age == 480 due (``< MARKET_REFRESH_S`` skip condition).
  4. COVERAGE-GAP RED (30741757-class, M007-M8 milestone) — a LIVE game
     with RECENT snapshots but NO recent ``market_observations`` rows
     is flagged by the coverage-audit query; a healthy game (recent
     snapshots + recent market obs) is NOT.  The query is a TEST
     HELPER (SQL string) here — no production audit wiring exists yet
     (docs/milestones/CURRENT.md: "NEXT: M007-M8 — collector
     market-refresh coverage audit"); production-side wiring is out of
     scope for this file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import blm_v4.collector as collector_mod
from blm_v4.classifications import Classification
from blm_v4.collector import (
    MARKET_BATCH,
    MARKET_REFRESH_S,
    PokerBetCollector,
)
from blm_v4.discovery import RowGame
from blm_v4.event_parser import parse_event_view
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _query(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _add_game(st: PokerBetStore, gid: str, home: str = "Home Virtual",
              away: str = "Away Virtual", status: str = "live") -> int:
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="18296756", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team=home, away_team=away,
        game_slug=f"{gid.lower()}-game", source_url=f"https://x/{gid}",
        status=status,
    )
    return st.upsert_game(game)


_GAME_FIELDS = set(PokerBetGame.model_fields)


def _game_from_row(row: dict | None) -> PokerBetGame:
    """DB row -> model, dropping storage-only columns (id, ...)."""
    assert row is not None, "game must exist in the store"
    return PokerBetGame(**{k: v for k, v in row.items() if k in _GAME_FIELDS})


def _tracked_collector(st: PokerBetStore, gid: str = "30741757",
                       home: str = "Home Virtual", away: str = "Away Virtual",
                       status: str = "live"):
    """Collector with one tracked game in _tracked + _market_queue."""
    gid_db = _add_game(st, gid, home=home, away=away, status=status)
    game = _game_from_row(st.get_game(gid))
    c = PokerBetCollector(store=st)
    c._tracked["BETUAL_NBA"][f"{home}|{away}"] = game
    c._market_queue.append(gid)
    return c, game, gid_db


def _event_view_text() -> str:
    """Realistic BetConstruct event-view page (Cyber Basketball 2K26,
    layout from blm_v4/event_parser.py docstring), padded to the real
    capture size (raw_json > 5000B; ~6.4KB observed on real games)."""
    lines = [
        "Cyber Basketball. 2K26 Matches",
        "4th Quarter",
        "09:46'",
        "Oklahoma City Thunder Cyber",
        "San Antonio Spurs Cyber",
        "1 32 22  2 28 23  3 33 22  4 7 6  Quarter 100 73",
        "100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46",
        "All Match Totals Handicaps Markets",
        "Points Handicap",
        "Oklahoma City Thunder Cyber",
        "San Antonio Spurs Cyber",
        "-26.5 1.95  +26.5 1.75  -25.5 1.75  +25.5 1.95",
    ]
    for i in range(24):  # full handicap ladder (real pages list many lines)
        lines.append(
            f"-{25 - i}.5 1.95  +{25 - i}.5 1.75  -{24 - i}.5 1.75  +{24 - i}.5 1.95")
    lines += ["Total Points", "Over Under"]
    for i in range(80):  # full O/U ladder — this is what pushes raw > 5000B
        lines.append(
            f"{216.5 - i:.1f} 1.70 2.02   {217.5 - i:.1f} 1.80 1.90   "
            f"{218.5 - i:.1f} 1.90 1.80")
    lines += [
        "Oklahoma City Thunder Cyber Total Points",
        "Over Under",
        "121.5 1.75 1.95",
        "San Antonio Spurs Cyber Total Points",
        "Over Under",
        "95.5 1.75 1.95",
    ]
    return "\n".join(lines)


EVENT_VIEW_TEXT = _event_view_text()
assert len(json.dumps(EVENT_VIEW_TEXT)) > 5000, "event text must exceed 5000B raw"
# sanity: the REAL parser must extract the market from the fixture text
_PARSED_FIXTURE = parse_event_view(EVENT_VIEW_TEXT)
assert _PARSED_FIXTURE["total"].get("first_line") == 216.5
assert _PARSED_FIXTURE["home_team"] == "Oklahoma City Thunder Cyber"
assert _PARSED_FIXTURE["home_score"] == 100 and _PARSED_FIXTURE["away_score"] == 73


def _parsed_dict(home: str = "Home Virtual", away: str = "Away Virtual",
                 hs: int = 20, as_: int = 18, first_line: float = 189.5) -> dict:
    """A parse_event_view-shaped dict for control-flow tests."""
    total = {"first_line": first_line, "over_odds": 1.85, "under_odds": 1.95,
             "ladder": [{"line": first_line, "over": 1.85, "under": 1.95}]}
    return {
        "home_team": home, "away_team": away,
        "home_score": hs, "away_score": as_,
        "period_label": "3rd Quarter", "quarter": 3, "clock": "06:00",
        "quarter_scores": [], "simulated_note": True,
        "total": total, "handicap": {}, "team_totals": {}, "match_winner": {},
        "raw_json": json.dumps("fake event-view page text"),
        "markets_json": json.dumps(
            {"total": total, "handicap": {}, "team_totals": {},
             "match_winner": {}, "quarter_scores": [], "simulated_note": True},
            default=str),
    }


class _FakePage:
    """Minimal Playwright Page stand-in: navigation + DOM reads are
    no-ops/recorders — enough for _capture_slow_market's control flow."""

    def __init__(self, url: str = "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/Virtual%20Matches/18296756/betual-nba/30741757"):
        self.url = url
        self.goto_calls: list[str] = []
        self.timeout_calls: list[int] = []

    def goto(self, url: str, **kw) -> None:
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        self.timeout_calls.append(ms)

    def wait_for_selector(self, *a, **k) -> bool:
        return True

    def inner_text(self, *a, **k) -> str:
        return "fake event-view body text (parse is monkeypatched)"


def _freshness_harness(st: PokerBetStore, monkeypatch, gid: str = "30741757"):
    """Collector + fake slow page + parse monkeypatches for
    _capture_slow_market (the STEP-2 decoupled event-view path)."""
    c, game, _ = _tracked_collector(st, gid=gid)
    page = _FakePage()
    c._slow_page = page
    clicks: list[str] = []
    monkeypatch.setattr(c, "_click_tracked_row",
                        lambda page, g: (clicks.append(g.source_game_id) or True))
    monkeypatch.setattr(collector_mod, "parse_event_view",
                        lambda text: _parsed_dict())
    monkeypatch.setattr(collector_mod, "parse_event_url", lambda url: None)
    c._reconcile = lambda *a, **k: None  # no reconciliation writes in unit tests
    return c, game, page, clicks


# ═══════════════════════════════════════════════════════════════════
# 1. EVENT-VIEW CAPTURE SYNC INVARIANT
# ═══════════════════════════════════════════════════════════════════

def test_event_view_capture_stores_total_line_and_markets_json_in_sync(tmp_path):
    """The event-view capture path stores total_line AND
    markets_json['total']['first_line'] from the SAME parse and they
    AGREE — the prod audit '0 rows with first_line in JSON but NULL
    column' as a test.  Runs the REAL parser on a realistic event-view
    page (no mocks on the parse side)."""
    st = PokerBetStore(tmp_path / "blm.db")
    gid_db = _add_game(st, "30740001", home="Oklahoma City Thunder Cyber",
                       away="San Antonio Spurs Cyber")
    game = PokerBetGame(**st.get_game("30740001"))
    c = PokerBetCollector(store=st)

    ok = c._capture_event_state(None, Classification.BETUAL_NBA, game,
                                EVENT_VIEW_TEXT)
    assert ok is True
    assert c.stats["snapshots"] == 1

    row = _query(tmp_path / "blm.db",
                 "SELECT * FROM snapshots WHERE game_id=?",
                 (gid_db,))[0]
    assert row["total_line"] == 216.5                       # first O/U ladder line
    mj = json.loads(row["markets_json"])
    assert mj["total"]["first_line"] == 216.5
    # the sync invariant the prod audit enforces
    assert row["total_line"] == mj["total"]["first_line"]
    # event-view-class row: raw payload large enough to carry markets
    assert len(row["raw_json"]) > 5000
    # event-view extras present
    assert row["total_over_odds"] == 1.70 and row["total_under_odds"] == 2.02
    assert row["home_total_line"] == 121.5 and row["away_total_line"] == 95.5
    assert row["quarter"] == 4 and row["period_label"] == "4th Quarter"


def test_event_view_sync_audit_zero_violations(tmp_path):
    """The audit query form: 0 rows with first_line in JSON but NULL
    column, and 0 rows with a column line but no JSON first_line."""
    st = PokerBetStore(tmp_path / "blm.db")
    gid_db = _add_game(st, "30740002", home="Oklahoma City Thunder Cyber",
                       away="San Antonio Spurs Cyber")
    game = PokerBetGame(**st.get_game("30740002"))
    c = PokerBetCollector(store=st)
    # two captures with different lines + one list stub interleaved
    c._capture_event_state(None, Classification.BETUAL_NBA, game, EVENT_VIEW_TEXT)
    time.sleep(0.01)  # distinct captured_at (UNIQUE game_id+captured_at)
    stub = RowGame(home_team="Oklahoma City Thunder Cyber",
                   away_team="San Antonio Spurs Cyber",
                   home_score=102, away_score=75,
                   period_label="4th Quarter", clock="09:30",
                   w1_odds=1.85, w2_odds=1.95, spread_indicator="-26.5")
    c._store_list_snapshot(game, stub, Classification.BETUAL_NBA)
    time.sleep(0.01)
    c._capture_event_state(None, Classification.BETUAL_NBA, game, EVENT_VIEW_TEXT)

    db = tmp_path / "blm.db"
    # JSON carries a first_line but the column is NULL  → must be 0
    assert _query(db, """
        SELECT COUNT(*) AS c FROM snapshots
        WHERE json_extract(markets_json, '$.total.first_line') IS NOT NULL
          AND total_line IS NULL""")[0]["c"] == 0
    # column carries a line but the JSON does not  → must be 0
    assert _query(db, """
        SELECT COUNT(*) AS c FROM snapshots
        WHERE total_line IS NOT NULL
          AND json_extract(markets_json, '$.total.first_line') IS NULL""")[0]["c"] == 0
    # both real captures landed with agreeing values
    rows = _query(db, "SELECT total_line, markets_json FROM snapshots "
                      "WHERE total_line IS NOT NULL")
    assert len(rows) == 2
    for r in rows:
        assert r["total_line"] == json.loads(r["markets_json"])["total"]["first_line"]


# ═══════════════════════════════════════════════════════════════════
# 2. LIST-STUB vs EVENT-VIEW (LENGTH(raw_json) heuristic)
# ═══════════════════════════════════════════════════════════════════

def test_list_stub_snapshot_never_carries_market(tmp_path):
    """_store_list_snapshot (the list-level path) writes total_line NULL
    and markets_json '{}' — a stub never carries a market."""
    st = PokerBetStore(tmp_path / "blm.db")
    gid_db = _add_game(st, "30740003")
    game = PokerBetGame(**st.get_game("30740003"))
    c = PokerBetCollector(store=st)
    row = RowGame(home_team="Home Virtual", away_team="Away Virtual",
                  home_score=20, away_score=18,
                  period_label="3rd Quarter", clock="06:00",
                  w1_odds=1.85, w2_odds=1.95, spread_indicator="+4")
    c._store_list_snapshot(game, row, Classification.BETUAL_NBA)
    assert c.stats["snapshots"] == 1

    s = _query(tmp_path / "blm.db", "SELECT * FROM snapshots WHERE game_id=?",
               (gid_db,))[0]
    assert s["total_line"] is None
    assert s["total_over_odds"] is None and s["total_under_odds"] is None
    assert s["markets_json"] == "{}"
    # list-stub raw payload: small, no market structure (M006 pitfall pin)
    assert len(s["raw_json"]) < 500
    # list-level fields still recorded
    assert s["home_score"] == 20 and s["away_score"] == 18
    assert s["period_label"] == "3rd Quarter" and s["clock"] == "06:00"


def test_length_raw_json_heuristic_pins_snapshot_kinds(tmp_path):
    """Interleaved stub + event-view rows: LENGTH(raw_json) < 500 rows
    NEVER carry total_line; LENGTH(raw_json) > 5000 rows ALWAYS do."""
    st = PokerBetStore(tmp_path / "blm.db")
    gid_db = _add_game(st, "30740004", home="Oklahoma City Thunder Cyber",
                       away="San Antonio Spurs Cyber")
    game = PokerBetGame(**st.get_game("30740004"))
    c = PokerBetCollector(store=st)
    stub = RowGame(home_team="Oklahoma City Thunder Cyber",
                   away_team="San Antonio Spurs Cyber",
                   home_score=100, away_score=73,
                   period_label="4th Quarter", clock="09:46",
                   w1_odds=1.85, w2_odds=1.95, spread_indicator="-26.5")
    c._store_list_snapshot(game, stub, Classification.BETUAL_NBA)
    time.sleep(0.01)
    c._capture_event_state(None, Classification.BETUAL_NBA, game, EVENT_VIEW_TEXT)
    time.sleep(0.01)
    c._store_list_snapshot(game, stub, Classification.BETUAL_NBA)

    db = tmp_path / "blm.db"
    rows = _query(db, "SELECT total_line, markets_json, "
                      "LENGTH(raw_json) AS raw_len FROM snapshots")
    assert len(rows) == 3
    stubs = [r for r in rows if r["raw_len"] < 500]
    evs = [r for r in rows if r["raw_len"] > 5000]
    assert len(stubs) == 2 and len(evs) == 1
    # the M006 pitfall pinned: small raw = list stub = NO market
    assert _query(db, """
        SELECT COUNT(*) AS c FROM snapshots
        WHERE LENGTH(raw_json) < 500 AND total_line IS NOT NULL""")[0]["c"] == 0
    # large raw = event view = market present
    assert _query(db, """
        SELECT COUNT(*) AS c FROM snapshots
        WHERE LENGTH(raw_json) > 5000 AND total_line IS NULL""")[0]["c"] == 0
    assert all(r["total_line"] is None and r["markets_json"] == "{}"
               for r in stubs)
    assert evs[0]["total_line"] == 216.5
    assert json.loads(evs[0]["markets_json"])["total"]["first_line"] == 216.5


# ═══════════════════════════════════════════════════════════════════
# 3. MARKET_REFRESH_S per-game refresh window
# ═══════════════════════════════════════════════════════════════════

def test_market_refresh_constant_is_480s():
    """Pin the documented per-game market refresh window."""
    assert MARKET_REFRESH_S == 480
    assert MARKET_BATCH == 1  # one event view per tick


def test_refresh_window_skips_game_captured_recently(tmp_path, monkeypatch):
    """A game whose last event-view capture is inside the window is
    skipped by the queue scan — no click, no snapshot, timestamp kept."""
    st = PokerBetStore(tmp_path / "blm.db")
    c, game, page, clicks = _freshness_harness(st, monkeypatch)
    c._last_market_at[game.source_game_id] = _iso(_now())  # captured just now

    c._capture_slow_market()

    assert clicks == []                       # never even clicked the row
    assert st.get_snapshots(game.source_game_id) == []
    assert c.stats["snapshots"] == 0
    assert c._last_market_at[game.source_game_id] is not None  # unchanged arm


def test_refresh_window_captures_game_due_for_refresh(tmp_path, monkeypatch):
    """A game captured OUTSIDE the window (10 min ago > 480s) is
    captured: row clicked, snapshot stored with market, window re-armed."""
    st = PokerBetStore(tmp_path / "blm.db")
    c, game, page, clicks = _freshness_harness(st, monkeypatch)
    c._last_market_at[game.source_game_id] = _iso(_now() - timedelta(seconds=600))
    before = c._last_market_at[game.source_game_id]

    c._capture_slow_market()

    assert clicks == [game.source_game_id]
    snaps = st.get_snapshots(game.source_game_id)
    assert len(snaps) == 1
    assert snaps[0]["total_line"] == 189.5                      # market captured
    assert json.loads(snaps[0]["markets_json"])["total"]["first_line"] == 189.5
    assert c.stats["snapshots"] == 1
    # window re-armed to the capture moment (no longer the stale ts)
    assert c._last_market_at[game.source_game_id] > before


def test_refresh_window_boundary_at_480s(tmp_path, monkeypatch):
    """Exact boundary of the skip condition (< MARKET_REFRESH_S):
    age 479.9s → skipped (fresh); age 480.0s → due (captured)."""
    # 479.9s — inside the window → skip
    st1 = PokerBetStore(tmp_path / "a.db")
    c1, game1, page1, clicks1 = _freshness_harness(st1, monkeypatch)
    c1._last_market_at[game1.source_game_id] = "2026-08-31T00:00:00.000Z"
    monkeypatch.setattr(collector_mod, "_ts_age_s", lambda ts: 479.9)
    c1._capture_slow_market()
    assert clicks1 == [] and st1.get_snapshots(game1.source_game_id) == []

    # 480.0s — exactly at the window edge → due for refresh
    st2 = PokerBetStore(tmp_path / "b.db")
    c2, game2, page2, clicks2 = _freshness_harness(st2, monkeypatch)
    c2._last_market_at[game2.source_game_id] = "2026-08-31T00:00:00.000Z"
    monkeypatch.setattr(collector_mod, "_ts_age_s", lambda ts: 480.0)
    c2._capture_slow_market()
    assert clicks2 == [game2.source_game_id]
    assert len(st2.get_snapshots(game2.source_game_id)) == 1


# ═══════════════════════════════════════════════════════════════════
# 4. COVERAGE-GAP RED (30741757-class) — the M007-M8 audit query
# ═══════════════════════════════════════════════════════════════════

# windows for the audit (per the milestone: recent snapshots ≈ last
# 2 min; market obs gap ≈ last N minutes)
SNAPSHOT_RECENT_MIN = 2
MARKET_OBS_RECENT_MIN = 10

# The coverage-audit query the milestone earmarked.  No production
# implementation exists yet (docs/milestones/CURRENT.md M007-M8) — it
# lives here as a TEST HELPER and is exercised against seeded data.
COVERAGE_GAP_QUERY = """
    SELECT g.source_game_id
    FROM games g
    WHERE g.status = 'live'
      AND EXISTS (
          SELECT 1 FROM snapshots s
          WHERE s.game_id = g.id
            AND s.captured_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
      )
      AND NOT EXISTS (
          SELECT 1 FROM market_observations m
          WHERE m.game_id = g.id
            AND m.captured_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
      )
    ORDER BY g.source_game_id
"""


def _seed_audit_db(db: Path) -> None:
    """Seed the five coverage shapes (30741757 = the documented gap game)."""
    st = PokerBetStore(db)
    now = _now()

    def snap(gid_db: int, gid: str, t: datetime) -> None:
        st.insert_snapshot(gid_db, MarketObservation(
            source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
            captured_at=_iso(t), home_team="H", away_team="A",
            home_score=50, away_score=45, period_label="3rd Quarter",
            quarter=3, clock="06:00", game_status="live",
            total_line=None, markets_json="{}",
            raw_json=json.dumps({"stub": True})), force=True)

    def mkt(gid_db: int, gid: str, t: datetime) -> None:
        st.upsert_market_observation({
            "game_id": gid_db, "source_game_id": gid,
            "captured_at": _iso(t), "market_type": "MatchTotal",
            "market_name": "Total Points", "line_value": 204.5,
            "over_price": 1.94, "under_price": 1.87,
            "home_score": 50, "away_score": 45,
            "period_label": "3rd Quarter", "clock": "06:00", "raw_json": "{}",
        })

    # 30741757 — the DOCUMENTED gap: market obs STOPPED (last 15 min ago)
    # while snapshots kept flowing (docs/milestones/CURRENT.md M007-M7:
    # "NO WS market observations after 00:47:12Z while snapshots
    # continued to 02:48Z").  → MUST be flagged.
    gid = _add_game(st, "30741757", status="live")
    snap(gid, "30741757", now - timedelta(seconds=90))
    snap(gid, "30741757", now - timedelta(seconds=30))
    mkt(gid, "30741757", now - timedelta(minutes=15))

    # never any market obs at all, snapshots recent → MUST be flagged.
    gid = _add_game(st, "30741757-NO-MKT", status="live")
    snap(gid, "30741757-NO-MKT", now - timedelta(seconds=30))

    # healthy: recent snapshots AND recent market obs → NOT flagged.
    gid = _add_game(st, "30741757-HEALTHY", status="live")
    snap(gid, "30741757-HEALTHY", now - timedelta(seconds=30))
    mkt(gid, "30741757-HEALTHY", now - timedelta(seconds=75))

    # recent market obs but STALE snapshots → NOT flagged (the query
    # requires live snapshots — a game the collector stopped seeing).
    gid = _add_game(st, "30741757-STALE-SNAP", status="live")
    snap(gid, "30741757-STALE-SNAP", now - timedelta(minutes=30))
    mkt(gid, "30741757-STALE-SNAP", now - timedelta(seconds=60))

    # ended game with recent snapshots and no market obs → NOT flagged
    # (only LIVE games are in the coverage window).
    gid = _add_game(st, "30741757-ENDED", status="ended")
    snap(gid, "30741757-ENDED", now - timedelta(seconds=30))


def test_coverage_gap_audit_flags_live_games_without_recent_market_obs(tmp_path):
    """RED reproduction of the 30741757-class gap: the audit query must
    flag a live game with recent snapshots but no recent market
    observations, and must NOT flag healthy / stale-snapshot / ended
    games."""
    db = tmp_path / "blm.db"
    _seed_audit_db(db)

    flagged = [r["source_game_id"] for r in _query(
        db, COVERAGE_GAP_QUERY,
        (f"-{SNAPSHOT_RECENT_MIN} minutes", f"-{MARKET_OBS_RECENT_MIN} minutes"))]

    # the 30741757 shape IS flagged (stale market obs + fresh snapshots),
    # and so is the never-observed game
    assert "30741757" in flagged
    assert "30741757-NO-MKT" in flagged
    # healthy / stale-snap / ended are NOT flagged
    assert "30741757-HEALTHY" not in flagged
    assert "30741757-STALE-SNAP" not in flagged
    assert "30741757-ENDED" not in flagged
    assert set(flagged) == {"30741757", "30741757-NO-MKT"}


def test_coverage_gap_audit_window_is_parameterized(tmp_path):
    """The audit's market-obs window is a parameter: widen it and the
    stale-obs gap game drops out; narrow it and the healthy game is
    caught.  (Pins that the query is not hard-coding one N.)"""
    db = tmp_path / "blm.db"
    _seed_audit_db(db)
    # obs stopped 15 min ago → with a 20-min window the gap game is covered
    flagged = [r["source_game_id"] for r in _query(
        db, COVERAGE_GAP_QUERY,
        (f"-{SNAPSHOT_RECENT_MIN} minutes", "-20 minutes"))]
    assert "30741757" not in flagged
    # with a 1-min window even the healthy game's 75s-old obs is stale
    flagged = [r["source_game_id"] for r in _query(
        db, COVERAGE_GAP_QUERY,
        (f"-{SNAPSHOT_RECENT_MIN} minutes", "-1 minutes"))]
    assert "30741757-HEALTHY" in flagged
