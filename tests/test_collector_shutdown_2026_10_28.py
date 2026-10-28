"""COLLECTOR SHUTDOWN (2026-10-28) — a stop must not become a 30-90 s SIGKILL.

Measured on the production box: `blm-server` took 26-91 s to stop, one stop ended
in `code=killed, status=9/KILL ... Failed with result 'timeout'`, and the journal
filled with one `Tick error: TargetClosedError` per second.

Cause: systemd stops the whole cgroup (KillMode=control-group), so chromium dies
the instant SIGTERM lands — but the collector's tick loop answered the closed page
with `log -> sleep(SCRAPE_INTERVAL) -> continue` and kept doing it, while
`collector.stop()` sat at the END of the shutdown handler.  Every restart therefore
cost tens of seconds of 502s on /api/v4/status, which the board paints as
"API/DATA UNAVAILABLE — collector state cannot be verified".

These tests drive the REAL SnapshotCollector loop through a fake Playwright.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from blm_v1 import collector as v1collector
from blm_v2.collector.v1_adapter import V1CollectorAdapter


class FakeTargetClosed(Exception):
    """Stands in for playwright's TargetClosedError (matched by message too)."""


class _FakePage:
    def __init__(self, browser, mode):
        self._browser = browser
        self.mode = mode
        self.calls = 0

    def route(self, *a, **k):
        pass

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        time.sleep(0.01)

    def inner_text(self, selector, timeout=None):
        self.calls += 1
        if self.mode == "closed":
            raise FakeTargetClosed(
                "Target page, context or browser has been closed")
        return ""


class _FakeContext:
    def __init__(self, browser, mode):
        self.page = _FakePage(browser, mode)

    def new_page(self):
        return self.page


class _FakeBrowser:
    def __init__(self, mode):
        self.mode = mode
        self.closed = False
        self.context = None

    def new_context(self, **kwargs):
        self.context = _FakeContext(self, self.mode)
        return self.context

    def close(self):
        self.closed = True


class _FakePW:
    def __init__(self, mode, launches):
        self._mode = mode
        self._launches = launches
        self.chromium = self

    def launch(self, **kwargs):
        browser = _FakeBrowser(self._mode)
        self._launches.append(browser)
        return browser


class _FakeFactory:
    """Replacement for blm_v1.collector.sync_playwright()."""

    def __init__(self, mode):
        self.launches = []
        self._pw = _FakePW(mode, self.launches)

    def __call__(self):
        return self

    def __enter__(self):
        return self._pw

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, mode="empty"):
    factory = _FakeFactory(mode)
    monkeypatch.setattr(v1collector, "sync_playwright", factory)
    return factory


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_stop_wakes_the_inter_tick_wait_and_the_thread_ends(monkeypatch):
    factory = _install(monkeypatch, "empty")
    monkeypatch.setattr(v1collector, "SCRAPE_INTERVAL", 5.0)
    c = v1collector.SnapshotCollector()
    t = threading.Thread(target=c.start, daemon=True)
    t.start()
    assert _wait_for(lambda: factory.launches and factory.launches[0].context
                     and factory.launches[0].context.page.calls >= 1), \
        "collector never reached its first tick"

    t0 = time.monotonic()
    c.stop()
    t.join(3.0)
    elapsed = time.monotonic() - t0
    assert not t.is_alive(), "the collector thread must end after stop()"
    assert elapsed < 1.5, (
        "stop() must WAKE the inter-tick wait (SCRAPE_INTERVAL=5s here), took "
        "%.2fs" % elapsed)


def test_a_closed_page_ends_the_session_instead_of_spinning(monkeypatch):
    factory = _install(monkeypatch, "closed")
    c = v1collector.SnapshotCollector()
    t = threading.Thread(target=c.start, daemon=True)
    t.start()
    try:
        assert _wait_for(lambda: factory.launches
                         and factory.launches[0].context), "no session started"
        time.sleep(1.2)
        browser = factory.launches[0]
        page = browser.context.page
        assert page.calls == 1, (
            "a closed target is terminal for the session: it must be attempted "
            "once, not retried in a 1 Hz loop (attempts=%d)" % page.calls)
        assert browser.closed is True, (
            "the dead session's browser must be closed by the finally block")
    finally:
        c.stop()
        t.join(3.0)


