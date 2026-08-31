"""M007-M4 — historical checkpoint market values in the game-detail API.

Each prediction checkpoint must expose the market line FROZEN at capture
time (the latest verified observation at-or-before that checkpoint's
snapshot), never the current/latest line, never the closing line, never a
line reconstructed from later observations.  Missing markets stay NULL.

Covers:
  - game detail returns ordered checkpoint rows with frozen market values
  - snapshot-line markets freeze at-or-before (event-view path)
  - WS-observation markets freeze at-or-before (eu-swarm path)
  - a LATER market observation never rewrites a recorded checkpoint
  - edge = blm prediction - market at checkpoint (NULL when either missing)
  - ended games expose actual_final + error; live games show NULL honestly
  - /api/v4/live stays lean (no per-game checkpoint payload)
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
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Populated pipeline DB: G-A (ended, snapshot-line market),
    G-B (ended, WS-only market), G-C (live, no market)."""
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    st = PokerBetStore(dbfile)
    now = _now()
    base = now - timedelta(hours=2)  # ended games live in the past

    def add_game(gid: str, status: str, first: datetime, last: datetime):
        game = PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="comp-1", competition_slug="betual-tbsl",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=f"{gid} Home Virtual",
            away_team=f"{gid} Away Virtual",
            game_slug=f"{gid.lower()}-game",
            source_url=f"https://x/{gid}", status=status,
            first_seen_at=_iso(first), last_seen_at=_iso(last),
        )
        gid_db = st.upsert_game(game)

        def snap(t: datetime, hs: int, as_: int, q: int, clock: str,
                 total: float | None = None) -> None:
            obs = MarketObservation(
                source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
                captured_at=_iso(t),
                home_team=f"{gid} Home Virtual", away_team=f"{gid} Away Virtual",
                home_score=hs, away_score=as_,
                period_label=f"{q}th Quarter", quarter=q, clock=clock,
                game_status=status, total_line=total, spread=None,
                w1_odds=None, w2_odds=None,
                markets_json=json.dumps({"total": {"first_line": total}}) if total else "{}",
            )
            st.insert_snapshot(gid_db, obs, force=True)

        def market_obs(t: datetime, line: float) -> None:
            st.upsert_market_observation({
                "game_id": gid_db, "source_game_id": gid,
                "captured_at": _iso(t), "market_type": "MatchTotal",
                "market_name": "Match Total", "line_value": line,
                "over_price": 1.9, "under_price": 1.9,
                "home_score": 0, "away_score": 0,
                "period_label": "", "clock": "",
                "raw_json": "{}",
            })

        return snap, market_obs

    # G-A: ENDED, market lines carried on event-view snapshots only.
    snap_a, _ = add_game("G-A", "ended", base, base + timedelta(minutes=26))
    snap_a(base, 0, 0, 1, "08:00", 160.5)
    snap_a(base + timedelta(minutes=2), 10, 8, 1, "04:00", 160.5)
    snap_a(base + timedelta(minutes=6), 20, 18, 2, "08:00")
    snap_a(base + timedelta(minutes=8), 30, 28, 2, "04:00", 164.5)
    snap_a(base + timedelta(minutes=12), 40, 38, 3, "08:00")
    snap_a(base + timedelta(minutes=16), 50, 48, 4, "08:00")
    snap_a(base + timedelta(minutes=20), 52, 50, 4, "01:30")
    snap_a(base + timedelta(minutes=24), 55, 50, 4, "00:00")

    # G-B: ENDED, NO snapshot lines — market only via WS observations.
    snap_b, mkt_b = add_game("G-B", "ended", base, base + timedelta(minutes=26))
    snap_b(base, 0, 0, 1, "08:00")
    snap_b(base + timedelta(minutes=2), 10, 8, 1, "04:00")
    snap_b(base + timedelta(minutes=6), 20, 18, 2, "08:00")
    snap_b(base + timedelta(minutes=8), 30, 28, 2, "04:00")
    snap_b(base + timedelta(minutes=12), 40, 38, 3, "08:00")
    snap_b(base + timedelta(minutes=16), 50, 48, 4, "08:00")
    snap_b(base + timedelta(minutes=20), 52, 50, 4, "01:30")
    snap_b(base + timedelta(minutes=24), 55, 50, 4, "00:00")
    mkt_b(base - timedelta(minutes=1), 158.5)      # before q1
    mkt_b(base + timedelta(minutes=2, seconds=30), 160.5)   # after q1, before q2
    mkt_b(base + timedelta(minutes=6, seconds=30), 164.5)   # after q2, before q3
    mkt_b(base + timedelta(minutes=12, seconds=30), 172.5)  # after q3, before q4/final

    # G-C: LIVE, no market anywhere.
    snap_c, _ = add_game("G-C", "live", now - timedelta(minutes=5), now)
    snap_c(now - timedelta(minutes=5), 0, 0, 1, "08:00")
    snap_c(now - timedelta(minutes=3), 10, 8, 1, "04:00")
    snap_c(now - timedelta(minutes=1), 20, 18, 2, "08:00")

    # G-EMPTY: exists in games table but has NO snapshots -> no predictions.
    add_game("G-EMPTY", "live", now - timedelta(minutes=1), now)

    return st, dbfile


