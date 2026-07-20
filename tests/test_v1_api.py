"""Tests for BLM V1 Flask API.

Uses the Flask test client to avoid running a real server.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# We must set up the Flask app reference before importing blm_v1.app
# because blm_v1.app imports blm_v1.collector at module level.
from blm_v1.app import app


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ═════════════════════════════════════════════════════════════════════
# GET /api/live
# ═════════════════════════════════════════════════════════════════════


@patch("blm_v1.app.collector")
def test_api_live_with_collector(mock_collector, client):
    """/api/live returns collector state when available."""
    mock_collector.latest_state = {
        "home_team": "Warriors",
        "away_team": "Lakers",
        "home_score": 55,
        "away_score": 48,
        "quarter": 2,
        "clock": "7:32",
        "game_id": "g-1",
    }
    mock_collector.snapshot_count = 42

    resp = client.get("/api/live")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["home_team"] == "Warriors"
    assert data["away_team"] == "Lakers"
    assert data["home_score"] == 55
    assert data["snapshot_count"] == 42


@patch("blm_v1.app.collector", None)
@patch("blm_v1.app.get_live_game")
def test_api_live_from_db(mock_get_live, client):
    """/api/live falls back to database when collector has no state."""
    mock_get_live.return_value = {
        "game_id": "g-1",
        "home_team": "Bulls",
        "away_team": "Knicks",
    }

    resp = client.get("/api/live")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "game" in data
    assert data["game"]["game_id"] == "g-1"


@patch("blm_v1.app.collector", None)
@patch("blm_v1.app.get_live_game")
def test_api_live_no_game(mock_get_live, client):
    """/api/live returns no_game status when nothing is live."""
    mock_get_live.return_value = None

    resp = client.get("/api/live")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "no_game"


# ═════════════════════════════════════════════════════════════════════
# GET /api/history
# ═════════════════════════════════════════════════════════════════════


@patch("blm_v1.app.get_snapshots_chrono")
@patch("blm_v1.app.get_live_game")
def test_api_history_with_game_id(mock_get_live, mock_get_snaps, client):
    """/api/history returns snapshots for a given game_id."""
    mock_get_snaps.return_value = [
        {"timestamp": "t1", "home_score": 10, "away_score": 8},
        {"timestamp": "t2", "home_score": 20, "away_score": 15},
    ]

    resp = client.get("/api/history?game_id=g-99")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["game_id"] == "g-99"
    assert data["count"] == 2
    assert len(data["snapshots"]) == 2


@patch("blm_v1.app.get_snapshots_chrono")
@patch("blm_v1.app.get_live_game")
def test_api_history_fallback_to_live(mock_get_live, mock_get_snaps, client):
    """/api/history falls back to live game when no game_id provided."""
    mock_get_live.return_value = {"game_id": "g-live"}
    mock_get_snaps.return_value = [{"timestamp": "t1"}]

    resp = client.get("/api/history")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["game_id"] == "g-live"


@patch("blm_v1.app.get_live_game")
def test_api_history_no_game(mock_get_live, client):
    """/api/history returns no_game status when no game_id and no live."""
    mock_get_live.return_value = None

    resp = client.get("/api/history")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "no_game"


# ═════════════════════════════════════════════════════════════════════
# GET /api/games
# ═════════════════════════════════════════════════════════════════════


@patch("blm_v1.app.get_recent_games")
def test_api_games(mock_get_games, client):
    """/api/games returns the list of recent games."""
    mock_get_games.return_value = [
        {"game_id": "g-1", "home_team": "A", "away_team": "B"},
        {"game_id": "g-2", "home_team": "C", "away_team": "D"},
    ]

    resp = client.get("/api/games")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    assert len(data["games"]) == 2


@patch("blm_v1.app.get_recent_games")
def test_api_games_limit(mock_get_games, client):
    """/api/games respects the limit query parameter."""
    mock_get_games.return_value = [{"game_id": "g-1"}]

    resp = client.get("/api/games?limit=1")
    assert resp.status_code == 200
    mock_get_games.assert_called_with(limit=1)


# ═════════════════════════════════════════════════════════════════════
# Static file serving
# ═════════════════════════════════════════════════════════════════════


@patch("blm_v1.app.send_from_directory")
def test_static_index(mock_send, client):
    """GET / serves index.html from the static directory."""
    mock_send.return_value = "<html>test</html>"

    resp = client.get("/")
    mock_send.assert_called_once()
    assert resp.status_code == 200


@patch("blm_v1.app.send_from_directory")
def test_static_other(mock_send, client):
    """GET /<path> serves arbitrary static files."""
    mock_send.return_value = "css content"

    resp = client.get("/style.css")
    assert resp.status_code == 200
    mock_send.assert_called()
