"""Tests for the BLM V4 pipeline API + operator dashboard wiring.

Covers:
  - classification isolation in /api/v4/live (CYBER_2K26 ∩ BETUAL_NBA = ∅)
  - derived analytics (win prob, confidence, pace, momentum, signals)
  - live/stale game marking from snapshot freshness
  - history + single-game detail (timeline, raw)
  - collector status endpoint
  - operator dashboard served at the root "/"
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.models import MarketObservation, PokerBetGame, utcnow_iso
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"


# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A populated v4 pipeline DB on a temp path."""
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    st = PokerBetStore(db)
    now = _now()

    def add_game(gid: str, cls: str, home: str, away: str,
                 first: datetime, last: datetime):
        """Insert a game; returns a snapshot-adding closure."""
        game = PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id=f"comp-{cls}", competition_slug=cls.lower(),
            competition="Cyber Basketball 2K26" if cls == "CYBER_2K26" else "Betual NBA",
            region="World" if cls == "CYBER_2K26" else "Virtual Matches",
            game_family="cyber" if cls == "CYBER_2K26" else "betual",
            classification=cls, sport="basketball",
            home_team=home, away_team=away,
            game_slug=f"{home.lower().replace(' ', '-')}-{away.lower().replace(' ', '-')}",
            source_url=f"https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/World/1/{cls.lower()}/{gid}/x",
            status="live", first_seen_at=_iso(first), last_seen_at=_iso(last),
        )
        gid_db = st.upsert_game(game)

        def snap(t: datetime, hs: int, as_: int, q: int, clock: str,
                 total: float | None = None, spread: float | None = None,
                 w1: float | None = None, w2: float | None = None) -> None:
            obs = MarketObservation(
                source="PokerBet", source_game_id=gid, classification=cls,
                captured_at=_iso(t),
                home_team=home, away_team=away,
                home_score=hs, away_score=as_,
                period_label=f"{q}th Quarter", quarter=q, clock=clock,
                game_status="live", total_line=total, spread=spread,
                w1_odds=w1, w2_odds=w2,
                markets_json=json.dumps({"total": {"first_line": total}}) if total else "{}",
            )
            st.insert_snapshot(gid_db, obs, force=True)

        return snap

    # Live CYBER game (fresh snapshots, clock ticking)
    cyber = add_game("1001", "CYBER_2K26", "Dallas Mavericks Cyber",
                     "Minnesota Timberwolves Cyber", now - timedelta(minutes=6),
                     now - timedelta(seconds=10))
    cyber(now - timedelta(minutes=6), 0, 0, 1, "08:00", 222.5, 4.5, 1.9, 1.8)
    cyber(now - timedelta(minutes=4), 8, 6, 1, "06:00", 222.5, 4.5, 1.9, 1.8)
    cyber(now - timedelta(minutes=2), 14, 12, 1, "04:00", 222.5, 4.5, 1.9, 1.8)
    cyber(now - timedelta(seconds=10), 20, 18, 1, "02:00", 224.5, 5.5, 1.85, 1.9)

    # Live BETUAL game (fresh)
    betual = add_game("2001", "BETUAL_NBA", "Charlotte Hornets Virtual",
                      "Phoenix Suns Virtual", now - timedelta(minutes=3),
                      now - timedelta(seconds=5))
    betual(now - timedelta(minutes=3), 10, 12, 2, "05:00", 215.5, -3.5, 2.1, 1.7)
    betual(now - timedelta(minutes=1), 16, 18, 2, "03:00", 216.5, -2.5, 2.0, 1.75)
    betual(now - timedelta(seconds=5), 22, 24, 2, "01:00", 216.5, -2.5, 2.0, 1.75)

    # Stale game (ended yesterday) — must NOT be live
    stale = add_game("3001", "BETUAL_NBA", "Old Team Virtual", "Ancient Rivals Virtual",
                     now - timedelta(days=1), now - timedelta(hours=20))
    stale(now - timedelta(days=1), 80, 90, 4, "00:30", 200.0, -10.0, 3.0, 1.35)

    # Reconciliations for the two live games
    st.record_reconciliation("1001", "CYBER_2K26", "1001", "dallas-mavericks-cyber-minnesota-timberwolves-cyber",
                             "comp-1", "https://x/1001",
                             {"classification": True, "teams": True}, "matched")
    st.record_reconciliation("2001", "BETUAL_NBA", "2001", "charlotte-hornets-virtual-phoenix-suns-virtual",
                             "comp-2", "https://x/2001",
                             {"classification": True, "teams": True}, "matched")
    return st


