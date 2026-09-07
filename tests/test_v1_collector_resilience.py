"""Resilience tests for the V1 SnapshotCollector thread lifecycle.

The V2 scheduler (server.py -> V1CollectorAdapter -> blm_v1 collector)
depends on the collector thread surviving transient startup failures.
Proven incident (2026-09-03 19:45:14Z): a single
``net::ERR_INTERNET_DISCONNECTED`` during ``page.goto`` killed the
collector's ``start()`` thread permanently — no retry existed — and the
SnapshotScheduler polled ``latest_snapshot`` = None forever, starving
blm_ts.db and reporting active_games=0 while the v4 pipeline was healthy.

These tests stub ``sync_playwright`` (no real browser) and assert the
thread lifecycle contract: a startup crash must be retried, and stop()
must terminate a running session cleanly.
"""

from __future__ import annotations

import threading
import time

import pytest

from blm_v1 import collector as v1_collector
from blm_v1.collector import SnapshotCollector


class _StubPage:
    def route(self, *a, **k):
        pass

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def inner_text(self, *a, **k):
        # No parseable game state -> the scrape loop idles until stop().
        return ""


class _StubContext:
    def __init__(self):
        self.page = _StubPage()

    def new_page(self, *a, **k):
        return self.page


class _StubBrowser:
    def __init__(self):
        self.context = _StubContext()

    def new_context(self, *a, **k):
        return self.context

    def close(self, *a, **k):
        pass


class _StubChromium:
    def launch(self, *a, **k):
        return _StubBrowser()


class _StubPlaywright:
    """Context-manager stand-in for sync_playwright()."""

    def __init__(self):
        self.chromium = _StubChromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fast_retry(monkeypatch) -> None:
    """Keep the retry/backoff and scrape intervals near-instant."""
    monkeypatch.setattr(v1_collector, "SCRAPE_INTERVAL", 0.01)
    monkeypatch.setattr(v1_collector, "START_RETRY_BACKOFF_S", 0.01)
    monkeypatch.setattr(v1_collector, "MAX_RETRY_BACKOFF_S", 0.05)


def test_startup_crash_is_retried_not_fatal(monkeypatch):
    """A collector whose first browser session crashes must start a
    second session instead of letting the thread die."""
    calls = {"n": 0}

    def flaky_playwright():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")
        return _StubPlaywright()

    monkeypatch.setattr(v1_collector, "sync_playwright", flaky_playwright)
    _fast_retry(monkeypatch)

    c = SnapshotCollector(headless=True)
    t = threading.Thread(target=c.start, daemon=True)
    t.start()

    # Wait for the retry to enter the second session.
    deadline = time.monotonic() + 5.0
    while calls["n"] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    c.stop()
    t.join(timeout=5.0)

    assert calls["n"] >= 2, (
        f"collector made {calls['n']} session attempt(s); a startup crash "
        "must be retried, not fatal"
    )
    assert not t.is_alive(), "collector thread must exit after stop()"


def test_stop_terminates_running_session(monkeypatch):
    """stop() must end a healthy session promptly (no retry loop keeps
    the thread alive after shutdown)."""
    calls = {"n": 0}

    def healthy_playwright():
        calls["n"] += 1
        return _StubPlaywright()

    monkeypatch.setattr(v1_collector, "sync_playwright", healthy_playwright)
    _fast_retry(monkeypatch)

    c = SnapshotCollector(headless=True)
    t = threading.Thread(target=c.start, daemon=True)
    t.start()

    deadline = time.monotonic() + 5.0
    while calls["n"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls["n"] == 1

    c.stop()
    t.join(timeout=5.0)
    assert not t.is_alive(), "collector thread must exit promptly after stop()"
    # After a clean stop the retry loop must not spawn another session.
    time.sleep(0.1)
    assert calls["n"] == 1
