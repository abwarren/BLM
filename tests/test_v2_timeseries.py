"""Tests for BLM V2 Time Series — Abstract interface and SQLite fallback implementation.

Uses a temporary SQLite database to keep tests isolated and deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from blm_v2.timeseries.base import TimeSeriesDB, TimeSeriesDBProtocol
from blm_v2.timeseries.sqlite_fallback import SQLiteTimeSeries, SnapshotData


# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def sqlite_ts():
    """Return a SQLiteTimeSeries backed by a temp database."""
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blm_ts.db"
        ts = SQLiteTimeSeries(db_path=db_path)
        yield ts


@pytest.fixture
def sample_snapshot() -> SnapshotData:
    return {
        "game_id": "game-001",
        "timestamp": "2026-07-20T12:00:00Z",
        "quarter": 1,
        "clock": "10:00",
        "home_score": 5,
        "away_score": 3,
        "total_line": 220.5,
        "spread": -2.5,
        "home_projection": 105.0,
        "away_projection": 102.0,
        "pace": 72.0,
        "possessions": 12,
        "composite_confidence": 0.72,
        "momentum_score": 55.0,
        "trap_meter": 0.15,
    }


# ═════════════════════════════════════════════════════════════════════
# Interface compliance
# ═════════════════════════════════════════════════════════════════════


def test_ts_interface_abstract():
    """TimeSeriesDB cannot be instantiated directly (abstract methods)."""
    with pytest.raises(TypeError):
        TimeSeriesDB()  # type: ignore


def test_ts_protocol_runtime_checkable(sqlite_ts):
    """SQLiteTimeSeries satisfies the TimeSeriesDBProtocol."""
    assert isinstance(sqlite_ts, TimeSeriesDBProtocol)


def test_ts_has_all_methods(sqlite_ts):
    """SQLiteTimeSeries implements all TimeSeriesDB methods."""
    assert hasattr(sqlite_ts, "write_snapshot")
    assert hasattr(sqlite_ts, "query_snapshots")
    assert hasattr(sqlite_ts, "query_latest")
    assert hasattr(sqlite_ts, "list_games")
    assert hasattr(sqlite_ts, "count_active_games")
    assert hasattr(sqlite_ts, "delete_game")


# ═════════════════════════════════════════════════════════════════════
# SQLite write / read
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sqlite_fallback_write_read(sqlite_ts, sample_snapshot):
    """Written snapshots can be read back."""
    await sqlite_ts.write_snapshot(sample_snapshot)
    snapshots = await sqlite_ts.query_snapshots("game-001")

    assert len(snapshots) == 1
    result = snapshots[0]
    assert result["game_id"] == "game-001"
    assert result["home_score"] == 5
    assert result["away_score"] == 3
    assert result["total_line"] == 220.5


@pytest.mark.asyncio
async def test_write_multiple_snapshots(sqlite_ts, sample_snapshot):
    """Multiple writes for the same game are all stored."""
    for i in range(5):
        snap = dict(sample_snapshot)
        snap["timestamp"] = f"2026-07-20T12:00:{i:02d}Z"
        snap["home_score"] = i * 10
        await sqlite_ts.write_snapshot(snap)

    snapshots = await sqlite_ts.query_snapshots("game-001")
    assert len(snapshots) == 5


@pytest.mark.asyncio
async def test_write_empty_fields(sqlite_ts):
    """Writing a snapshot with minimal fields doesn't crash."""
    snap = {"game_id": "game-min", "timestamp": "2026-01-01T00:00:00Z"}
    await sqlite_ts.write_snapshot(snap)
    snapshots = await sqlite_ts.query_snapshots("game-min")
    assert len(snapshots) == 1


