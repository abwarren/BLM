"""Tests for BLM V1 Playwright snapshot collector.

Tests the current text-based collector API (extract_game_state,
BandwidthTracker, SnapshotCollector storage) — no real browser or
network calls.
"""

from __future__ import annotations

from unittest.mock import patch

from blm_v1.collector import (
    BandwidthTracker,
    SnapshotCollector,
    extract_game_state,
)

# ═════════════════════════════════════════════════════════════════════
# extract_game_state — text parsing
# ═════════════════════════════════════════════════════════════════════


CYBER_BODY = """SIGN IN
Live
Basketball
World
Cyber Basketball. 2K26 Matches
3
Oklahoma City Thunder Cyber
100
San Antonio Spurs Cyber
73
4th Quarter
+12
100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46
15:45
W1
1.25
W2
3.57
Total Points
Over
Under
216.5 1.70 2.02
217.5 1.80 1.90
Points Handicap
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
-26.5 1.95 +26.5 1.75
"""


def test_extract_game_state_full_parse():
    """extract_game_state parses teams, scores, quarter, clock, total, spread."""
    state = extract_game_state(CYBER_BODY)
    assert state is not None
    assert state["home_team"] == "Oklahoma City Thunder Cyber"
    assert state["away_team"] == "San Antonio Spurs Cyber"
    assert state["home_score"] == 100
    assert state["away_score"] == 73
    assert state["quarter"] == 4
    assert state["clock"] == "09:46"
    assert state["total_line"] == 216.5
    assert state["spread"] == -26.5


def test_extract_game_state_no_game():
    """extract_game_state returns None when no Cyber game present."""
    assert extract_game_state("no teams here at all") is None
    assert extract_game_state("") is None


def test_extract_game_state_half_time():
    """Half End maps to quarter 2."""
    body = """World
Cyber Basketball. 2K26 Matches
Science City Jena Cyber
38
WKS Slask Wroclaw Cyber
45
Half End
+23
38 : 45, (21:22), (17:23) 00:00
15:30"""
    state = extract_game_state(body)
    assert state is not None
    assert state["quarter"] == 2
    assert state["home_score"] == 38
    assert state["away_score"] == 45


# ═════════════════════════════════════════════════════════════════════
# BandwidthTracker
# ═════════════════════════════════════════════════════════════════════


def test_bandwidth_tracker():
    tracker = BandwidthTracker()
    tracker.record("document", 2048)
    tracker.record("script", 4096)
    tracker.record_blocked(1024)
    assert tracker.total_kb == pytest_approx(6.0)
    assert tracker.saved_kb == pytest_approx(1.0)
    assert tracker._blocked_count == 1
    assert "DL=6KB" in tracker.summary()


def pytest_approx(value: float, tol: float = 0.01):
    """Tiny helper — avoid importing pytest just for approx."""

    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= tol

    return _Approx()


# ═════════════════════════════════════════════════════════════════════
# SnapshotCollector storage
# ═════════════════════════════════════════════════════════════════════


def test_snapshot_collector_stores():
    """_store_snapshot persists a game + snapshot and keeps game_id stable."""
    collector = SnapshotCollector(headless=True)
    state = {
        "home_team": "Oklahoma City Thunder Cyber",
        "away_team": "San Antonio Spurs Cyber",
        "home_score": 100,
        "away_score": 73,
        "quarter": 4,
        "clock": "09:46",
        "total_line": 216.5,
        "spread": -26.5,
    }
    with patch("blm_v1.collector.upsert_game") as mock_upsert, \
            patch("blm_v1.collector.insert_snapshot") as mock_insert:
        collector._store_snapshot(state)
        first_gid = collector.game_id
        assert collector.snapshot_count == 1
        assert first_gid is not None and first_gid.startswith(
            "Oklahoma City Thunder Cyber-vs-San Antonio Spurs Cyber-"
        )
        mock_upsert.assert_called_once()
        mock_insert.assert_called_once()

        # second snapshot: same game_id, no new game upsert
        collector._store_snapshot({**state, "home_score": 102, "away_score": 75})
        assert collector.game_id == first_gid
        assert collector.snapshot_count == 2
        assert mock_upsert.call_count == 2  # upsert is idempotent by game_id


def test_snapshot_collector_latest_state():
    """latest_state carries timestamp + game_id."""
    collector = SnapshotCollector(headless=True)
    with patch("blm_v1.collector.upsert_game"), \
            patch("blm_v1.collector.insert_snapshot"):
        collector._store_snapshot({
            "home_team": "A Cyber", "away_team": "B Cyber",
            "home_score": 10, "away_score": 8, "quarter": 1, "clock": "10:00",
        })
        latest = collector.latest_state
        assert latest is not None
        assert latest["game_id"] == collector.game_id
        assert "timestamp" in latest
