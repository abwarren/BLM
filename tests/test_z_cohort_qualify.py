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