# ═════════════════════════════════════════════════════════════════════
# Query by game ID
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_query_by_game_id(sqlite_ts, sample_snapshot):
    """query_snapshots filters by game_id."""
    s1 = dict(sample_snapshot, game_id="game-a", timestamp="2026-01-01T00:00:00Z")
    s2 = dict(sample_snapshot, game_id="game-b", timestamp="2026-01-01T00:00:00Z")
    await sqlite_ts.write_snapshot(s1)
    await sqlite_ts.write_snapshot(s2)

    results = await sqlite_ts.query_snapshots("game-a")
    assert all(r["game_id"] == "game-a" for r in results)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_query_by_game_id_empty(sqlite_ts):
    """query_snapshots returns empty list for unknown game."""
    results = await sqlite_ts.query_snapshots("non-existent")
    assert results == []


# ═════════════════════════════════════════════════════════════════════
# Query between dates
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_query_between_dates(sqlite_ts, sample_snapshot):
    """query_snapshots with from_ts/to_ts filters correctly."""
    base = dict(sample_snapshot, game_id="game-range")
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-01-01T00:00:00Z"))
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-01-15T00:00:00Z"))
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-02-01T00:00:00Z"))

    results = await sqlite_ts.query_snapshots(
        "game-range",
        from_ts="2026-01-10",
        to_ts="2026-01-20",
    )
    assert len(results) == 1
    assert "01-15" in results[0]["timestamp"]


@pytest.mark.asyncio
async def test_query_from_only(sqlite_ts, sample_snapshot):
    """from_ts without to_ts includes everything from that point onward."""
    base = dict(sample_snapshot, game_id="game-from")
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-01-01T00:00:00Z"))
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-02-01T00:00:00Z"))

    results = await sqlite_ts.query_snapshots("game-from", from_ts="2026-01-15")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_query_to_only(sqlite_ts, sample_snapshot):
    """to_ts without from_ts includes everything up to that point."""
    base = dict(sample_snapshot, game_id="game-to")
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-01-01T00:00:00Z"))
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-02-01T00:00:00Z"))

    results = await sqlite_ts.query_snapshots("game-to", to_ts="2026-01-15")
    assert len(results) == 1


# ═════════════════════════════════════════════════════════════════════
# query_latest
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_query_latest(sqlite_ts, sample_snapshot):
    """query_latest returns the most recent snapshot."""
    base = dict(sample_snapshot, game_id="game-latest")
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-01-01T00:00:00Z", home_score=10))
    await sqlite_ts.write_snapshot(dict(base, timestamp="2026-01-02T00:00:00Z", home_score=20))

    latest = await sqlite_ts.query_latest("game-latest")
    assert latest is not None
    assert latest["home_score"] == 20


@pytest.mark.asyncio
async def test_query_latest_empty(sqlite_ts):
    """query_latest returns None for unknown game."""
    latest = await sqlite_ts.query_latest("non-existent")
    assert latest is None


# ═════════════════════════════════════════════════════════════════════
# list_games / delete_game
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_games(sqlite_ts, sample_snapshot):
    """list_games returns distinct game IDs."""
    for gid in ["game-a", "game-b", "game-c"]:
        await sqlite_ts.write_snapshot(dict(sample_snapshot, game_id=gid, timestamp="2026-01-01T00:00:00Z"))

    games = await sqlite_ts.list_games()
    assert sorted(games) == ["game-a", "game-b", "game-c"]


@pytest.mark.asyncio
async def test_delete_game(sqlite_ts, sample_snapshot):
    """delete_game removes all snapshots for a game."""
    await sqlite_ts.write_snapshot(dict(sample_snapshot, game_id="game-del", timestamp="2026-01-01T00:00:00Z"))
    await sqlite_ts.write_snapshot(dict(sample_snapshot, game_id="game-keep", timestamp="2026-01-01T00:00:00Z"))

    await sqlite_ts.delete_game("game-del")

    assert await sqlite_ts.query_snapshots("game-del") == []
    assert len(await sqlite_ts.query_snapshots("game-keep")) == 1
    assert "game-del" not in await sqlite_ts.list_games()


