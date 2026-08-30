"""Regression tests: league statistics must NEVER leak across leagues.

The HistoricalEngine previously computed every league profile from the
WHOLE snapshots_v2 table (no league filter) — the exact cross-
contamination the BLM constitution forbids.  These tests prove the
league filter is applied to every aggregation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from blm_v2.analytics.historical import HistoricalEngine, LeagueProfile


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "blm_ts_test.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE snapshots_v2 (
            game_id TEXT, timestamp TEXT, quarter INTEGER, clock TEXT,
            home_score INTEGER, away_score INTEGER,
            total_line REAL, spread REAL, pace REAL, possessions INTEGER,
            data_json TEXT
        )
    """)
    # League A: total lines 200, 201, 202  → OLV mean 201
    # League B: total lines 240, 242, 244  → OLV mean 242
    seeds = []
    for i, line in enumerate([200, 201, 202]):
        seeds.append((
            f"cyber-game-{i}", f"2026-08-30T10:0{i}:00Z", 2, "5:00",
            60 + i, 55 + i, line, -3.5, 70, 20,
            json.dumps({"league": "Cyber 2K26", "total_line": line}),
        ))
    for i, line in enumerate([240, 242, 244]):
        seeds.append((
            f"betual-game-{i}", f"2026-08-30T11:0{i}:00Z", 3, "4:00",
            70 + i, 65 + i, line, -1.5, 75, 22,
            json.dumps({"league": "Betual NBA", "total_line": line}),
        ))
    conn.executemany(
        "INSERT INTO snapshots_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)", seeds
    )
    conn.commit()
    conn.close()
    return db


def test_league_profiles_are_isolated(tmp_path):
    """Each league's OLV statistics come from ITS OWN games only."""
    db = _make_db(tmp_path)
    engine = HistoricalEngine(db_path=db)
    engine._refresh_cache()

    cyber = engine.get_profile("Cyber 2K26")
    betual = engine.get_profile("Betual NBA")

    assert cyber.olv_mean == 201.0, f"cyber OLV mean {cyber.olv_mean} != 201 (leak!)"
    assert betual.olv_mean == 242.0, f"betual OLV mean {betual.olv_mean} != 242 (leak!)"
    assert cyber.olv_median == 201.0
    assert betual.olv_median == 242.0
    # Sample sizes must be league-scoped too
    assert cyber.total_snapshots == 3, f"cyber snapshots {cyber.total_snapshots}"
    assert betual.total_snapshots == 3, f"betual snapshots {betual.total_snapshots}"
    assert cyber.total_games == 3 and betual.total_games == 3


def test_league_profiles_are_not_global(tmp_path):
    """The profile must NOT equal the global (unfiltered) statistics."""
    db = _make_db(tmp_path)
    engine = HistoricalEngine(db_path=db)
    engine._refresh_cache()

    cyber = engine.get_profile("Cyber 2K26")
    # Global OLV mean across both leagues would be (201+242)/2 = 221.5
    assert cyber.olv_mean != 221.5, "profile contains global (leaked) statistics"
    assert cyber.olv_mean < betual_mean_of(engine), "cyber stats must stay below betual's"


def betual_mean_of(engine: HistoricalEngine) -> float:
    return engine.get_profile("Betual NBA").olv_mean


def test_league_where_fragment():
    """_league_where produces a fragment + params scoping one league."""
    frag, params = HistoricalEngine._league_where("Cyber 2K26")
    assert "json_extract(data_json, '$.league') = ?" in frag
    assert "json_extract(data_json, '$.metadata.league') = ?" in frag
    assert params == ("Cyber 2K26", "Cyber 2K26")
