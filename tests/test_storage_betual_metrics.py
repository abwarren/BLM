"""Regression: betual_collection_metrics must never self-deadlock (GATE 12).

2026-09-27 incident: betual_collection_metrics() acquired
PokerBetStore._lock (threading.Lock — NOT reentrant) and then called
self.betual_line_distinct_count(), which tried to acquire the same lock
again.  The collector's fast-path thread parked in futex_wait forever:
tick 1 completed, tick 2 never started, _last_fast_completed_at froze,
the self-watchdog starved and systemd SIGABRT-restarted the unit every
~100s (WatchdogSec=90), re-entering the identical deadlock each boot.

The fix reads the O(1) counter row inline on the connection the method
already holds (same lock scope, no nested acquire), falling back to
COUNT(DISTINCT ...) when the counter table is not initialized.

These tests fail under the old behavior (the call deadlocks) and pass
under the new behavior.
"""

import threading

from blm_v4.storage import PokerBetStore


def test_betual_collection_metrics_does_not_self_deadlock(tmp_path):
    """The fast-path metrics call must complete while holding _lock.

    Old code: deadlocked in futex_wait on the nested acquire → this test
    times out waiting for the worker thread and FAILS via is_alive().
    """
    store = PokerBetStore(tmp_path / "b.db")
    result = {}

    def run():
        try:
            result["metrics"] = store.betual_collection_metrics()
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            result["error"] = exc

    worker = threading.Thread(target=run, name="metrics-under-lock",
                              daemon=True)
    worker.start()
    worker.join(timeout=10.0)
    assert not worker.is_alive(), (
        "betual_collection_metrics() deadlocked: nested non-reentrant "
        "PokerBetStore._lock acquisition (2026-09-27 incident)")
    assert "error" not in result, repr(result.get("error"))
    metrics = result["metrics"]
    # Fresh store: counter table not initialized → COUNT DISTINCT fallback
    # must yield 0 without failing, preserving the metrics contract.
    assert metrics["games_with_line_data"] == 0
    assert metrics["line_observations"] == 0


def test_counter_read_matches_count_distinct_after_inserts(tmp_path):
    """Correctness invariant of the inline read: the counter value equals
    COUNT(DISTINCT source_game_id) over betual_line_observations."""
    store = PokerBetStore(tmp_path / "b.db")
    store._ensure_betual_line_counter()
    for gid in ("B-1", "B-2", "B-3"):
        for ts in ("T1", "T2"):
            store.insert_betual_line_observation({
                "source_game_id": gid, "classification": "BETUAL_NBA",
                "captured_at": ts, "market_id": "m", "market_name": "Total",
                "period": "Q1", "line": 150.5, "line_previous": None,
                "line_change": None, "seconds_since_previous_line": None,
                "line_velocity": None, "internal_game_time": None,
                "internal_elapsed_seconds": None, "quarter": "Q1",
                "quarter_remaining_seconds": None,
            })
    with store._lock:
        conn = store._connect()
        try:
            row = conn.execute(
                "SELECT distinct_count FROM betual_line_distinct_games "
                "WHERE id = 1").fetchone()
            counter = row[0] if row else 0
            actual = conn.execute(
                "SELECT COUNT(DISTINCT source_game_id) FROM "
                "betual_line_observations").fetchone()[0]
        finally:
            conn.close()
    assert counter == actual == 3