@pytest.fixture
def sc(db):
    st, dbfile = db
    sc = Scorecard(dbfile)
    sc.record_predictions()
    sc.capture_results()
    sc.score_all()
    return sc, st


@pytest.fixture
def app(db):
    application = FastAPI()
    application.include_router(v4_router)
    application.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
                      name="test_dashboard_static")

    @application.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))

    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _cps(client, gid):
    resp = client.get(f"/api/v4/game/{gid}")
    assert resp.status_code == 200
    return resp.json()["checkpoints"]


# ═════════════════════════════════════════════════════════════════════

def test_detail_checkpoints_ordered_and_shaped(client, sc):
    """Checkpoints arrive ordered by source snapshot, with the exact
    column set the detail table needs."""
    cps = _cps(client, "G-A")
    assert cps, "ended game must have checkpoint rows"
    stamps = [c["source_snapshot_at"] for c in cps]
    assert stamps == sorted(stamps)
    for c in cps:
        for key in ("check", "label", "predicted_at", "source_snapshot_at",
                    "blm_prediction", "market_at_checkpoint", "edge",
                    "actual_final", "error"):
            assert key in c, f"missing {key}"
    assert [c["check"] for c in cps][:4] == ["q1", "q2", "q3", "q4"]


def test_snapshot_line_market_freezes_at_or_before(client, sc):
    """G-A: market comes from the last snapshot line at-or-before the
    checkpoint (carried forward), never a later line."""
    cps = {c["check"]: c for c in _cps(client, "G-A")}
    assert cps["q1"]["market_at_checkpoint"] == 160.5
    assert cps["q2"]["market_at_checkpoint"] == 160.5   # no new line yet
    assert cps["q3"]["market_at_checkpoint"] == 164.5   # line moved at q2 04:00
    assert cps["q4"]["market_at_checkpoint"] == 164.5
    assert cps["final"]["market_at_checkpoint"] == 164.5
    for c in cps.values():
        assert c["edge"] == round(c["blm_prediction"] - c["market_at_checkpoint"], 2)


def test_ws_market_freezes_at_or_before(client, sc):
    """G-B: no snapshot lines — the frozen market is the WS observation
    at-or-before each checkpoint (distinct per checkpoint, proving
    historical selection, not 'latest')."""
    cps = {c["check"]: c for c in _cps(client, "G-B")}
    assert cps["q1"]["market_at_checkpoint"] == 158.5
    assert cps["q2"]["market_at_checkpoint"] == 160.5
    assert cps["q3"]["market_at_checkpoint"] == 164.5
    assert cps["q4"]["market_at_checkpoint"] == 172.5


def test_later_observation_never_rewrites_checkpoint(client, sc):
    """Insert a LATER, very different WS line and rebase — every recorded
    checkpoint must keep its frozen market (at-or-before is immutable)."""
    _, st = sc
    cps_before = {c["check"]: c["market_at_checkpoint"]
                  for c in _cps(client, "G-B")}
    now = _now()
    st.upsert_market_observation({
        "game_id": None, "source_game_id": "G-B",
        "captured_at": _iso(now), "market_type": "MatchTotal",
        "market_name": "Match Total", "line_value": 190.5,
        "over_price": 1.9, "under_price": 1.9,
        "home_score": 0, "away_score": 0,
        "period_label": "", "clock": "", "raw_json": "{}",
    })
    sc[0].record_predictions()  # full rebase must not rewrite frozen lines
    cps_after = {c["check"]: c["market_at_checkpoint"]
                 for c in _cps(client, "G-B")}
    assert cps_before == cps_after
    assert cps_after["q1"] == 158.5
    assert cps_after["final"] == 172.5


