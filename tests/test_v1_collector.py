"""Tests for BLM V1 Playwright snapshot collector.

Tests use mock Page objects to keep things fast and deterministic — no
real browser or network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from blm_v1.collector import (
    SEL,
    SnapshotCollector,
    extract_float,
    extract_int,
    extract_text,
    scrape_game_state,
)


# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_page():
    """Return a MagicMock that behaves like a Playwright Page."""
    page = MagicMock()
    # Default: all selectors return None (no element found)
    page.query_selector.return_value = None
    return page


@pytest.fixture
def mock_element():
    """Return a MagicMock that behaves like a Playwright ElementHandle."""
    el = MagicMock()
    el.inner_text.return_value = "  Test Text  "
    return el


@pytest.fixture
def collector():
    """Return a SnapshotCollector with headless=True (no browser launch)."""
    return SnapshotCollector(headless=True)


# ═════════════════════════════════════════════════════════════════════
# Unit: extraction helpers
# ═════════════════════════════════════════════════════════════════════


def test_extract_text(mock_page, mock_element):
    """extract_text returns trimmed inner text when element exists."""
    mock_page.query_selector.return_value = mock_element
    result = extract_text(mock_page, ".some-selector")
    assert result == "Test Text"
    mock_page.query_selector.assert_called_once_with(".some-selector")


def test_extract_text_no_element(mock_page):
    """extract_text returns None when no element found."""
    mock_page.query_selector.return_value = None
    result = extract_text(mock_page, ".missing")
    assert result is None


def test_extract_text_exception(mock_page):
    """extract_text returns None when inner_text raises."""
    el = MagicMock()
    el.inner_text.side_effect = Exception("timeout")
    mock_page.query_selector.return_value = el
    result = extract_text(mock_page, ".broken")
    assert result is None


def test_extract_int(mock_page):
    """extract_int parses digits from element text."""
    el = MagicMock()
    el.inner_text.return_value = "  85  "
    mock_page.query_selector.return_value = el
    result = extract_int(mock_page, ".score")
    assert result == 85


def test_extract_int_negative(mock_page):
    """extract_int handles negative values."""
    el = MagicMock()
    el.inner_text.return_value = "-110"
    mock_page.query_selector.return_value = el
    result = extract_int(mock_page, ".odds")
    assert result == -110


def test_extract_int_no_element(mock_page):
    """extract_int returns None when selector not found."""
    mock_page.query_selector.return_value = None
    result = extract_int(mock_page, ".missing")
    assert result is None


def test_extract_int_non_numeric(mock_page):
    """extract_int returns None when text has no digits."""
    el = MagicMock()
    el.inner_text.return_value = "N/A"
    mock_page.query_selector.return_value = el
    result = extract_int(mock_page, ".empty")
    assert result is None


def test_extract_float(mock_page):
    """extract_float parses float values."""
    el = MagicMock()
    el.inner_text.return_value = "220.5"
    mock_page.query_selector.return_value = el
    result = extract_float(mock_page, ".line")
    assert result == 220.5


def test_extract_float_with_commas(mock_page):
    """extract_float handles comma-separated thousands."""
    el = MagicMock()
    el.inner_text.return_value = "1,234.56"
    mock_page.query_selector.return_value = el
    result = extract_float(mock_page, ".big-number")
    assert result == 1234.56


def test_extract_float_no_element(mock_page):
    """extract_float returns None when selector not found."""
    mock_page.query_selector.return_value = None
    result = extract_float(mock_page, ".missing")
    assert result is None


# ═════════════════════════════════════════════════════════════════════
# Unit: scrape_game_state
# ═════════════════════════════════════════════════════════════════════


def test_scrape_game_state_success(mock_page):
    """scrape_game_state returns a complete state dict when all selectors match."""
    # Wire up selectors to return mock elements with specific text.
    elements = {
        SEL["game_cards"]: "game card",  # truthy → found
        SEL["home_team"]: "Warriors",
        SEL["away_team"]: "Lakers",
        SEL["home_score"]: "55",
        SEL["away_score"]: "48",
        SEL["clock"]: "7:32",
        SEL["quarter"]: "Quarter 2",
        SEL["total_line"]: "220.5",
        SEL["spread"]: "-3.5",
    }

    def _query_side_effect(sel):
        if sel in elements:
            el = MagicMock()
            el.inner_text.return_value = elements[sel]
            return el
        return None

    mock_page.query_selector.side_effect = _query_side_effect

    state = scrape_game_state(mock_page)
    assert state is not None
    assert state["home_team"] == "Warriors"
    assert state["away_team"] == "Lakers"
    assert state["home_score"] == 55
    assert state["away_score"] == 48
    assert state["quarter"] == 2
    assert state["clock"] == "7:32"
    assert state["total_line"] == 220.5
    assert state["spread"] == -3.5


def test_scrape_game_state_no_game_card(mock_page):
    """scrape_game_state returns None when no game card is found."""
    mock_page.query_selector.return_value = None
    state = scrape_game_state(mock_page)
    assert state is None


def test_scrape_game_state_missing_fields(mock_page):
    """scrape_game_state returns defaults for missing fields."""
    elements = {
        SEL["game_cards"]: "card",
        SEL["home_team"]: "Home Team",
        SEL["away_team"]: "Away Team",
    }

    def _query_side_effect(sel):
        if sel in elements:
            el = MagicMock()
            el.inner_text.return_value = elements[sel]
            return el
        return None

    mock_page.query_selector.side_effect = _query_side_effect

    state = scrape_game_state(mock_page)
    assert state is not None
    assert state["home_score"] == 0  # default
    assert state["away_score"] == 0
    assert state["quarter"] == 1  # default
    assert state["clock"] is None
    assert state["total_line"] is None
    assert state["spread"] is None


def test_scrape_game_state_quarter_detection(mock_page):
    """scrape_game_state correctly detects quarter from text."""
    elements = {
        SEL["game_cards"]: "card",
        SEL["home_team"]: "A",
        SEL["away_team"]: "B",
        SEL["quarter"]: "OT",
    }

    def _query_side_effect(sel):
        if sel in elements:
            el = MagicMock()
            el.inner_text.return_value = elements[sel]
            return el
        return None

    mock_page.query_selector.side_effect = _query_side_effect
    state = scrape_game_state(mock_page)
    assert state["quarter"] == 1  # "OT" doesn't contain "1"-"4", so defaults to 1


# ═════════════════════════════════════════════════════════════════════
# Unit: _store_snapshot
# ═════════════════════════════════════════════════════════════════════


@patch("blm_v1.collector.upsert_game")
@patch("blm_v1.collector.insert_snapshot")
def test_store_snapshot(mock_insert, mock_upsert, collector):
    """_store_snapshot persists state and updates latest_state."""
    state = {
        "home_team": "Warriors",
        "away_team": "Lakers",
        "home_score": 60,
        "away_score": 52,
        "quarter": 3,
        "clock": "5:00",
        "total_line": 221.0,
        "spread": -4.0,
    }

    collector._store_snapshot(state)

    assert mock_upsert.called
    assert mock_insert.called
    assert collector.snapshot_count == 1
    assert collector.latest_state is not None
    assert collector.latest_state["home_score"] == 60
    assert "timestamp" in collector.latest_state
    assert "game_id" in collector.latest_state


@patch("blm_v1.collector.upsert_game")
@patch("blm_v1.collector.insert_snapshot")
def test_store_snapshot_increments_count(mock_insert, mock_upsert, collector):
    """_store_snapshot increments snapshot_count on each call."""
    state = {"home_team": "A", "away_team": "B", "home_score": 0, "away_score": 0}

    collector._store_snapshot(state)
    assert collector.snapshot_count == 1
    collector._store_snapshot(state)
    assert collector.snapshot_count == 2
    collector._store_snapshot(state)
    assert collector.snapshot_count == 3


@patch("blm_v1.collector.upsert_game")
@patch("blm_v1.collector.insert_snapshot")
def test_store_snapshot_generates_game_id(mock_insert, mock_upsert, collector):
    """_store_snapshot generates a game_id from team names on first call."""
    state = {
        "home_team": "Bulls",
        "away_team": "Celtics",
        "home_score": 10,
        "away_score": 8,
    }

    collector._store_snapshot(state)
    assert collector.game_id is not None
    assert "Bulls" in collector.game_id
    assert "Celtics" in collector.game_id
