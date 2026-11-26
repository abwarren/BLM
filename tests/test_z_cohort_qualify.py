"""Z-SCORE AUTOBET COHORT — additive qualification layer (DISARMED BY DEFAULT).

One test per required property, no live DB, no network.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

from blm_v4.betting import qualify, z_cohort

GAME = {"game_id": "31684333", "competition_slug": "betual-nba",
        "competition_pace": {"avg_pace": 4.0},
        "projector": {"captured_at": "2026-11-25T00:38:23Z",
                      "progress_pct": 75.0, "required_pts_per_min": 6.0,
                      "actual_pts_per_min": 5.5}}


@pytest.fixture
def zdb(tmp_path):
    """A tiny mode=ro-friendly deviation_residuals DB with one game."""
    path = tmp_path / "metrics.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE deviation_residuals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_game_id TEXT, captured_at TEXT, z_score REAL)""")
    conn.executemany("INSERT INTO deviation_residuals VALUES (NULL,?,?,?)", [
        ("31684333", "2026-11-25T00:38:23Z", 2.0247),
        ("31684333", "2026-11-25T00:37:00Z", 3.0722),
        ("31684333#i2", "2026-11-25T00:36:00Z", 1.5),
    ])
    conn.commit()
    conn.close()
    return path


def test_disarmed_by_default_no_io(monkeypatch):
    monkeypatch.delenv(z_cohort.ENABLE_ENV, raising=False)
    result = z_cohort.evaluate(GAME, clean_db_path=Path("/nonexistent.db"))
    assert result["applicable"] is False
    assert result["qualified"] is True
    assert result["reason"] == z_cohort.DISARMED


