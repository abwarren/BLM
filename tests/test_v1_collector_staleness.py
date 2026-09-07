"""Staleness watchdog tests for the V1 SnapshotCollector.

Proven incident (2026-09-03, recurring in every boot): POKERBET_URL is
a single hardcoded game.  When that game ends — or the page otherwise
stops updating — the DOM keeps returning the same content:
extract_game_state yields the same frozen score/clock forever,
``page.goto`` never raises, and the inner scrape loop spins at 1 log
line/sec indefinitely (67% CPU, ``DL=0KB saved=0KB reqs=0 blocked=N``,
~86k identical journal lines/day) because the crash-retry backoff only
fires on exceptions.  A wedged page never raises.

These tests stub ``sync_playwright`` (no real browser) and assert the
watchdog contract:
  * a page whose game progress is frozen AND which makes no live
    requests must trigger a bounded session restart instead of
    spinning forever;
  * an advancing (live) page must never restart;
  * a frozen page that is still receiving live requests (halftime,
    slow clock) must never restart.
"""

from __future__ import annotations

import threading
import time

import pytest

from blm_v1 import collector as v1_collector
from blm_v1.collector import SnapshotCollector


# ── Playwright stubs (mirror tests/test_v1_collector_resilience.py) ──

class _StubPage:
    def __init__(self, body: str = ""):
        self._body = body

    def route(self, *a, **k):
        pass

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def inner_text(self, *a, **k):
        return self._body


class _StubContext:
    def __init__(self, body: str = ""):
        self.page = _StubPage(body)

    def new_page(self, *a, **k):
        return self.page


class _StubBrowser:
    def __init__(self, body: str = ""):
        self.context = _StubContext(body)

    def new_context(self, *a, **k):
        return self.context

    def close(self, *a, **k):
        pass


class _StubChromium:
    def __init__(self, body: str = ""):
        self._body = body

    def launch(self, *a, **k):
        return _StubBrowser(self._body)


class _StubPlaywright:
    """Context-manager stand-in for sync_playwright()."""

    def __init__(self, body: str = ""):
        self.chromium = _StubChromium(body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _frozen_state(clock: str = "10:00", home: int = 54, away: int = 57) -> dict:
    """A parseable state whose game-progress signature never changes."""
    return {
        "home_team": "Denver Cyber", "away_team": "Houston Cyber",
        "home_score": home, "away_score": away,
        "quarter": 1, "clock": clock,
        "total_line": 220.5, "spread": 1.5,
    }


def _install_noop_db(monkeypatch) -> dict:
    """Neutralise DB writes; return a counter of stored snapshots."""
    calls = {"stores": 0}

    def _noop_upsert(**kwargs):
        pass

    def _noop_insert(**kwargs):
        calls["stores"] += 1

    monkeypatch.setattr(v1_collector, "upsert_game", _noop_upsert)
    monkeypatch.setattr(v1_collector, "insert_snapshot", _noop_insert)
    return calls


def _fast_timers(monkeypatch) -> None:
    monkeypatch.setattr(v1_collector, "SCRAPE_INTERVAL", 0.01)
    monkeypatch.setattr(v1_collector, "START_RETRY_BACKOFF_S", 0.01)
    monkeypatch.setattr(v1_collector, "MAX_RETRY_BACKOFF_S", 0.05)
    monkeypatch.setattr(v1_collector, "STALE_TICK_LIMIT", 3)


def _start_collector(monkeypatch, *, state_fn, body: str = "") -> tuple:
    """Start a collector thread whose page yields ``state_fn`` per tick.

    Returns (collector, thread, launches) where launches counts how many
    browser sessions were started (sync_playwright calls).
    """
    launches = {"n": 0}

    def counting_playwright():
        launches["n"] += 1
        return _StubPlaywright(body)

    monkeypatch.setattr(v1_collector, "sync_playwright", counting_playwright)
    monkeypatch.setattr(v1_collector, "extract_game_state", state_fn)
    v1_collector._tracker.reset()

    c = SnapshotCollector(headless=True)
    t = threading.Thread(target=c.start, daemon=True)
    t.start()
    return c, t, launches


def test_frozen_silent_page_restarts_session_not_spins(monkeypatch):
    """A page frozen on the same score/clock with zero live requests
    must terminate the session (bounded restart via the existing
    backoff) instead of spinning forever."""
    _fast_timers(monkeypatch)
    _install_noop_db(monkeypatch)
    c, t, launches = _start_collector(
        monkeypatch, state_fn=lambda text: _frozen_state()
    )

    # The watchdog (STALE_TICK_LIMIT=3 frozen ticks) must end this
    # session and start a new one within the deadline.
    deadline = time.monotonic() + 5.0
    while launches["n"] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    c.stop()
    t.join(timeout=5.0)

    assert launches["n"] >= 2, (
        f"collector made {launches['n']} session attempt(s) on a frozen "
        "silent page; the staleness watchdog must restart the session, "
        "not spin forever"
    )
    assert not t.is_alive(), "collector thread must exit after stop()"


def test_advancing_page_never_restarts(monkeypatch):
    """A live page (score/clock advancing every tick) must keep its
    session indefinitely — the watchdog must not false-positive."""
    _fast_timers(monkeypatch)
    _install_noop_db(monkeypatch)
    tick = {"n": 0}

    def advancing_state(text):
        tick["n"] += 1
        # Clock advances one game-second per scrape tick.
        clock = f"{max(0, 9 - tick['n'] // 60):02d}:{max(0, 59 - tick['n']):02d}"
        return _frozen_state(clock=clock, home=54 + tick["n"], away=57)

    c, t, launches = _start_collector(
        monkeypatch, state_fn=advancing_state
    )

    # Run well past the stale limit (3 ticks): ~15 ticks of healthy
    # advancing captures must not trigger a restart.
    time.sleep(0.3)
    c.stop()
    t.join(timeout=5.0)

    assert launches["n"] == 1, (
        f"advancing page caused {launches['n']} session attempt(s); a "
        "live page must never trip the staleness watchdog"
    )
    assert not t.is_alive(), "collector thread must exit after stop()"


def test_frozen_page_with_live_requests_never_restarts(monkeypatch):
    """A frozen game that is still receiving page requests (halftime,
    stalled clock, odds still polling) must NOT be treated as stale."""
    _fast_timers(monkeypatch)
    _install_noop_db(monkeypatch)
    c, t, launches = _start_collector(
        monkeypatch, state_fn=lambda text: _frozen_state()
    )

    # Simulate the page's own live polling: record a real request every
    # few ms so _tracker.total_requests > 0 at every scrape tick.
    stop_requests = threading.Event()

    def feed_requests():
        while not stop_requests.is_set():
            v1_collector._tracker.record("xhr", 100)
            time.sleep(0.005)

    feeder = threading.Thread(target=feed_requests, daemon=True)
    feeder.start()
    try:
        # Run well past the stale limit (3 ticks) with requests flowing.
        time.sleep(0.3)
    finally:
        stop_requests.set()
        feeder.join(timeout=2.0)

    c.stop()
    t.join(timeout=5.0)

    assert launches["n"] == 1, (
        f"frozen page with live requests caused {launches['n']} session "
        "attempt(s); network-silence is required for staleness"
    )
    assert not t.is_alive(), "collector thread must exit after stop()"
