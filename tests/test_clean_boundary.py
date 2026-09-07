"""Clean-data boundary tests — frontend isolation from legacy/pre-epoch data.

Proves the 10 required cases:
 1. pre-epoch row cannot enter LIVE data
 2. pre-epoch row cannot enter clean historical analysis
 3. post-epoch row enters correctly
 4. legacy history accessible only through explicit legacy path
 5. projector cannot consume legacy data
 6. dashboard API does not fall back from clean DB to legacy DB
 7. mixed clean/legacy datasets correctly partitioned
 8. timestamp boundary is exact
 9. NULL clean fields remain NULL rather than filled from legacy
10. current live game remains visible correctly

No code under test is modified here — the boundary is asserted as it is.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.clean_boundary import (CLEAN, CLEAN_DATA_EPOCH, LEGACY,
                                   game_data_quality, is_clean_ts)
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import SCORECARD_SCHEMA
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"

EPOCH = datetime(2026, 9, 5, 5, 40, 41, 782315, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _game(gid: str, first: datetime) -> PokerBetGame:
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-betual", competition_slug="betual_nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=f"Home {gid}", away_team=f"Away {gid}",
        game_slug=f"home-{gid}-away-{gid}",
        source_url=f"https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/World/1/betual/{gid}/x",
        status="live", first_seen_at=_iso(first), last_seen_at=_iso(first),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp operational DB with one LEGACY (pre-epoch) game and one CLEAN
    (post-epoch) game, plus scorecard tables with legacy + clean rows."""
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    st = PokerBetStore(db)

    now = datetime.now(timezone.utc)
    # CLEAN game timestamps: guaranteed post-epoch regardless of wall clock
    # (clamped forward if the test machine's clock is near the epoch).
    c0 = max(now - timedelta(minutes=5), EPOCH + timedelta(seconds=1))
    c_last = c0 + timedelta(minutes=5)

    def snap(gid_db: int, gid: str, t: datetime, hs: int, as_: int,
             q: int, clock: str, total: float) -> None:
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
            captured_at=_iso(t), home_team=f"Home {gid}", away_team=f"Away {gid}",
            home_score=hs, away_score=as_,
            period_label=f"{q}th Quarter", quarter=q, clock=clock,
            game_status="live", total_line=total,
            markets_json=json.dumps({"total": {"first_line": total}}),
        )
        st.insert_snapshot(gid_db, obs, force=True)

    # LEGACY game — first observation BEFORE the clean epoch; carries one
    # pre-epoch snapshot AND one post-epoch snapshot (mixed).  The store
    # sets first_seen_at=now on upsert, so pin the historical start
    # explicitly (this mirrors a game the collector first saw pre-epoch).
    legacy = st.upsert_game(_game("L1", EPOCH - timedelta(hours=2)))
    snap(legacy, "L1", EPOCH - timedelta(hours=1), 50, 50, 2, "05:00", 185.5)
    snap(legacy, "L1", EPOCH + timedelta(minutes=1), 55, 55, 2, "03:00", 185.5)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE games SET first_seen_at=? WHERE source_game_id=?",
                 (_iso(EPOCH - timedelta(hours=2)), "L1"))
    conn.commit()
    conn.close()

    # CLEAN game — fully post-epoch and fresh (live).
    clean = st.upsert_game(_game("C1", c0))
    snap(clean, "C1", c0, 10, 12, 2, "05:00", 215.5)
    snap(clean, "C1", c0 + timedelta(minutes=3), 16, 18, 2, "03:00", 216.5)
    snap(clean, "C1", c_last, 22, 24, 2, "01:00", 216.5)

    # scorecard tables + rows: one legacy, one clean.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCORECARD_SCHEMA)
    for gid, fh, fa, ft in (("L1", 100, 90, 190), ("C1", 101, 99, 200)):
        conn.execute(
            "INSERT INTO game_results (source_game_id, classification,"
            " final_home, final_away, final_total, result_at,"
            " final_result_status) VALUES (?,?,?,?,?,?,?)",
            (gid, "BETUAL_NBA", fh, fa, ft, _iso(EPOCH + timedelta(hours=3)), "OK"),
        )
    # predictions: legacy frozen at a pre-epoch snapshot, clean post-epoch
    conn.execute(
        "INSERT INTO predictions (source_game_id, classification, model_version,"
        " checkpoint, checkpoint_percent, predicted_at, source_snapshot_at,"
        " progress, elapsed_minutes, projected_total, market_total)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("L1", "BETUAL_NBA", "v4-pace-1", "pct30", 0.30,
         _iso(EPOCH - timedelta(minutes=30)), _iso(EPOCH - timedelta(hours=1)),
         0.30, 12.0, 185.0, 185.5),
    )
    conn.execute(
        "INSERT INTO predictions (source_game_id, classification, model_version,"
        " checkpoint, checkpoint_percent, predicted_at, source_snapshot_at,"
        " progress, elapsed_minutes, projected_total, market_total)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("C1", "BETUAL_NBA", "v4-pace-1", "pct30", 0.30,
         _iso(c0 + timedelta(minutes=3)), _iso(c0 + timedelta(minutes=3)),
         0.30, 12.0, 201.0, 216.5),
    )
    # prediction_scores: one per game (simple literal inserts)
    pid = {}
    for gid in ("L1", "C1"):
        pid[gid] = conn.execute(
            "SELECT id FROM predictions WHERE source_game_id=? ORDER BY id",
            (gid,),
        ).fetchone()[0]
    conn.execute(
        "INSERT INTO prediction_scores (prediction_id, source_game_id,"
        " classification, model_version, total_error, abs_total_error,"
        " total_pct_error, model_total, market_total, actual_total,"
        " market_error, model_beat_market, ou_prediction, ou_result,"
        " ou_correct, scored_at, fragment)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid["C1"], "C1", "BETUAL_NBA", "v4-pace-1", 1.0, 1.0, 0.5,
         201.0, 216.5, 200, 16.5, 1, -1, -1, 1, _iso(c_last), 0),
    )
    conn.execute(
        "INSERT INTO prediction_scores (prediction_id, source_game_id,"
        " classification, model_version, total_error, abs_total_error,"
        " total_pct_error, model_total, market_total, actual_total,"
        " market_error, model_beat_market, ou_prediction, ou_result,"
        " ou_correct, scored_at, fragment)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid["L1"], "L1", "BETUAL_NBA", "v4-pace-1", -5.0, 5.0, 2.6,
         185.0, 185.5, 190, -4.5, 0, -1, 1, 0, _iso(EPOCH + timedelta(minutes=2)), 0),
    )
    # market_history: one legacy, one clean
    conn.execute(
        "INSERT INTO market_history (source_game_id, classification,"
        " started_at, analytics_tz, started_hour, recorded_at,"
        " opening_total, closing_total, total_line_move, market_move,"
        " final_home, final_away, final_total, outcome_olvc, outcome_clv,"
        " opening_total_edge, closing_total_edge)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("C1", "BETUAL_NBA", _iso(c0), "UTC", 0, _iso(c_last),
         216.5, 216.5, 0.0, "UNCHANGED", 101, 99, 200,
         "OVER", "OVER", 0.5, 0.5),
    )
    conn.execute(
        "INSERT INTO market_history (source_game_id, classification,"
        " started_at, analytics_tz, started_hour, recorded_at,"
        " opening_total, closing_total, total_line_move, market_move,"
        " final_home, final_away, final_total, outcome_olvc, outcome_clv,"
        " opening_total_edge, closing_total_edge)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("L1", "BETUAL_NBA", _iso(EPOCH - timedelta(hours=2)), "UTC", 0,
         _iso(EPOCH + timedelta(minutes=2)), 185.5, 185.5, 0.0, "UNCHANGED",
         100, 90, 190, "OVER", "OVER", 0.5, 0.5),
    )
    # checkpoint_market rows for the events dataset (legacy + clean)
    conn.execute(
        "INSERT INTO checkpoint_market (source_game_id, classification,"
        " checkpoint_pct, checkpoint_timestamp, live_market_line,"
        " blm_fair_value, market_vs_fair, actual_final_total,"
        " model_version, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("L1", "BETUAL_NBA", 50, _iso(EPOCH - timedelta(hours=1)),
         185.5, 187.0, 1.5, 190, "v4-pace-1",
         _iso(EPOCH + timedelta(minutes=2))),
    )
    conn.execute(
        "INSERT INTO checkpoint_market (source_game_id, classification,"
        " checkpoint_pct, checkpoint_timestamp, live_market_line,"
        " blm_fair_value, market_vs_fair, actual_final_total,"
        " model_version, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("C1", "BETUAL_NBA", 50, _iso(c0 + timedelta(minutes=3)),
         216.5, 214.0, -2.5, 200, "v4-pace-1", _iso(c_last)),
    )
    conn.commit()
    conn.close()
    return {"db": db, "store": st}


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")

    @app.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))

    return TestClient(app)