@pytest.mark.asyncio
async def test_delete_game_idempotent(sqlite_ts):
    """delete_game on a non-existent game doesn't raise."""
    await sqlite_ts.delete_game("does-not-exist")  # Should not raise


@pytest.mark.asyncio
async def test_count_active_games_counts_recently_ingested_only():
    """Live-game count derives from snapshot ingestion recency — the ts
    store is the scheduler's only write target (a game with no snapshot
    ingested inside the staleness window is no longer being collected)."""
    import sqlite3 as _sqlite3
    from datetime import datetime, timezone

    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blm_ts.db"
        ts = SQLiteTimeSeries(db_path=db_path)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")[:-4] + "Z"

        await ts.write_snapshot({
            "game_id": "live-1", "timestamp": now, "quarter": 1,
            "clock": "10:00", "home_score": 0, "away_score": 0,
            "total_line": 220.5,
        })
        await ts.write_snapshot({
            "game_id": "done-1", "timestamp": now, "quarter": 4,
            "clock": "0:01", "home_score": 101, "away_score": 99,
            "total_line": 220.5,
        })
        # Force the finished game's ingestion time far outside the window.
        conn = _sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE snapshots_v2 SET ingested_at = '2026-01-01T00:00:00.000Z' "
            "WHERE game_id = 'done-1'")
        conn.commit()
        conn.close()

        assert await ts.count_active_games() == 1


def test_ingested_at_cutoff_matches_sqlite_default_format():
    """The lexical cutoff must be byte-compatible with SQLite's
    strftime('%Y-%m-%dT%H:%M:%fZ','now') DEFAULT (dot + 3-digit ms).
    A missing dot or wrong precision silently corrupts the boundary."""
    import re
    from blm_v2.timeseries.sqlite_fallback import _ingested_at_cutoff
    cutoff = _ingested_at_cutoff(180.0)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", cutoff), cutoff


@pytest.mark.asyncio
async def test_count_active_games_respects_staleness_boundary(monkeypatch):
    """A game ingested 100 s ago counts only when the staleness window
    covers it, driven by a FIXED clock.

    The first version of this test used datetime.now() and passed even
    against the buggy minute-floor cutoff for ~50 of every 60 wall-clock
    seconds (the wrong-inclusion band is [minute floor of now-staleness,
    now-staleness), which only overlaps the 100 s-old row when the wall
    clock's seconds land in a ~10 s window).  A fixed reference time
    (19:55:45) makes the wrong inclusion reproducible: with staleness 50 s
    the cutoff minute is 19:54 and the stale row sits at 19:54:05 — the
    same minute the buggy cutoff covers, so the old code counts it.
    """
    import sqlite3 as _sqlite3
    from datetime import datetime as _real_datetime, timedelta as _timedelta
    from blm_v2.timeseries import sqlite_fallback as _sf

    fixed_now = _real_datetime.fromisoformat("2026-09-03T19:55:45+00:00")

    class _FixedClock:
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    # The module's ONLY Python-side now() is inside _ingested_at_cutoff;
    # ingested_at DEFAULTs are SQLite strftime, so stubbing is safe here.
    monkeypatch.setattr(_sf, "datetime", _FixedClock)

    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_blm_ts.db"
        ts = SQLiteTimeSeries(db_path=db_path)
        past = (fixed_now - _timedelta(seconds=100)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"

        await ts.write_snapshot({
            "game_id": "edge-1", "timestamp": fixed_now.strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z",
            "quarter": 1, "clock": "10:00",
            "home_score": 0, "away_score": 0, "total_line": 220.5,
        })
        conn = _sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE snapshots_v2 SET ingested_at = ? WHERE game_id = 'edge-1'",
            (past,))
        conn.commit()
        conn.close()

        assert await ts.count_active_games(staleness_s=50) == 0  # too old
        assert await ts.count_active_games(staleness_s=200) == 1  # covered
