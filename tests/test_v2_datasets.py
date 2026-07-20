"""Tests for BLM V2 Dataset Builder.

Uses a mock TS interface to test the builder in isolation without any
database backend.  CSV and Parquet exports are verified on disk then cleaned up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyarrow.parquet as pq
import pytest

from blm_v2.datasets.builder import (
    DatasetBuilder,
    FEATURES,
    TARGETS,
)


# ═════════════════════════════════════════════════════════════════════
# Mock TS interface
# ═════════════════════════════════════════════════════════════════════


class MockTSInterface:
    """In-memory snapshot store that mimics the TimeSeriesDB query interface."""

    def __init__(self):
        self._snapshots: Dict[str, List[Dict[str, Any]]] = {}

    def add_game(self, game_id: str, snapshots: List[Dict[str, Any]]):
        self._snapshots[game_id] = snapshots

    async def query_snapshots(self, game_id: str, limit: int = 10000):
        return self._snapshots.get(game_id, [])

    async def list_games(self):
        return list(self._snapshots.keys())


class MockStorageInterface:
    """Mock storage interface for build_all."""

    def __init__(self):
        self._games: List[Dict[str, Any]] = []

    def add_game(self, game_id: str, status: str = "ended"):
        self._games.append({"game_id": game_id, "status": status})

    async def list_games(self):
        return self._games


# ═════════════════════════════════════════════════════════════════════
# Factory helpers
# ═════════════════════════════════════════════════════════════════════


def make_snapshot(
    home_score: int = 50,
    away_score: int = 45,
    quarter: int = 2,
    clock: str = "5:00",
    timestamp: str = "2026-07-20T12:00:00Z",
    **extra,
) -> Dict[str, Any]:
    """Create a realistic snapshot dict with nested structure."""
    snap = {
        "game_id": "game-test",
        "timestamp": timestamp,
        "quarter": quarter,
        "clock": clock,
        "home_score": home_score,
        "away_score": away_score,
        "home_team": "Warriors",
        "away_team": "Lakers",
        "total_line": 220.5,
        "spread": -3.5,
        "composite_confidence": 0.72,
        "momentum_score": 55.0,
        "momentum_velocity": 2.5,
        "momentum_acceleration": 0.3,
        "trap_meter": 0.15,
        "steam_movement": 0.8,
        "expected_total": 218.0,
        "expected_margin": 4.5,
        "home_projection": 111.0,
        "away_projection": 107.0,
        "win_probability": 0.65,
        "pace": 72.0,
        "possessions": 25,
        # Nested structure
        "metadata": {
            "game_id": "game-test",
            "quarter": quarter,
            "clock": clock,
            "timestamp": timestamp,
        },
        "game_state": {
            "home_score": home_score,
            "away_score": away_score,
            "home_team": "Warriors",
            "away_team": "Lakers",
            "total": home_score + away_score,
            "margin": home_score - away_score,
        },
        "blm": {
            "expected_winner": "home",
            "win_probability": 0.65,
            "confidence": 0.72,
            "expected_margin": 4.5,
            "expected_total": 218.0,
        },
        "pace": {
            "real_pace": 72.0,
            "expected_pace": 71.0,
            "possessions": 25,
        },
        "betting_market": {
            "spread": -3.5,
            "live_spread": -4.0,
            "total": 220.5,
            "steam_movement": 0.8,
        },
        "trap_detection": {
            "trap_meter": 0.15,
        },
        "momentum": {
            "score": 55.0,
            "velocity": 2.5,
            "acceleration": 0.3,
            "direction": "up",
        },
        "confidence_inputs": {
            "composite_confidence": 0.72,
        },
        "team_totals": {
            "home_projection": 111.0,
            "away_projection": 107.0,
        },
    }
    snap.update(extra)
    return snap


@pytest.fixture
def ts():
    return MockTSInterface()


@pytest.fixture
def storage():
    return MockStorageInterface()


@pytest.fixture
def builder(tmp_path):
    return DatasetBuilder(output_dir=str(tmp_path))


# ═════════════════════════════════════════════════════════════════════
# Builder: build single game
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_single_game(ts, builder):
    """build() produces samples from snapshots and exports them."""
    ts.add_game("game-test", [
        make_snapshot(home_score=50, away_score=45, timestamp="t1"),
        make_snapshot(home_score=55, away_score=48, timestamp="t2"),
        make_snapshot(home_score=102, away_score=95, timestamp="final"),
    ])

    path = await builder.build("game-test", ts_interface=ts, output_format="csv")
    assert os.path.exists(path)

    df = pd.read_csv(path)
    assert len(df) == 3  # All snapshots become samples (no exclusion)
    for col in FEATURES:
        assert col in df.columns, f"Missing feature: {col}"
    for col in TARGETS:
        assert col in df.columns, f"Missing target: {col}"


@pytest.mark.asyncio
async def test_build_single_game_invalid_format(ts, builder):
    """build() raises ValueError for unsupported format."""
    ts.add_game("game-test", [make_snapshot()])
    with pytest.raises(ValueError, match="Unsupported format"):
        await builder.build("game-test", ts_interface=ts, output_format="xlsx")


@pytest.mark.asyncio
async def test_build_single_game_no_snapshots(builder):
    """build() raises ValueError when no snapshots exist."""
    ts_empty = MockTSInterface()
    with pytest.raises(ValueError, match="No snapshots found"):
        await builder.build("nonexistent", ts_interface=ts_empty)


# ═════════════════════════════════════════════════════════════════════
# Dataset schema
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dataset_schema(ts, builder):
    """Built dataset has the correct feature and target columns."""
    ts.add_game("game-test", [
        make_snapshot(home_score=10, away_score=8, timestamp="t1"),
        make_snapshot(home_score=100, away_score=90, timestamp="final"),
    ])

    path = await builder.build("game-test", ts_interface=ts, output_format="csv")
    df = pd.read_csv(path)

    assert len(df) == 2
    for col in FEATURES:
        assert col in df.columns, f"Missing feature column: {col}"
    for col in TARGETS:
        assert col in df.columns, f"Missing target column: {col}"

    os.unlink(path)


# ═════════════════════════════════════════════════════════════════════
# CSV export
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_export_csv(ts, builder):
    """CSV export produces a valid CSV file with correct data."""
    ts.add_game("game-test", [
        make_snapshot(home_score=10, away_score=8, timestamp="t1"),
        make_snapshot(home_score=100, away_score=95, timestamp="final"),
    ])

    path = await builder.build("game-test", ts_interface=ts, output_format="csv")

    with open(path) as f:
        content = f.read()
    assert "quarter" in content
    assert "winner" in content
    assert "margin" in content

    df = pd.read_csv(path)
    assert len(df) == 2
    assert df.iloc[0]["home_score"] == 10

    os.unlink(path)


# ═════════════════════════════════════════════════════════════════════
# Parquet export
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_export_parquet(ts, builder):
    """Parquet export produces a valid Parquet file."""
    ts.add_game("game-test", [
        make_snapshot(home_score=10, away_score=8, timestamp="t1"),
        make_snapshot(home_score=100, away_score=95, timestamp="final"),
    ])

    path = await builder.build("game-test", ts_interface=ts, output_format="parquet")
    assert str(path).endswith(".parquet")

    table = pq.read_table(path)
    assert table.num_rows == 2
    assert "home_score" in table.column_names
    assert "winner" in table.column_names

    os.unlink(path)


@pytest.mark.asyncio
async def test_export_parquet_multiple_games(ts, storage, builder):
    """build_all creates a combined Parquet from all games."""
    ts.add_game("game-a", [
        make_snapshot(home_score=80, away_score=70, timestamp="final"),
    ])
    ts.add_game("game-b", [
        make_snapshot(home_score=95, away_score=88, timestamp="final"),
    ])
    storage.add_game("game-a")
    storage.add_game("game-b")

    path = await builder.build_all(storage_interface=storage, ts_interface=ts, output_format="parquet")
    assert os.path.exists(path)

    table = pq.read_table(path)
    assert table.num_rows == 2

    os.unlink(path)


# ═════════════════════════════════════════════════════════════════════
# Feature / target lists
# ═════════════════════════════════════════════════════════════════════


def test_feature_and_target_lists():
    """FEATURES and TARGETS are non-overlapping lists."""
    assert len(FEATURES) == 20
    assert len(TARGETS) == 6
    # No overlap
    assert set(FEATURES).isdisjoint(TARGETS)