def test_ended_game_exposes_actual_final_and_error(client, sc):
    cps = {c["check"]: c for c in _cps(client, "G-A")}
    for c in cps.values():
        assert c["actual_final"] == 105          # 55 + 50
        assert c["error"] == round(c["blm_prediction"] - 105, 2)


def test_live_game_checkpoints_null_actual_and_null_market(client, sc):
    """A live game with no market: market_at_checkpoint/edge stay NULL —
    missing data is shown as missing, never fabricated."""
    cps = _cps(client, "G-C")
    assert cps, "live game still records checkpoints"
    for c in cps:
        assert c["actual_final"] is None
        assert c["error"] is None
        assert c["market_at_checkpoint"] is None
        assert c["edge"] is None


def test_game_without_predictions_has_empty_checkpoints(client, sc):
    cps = _cps(client, "G-EMPTY")
    assert cps == []


def test_live_list_payload_stays_lean(client, sc):
    """/api/v4/live must not balloon with per-game checkpoint arrays."""
    games = client.get("/api/v4/live").json()["games"]
    assert games
    for g in games:
        assert "checkpoints" not in g


def test_ws_multi_line_batch_freezes_lowest_line(client, db, sc):
    """The WS feed carries 3 O/U lines per capture; the frozen checkpoint
    market is the LOWEST line of the latest batch at-or-before (event-view
    parity, same rule as storage.market_observations_before) — never an
    arbitrary row of the batch."""
    st, dbfile = db
    now = _now()
    base = now - timedelta(hours=1)
    game = PokerBetGame(
        source="PokerBet", source_game_id="G-D",
        competition_id="comp-1", competition_slug="betual-tbsl",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team="G-D Home Virtual",
        away_team="G-D Away Virtual", game_slug="gd-game",
        source_url="https://x/G-D", status="ended",
        first_seen_at=_iso(base), last_seen_at=_iso(base + timedelta(minutes=8)),
    )
    gid_db = st.upsert_game(game)

    def snap(t, hs, as_, q, clock):
        obs = MarketObservation(
            source="PokerBet", source_game_id="G-D", classification="BETUAL_NBA",
            captured_at=_iso(t), home_team="G-D Home Virtual",
            away_team="G-D Away Virtual", home_score=hs, away_score=as_,
            period_label=f"{q}th Quarter", quarter=q, clock=clock,
            game_status="ended", total_line=None, spread=None,
            w1_odds=None, w2_odds=None, markets_json="{}",
        )
        st.insert_snapshot(gid_db, obs, force=True)

    snap(base, 0, 0, 1, "08:00")
    snap(base + timedelta(minutes=2), 10, 8, 1, "04:00")
    snap(base + timedelta(minutes=6), 20, 18, 2, "08:00")
    # one 3-line WS batch at the same captured_at — inserted NOT lowest-first
    for line in (164.5, 168.5, 160.5):
        st.upsert_market_observation({
            "game_id": gid_db, "source_game_id": "G-D",
            "captured_at": _iso(base + timedelta(minutes=2, seconds=30)),
            "market_type": "MatchTotal", "market_name": "Match Total",
            "line_value": line, "over_price": 1.9, "under_price": 1.9,
            "home_score": 10, "away_score": 8,
            "period_label": "1st Quarter", "clock": "04:00", "raw_json": "{}",
        })
    Scorecard(dbfile).record_predictions()
    cps = {c["check"]: c for c in _cps(client, "G-D")}
    # q1: batch is after its snapshot -> no market at-or-before (honest NULL)
    assert cps["q1"]["market_at_checkpoint"] is None
    assert cps["q1"]["edge"] is None
    # q2: latest batch at-or-before -> LOWEST of the 3 inserted lines
    assert cps["q2"]["market_at_checkpoint"] == 160.5


def test_dashboard_detail_modal_renders_checkpoint_table(client, sc):
    """The deployed detail modal must carry the checkpoint table columns."""
    js = client.get("/static/dashboard.js").text
    assert "CHECKPOINTS — frozen market at each checkpoint" in js
    assert "Market @CP" in js
    assert "market_at_checkpoint" in js
