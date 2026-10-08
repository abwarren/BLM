"""The betting worker must SAY what each pass decided.

Before this, a stale/empty payload and a switched-off engine were both
invisible: the worker is silent by design, so "no candidates" and "no
input at all" looked identical from outside.  That ambiguity hid a stale
feed for hours.

These pin the properties that make it observable:
  * every pass is summarised (throttled to once a minute when nothing fires);
  * a pass that produced a candidate is logged immediately, unthrottled;
  * a disabled engine is still summarised (the switch-off was invisible too);
  * the summary carries the balance, because R5 sizes the stake from it.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import blm_v4.betting.worker as worker_mod  # noqa: E402


class _Recorder:
    """Captures the structured events the worker emits."""

    def __init__(self):
        self.events = []

    def info(self, event, **kw):
        self.events.append((event, kw))

    def warning(self, event, **kw):
        self.events.append((event, kw))


class _Store:
    def __init__(self, enabled=True, unit=40.0):
        self._enabled = enabled
        self._unit = unit

    def is_enabled(self):
        return self._enabled

    def get_unit_price(self):
        return self._unit

    def today_stats(self):
        return {}

    def is_game_enabled(self, game_id):
        return True


class _Provider:
    def account_balance(self):
        return 127.29


def _worker(games, enabled=True):
    """A real BettingWorker wired to fakes, without building a provider."""
    w = worker_mod.BettingWorker.__new__(worker_mod.BettingWorker)
    w.cfg = None
    w.store = _Store(enabled=enabled)
    w._live_payload_fn = lambda: list(games)
    w.poll_interval_s = 5.0
    w.trigger_feed = None
    w.provider = _Provider()
    w._stop = None
    w._thread = None
    w.last_error = None
    w._last_pass_log = 0.0
    return w


def _capture(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(worker_mod, "logger", rec)
    return rec


def test_a_quiet_pass_is_summarised_once_a_minute(monkeypatch):
    """First pass logs; an immediate second pass does not (throttle)."""
    rec = _capture(monkeypatch)
    w = _worker(games=[])
    w.poll_once()
    w.poll_once()
    assert len(rec.events) == 1, rec.events
    assert rec.events[0][0] == "betting_pass"


def test_a_pass_with_a_candidate_logs_immediately_despite_the_throttle(monkeypatch):
    """A firing pass must never be hidden by the throttle."""
    rec = _capture(monkeypatch)
    w = _worker(games=[])
    w._last_pass_log = time.monotonic()  # pretend a summary was just logged
    w._log_pass({"enabled": True, "games": 1, "candidates": 1,
                 "would_bet": 0, "executed": 0, "no_bet": 0, "balance": 127.29})
    assert len(rec.events) == 1, rec.events
    assert rec.events[0][0] == "betting_pass"


def test_a_disabled_engine_is_still_summarised(monkeypatch):
    """The kill switch being off was invisible; it must be visible now."""
    rec = _capture(monkeypatch)
    w = _worker(games=[{"game_id": "x"}], enabled=False)
    summary = w.poll_once()
    assert summary["enabled"] is False
    assert [e[0] for e in rec.events] == ["betting_pass"]
    assert rec.events[0][1]["enabled"] is False


def test_the_logged_pass_carries_games_and_balance(monkeypatch):
    """R5 sizes the stake from the balance; the count proves the payload.

    Both ride on the LOG, not the returned summary — that dict's exact
    shape is an existing contract (tests/test_betting_execution.py).
    """
    rec = _capture(monkeypatch)
    w = _worker(games=[])
    w.poll_once()
    assert rec.events[0][1]["games"] == 0
    assert rec.events[0][1]["balance"] == 127.29