# ── 8. timestamp boundary is exact ───────────────────────────────────

def test_boundary_is_exact():
    assert is_clean_ts(CLEAN_DATA_EPOCH) is True
    assert is_clean_ts("2026-09-05T05:40:41.782314Z") is False
    assert is_clean_ts("2026-09-05T05:40:41.782316Z") is True
    assert is_clean_ts(None) is False
    assert is_clean_ts("") is False
    assert game_data_quality(CLEAN_DATA_EPOCH) == CLEAN
    assert game_data_quality("2026-09-05T05:40:41.782314Z") == LEGACY


# ── 1. + 3. + 10. /live: pre-epoch rows never enter; clean enters ────

def test_live_excludes_legacy_includes_clean(client):
    resp = client.get("/api/v4/live")
    assert resp.status_code == 200
    d = resp.json()
    assert d["data_quality"] == CLEAN
    assert d["data_epoch"] == CLEAN_DATA_EPOCH
    games = {g["game_id"]: g for g in d["games"]}
    assert "L1" not in games, "legacy (pre-epoch) game leaked into /live"
    assert "C1" in games
    g = games["C1"]
    # post-epoch data present (case 3)
    assert g["home_score"] == 22 and g["away_score"] == 24
    assert g["live"] is True
    assert g["data_quality"] == CLEAN


