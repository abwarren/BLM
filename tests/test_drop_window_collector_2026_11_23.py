"""Overflow drops are stamped, so health can judge a window, not a lifetime.

`dropped` is lifetime: one minute-long capture stall cost 6,869 observations
and then read DEGRADED for ever; the only cure was a restart, which erased the
evidence.  The collector now stamps each drop and reports `dropped_recent_15m`.
"""
from __future__ import annotations

import time
from collections import deque
from types import SimpleNamespace

from blm_v4.collector import (DROP_WINDOW_S, MAX_TRACKED_DROP_TIMES,
                              SLOW_TICK_S, PokerBetCollector)


def _fake(*offsets: float) -> SimpleNamespace:
    now = time.monotonic()
    return SimpleNamespace(
        _betual_line_drop_times=deque(now - o for o in offsets))


def test_drops_inside_the_window_are_counted_and_old_ones_pruned():
    fake = _fake(3600, 600, 1)                    # one hour, ten minutes, now
    assert PokerBetCollector._drops_within(fake, DROP_WINDOW_S) == 2
    assert len(fake._betual_line_drop_times) == 2   # the 1h-old entry is gone


def test_an_empty_window_reports_zero():
    assert PokerBetCollector._drops_within(_fake(), DROP_WINDOW_S) == 0


def test_the_window_is_short_enough_to_clear_and_long_enough_to_matter():
    assert 300 <= DROP_WINDOW_S <= 3600            # 5–60 minutes
    assert SLOW_TICK_S < DROP_WINDOW_S / 10        # a stall is far shorter
    assert MAX_TRACKED_DROP_TIMES >= 4096          # survives a full-queue burst


def test_the_stats_payload_carries_the_window():
    src = PokerBetCollector.betual_line_worker_stats.__doc__ or ""
    import inspect
    body = inspect.getsource(PokerBetCollector.betual_line_worker_stats)
    assert "dropped_recent_15m" in body
    assert "drop_window_s" in body
    assert isinstance(src, str)
