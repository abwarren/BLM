"""Paced deviation backfill (2026-11-01 collector wedge).

The one-time deviation backfill re-runs at EVERY collector start and its
back-to-back per-game reads starved the fast tick's per-snapshot
``clean_metrics._last_valid`` past the 80 s watchdog deadline on 2026-11-01:

  20:45:36  fast tick 51 start, never completed
  20:46:51  watchdog: no completed fast cycle within 80s -> WATCHDOG=1 stopped
  20:51:10  systemd restarted the unit (NRestarts=1)
  20:52:10  tick 7 start, wedged again 2 min into the new process

py-spy at the wedge: MainThread inside ``_last_valid_measured`` while
``blm-deviation-backfill`` was active in ``game_projection_rows``.

This pins the fix: a configurable gap between games, a bound on the pass, an
off switch, and — the property that matters — the previous behaviour when no
pacing is requested.
"""
from __future__ import annotations

import time

import pytest

from blm_v4.collector import _backfill_settings
from blm_v4.deviation import DeviationEngine


# ── settings ─────────────────────────────────────────────────────────
def test_backfill_settings_defaults_pace_and_bound():
    assert _backfill_settings({}) == (True, 0.25, 600.0)


def test_backfill_settings_off_switch_and_overrides():
    assert _backfill_settings({"BLM_DEVIATION_BACKFILL": "0"})[0] is False
    assert _backfill_settings({"BLM_DEVIATION_BACKFILL": "false"})[0] is False
    assert _backfill_settings({"BLM_DEVIATION_BACKFILL": "1"})[0] is True
    on, gap, cap = _backfill_settings(
        {"BLM_DEVIATION_BACKFILL_GAP_S": "2.5",
         "BLM_DEVIATION_BACKFILL_MAX_S": "30"})
    assert (on, gap, cap) == (True, 2.5, 30.0)


def test_backfill_settings_bad_values_fall_back():
    on, gap, cap = _backfill_settings(
        {"BLM_DEVIATION_BACKFILL_GAP_S": "garbage",
         "BLM_DEVIATION_BACKFILL_MAX_S": "NaN-ish"})
    assert (on, gap, cap) == (True, 0.25, 600.0)
    assert _backfill_settings(
        {"BLM_DEVIATION_BACKFILL_GAP_S": "-5"})[1] == 0.0


# ── the pass itself ──────────────────────────────────────────────────
class _Store:
    def __init__(self, n):
        self._ids = ["g%02d" % i for i in range(n)]

    def game_ids_with_projections(self):
        return list(self._ids)


def _engine(n_games, cost_s=0.0, seen=None):
    eng = DeviationEngine.__new__(DeviationEngine)   # no DB in a unit test
    eng.store = _Store(n_games)

    def fake_refresh(gid):
        if seen is not None:
            seen.append(time.perf_counter())
        if cost_s:
            time.sleep(cost_s)
        return {"added": 1, "existing": 0}

    eng.refresh_game = fake_refresh
    return eng


def test_refresh_all_unpaced_keeps_previous_behaviour():
    eng = _engine(5)
    out = eng.refresh_all()
    assert out["games"] == 5 and out["added"] == 5 and out["existing"] == 0
    assert out["truncated"] is False
    assert "elapsed_s" in out


def test_refresh_all_paces_between_games():
    seen = []
    eng = _engine(4, seen=seen)
    eng.refresh_all(gap_s=0.05)
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert len(seen) == 4
    assert min(gaps) >= 0.045, "every game must be followed by the gap (%s)" % gaps


def test_refresh_all_stops_at_the_cap_and_says_so():
    eng = _engine(50, cost_s=0.02)
    out = eng.refresh_all(gap_s=0.0, max_s=0.15)
    assert out["truncated"] is True
    assert out["games"] < 50, "the cap must end the pass early"
    assert out["elapsed_s"] >= 0.15


def test_refresh_all_progress_callback_is_failure_isolated():
    eng = _engine(210)

    def boom(*a):
        raise RuntimeError("progress must never kill the pass")

    out = eng.refresh_all(progress=boom)
    assert out["games"] == 210