def test_strict_floor_above_one(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    monkeypatch.setenv(z_cohort.FLOOR_ENV, "1.0")
    r = z_cohort.evaluate(GAME, clean_db_path=zdb)
    assert (r["qualified"], r["reason"], r["z"]) == (
        True, z_cohort.QUALIFIED, 2.0247)


def test_below_floor_refuses(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    monkeypatch.setenv(z_cohort.FLOOR_ENV, "2.5")
    r = z_cohort.evaluate(GAME, clean_db_path=zdb)
    assert (r["qualified"], r["reason"]) == (False, z_cohort.BELOW_Z_FLOOR)


def test_floor_is_strict_not_inclusive(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    monkeypatch.setenv(z_cohort.FLOOR_ENV, "3.0722")
    r = z_cohort.evaluate(GAME, clean_db_path=zdb)
    assert (r["qualified"], r["reason"]) == (False, z_cohort.BELOW_Z_FLOOR)


def test_missing_game_refuses(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    game = dict(GAME, game_id="99999999")
    r = z_cohort.evaluate(game, clean_db_path=zdb)
    assert (r["qualified"], r["reason"]) == (False, z_cohort.MISSING_Z)


def test_stale_z_refuses(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    monkeypatch.setenv(z_cohort.MAX_AGE_ENV, "1")
    stale_game = dict(GAME)
    stale_game["projector"] = dict(GAME["projector"],
                                   captured_at="2026-11-25T01:00:00Z")
    r = z_cohort.evaluate(stale_game, clean_db_path=zdb)
    assert (r["qualified"], r["reason"]) == (False, z_cohort.STALE_Z)


def test_unreadable_db_fails_closed(monkeypatch):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    r = z_cohort.evaluate(GAME, clean_db_path=Path("/nonexistent/dir/m.db"))
    assert (r["qualified"], r["reason"]) == (False, z_cohort.DB_UNAVAILABLE)


def test_nonfinite_z_refuses(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    conn = sqlite3.connect(zdb)
    conn.execute("UPDATE deviation_residuals SET z_score=? WHERE source_game_id=?",
                 (float("nan"), "31684333"))
    conn.commit(); conn.close()
    r = z_cohort.evaluate(GAME, clean_db_path=zdb)
    assert (r["qualified"], r["reason"]) == (False, z_cohort.MISSING_Z)


def test_check_game_disarmed_is_noop(monkeypatch):
    monkeypatch.delenv(z_cohort.ENABLE_ENV, raising=False)
    game = dict(GAME, competition_slug="betual-euroleague")
    block = qualify.check_game(game)
    assert block["qualified"] is True
    assert block["z_cohort"]["applicable"] is False


def test_check_game_overlay_never_refuses(monkeypatch, zdb):
    """STRENGTH still decides in the default mode; overlays only label."""
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    monkeypatch.setenv(z_cohort.FLOOR_ENV, "10")   # z too low for any layer
    monkeypatch.delenv("AUTOBET_Z1_ENABLED", raising=False)
    monkeypatch.delenv("AUTOBET_Z2_ENABLED", raising=False)
    monkeypatch.delenv("AUTOBET_Z2TCX_ENABLED", raising=False)
    monkeypatch.delenv("AUTOBET_TCX_ENABLED", raising=False)
    monkeypatch.setenv(z_cohort.DB_ENV, str(zdb))
    game = dict(GAME, competition_slug="betual-euroleague")
    block = qualify.check_game(game)
    assert block["qualified"] is True         # incumbent gate decided
    assert block["cohort_layer"] is None      # no enabled overlay matched
    assert block["z_cohort"]["qualified"] is False


def test_unprovable_observation_time_refuses(monkeypatch, zdb):
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    game = dict(GAME)
    game["projector"] = {k: v for k, v in GAME["projector"].items()
                         if k != "captured_at"}
    r = z_cohort.evaluate(game, clean_db_path=zdb)
    assert (r["qualified"], r["reason"]) == (False, z_cohort.STALE_Z)


def test_junk_floor_env_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(z_cohort.FLOOR_ENV, "not-a-number")
    assert z_cohort.z_floor() == z_cohort.DEFAULT_FLOOR


def test_z_only_can_admit_a_strength_refusal(monkeypatch, zdb):
    """Z_ONLY is the ONE mode that widens: the armed z layer decides."""
    from blm_v4.betting import cohort_select as sel
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    monkeypatch.setenv(z_cohort.DB_ENV, str(zdb))
    monkeypatch.setattr(sel, "_mode", sel.Z_ONLY)
    game = dict(GAME)
    game["projector"] = dict(GAME["projector"], progress_pct=50.0)
    block = qualify.check_game(game)
    assert block["cohort_mode"] == "Z_ONLY"
    assert block["qualified"] is True          # z 2.0247 > floor 1.0
    assert block["reason"] == "MODE_Z_ONLY_ADMITTED"


# ── ON-DEMAND z (2026-11-26): the stored writer cannot keep up with a live
#    decision, so the current clean projection is standardised in place.

AS_OF = "2026-11-25T00:38:23Z"


def _seed_benchmark(con, n=100, key="BETUAL_NBA|Q4"):
    """n prior residuals cycling -2,-1,0,1,2 → mean 0, sample std 1.4213."""
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 11, 25, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        ts = (start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT INTO deviation_residuals (observation_id, model_version,"
            " source_game_id, classification, period, benchmark_key,"
            " captured_at, market_trajectory_residual, z_score)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (i + 1, "v", "oldgame", "BETUAL_NBA", 4, key, ts,
             float(-2 + (i % 5)), 0.0))


@pytest.fixture
def on_demand_db(tmp_path, monkeypatch):
    path = tmp_path / "metrics_od.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE deviation_residuals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER,
        model_version TEXT, source_game_id TEXT, classification TEXT,
        period INTEGER, benchmark_key TEXT, captured_at TEXT,
        market_trajectory_residual REAL, z_score REAL)""")
    con.execute("""CREATE TABLE clean_projections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER,
        source_game_id TEXT, classification TEXT, captured_at TEXT,
        period_label TEXT, progress_pct REAL, live_total_line REAL,
        projected_final_total REAL, terminal INTEGER)""")
    _seed_benchmark(con)
    con.execute("INSERT INTO clean_projections (observation_id, source_game_id,"
                " classification, captured_at, period_label, progress_pct,"
                " live_total_line, projected_final_total, terminal)"
                " VALUES (9999,'31684333','BETUAL_NBA',?,'4th Quarter',85.0,"
                " 150.0,147.0,0)", (AS_OF,))
    con.commit()
    con.close()
    monkeypatch.setenv(z_cohort.ENABLE_ENV, "true")
    return path


def _expected_z(residual, values):
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return round((residual - mean) / math.sqrt(var), 4)


def test_on_demand_computes_the_stored_maths(on_demand_db):
    r = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    assert r["reason"] is None and r["source"] == "computed"
    assert r["z"] == _expected_z(3.0, [-2, -1, 0, 1, 2] * 20)


def test_on_demand_prefers_a_fresh_stored_row(on_demand_db):
    con = sqlite3.connect(on_demand_db)
    con.execute("INSERT INTO deviation_residuals (observation_id, model_version,"
                " source_game_id, classification, period, benchmark_key,"
                " captured_at, market_trajectory_residual, z_score)"
                " VALUES (9999,'v','31684333','BETUAL_NBA',4,'BETUAL_NBA|Q4',"
                " ?, 3.0, 9.9)", (AS_OF,))
    con.commit(); con.close()
    r = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    assert (r["z"], r["source"]) == (9.9, "stored")


def test_on_demand_excludes_later_residuals(on_demand_db):
    before = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    con = sqlite3.connect(on_demand_db)
    con.execute("INSERT INTO deviation_residuals (observation_id, model_version,"
                " source_game_id, classification, period, benchmark_key,"
                " captured_at, market_trajectory_residual, z_score)"
                " VALUES (10001,'v','other','BETUAL_NBA',4,'BETUAL_NBA|Q4',"
                " '2026-11-25T00:39:00Z', 500.0, 0.0)")
    con.commit(); con.close()
    after = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    assert after["z"] == before["z"]


def test_on_demand_refuses_without_a_usable_benchmark(on_demand_db):
    con = sqlite3.connect(on_demand_db)
    con.execute("DELETE FROM deviation_residuals")
    con.execute("INSERT INTO deviation_residuals (observation_id, model_version,"
                " source_game_id, classification, period, benchmark_key,"
                " captured_at, market_trajectory_residual, z_score)"
                " VALUES (1,'v','old','BETUAL_NBA',4,'BETUAL_NBA|Q4',"
                " '2026-11-25T00:00:00Z', 1.0, 0.0)")
    con.commit(); con.close()
    r = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    assert (r["z"], r["reason"]) == (None, z_cohort.MISSING_Z)


def test_on_demand_refuses_a_terminal_projection(on_demand_db):
    con = sqlite3.connect(on_demand_db)
    con.execute("UPDATE clean_projections SET terminal=1")
    con.commit(); con.close()
    r = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    assert (r["z"], r["reason"]) == (None, z_cohort.MISSING_Z)


def test_on_demand_can_be_disabled(on_demand_db, monkeypatch):
    monkeypatch.setenv(z_cohort.ONDEMAND_ENV, "false")
    r = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    assert (r["z"], r["reason"]) == (None, z_cohort.MISSING_Z)


def test_on_demand_evaluate_reports_its_source(on_demand_db):
    r = z_cohort.evaluate(GAME, clean_db_path=on_demand_db)
    assert r["qualified"] is True
    assert r["z_source"] == "computed"
    assert r["z_benchmark"]["status"] in ("EXPLORATORY", "PROVISIONAL",
                                          "ESTABLISHED")


def test_on_demand_benchmark_is_windowed(on_demand_db, monkeypatch):
    """The computed z uses the most recent N priors, not all history."""
    monkeypatch.setenv(z_cohort.WINDOW_ENV, "4")
    r = z_cohort.latest_z("31684333", clean_db_path=on_demand_db, as_of=AS_OF)
    # priors cycle -2,-1,0,1,2 ; the last four are -1,0,1,2
    assert r["z"] == _expected_z(3.0, [-1, 0, 1, 2])
    assert r["benchmark_n"] == 4