# ── 2. pre-epoch rows cannot enter clean historical analysis ─────────

def test_scorecard_aggregates_exclude_legacy(env):
    from blm_v4.scorecard import (_by_progress_sql, _fixed_checkpoints_sql,
                                  _market_compare_sql, _summary_sql)
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    try:
        summary = _summary_sql(conn)
        ver = summary["versions"]["v4-pace-1"]
        # only the CLEAN game's prediction is headline (legacy excluded)
        assert ver["predictions"] == 1
        assert ver["games"] == 1
        assert summary["versions"]["_quality"]["recorded_predictions"] == 1
        fx = {f["percent"]: f for f in _fixed_checkpoints_sql(conn)}
        assert fx[30]["n"] == 1, "legacy checkpoint prediction leaked"
        bands = {b["band"]: b for b in _by_progress_sql(conn)}
        assert bands["25-50%"]["n"] == 1
        mc = _market_compare_sql(conn)
        assert mc["n"] == 1, "legacy market-compare row leaked"
    finally:
        conn.close()


def test_market_vs_fair_excludes_legacy(env):
    from blm_v4.scorecard import _market_vs_fair_sql
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    try:
        mv = _market_vs_fair_sql(conn)
        gids = {g["source_game_id"] for g in mv["games"]}
        assert gids == {"C1"}, f"legacy game in market-vs-fair: {gids}"
        cp50 = next(c for c in mv["checkpoints"] if c["checkpoint_pct"] == 50)
        assert cp50["n"] == 1  # clean row only
    finally:
        conn.close()


def test_trends_exclude_legacy(env):
    from blm_v4.trends import market_performance
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    try:
        mp = market_performance(conn)
        assert mp["olvc"]["n"] == 1, "legacy market_history row leaked"
    finally:
        conn.close()


# ── 4. legacy history accessible only through explicit legacy path ───

