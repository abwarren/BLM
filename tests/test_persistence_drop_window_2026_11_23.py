"""Persistence drops are judged on a WINDOW, not a lifetime counter.

The collector's `dropped` counter is lifetime: one minute-long capture stall
(tick 4780: 63.06 s, 6,869 observations) read DEGRADED for ever afterwards and
the only cure was a restart — which erased the evidence and fixed nothing.
Health now judges `dropped_recent_15m` and keeps the lifetime total in the text.
An older collector that cannot report a window keeps the old behaviour: never
silently ignore a drop.
"""
from __future__ import annotations

import time

from blm_v4.monitoring.health import Inputs, evaluate


def _worker(**over) -> dict:
    w = {"worker_alive": True, "queue_depth": 0, "enqueued": 1000,
         "persisted": 1000, "failed": 0, "dropped": 7347,
         "dropped_recent_15m": 0, "drop_window_s": 900}
    w.update(over)
    return w


def _persistence(worker: dict) -> dict:
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1))
    state = {"betual_line_worker": worker, "last_success_at": iso,
             "last_tick_at": iso, "snapshots_total": 110, "tick": 1}
    out = evaluate(Inputs(now=now, state=state,
                          snapshot_history=[(now - 5, 100), (now, 110)]))
    return next(c for c in out["components"] if c["key"] == "persistence")


def test_lifetime_drops_outside_the_window_do_not_alarm():
    c = _persistence(_worker())
    assert c["state"] == "HEALTHY"
    assert "7347" in c["detail"] and "0 in 15m" in c["detail"]
    assert c["metrics"]["dropped_recent"] == 0


def test_drops_inside_the_window_degrade():
    c = _persistence(_worker(dropped_recent_15m=3))
    assert c["state"] == "DEGRADED"
    assert "3 in 15m" in c["detail"]


def test_an_old_collector_without_a_window_stays_conservative():
    w = _worker()
    w.pop("dropped_recent_15m")
    w.pop("drop_window_s")
    c = _persistence(w)
    assert c["state"] == "DEGRADED"          # never silently ignore drops
    assert "7347" in c["detail"]


def test_no_drops_reports_a_clean_healthy_line():
    c = _persistence(_worker(dropped=0))
    assert c["state"] == "HEALTHY"
    assert "dropped" not in c["detail"]


def test_a_full_queue_or_a_dead_worker_is_still_failed():
    assert _persistence(_worker(queue_depth=999999))["state"] == "FAILED"
    assert _persistence(_worker(worker_alive=False))["state"] == "FAILED"
