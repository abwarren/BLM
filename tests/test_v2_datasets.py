"""Tests for BLM V2 Dataset Builder.

Uses a mock snapshot loader to test the builder in isolation without any
database backend.  CSV and Parquet exports are verified on disk then cleaned up.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyarrow.feather as paf
import pyarrow.parquet as pq
import pytest

from blm_v2.datasets.builder import (
    DatasetBuilder,
    DatasetSample,
    FEATURES,
    TARGETS,
)


# ═════════════════════════════════════════════════════════════════════
# Mock snapshot loader
# ═════════════════════════════════════════════════════════════════════


class MockSnapshotLoader:
    """In-memory snapshot store that mimics the TimeSeriesDB query interface."""

    def __init__(self):
        self._snapshots: Dict[str, List[Dict[str, Any]]] = {}

    def add_game(self, game_id: str, snapshots: List[Dict[str, Any]]):
        self._snapshots[game_id] = snapshots

    def query_snapshots(self, game_id: str, from_ts: Optional[str] = None, to_ts: Optional[str] = None) -> List[Dict[str, Any]]:
        snaps = self._snapshots.get(game_id, [])
        if from_ts:
            snaps = [s for s in snaps if (s.get("timestamp") or "") >= from_ts]
        if to_ts:
            snaps = [s for s in snaps if (s.get("timestamp") or "") <= to_ts]
        return snaps

    def list_games(self) -> List[str]:
        return list(self._snapshots.keys())


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
            "expected_winner": "Warriors",
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
            "momentum_score": 55.0,
            "momentum_velocity": 2.5,
            "momentum_acceleration": 0.3,
            "momentum_direction": "up",
        },
        "confidence": {
            "composite_confidence": 0.72,
        },
        "team_totals": {
            "home_projection": 111.0,
            "away_projection": 107.0,
        },
    }
    # Override with extra fields.
    snap.update(extra)
    return snap


@pytest.fixture
def loader():
    return MockSnapshotLoader()


@pytest.fixture
def builder(loader):
    return DatasetBuilder(snapshot_loader=loader)


# ═════════════════════════════════════════════════════════════════════
# DatasetSample
# ═════════════════════════════════════════════════════════════════════


def test_dataset_sample_defaults():
    """DatasetSample initialises with default values."""
    s = DatasetSample()
    assert s.quarter == 0
    assert s.home_score == 0
    assert s.away_score == 0
    assert s.winner == 0
    assert s.prediction_accuracy == 0


def test_dataset_sample_to_dict():
    """to_dict() returns a flat dict with all feature and target keys."""
    s = DatasetSample(
        quarter=3,
        clock_seconds=300.0,
        home_score=70,
        away_score=65,
        confidence=0.85,
        winner=1,
        margin=5.0,
        final_total=135.0,
    )
    d = s.to_dict()
    assert d["quarter"] == 3
    assert d["home_score"] == 70
    assert d["winner"] == 1
    assert d["margin"] == 5.0
    assert all(k in d for k in FEATURES + TARGETS)


# ═════════════════════════════════════════════════════════════════════
# Builder: build single game
# ═════════════════════════════════════════════════════════════════════


def test_build_single_game(loader, builder):
    """build() produces samples from snapshots and exports them."""
    loader.add_game("game-test", [
        make_snapshot(home_score=50, away_score=45, timestamp="t1"),
        make_snapshot(home_score=55, away_score=48, timestamp="t2"),
        make_snapshot(home_score=60, away_score=52, timestamp="t3"),
        make_snapshot(home_score=102, away_score=95, timestamp="final"),  # final
    ])

    filepath = builder.build("game-test", output_format="csv")
    assert os.path.exists(filepath)

    # Verify content.
    df = pd.read_csv(filepath)
    assert len(df) == 3  # 3 training samples (excludes final)
    assert set(FEATURES + TARGETS).issubset(df.columns)

    # Clean up.
    os.unlink(filepath)


def test_build_single_game_invalid_format(loader, builder):
    """build() raises ValueError for unsupported format."""
    loader.add_game("game-test", [make_snapshot()])
    with pytest.raises(ValueError, match="Unsupported output format"):
        builder.build("game-test", output_format="xlsx")


def test_build_single_game_no_snapshots(builder):
    """build() raises RuntimeError when no snapshots exist."""
    with pytest.raises(RuntimeError, match="No snapshots found"):
        builder.build("nonexistent")


# ═════════════════════════════════════════════════════════════════════
# Dataset schema verification
# ═════════════════════════════════════════════════════════════════════


def test_dataset_schema(loader, builder):
    """Built dataset has the correct feature and target columns."""
    loader.add_game("game-test", [
        make_snapshot(home_score=10, away_score=8, timestamp="t1"),
        make_snapshot(home_score=20, away_score=15, timestamp="t2"),
        make_snapshot(home_score=100, away_score=90, timestamp="final"),
    ])

    filepath = builder.build("game-test", output_format="csv")
    df = pd.read_csv(filepath)

    assert len(df) == 2

    # Check all features present.
    for col in FEATURES:
        assert col in df.columns, f"Missing feature column: {col}"

    # Check all targets present.
    for col in TARGETS:
        assert col in df.columns, f"Missing target column: {col}"

    # Check types.
    assert df["winner"].dtype in ("int64", "int32", "int"), "winner should be int"
    assert df["margin"].dtype in ("float64", "float32"), "margin should be float"
    assert df["final_total"].dtype in ("float64", "float32"), "final_total should be float"

    os.unlink(filepath)


def test_dataset_schema_empty_game(loader, builder):
    """Building from a 1-snapshot game produces 0 samples."""
    loader.add_game("game-test", [
        make_snapshot(home_score=100, away_score=95, timestamp="final"),
    ])
    with pytest.raises(RuntimeError, match="No snapshots found"):
        builder.build("game-test")


# ═════════════════════════════════════════════════════════════════════
# CSV export
# ═════════════════════════════════════════════════════════════════════


def test_export_csv(loader, builder):
    """CSV export produces a valid CSV file with correct data."""
    loader.add_game("game-test", [
        make_snapshot(home_score=10, away_score=8, timestamp="t1"),
        make_snapshot(home_score=100, away_score=95, timestamp="final"),
    ])

    filepath = builder.build("game-test", output_format="csv")

    with open(filepath) as f:
        content = f.read()
    assert "quarter" in content
    assert "winner" in content
    assert "margin" in content

    df = pd.read_csv(filepath)
    assert len(df) == 1
    assert df.iloc[0]["home_score"] == 10
    assert df.iloc[0]["away_score"] == 8

    os.unlink(filepath)


# ═════════════════════════════════════════════════════════════════════
# Parquet export
# ═════════════════════════════════════════════════════════════════════


def test_export_parquet(loader, builder):
    """Parquet export produces a valid Parquet file."""
    loader.add_game("game-test", [
        make_snapshot(home_score=10, away_score=8, timestamp="t1"),
        make_snapshot(home_score=100, away_score=95, timestamp="final"),
    ])

    filepath = builder.build("game-test", output_format="parquet")
    assert filepath.endswith(".parquet")

    table = pq.read_table(filepath)
    assert table.num_rows == 1
    assert "home_score" in table.column_names
    assert "winner" in table.column_names

    os.unlink(filepath)


def test_export_parquet_multiple_games(loader, builder):
    """build_all creates a combined Parquet from all games."""
    loader.add_game("game-a", [
        make_snapshot(home_score=10, away_score=5, timestamp="t1"),
        make_snapshot(home_score=80, away_score=70, timestamp="final"),
    ])
    loader.add_game("game-b", [
        make_snapshot(home_score=15, away_score=10, timestamp="t1"),
        make_snapshot(home_score=95, away_score=88, timestamp="final"),
    ])

    filepath = builder.build_all(output_format="parquet", output_dir="/tmp/test_blm_parquet")
    assert os.path.exists(filepath)

    table = pq.read_table(filepath)
    assert table.num_rows == 2  # one sample per game

    # Clean up.
    os.unlink(filepath)
    meta = filepath.replace(".parquet", ".json")
    if os.path.exists(meta):
        os.unlink(meta)


# ═════════════════════════════════════════════════════════════════════
# Feature / target lists
# ═════════════════════════════════════════════════════════════════════


def test_feature_and_target_lists():
    """FEATURES and TARGETS are non-overlapping and cover all columns."""
    assert len(FEATURES) == 20
    assert len(TARGETS) == 6
    # No overlap.
    assert set(FEATURES).isdisjoint(TARGETS)
    # All columns covered.
    expected_keys = set(FEATURES + TARGETS)
    sample = DatasetSample()
    assert set(sample.to_dict().keys()) == expected_keys