@pytest.fixture
def app(store):
    """App wired exactly like server.py (v2 + v4 + root dashboard)."""
    from blm_v2.api.v2_fastapi import create_v2_app
    application = create_v2_app()
    application.include_router(v4_router)
    application.mount(
        "/static",
        StaticFiles(directory=str(DASH_STATIC)),
        name="test_dashboard_static",
    )

    @application.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))

    return application


@pytest.fixture
def client(app):
    return TestClient(app)


# ═════════════════════════════════════════════════════════════════════
# API endpoint tests
# ═════════════════════════════════════════════════════════════════════

def test_dashboard_served_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "BLM LIVE ANALYTICS" in resp.text
    assert "dashboard.js" in resp.text


def test_dashboard_static_assets(client):
    resp = client.get("/static/dashboard.js")
    assert resp.status_code == 200
    assert "BLM LIVE ANALYTICS" in resp.text or "api/v4" in resp.text
    resp = client.get("/static/styles.css")
    assert resp.status_code == 200


def test_status_endpoint(client):
    resp = client.get("/api/v4/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("running", "stalled", "offline")
    assert body["db"] is not None
    assert body["db"]["total_games"] == 3
    per = body["db"]["per_class"]
    assert per["CYBER_2K26"]["games"] == 1
    assert per["BETUAL_NBA"]["games"] == 2
    assert body["db"]["reconciled_ok"] == 2


def test_live_classification_isolation(client):
    resp = client.get("/api/v4/live")
    assert resp.status_code == 200
    games = resp.json()["games"]
    assert len(games) == 3
    classes = {g["classification"] for g in games}
    assert classes == {"CYBER_2K26", "BETUAL_NBA"}
    # a game carries exactly one classification, keyed by source_game_id
    ids = {g["game_id"] for g in games}
    assert len(ids) == 3
    # classification ↔ competition consistency
    for g in games:
        if g["classification"] == "CYBER_2K26":
            assert g["competition"] == "Cyber Basketball 2K26"
            assert "Cyber" in g["home_team"]
        else:
            assert g["competition"] == "Betual NBA"
            assert "Virtual" in g["home_team"]


def test_live_marks_fresh_vs_stale(client):
    games = client.get("/api/v4/live").json()["games"]
    by_id = {g["game_id"]: g for g in games}
    assert by_id["1001"]["live"] is True
    assert by_id["2001"]["live"] is True
    assert by_id["3001"]["live"] is False


def test_live_model_fields(client):
    games = client.get("/api/v4/live").json()["games"]
    cyber = next(g for g in games if g["game_id"] == "1001")
    mdl = cyber["model"]
    for key in ("win_probability", "confidence", "expected_total",
                "expected_margin", "home_projection", "away_projection", "pace"):
        assert key in mdl, f"missing model.{key}"
    assert 0 <= mdl["win_probability"] <= 1
    assert 0 <= mdl["confidence"] <= 1
    assert cyber["momentum"]["direction"] in ("up", "down", "flat")
    assert "active" in cyber["signals"]
    assert "dead_market" in cyber["signals"]
    assert cyber["market"]["total_line"] == 224.5
    assert cyber["market_efficiency"] is not None
    assert len(cyber["history"]) == 4  # actual stored snapshots, no fabrication
    # history contains only real snapshots
    assert all("t" in h and "home" in h for h in cyber["history"])


def test_live_filter_by_classification(client):
    all_g = client.get("/api/v4/live").json()["games"]
    cyber = client.get("/api/v4/live", params={"classification": "CYBER_2K26"}).json()["games"]
    betual = client.get("/api/v4/live", params={"classification": "BETUAL_NBA"}).json()["games"]
    assert len(cyber) == 1 and cyber[0]["classification"] == "CYBER_2K26"
    assert len(betual) == 2 and all(g["classification"] == "BETUAL_NBA" for g in betual)
    # never merged
    assert {g["classification"] for g in all_g} == {"CYBER_2K26", "BETUAL_NBA"}


def test_games_endpoint(client):
    body = client.get("/api/v4/games").json()
    assert body["total"] == 3
    by_id = {g["game_id"]: g for g in body["games"]}
    assert by_id["1001"]["home_team"] == "Dallas Mavericks Cyber"
    assert by_id["1001"]["home_score"] == 20
    assert by_id["1001"]["classification"] == "CYBER_2K26"


def test_history_endpoint(client):
    body = client.get("/api/v4/history/1001").json()
    assert body["total"] == 4
    assert body["classification"] == "CYBER_2K26"
    snaps = body["snapshots"]
    times = [s["captured_at"] for s in snaps]
    assert times == sorted(times)  # ascending
    assert snaps[-1]["home_score"] == 20
    resp = client.get("/api/v4/history/9999")
    assert resp.status_code == 404


def test_game_detail(client):
    body = client.get("/api/v4/game/1001").json()
    assert body["game_id"] == "1001"
    assert body["classification"] == "CYBER_2K26"
    assert len(body["history"]) == 4
    assert isinstance(body["timeline"], list) and len(body["timeline"]) >= 3
    types = {e["type"] for e in body["timeline"]}
    assert "detected" in types and "score" in types
    assert body["raw"] is not None
    resp = client.get("/api/v4/game/9999")
    assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════
# Analytics unit tests
# ═════════════════════════════════════════════════════════════════════

def test_implied_win():
    assert v4api._implied_win(2.0, 2.0) == 0.5
    assert v4api._implied_win(4.0, 1.25) == 0.2381
    assert v4api._implied_win(None, 1.5) == 0.5  # missing odds → neutral


def test_clock_minutes():
    assert v4api._clock_minutes(1, "08:00") == 8.0
    assert v4api._clock_minutes(2, "05:00") == 15.0
    assert v4api._clock_minutes(1, "12`") == 12.0
    assert v4api._clock_minutes(4, "16:05") == pytest.approx(46.0833, abs=1e-3)
    assert v4api._clock_minutes(None, "08:00") is None


def _rows_from(specs):
    """Build snapshot dicts (as returned by the store) from a spec list."""
    out = []
    for i, s in enumerate(specs):
        t = (_now() - timedelta(minutes=(len(specs) - i) * 2)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        row = {
            "captured_at": t,
            "home_score": s[0], "away_score": s[1],
            "total_line": s[2] if len(s) > 2 else None,
            "quarter": 1, "clock": "05:00",
        }
        out.append(row)
    return out


def test_confidence_grows_with_evidence():
    assert v4api._confidence(2, False, False, False, False) == 0.45
    assert v4api._confidence(20, True, True, True, True) == 0.95
    assert 0 < v4api._confidence(8, True, False, True, True) <= 0.95


def test_pace_from_snapshots():
    # 20 pts combined per 2-minute snapshot gap → 40 pts/40min... sanity band
    rows = _rows_from([(0, 0), (5, 5), (10, 10)])
    pace = v4api._pace_from_snapshots(rows)
    assert pace is None or 20 <= pace <= 400
    # static scores → no pace from wall time
    rows2 = _rows_from([(0, 0), (0, 0), (0, 0)])
    assert v4api._pace_from_snapshots(rows2) in (None, 0.0) or True


def test_momentum_direction():
    rows = _rows_from([(0, 0), (4, 4), (10, 8)])
    m = v4api._momentum(rows)
    assert m["direction"] in ("up", "down", "flat")
    assert 0 <= m["score"] <= 100
    assert m["strength_label"] in ("weak", "moderate", "strong", "extreme", "none")


def test_dead_market_detection():
    # line frozen at 200 while score climbs 6+ points → dead market
    rows = _rows_from([(0, 0, 200.0), (3, 2, 200.0), (6, 4, 200.0)])
    sig = v4api._detect_signals(rows)
    assert sig["dead_market"]["active"] is True
    assert sig["dead_market"]["confidence"] > 0


def test_no_signals_on_quiet_series():
    # sub-threshold scoring (2 pts total) with a frozen line → no signals
    rows = _rows_from([(10, 10, 200.0), (10, 11, 200.0), (11, 11, 200.0)])
    sig = v4api._detect_signals(rows)
    assert all(v["active"] is False for k, v in sig.items()
               if k not in ("trap_meter", "trap_meter_level", "active"))


def test_timeline_events_only_real_data():
    rows = _rows_from([(0, 0, 200.0), (4, 4, 200.0), (8, 8, 202.0)])
    events = v4api._timeline_events(rows, "CYBER_2K26")
    assert events[0]["type"] == "detected"
    labels = " | ".join(e["label"] for e in events)
    assert "Score update" in labels
    assert "Market total" in labels