class _CooperativeCollector:
    def __init__(self, headless: bool = True):
        self._gate = threading.Event()
        self.latest_state = None

    def start(self):
        self._gate.wait()

    def stop(self):
        self._gate.set()


class _StubbornCollector:
    def __init__(self, headless: bool = True):
        self._gate = threading.Event()
        self.latest_state = None

    def start(self):
        self._gate.wait()

    def stop(self):
        pass

    def release(self):
        self._gate.set()


def test_adapter_stop_joins_the_collector_thread(monkeypatch):
    monkeypatch.setattr("blm_v2.collector.v1_adapter.V1Collector",
                        _CooperativeCollector)
    adapter = V1CollectorAdapter()
    adapter.start()
    assert adapter.stop(timeout=2.0) is True, (
        "stop() must report that the collector thread ended")
    assert not adapter._thread.is_alive()


def test_adapter_stop_is_bounded_when_the_thread_will_not_end(monkeypatch):
    monkeypatch.setattr("blm_v2.collector.v1_adapter.V1Collector",
                        _StubbornCollector)
    adapter = V1CollectorAdapter()
    adapter.start()
    t0 = time.monotonic()
    assert adapter.stop(timeout=0.2) is False
    assert time.monotonic() - t0 < 1.5, "stop(timeout) must stay bounded"
    adapter._v1.release()


SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"


def test_the_shutdown_handler_stops_the_browser_owner_first():
    """The collector owned the browser; stopping it last is what kept the unit
    alive until SIGKILL."""
    src = SERVER_PY.read_text(encoding="utf-8")
    body = src[src.index("async def stop_pipeline"):]
    body = body[:body.index("pipeline_stopped")]
    marker = '_step("collector", collector.stop'
    assert marker in body, (
        "the shutdown handler must stop the collector with a bounded join")
    assert body.index(marker) < body.index("await scheduler.stop()"), (
        "the collector must be stopped BEFORE the scheduler and workers")


def test_network_producers_are_stopped_before_the_scheduler():
    """The reconciler kept fetching for the WHOLE 90 s stop because its stop()
    sat behind a 25 s scheduler join.  Everything that touches the network or
    the browser must stop first."""
    src = SERVER_PY.read_text(encoding="utf-8")
    body = src[src.index("async def stop_pipeline"):
               src.index("pipeline_stopped")]
    sched = body.index("await scheduler.stop()")
    for token in ('_step("collector"', '_step("result_reconciler"',
                  '_step("fast_lane"', '_step("betting_worker"',
                  '_step("result_verifier"'):
        assert token in body, token + " must be stopped by the shutdown handler"
        assert body.index(token) < sched, token + " must stop BEFORE the scheduler"


def test_every_shutdown_step_is_bounded_and_logged():
    src = SERVER_PY.read_text(encoding="utf-8")
    assert "def _step(name, fn, timeout)" in src
    assert "shutdown_step step=%s ok=%s seconds=" in src, (
        "a wedged step must name itself in the journal instead of costing the "
        "unit's whole stop timeout")


def test_the_server_bounds_its_graceful_wait_so_shutdown_can_run():
    """uvicorn fires the app's shutdown handlers only after every connection
    closes; one hung request used to make every step unreachable."""
    src = SERVER_PY.read_text(encoding="utf-8")
    i = src.index("uvicorn.run(")
    call = src[i:i + 600]
    assert "timeout_graceful_shutdown=" in call, (
        "without it, a hung request prevents the shutdown handler from running "
        "and the unit is SIGKILLed at its stop timeout")
