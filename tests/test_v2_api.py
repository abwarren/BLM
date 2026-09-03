"""Tests for BLM V2 FastAPI API.

Uses httpx AsyncClient with FastAPI's ASGI transport for async endpoint testing.
All external dependencies (db, engine, metrics) are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from blm_v2.api.v2_fastapi import create_v2_app


# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def app():
    """Return a clean V2 app with mocked dependencies."""
    application = create_v2_app()

    from blm_v2.api.dependencies import _deps

    mock_ts = AsyncMock()
    mock_storage = AsyncMock()
    mock_engine = AsyncMock()

    mock_ts.get_live_game.return_value = None
    mock_ts.get_game_detail.return_value = {
        "game_id": "g-1",
        "home_team": "Warriors",
        "away_team": "Lakers",
        "status": "live",
        "start_time": "2026-07-20T12:00:00Z",
        "home_score": 55,
        "away_score": 48,
        "quarter": 2,
        "clock": "7:32",
    }
    mock_ts.get_snapshots.return_value = []
    mock_ts.list_games.return_value = []
    mock_ts.get_replay_snapshots.return_value = []
    mock_ts.get_chart_data.return_value = []

    mock_storage.list_games.return_value = []
    mock_storage.get_events.return_value = []
    mock_storage.get_alerts.return_value = []
    mock_storage.get_traps.return_value = {}

    mock_engine.enrich_snapshot.return_value = {}
    mock_engine.detect_traps.return_value = []
    mock_engine.get_config.return_value = {}
    mock_engine.get_processed_count.return_value = 0

    _deps.ts_interface = mock_ts
    _deps.storage_interface = mock_storage
    _deps.blm_engine = mock_engine

    yield application

    _deps.ts_interface = None
    _deps.storage_interface = None
    _deps.blm_engine = None


@pytest.fixture
def client(app):
    """Synchronous test client for non-async tests."""
    from fastapi.testclient import TestClient
    return TestClient(app)


# ═════════════════════════════════════════════════════════════════════
# Helper: create an async client for a given app
# ═════════════════════════════════════════════════════════════════════


def _async_client(app: FastAPI):
    """Return an httpx AsyncClient wired to the app via ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


# ═════════════════════════════════════════════════════════════════════
# GET /api/v2/health
# ═════════════════════════════════════════════════════════════════════


def test_health_endpoint(client):
    """GET /api/v2/health returns 200 with status ok."""
    resp = client.get("/api/v2/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "2.0.0"
    assert "uptime_seconds" in data


# ═════════════════════════════════════════════════════════════════════
# GET /api/v2/live
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_live_endpoint_no_game(app):
    """GET /api/v2/live returns 404 when no live game."""
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/live")
    assert resp.status_code == 404
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_live_endpoint_with_game(app):
    """GET /api/v2/live returns live game when available."""
    from blm_v2.api.dependencies import _deps

    _deps.ts_interface.get_live_game.return_value = {
        "game_id": "g-live",
        "home_team": "Warriors",
        "away_team": "Lakers",
        "home_score": 60,
        "away_score": 55,
        "quarter": 3,
        "clock": "5:00",
        "status": "live",
    }
    _deps.blm_engine.enrich_snapshot.return_value = {
        "game_id": "g-live",
        "status": "live",
        "home_team": "Warriors",
        "away_team": "Lakers",
        "home_score": 60,
        "away_score": 55,
        "quarter": 3,
        "clock": "5:00",
        "blm_score": 65.0,
        "confidence": 0.72,
    }

    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/live")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] == "g-live"
    assert data["home_team"] == "Warriors"


# ═════════════════════════════════════════════════════════════════════
# GET /api/v2/game/{game_id}
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_game_endpoint(app):
    """GET /api/v2/game/{game_id} returns game detail."""
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/game/g-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] == "g-1"
    assert data["home_team"] == "Warriors"
    assert data["away_team"] == "Lakers"


@pytest.mark.asyncio
async def test_game_endpoint_not_found(app):
    """GET /api/v2/game/{game_id} returns 404 for unknown game."""
    from blm_v2.api.dependencies import _deps

    _deps.ts_interface.get_game_detail.return_value = None

    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/game/non-existent")
    assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════════
# GET /api/v2/history/{game_id}
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_history_endpoint(app):
    """GET /api/v2/history/{game_id} returns snapshot history."""
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/history/g-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] == "g-1"
    assert "snapshots" in data


@pytest.mark.asyncio
async def test_history_with_params(app):
    """GET /api/v2/history respects query parameters."""
    from blm_v2.api.dependencies import _deps

    async with _async_client(app) as ac:
        resp = await ac.get(
            "/api/v2/history/g-1",
            params={"from": "2026-01-01", "to": "2026-02-01", "limit": 50},
        )
    assert resp.status_code == 200

    _deps.ts_interface.get_snapshots.assert_called_with(
        game_id="g-1",
        from_ts="2026-01-01",
        to_ts="2026-02-01",
        limit=50,
        offset=0,
    )


# ═════════════════════════════════════════════════════════════════════
# Additional endpoints (smoke tests)
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_games_endpoint(app):
    """GET /api/v2/games returns game list."""
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/games")
    assert resp.status_code == 200
    data = resp.json()
    assert "games" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_model_endpoint(app):
    """GET /api/v2/model returns model state."""
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/model")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "status" in data


@pytest.mark.asyncio
async def test_model_active_games_reads_timeseries_not_storage(app):
    """active_games must come from the live time-series store — the only
    store the V2 scheduler writes (blm_ts.db snapshots_v2) — never the
    storage games table (blm_v2.db), which has no live writer and would
    report 0 while a game is actively being collected."""
    from blm_v2.api.dependencies import _deps

    # Storage claims zero active games; the ts store says one is live.
    _deps.ts_interface.count_active_games = AsyncMock(return_value=1)
    _deps.storage_interface.get_model_state = AsyncMock(
        return_value={"status": "running", "total_games": 0, "active_games": 0})

    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/model")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_games"] == 1
    _deps.ts_interface.count_active_games.assert_awaited()


@pytest.mark.asyncio
async def test_openapi_docs(app):
    """GET /api/v2/docs returns Swagger UI."""
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/docs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_games_endpoint_dedupes_int_vs_str_game_ids(app):
    """ts ids are str; a storage game_id may arrive as int.  The merge
    key must be normalized to str or '123' vs 123 become TWO list items."""
    from blm_v2.api.dependencies import _deps
    _deps.ts_interface.list_games.return_value = ["123"]
    _deps.storage_interface.list_games.return_value = [{
        "game_id": 123, "home_team": "Warriors", "away_team": "Lakers",
        "status": "live", "start_time": "2026-07-20T12:00:00Z",
        "home_score": 55, "away_score": 48,
    }]
    async with _async_client(app) as ac:
        resp = await ac.get("/api/v2/games")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1, f"int/str dup not merged: {data['games']}"
    assert data["games"][0]["game_id"] == "123"
    assert data["games"][0]["home_team"] == "Warriors"  # storage metadata won