def test_history_audit_path_serves_legacy(client):
    resp = client.get("/api/v4/history/L1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["section"] == "history_audit"
    assert d["data_quality"] == LEGACY
    assert d["data_epoch"] == CLEAN_DATA_EPOCH
    # full history incl. the pre-epoch snapshot, explicitly labeled audit
    assert d["total"] == 2
    assert any(s["captured_at"] < CLEAN_DATA_EPOCH for s in d["snapshots"])


def test_events_quality_partition(client):
    # default analytical view = CLEAN only
    clean = client.get("/api/v4/scorecard/events").json()
    assert clean["data_quality"] == CLEAN
    assert {r["game"] for r in clean["rows"]} == {"C1"}
    # explicit legacy audit view
    legacy = client.get("/api/v4/scorecard/events?quality=LEGACY").json()
    assert legacy["data_quality"] == LEGACY
    assert legacy["source"] == "legacy_audit"
    assert {r["game"] for r in legacy["rows"]} == {"L1"}


def test_events_rows_tagged_with_quality(client):
    clean = client.get("/api/v4/scorecard/events").json()
    for r in clean["rows"]:
        assert r["data_quality"] == CLEAN
    legacy = client.get("/api/v4/scorecard/events?quality=LEGACY").json()
    for r in legacy["rows"]:
        assert r["data_quality"] == LEGACY


# ── 5. projector cannot consume legacy data ──────────────────────────

def test_projector_has_no_legacy_path(env, tmp_path):
    # The projector reads EXCLUSIVELY blm_metrics_clean.db — the operational
    # DB is never a projection source.  With no clean DB / no clean rows the
    # projector returns None (no fallback to the operational history).
    assert v4api._pace_projector_for("L1") is None
    assert v4api._pace_projector_for("C1") is None
    # a clean DB that exists but holds no rows for the legacy game: None too
    clean_db = tmp_path / "blm_metrics_clean.db"
    from blm_v4.clean_metrics import CleanMetricsStore
    from blm_v4.pace_projector import PaceProjector
    store = CleanMetricsStore(clean_db)  # real schema, zero rows
    pj = PaceProjector().latest_for_game(store, "L1")
    assert pj is None
    assert store.count_observations() == 0


# ── 6. + 9. no legacy fallback; NULLs stay NULL ─────────────────────

def test_game_detail_clean_game_full_data(client):
    d = client.get("/api/v4/game/C1").json()
    assert d["data_quality"] == CLEAN
    assert d["clean_snapshot_count"] == 3
    assert d["home_score"] == 22
    assert d["market"]["total_line"] is not None
    # prediction generation is frozen: the live-state object is
    # descriptive only — no model block, no signals/traps anywhere
    assert "model" not in d and "signals" not in d
    assert "expected_total" not in d and "win_probability" not in d


def test_legacy_game_detail_uses_only_post_epoch_rows(client):
    d = client.get("/api/v4/game/L1").json()
    assert d["data_quality"] == LEGACY
    # L1's latest POST-epoch snapshot is (55,55); analysis uses post-epoch
    # rows only — the pre-epoch 50-50 row never surfaces as current state.
    assert d["home_score"] == 55
    # checkpoint tables: pre-epoch frozen rows excluded (audit only)
    assert all((c.get("source_snapshot_at") or "") >= CLEAN_DATA_EPOCH
               for c in d["checkpoints"])
    assert all((c.get("checkpoint_timestamp") or "") >= CLEAN_DATA_EPOCH
               for c in d["market_vs_fair"])


def test_clean_fields_null_when_no_post_epoch_rows(client, env):
    # a game with zero post-epoch snapshots: clean fields are NULL, never
    # filled from legacy data.
    old = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    st = env["store"]
    gid = st.upsert_game(_game("L-ONLY", old))
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE games SET first_seen_at=? WHERE source_game_id=?",
                 (_iso(old), "L-ONLY"))
    conn.commit()
    conn.close()
    obs = MarketObservation(
        source="PokerBet", source_game_id="L-ONLY", classification="BETUAL_NBA",
        captured_at=_iso(old + timedelta(minutes=10)),
        home_team="Home L-ONLY", away_team="Away L-ONLY",
        home_score=60, away_score=58, period_label="3rd Quarter", quarter=3,
        clock="05:00", game_status="live", total_line=190.0,
        markets_json="{}",
    )
    st.insert_snapshot(gid, obs, force=True)
    d = client.get("/api/v4/game/L-ONLY").json()
    assert d["data_quality"] == LEGACY
    assert d["clean_snapshot_count"] == 0
    # No pre-epoch observation may surface as current state — the score,
    # market line and checkpoint tables are all empty/NULL, never filled
    # from the legacy 60-58 / 190.0 row.  (model.expected_total carries
    # project()'s documented empty-input default 100.0 — frozen model
    # behavior, unrelated to data isolation.)
    assert d["home_score"] is None and d["away_score"] is None
    assert d["market"]["total_line"] is None
    assert d["checkpoints"] == []
    assert d["market_vs_fair"] == []


# ── 7. mixed datasets correctly partitioned ──────────────────────────

def test_games_list_partitions_clean_and_legacy(client):
    d = client.get("/api/v4/games").json()
    by_id = {g["game_id"]: g for g in d["games"]}
    assert by_id["C1"]["data_quality"] == CLEAN
    assert by_id["L1"]["data_quality"] == LEGACY
    assert d["totals"]["clean"] >= 1
    assert d["totals"]["legacy"] >= 1
    # latest-state rows come from post-epoch snapshots only
    assert by_id["L1"]["home_score"] == 55  # post-epoch row, not 50


# ── scorecard endpoint metadata ──────────────────────────────────────

def test_scorecard_endpoint_metadata(client):
    d = client.get("/api/v4/scorecard").json()
    assert d["data_quality"] == CLEAN
    assert d["data_epoch"] == CLEAN_DATA_EPOCH
    assert d["source"] == "clean_post_epoch"