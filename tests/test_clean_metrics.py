"""Clean metrics database (blm_metrics_clean.db) foundation tests.

The clean statistical population starts at ZERO — historical
blm_pokerbet.db data is never imported.  Every observation retains the
raw state needed to reproduce its derived metrics; only status='VALID'
rows form the statistical population; rejected/replayed rows are
retained with a reason for auditability; z_score stays NULL until a
separately authorized clean baseline exists.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.clean_metrics import (CleanMetricsStore, period_metrics,
                                  pace_metrics)
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _game(gid: str = "G-1", cls: str = "BETUAL_NBA",
          ts: str | None = None) -> PokerBetGame:
    return PokerBetGame(
        source="PokerBet", source_game_id=gid, classification=cls,
        home_team=f"{gid} Home", away_team=f"{gid} Away",
        status="live", first_seen_at=ts or _iso(_now() - timedelta(minutes=5)),
        last_seen_at=ts or _iso(_now()),
    )


def _obs(gid: str, cls: str, ts: str, *, quarter=None,
         period_label: str = "2nd Quarter", clock: str = "05:00",
         hs: int = 45, aw: int = 40, total_line=None,
         over=None, under=None) -> MarketObservation:
    return MarketObservation(
        source="PokerBet", source_game_id=gid, classification=cls,
        captured_at=ts, home_team=f"{gid} Home", away_team=f"{gid} Away",
        home_score=hs, away_score=aw, period_label=period_label,
        quarter=quarter, clock=clock, game_status="live",
        total_line=total_line, total_over_odds=over, total_under_odds=under,
        markets_json="{}",
    )


def _ws_obs(gid: str, ts: str, line: float, over=1.9, under=1.9) -> dict:
    return {
        "game_id": None, "source_game_id": gid, "captured_at": ts,
        "market_type": "MatchTotal", "market_name": "Total",
        "line_value": line, "over_price": over, "under_price": under,
        "home_score": 45, "away_score": 40,
        "period_label": "2nd Quarter", "clock": "05:00",
        "raw": {"src": "test"},
    }


@pytest.fixture
def main(tmp_path):
    """A main operational store (blm_pokerbet.db) + a clean store."""
    dbfile = tmp_path / "blm_pokerbet.db"
    st = PokerBetStore(dbfile)
    clean = CleanMetricsStore(tmp_path / "blm_metrics_clean.db")
    return {"st": st, "clean": clean, "dbfile": dbfile}


def _seed(main, gid="G-1", cls="BETUAL_NBA", ts=None) -> tuple[int, PokerBetGame]:
    st = main["st"]
    ts = ts or _iso(_now() - timedelta(minutes=5))
    game = _game(gid, cls, ts)
    gid_db = st.upsert_game(game)
    return gid_db, game


# ── fresh database / no historical import ────────────────────────────


def test_fresh_db_empty_and_start_marker(main):
    clean = main["clean"]
    assert clean.started_at() is not None
    assert clean.count_observations() == 0
    assert clean.count_snapshots() == 0
    assert clean.count_market_observations() == 0


def test_historical_data_never_imported(main):
    """Seed the MAIN db with a game + snapshots + markets; the clean db
    must still be empty — the clean population only grows from new
    validated observations."""
    gid_db, game = _seed(main, "G-HIST")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=5))
    st.insert_snapshot(gid_db, _obs("G-HIST", "BETUAL_NBA", ts,
                                    total_line=185.5), force=True)
    st.upsert_market_observation(_ws_obs("G-HIST", ts, 180.5))
    clean = main["clean"]
    assert clean.count_observations() == 0
    assert clean.count_snapshots() == 0
    assert clean.count_market_observations() == 0
    assert clean.game("G-HIST") is None


# ── game time: classification-aware, label fallback ──────────────────


def test_betual_label_only_metrics(main):
    gid_db, game = _seed(main, "G-BET")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-BET", "BETUAL_NBA", ts, period_label="2nd Quarter",
               clock="05:00", hs=45, aw=40, total_line=185.5, over=1.9, under=1.91)
    assert st.insert_snapshot(gid_db, obs) is not None
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["status"] == "VALID"
    assert r["total_game_minutes"] == 40.0
    # Q2 05:00 on a 10-min clock: elapsed = 10 + 5 = 15, remaining = 25
    assert r["elapsed_game_minutes"] == 15.0
    assert r["remaining_game_minutes"] == 25.0
    assert r["progress_pct"] == pytest.approx(37.5, abs=1e-3)
    # pace: actual = 85/15 = 5.6667; required = (185.5-85)/25 = 4.02
    assert r["actual_pts_per_min"] == pytest.approx(85 / 15, abs=1e-3)
    assert r["required_pts_per_min"] == pytest.approx(4.02, abs=1e-2)
    assert r["required_pace_target"] == "LIVE_TOTAL_LINE"
    assert r["pace_difference"] == pytest.approx(85 / 15 - 4.02, abs=1e-2)
    # market identity + model separation
    assert r["live_total_line"] == 185.5
    assert r["market_source"] == "event_view"
    assert r["over_price"] == 1.9 and r["under_price"] == 1.91
    assert r["fair_total"] is not None
    assert r["projected_final_total"] == r["fair_total"]
    assert r["fair_total"] != r["live_total_line"]  # never collapsed
    assert r["market_fair_difference"] == pytest.approx(
        r["fair_total"] - 185.5, abs=1e-2)
    assert r["z_score"] is None


def test_cyber_duration_48(main):
    gid_db, game = _seed(main, "G-CYB", "CYBER_2K26")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-CYB", "CYBER_2K26", ts, period_label="2nd Quarter",
               clock="06:00", hs=60, aw=54, total_line=200.0)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["total_game_minutes"] == 48.0
    # Q2 06:00 on a 12-min clock: elapsed = 12 + 6 = 18, remaining = 30
    assert r["elapsed_game_minutes"] == 18.0
    assert r["remaining_game_minutes"] == 30.0
    assert r["progress_pct"] == pytest.approx(37.5, abs=1e-3)  # 18/48
    assert r["actual_pts_per_min"] == pytest.approx(114 / 18, abs=1e-3)


def test_halftime_boundary_elapsed(main):
    gid_db, game = _seed(main, "G-HALF")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-HALF", "BETUAL_NBA", ts, period_label="Half Time",
               clock="10:00", hs=50, aw=50, total_line=190.0)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["elapsed_game_minutes"] == 20.0
    assert r["progress_pct"] == 50.0
    assert r["remaining_game_minutes"] == 20.0


def test_missing_clock_flagged_not_dropped(main):
    gid_db, game = _seed(main, "G-NOCLOCK")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-NOCLOCK", "BETUAL_NBA", ts, period_label="",
               clock=None, hs=45, aw=40)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["status"] == "MISSING_CLOCK"
    assert r["elapsed_game_minutes"] is None
    assert main["clean"].count_observations() == 1       # retained
    assert main["clean"].count_observations("VALID") == 0  # not in population


def test_invalid_classification_rejected(main):
    gid_db, game = _seed(main, "G-UNK", "SOMETHING_ELSE")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-UNK", "SOMETHING_ELSE", ts, period_label="2nd Quarter",
               clock="05:00", hs=45, aw=40)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["status"] == "INVALID"
    assert "classification" in r["reason"]


# ── required pace / negative preservation ────────────────────────────


def test_required_pace_negative_preserved(main):
    """Score already past the target line -> negative required pace is
    stored, never clamped to zero (mathematically meaningful)."""
    gid_db, game = _seed(main, "G-NEG")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    # Q4 05:00 -> elapsed 35, remaining 5; total 200 vs line 190.5
    obs = _obs("G-NEG", "BETUAL_NBA", ts, period_label="4th Quarter",
               clock="05:00", hs=100, aw=100, total_line=190.5)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["status"] == "VALID"
    assert r["required_remaining_points"] == pytest.approx(-9.5, abs=1e-3)
    assert r["required_pts_per_min"] == pytest.approx(-1.9, abs=1e-2)
    assert r["pace_difference"] == pytest.approx(
        200 / 35 - (-1.9), abs=1e-2)


def test_no_pace_without_elapsed():
    tm = period_metrics("BETUAL_NBA", None, "", None)
    pm = pace_metrics(tm["elapsed_game_minutes"], tm["remaining_game_minutes"],
                      85, 185.5)
    assert pm["actual_pts_per_min"] is None
    assert pm["required_pts_per_min"] is None
    assert pm["required_pace_target"] is None


# ── market line identity / selection ─────────────────────────────────


def test_event_view_line_wins_over_ws(main):
    gid_db, game = _seed(main, "G-MIX")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    # WS batch with several lines at one capture
    st.upsert_market_observation(_ws_obs("G-MIX", ts, 178.5))
    st.upsert_market_observation(_ws_obs("G-MIX", ts, 180.5))
    st.upsert_market_observation(_ws_obs("G-MIX", ts, 182.5))
    # event-view snapshot carries its own line
    obs = _obs("G-MIX", "BETUAL_NBA", ts, period_label="2nd Quarter",
               clock="05:00", hs=45, aw=40, total_line=185.5)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["live_total_line"] == 185.5
    assert r["market_source"] == "event_view"
    # all distinct lines preserved (never averaged): 178.5/180.5/182.5 + 185.5
    lines = {(m["line_value"], m["source"]) for m in
             main["clean"].list_market_lines("G-MIX")}
    assert (178.5, "ws") in lines and (180.5, "ws") in lines
    assert (182.5, "ws") in lines and (185.5, "event_view") in lines
    assert len(lines) == 4


def test_ws_fallback_lowest_of_latest_batch(main):
    gid_db, game = _seed(main, "G-WS")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    st.upsert_market_observation(_ws_obs("G-WS", ts, 178.5))
    st.upsert_market_observation(_ws_obs("G-WS", ts, 180.5))
    # snapshot WITHOUT a carried line -> WS lowest-of-batch convention
    obs = _obs("G-WS", "BETUAL_NBA", ts, period_label="2nd Quarter",
               clock="05:00", hs=45, aw=40)
    st.insert_snapshot(gid_db, obs)
    r = main["clean"].record_snapshot_obs(game, obs, st)
    assert r["live_total_line"] == 178.5
    assert r["market_source"] == "ws"
    assert main["clean"].count_market_observations() == 2


# ── replay / regression gates (instance-scoped) ──────────────────────


def test_score_regression_rejected_as_replay(main):
    gid_db, game = _seed(main, "G-REGR")
    st = main["st"]
    clean = main["clean"]
    t1 = _iso(_now() - timedelta(minutes=3))
    t2 = _iso(_now() - timedelta(minutes=2))
    obs1 = _obs("G-REGR", "BETUAL_NBA", t1, period_label="4th Quarter",
                clock="00:30", hs=120, aw=120, total_line=230.0)
    obs2 = _obs("G-REGR", "BETUAL_NBA", t2, period_label="4th Quarter",
                clock="01:30", hs=118, aw=117, total_line=230.0)
    assert st.insert_snapshot(gid_db, obs1) is not None
    assert st.insert_snapshot(gid_db, obs2) is not None
    r1 = clean.record_snapshot_obs(game, obs1, st)
    r2 = clean.record_snapshot_obs(game, obs2, st)
    assert r1["status"] == "VALID"
    assert r2["status"] == "REPLAY"
    assert "score regression" in r2["reason"]
    assert clean.count_observations() == 2        # retained for audit
    assert clean.count_observations("VALID") == 1  # excluded from population


def test_clock_regression_rejected_as_replay(main):
    gid_db, game = _seed(main, "G-CLKREGR")
    st = main["st"]
    clean = main["clean"]
    t1 = _iso(_now() - timedelta(minutes=3))
    t2 = _iso(_now() - timedelta(minutes=2))
    obs1 = _obs("G-CLKREGR", "BETUAL_NBA", t1, period_label="4th Quarter",
                clock="01:30", hs=120, aw=120, total_line=240.0)
    # later state with HIGHER total but EARLIER clock (Q2) -> clock gate
    obs2 = _obs("G-CLKREGR", "BETUAL_NBA", t2, period_label="2nd Quarter",
                clock="05:00", hs=125, aw=125, total_line=240.0)
    st.insert_snapshot(gid_db, obs1)
    st.insert_snapshot(gid_db, obs2)
    clean.record_snapshot_obs(game, obs1, st)
    r2 = clean.record_snapshot_obs(game, obs2, st)
    assert r2["status"] == "REPLAY"
    assert "clock regression" in r2["reason"]


def test_exact_duplicate_capture_rejected_upstream(main):
    """Exact duplicate captures never reach the clean writer: the main
    store rejects (game, captured_at) duplicates and the collector only
    records on a truthy insert."""
    gid_db, game = _seed(main, "G-DUP")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-DUP", "BETUAL_NBA", ts, period_label="2nd Quarter",
               clock="05:00", hs=45, aw=40, total_line=185.5)
    # first capture accepted -> collector records it in the clean db
    assert st.insert_snapshot(gid_db, obs) is not None
    clean = main["clean"]
    clean.record_snapshot_obs(game, obs, st)
    # exact duplicate capture rejected upstream (UNIQUE game+captured_at)
    # -> the collector gate (`if row_id: _record_clean(...)`) never fires
    # again, so the clean population holds exactly one observation
    assert st.insert_snapshot(gid_db, obs) is None
    assert clean.count_snapshots() == 1
    assert clean.count_observations() == 1


# ── instance isolation / finals / raw reproducibility ────────────────


def test_instance_isolation(main):
    """#iN instances are separate clean populations — a replay routed to a
    fresh instance never regresses the finished base instance."""
    gid_db, game = _seed(main, "3080#i1")
    st = main["st"]
    clean = main["clean"]
    ts = _iso(_now() - timedelta(minutes=2))
    obs = _obs("3080#i1", "BETUAL_NBA", ts, period_label="4th Quarter",
               clock="01:30", hs=118, aw=117, total_line=230.0)
    st.insert_snapshot(gid_db, obs)
    r = clean.record_snapshot_obs(game, obs, st)
    assert r["status"] == "VALID"          # no base-instance history to regress
    assert clean.game("3080#i1")["base_game_id"] == "3080"


def test_final_result_recorded_without_overwriting(main):
    gid_db, game = _seed(main, "G-FIN")
    st = main["st"]
    clean = main["clean"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-FIN", "BETUAL_NBA", ts, period_label="4th Quarter",
               clock="00:30", hs=120, aw=110, total_line=230.0)
    st.insert_snapshot(gid_db, obs)
    clean.record_snapshot_obs(game, obs, st)
    clean.finalize("G-FIN", final_home=120, final_away=110)
    g = clean.game("G-FIN")
    assert g["final_home"] == 120 and g["final_away"] == 110
    assert g["final_total"] == 230
    assert g["final_result_status"] == "FINAL"
    # live observation untouched by the final
    assert clean.latest_observation("G-FIN")["captured_at"] == obs.captured_at


def test_finalize_unknown_when_no_final_seen(main):
    _seed(main, "G-VAN")
    clean = main["clean"]
    clean.finalize("G-VAN")
    g = clean.game("G-VAN")
    assert g["final_result_status"] == "UNKNOWN"
    assert g["final_total"] is None


def test_raw_state_reproducible(main):
    """clean_snapshots retains the raw period/clock/scores so every
    derived metric can be recomputed from stored inputs."""
    gid_db, game = _seed(main, "G-RAW")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-RAW", "CYBER_2K26", ts, period_label="3rd Quarter",
               quarter=None, clock="08:00", hs=70, aw=65, total_line=210.0)
    st.insert_snapshot(gid_db, obs)
    clean = main["clean"]
    clean.record_snapshot_obs(game, obs, st)
    conn = sqlite3.connect(f"file:{clean.db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        snap = dict(conn.execute(
            "SELECT * FROM clean_snapshots WHERE source_game_id=?",
            ("G-RAW",)).fetchone())
    finally:
        conn.close()
    assert snap["period_label"] == "3rd Quarter"
    assert snap["quarter"] is None
    assert snap["clock"] == "08:00"
    assert snap["home_score"] == 70 and snap["away_score"] == 65
    assert snap["total_points"] == 135


def test_z_score_always_null(main):
    gid_db, game = _seed(main, "G-Z")
    st = main["st"]
    ts = _iso(_now() - timedelta(minutes=3))
    obs = _obs("G-Z", "BETUAL_NBA", ts, period_label="2nd Quarter",
               clock="05:00", hs=45, aw=40, total_line=185.5)
    st.insert_snapshot(gid_db, obs)
    main["clean"].record_snapshot_obs(game, obs, st)
    r = main["clean"].latest_observation("G-Z")
    assert r["z_score"] is None  # no clean baseline exists yet


# ── invariants ───────────────────────────────────────────────────────


def test_elapsed_plus_remaining_equals_total(main):
    for cls, qlen in (("BETUAL_NBA", 10.0), ("CYBER_2K26", 12.0)):
        for q, mm, ss in ((1, qlen, 0), (2, qlen // 2, 30), (3, qlen, 0),
                          (4, 1, 15)):
            tm = period_metrics(cls, q, f"{q}th Quarter",
                                f"{int(mm):02d}:{int(ss):02d}")
            assert tm["status"] == "VALID"
            assert tm["elapsed_game_minutes"] + tm["remaining_game_minutes"] \
                == pytest.approx(tm["total_game_minutes"], abs=0.01)


def test_required_pace_formula():
    pm = pace_metrics(elapsed=15.0, remaining=25.0, total_points=85,
                      live_line=185.5)
    assert pm["required_remaining_points"] == pytest.approx(100.5, abs=1e-3)
    assert pm["required_pts_per_min"] == pytest.approx(100.5 / 25, abs=1e-3)
    assert pm["pace_difference"] == pytest.approx(
        85 / 15 - 100.5 / 25, abs=1e-3)