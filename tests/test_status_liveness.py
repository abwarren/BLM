"""Collector-liveness regression tests (2026-09-26 directive).

Background: the dashboard read collector liveness from /api/v4/status,
whose last-snapshot freshness query was an UNINDEXED global scan over
the ~2M-row snapshots table (first MAX(captured_at), then — after the
2026-09-25 "fix" — ORDER BY captured_at DESC LIMIT 1 with no WHERE,
which scanned the same 2M index entries because captured_at is the
SECOND column of idx_snapshots_class_captured).  /status hung 87 s per
poll and the dashboard reported COLLECTOR OFFLINE during a healthy run.

These tests pin:
  1. _db_stats' per-class freshness query must be an index SEEK —
     equality on the leading classification column.  Verified two ways:
     the query plan must not be a full scan, and the wall-clock shape
     must survive a table large enough that a scan is obvious.
  2. The result equals the ground-truth MAX over classes.
  3. The collector self-watchdog only arms under systemd (WATCHDOG_USEC +
     NOTIFY_SOCKET present, non-test tick cadence) and only pets the
     dog while a fast cycle completed within the liveness deadline.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import threading
from pathlib import Path
from unittest import mock

import pytest

from blm_v4 import api as v4api
from blm_v4 import collector as colmod
from blm_v4.collector import PokerBetCollector


# ────────────────────────────────────────────────────────────────────────
# 1+2. /status last-snapshot freshness: per-class seek, correct value
# ────────────────────────────────────────────────────────────────────────

def _build_status_db(db_path: Path) -> Path:
    """Production-shaped pipeline DB: idx_snapshots_class_captured is the
    ONLY captured_at index — exactly the index that made the global
    ORDER BY a scan."""
    db = db_path
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT 'PokerBet',
            source_game_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'live',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(source, source_game_id)
        );
        CREATE INDEX idx_games_class ON games(classification);
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            source_game_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            quarter INTEGER
        );
        CREATE INDEX idx_snapshots_class_captured
            ON snapshots(classification, captured_at);
        CREATE INDEX idx_snapshots_game_ts
            ON snapshots(game_id, captured_at);
        CREATE TABLE reconciliation (
            id INTEGER PRIMARY KEY,
            result TEXT
        );
        CREATE TABLE market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER,
            source_game_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            market_type TEXT,
            market_name TEXT,
            line_value REAL,
            over_price REAL,
            under_price REAL,
            home_score INTEGER,
            away_score INTEGER,
            period_label TEXT,
            clock TEXT,
            raw_json TEXT
        );
        """
    )
    classes = ["BETUAL_NBA", "CYBER_2K26"]
    # enough rows that a full scan is measurable (20k; scan-shaped plans
    # cost seconds here, the seek costs microseconds)
    n_per_class = 10_000
    base = __import__("datetime").datetime(2026, 9, 1)
    for ci, cls in enumerate(classes):
        gid = ci + 1
        # APPEND-ONLY PREMISE (explicit): class 2's timestamps CONTINUE
        # where class 1 ended, so rowid order == captured_at order over
        # the whole table — the premise the rowid-desc walk relies on.
        # (In production each writer stamps captured_at at insert time;
        # the walk's correctness needs the newest complete row to also be
        # the highest-rowid complete row, which this establishes.)
        cls_base = base + __import__("datetime").timedelta(
            minutes=n_per_class * ci)
        conn.execute(
            "INSERT INTO games (source_game_id, classification, status,"
            " first_seen_at, last_seen_at) VALUES (?, ?, 'live', ?, ?)",
            (f"100{gid}", cls, "2026-09-01T00:00:00", "2026-09-26T00:00:00"),
        )
        rows = []
        for i in range(n_per_class):
            ts = (cls_base + __import__("datetime").timedelta(minutes=i)).isoformat() + "Z"
            # production premise: capture is APPEND-ONLY — rows are
            # inserted in chronological order, so rowid order ==
            # captured_at order.  A few old rows are deliberately
            # incomplete (as in production) to prove the walk skips them.
            if i % 97 == 13:
                rows.append((gid, f"100{gid}", cls, ts, None, None, None))
            else:
                rows.append((gid, f"100{gid}", cls, ts, 50 + (i % 60),
                             45 + (i % 55), 1 + (i % 4)))
        conn.executemany(
            "INSERT INTO snapshots (game_id, source_game_id, classification,"
            " captured_at, home_score, away_score, quarter)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def status_db(tmp_path: Path) -> Path:
    return _build_status_db(tmp_path / "pipeline.db")


def _freshness_query_plan(conn: sqlite3.Connection, cls: str) -> str:
    return conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT captured_at FROM snapshots "
        "WHERE classification = ? "
        "ORDER BY captured_at DESC LIMIT 1", (cls,)).fetchall()[0][3]


def test_status_last_snapshot_uses_index_seek_not_scan(status_db, monkeypatch):
    monkeypatch.setattr(v4api, "_db_path", lambda: str(status_db))
    conn = v4api._connect()
    try:
        for cls in ("BETUAL_NBA", "CYBER_2K26"):
            plan = _freshness_query_plan(conn, cls).upper()
            assert "SCAN" not in plan, (
                f"freshness query for {cls} is scan-shaped: {plan!r} — "
                "/status would hang again on the production DB")
    finally:
        conn.close()


def test_db_stats_last_snapshot_matches_ground_truth(status_db, monkeypatch):
    monkeypatch.setattr(v4api, "_db_path", lambda: str(status_db))
    conn = v4api._connect()
    try:
        truth = conn.execute(
            "SELECT MAX(captured_at) FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    stats = v4api._db_stats(v4api._connect(), now)
    assert stats["last_snapshot_at"] == truth
    assert stats["last_snapshot_age_s"] is not None


def test_freshest_game_state_rowid_walk_correct_and_scan_free(
        status_db, monkeypatch):
    """_freshest_game_state must find the newest COMPLETE row via a
    rowid-desc first-match walk (0.1 ms on production), never a
    captured_at MAX/window aggregate (>240 s standalone on production).

    Plan TEXT cannot discriminate a bounded reverse-rowid walk from a
    full scan (SQLite prints 'SCAN SNAPSHOTS' for both), so the pins are
    (a) the source shape — rowid-desc walk present, captured_at
    aggregates absent — and (b) the functional contract: incomplete
    newer rows are skipped, the newest complete row wins.
    """
    import inspect
    src_lines = inspect.getsource(v4api._freshest_game_state).splitlines()
    code = "\n".join(l for l in src_lines if not l.lstrip().startswith("#"))
    assert "ORDER BY rowid DESC" in code, (
        "_freshest_game_state lost the rowid-desc walk — /status would "
        "hang again on the production DB")
    assert "MAX(captured_at)" not in code
    assert "captured_at >= ?" not in code

    monkeypatch.setattr(v4api, "_db_path", lambda: str(status_db))
    wconn = sqlite3.connect(status_db)  # writable handle for the seed rows
    try:
        # APPEND-ONLY PREMISE, asserted: rowid order == captured_at order
        violations = wconn.execute(
            "SELECT COUNT(*) FROM snapshots s WHERE EXISTS ("
            "  SELECT 1 FROM snapshots p WHERE p.rowid = s.rowid - 1"
            "  AND p.captured_at > s.captured_at)").fetchone()[0]
        assert violations == 0, (
            f"fixture violates append-only premise ({violations} inversions) "
            "— the rowid walk's correctness depends on it")
        # NEWEST ROWS ARE INCOMPLETE (stale SPA tail as in production):
        #   rowid 19999 … complete
        #   rowid 20000 complete   ← newest COMPLETE (expected result)
        #   rowid 20001 incomplete
        #   rowid 20002 incomplete (newest row overall)
        # The walk must skip both incomplete rows and return rowid 20000's
        # captured_at — proving skip + correctness, not query shape alone.
        wconn.executemany(
            "INSERT INTO snapshots (game_id, source_game_id, classification,"
            " captured_at, home_score, away_score, quarter)"
            " VALUES (1, '1001', 'BETUAL_NBA', ?, NULL, NULL, NULL)",
            [("2026-09-27T00:00:00Z",), ("2026-09-27T00:01:00Z",)])
        wconn.commit()
        truth = wconn.execute(
            "SELECT MAX(captured_at) FROM snapshots "
            "WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND quarter IS NOT NULL").fetchone()[0]
        newest_overall = wconn.execute(
            "SELECT captured_at, home_score FROM snapshots "
            "ORDER BY rowid DESC LIMIT 1").fetchone()
        assert newest_overall[1] is None, "newest fixture row must be incomplete"
    finally:
        wconn.close()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    value = v4api._freshest_game_state(v4api._connect(), now)
    assert value["source"] == "dom"
    assert value["state_observed_at"] == truth, (
        "walk must return the newest COMPLETE row, skipping newer "
        "incomplete rows")
    assert value["state_age_seconds"] is not None


def test_db_stats_freshness_query_is_subsecond_on_production_shaped_db(
        status_db, monkeypatch):
    """The exact query shape _db_stats runs, timed: a regression back to
    the scan fails this on 20k rows (scan ≈ seconds, seek ≈ ms)."""
    monkeypatch.setattr(v4api, "_db_path", lambda: str(status_db))
    conn = v4api._connect()
    try:
        classes = [r["classification"] for r in conn.execute(
            "SELECT classification FROM games GROUP BY classification")]
        t0 = time.perf_counter()
        for cls in classes:
            conn.execute(
                "SELECT captured_at FROM snapshots "
                "WHERE classification = ? "
                "ORDER BY captured_at DESC LIMIT 1", (cls,)).fetchone()
        elapsed = time.perf_counter() - t0
    finally:
        conn.close()
    assert elapsed < 0.5, f"freshness seeks took {elapsed:.3f}s — scan regression"


# ────────────────────────────────────────────────────────────────────────
# 3. Collector self-watchdog: gating + liveness
# ────────────────────────────────────────────────────────────────────────

class _NotifyLog:
    def __init__(self):
        self.states: list[str] = []

    def __call__(self, state: str) -> bool:
        self.states.append(state)
        return True


def _make_collector(monkeypatch, *, tick_s: float, env: dict) -> PokerBetCollector:
    for k in ("WATCHDOG_USEC", "WATCHDOG_SEC", "NOTIFY_SOCKET"):
        monkeypatch.delenv(k, raising=False)
    notify_log = _NotifyLog()
    monkeypatch.setattr(colmod, "_sd_notify", notify_log)
    c = PokerBetCollector.__new__(PokerBetCollector)  # skip heavy __init__
    c.tick_s = tick_s
    c._running = False
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return c, notify_log


def _start(c: PokerBetCollector, monkeypatch) -> None:
    """Exercise start()'s REAL watchdog wiring without Playwright: mock
    the heavyweight init pieces and release start() as soon as READY=1
    has been sent (right before the fast loop would begin)."""
    c._watchdog_stop = threading.Event()
    c._watchdog_thread = None
    c._last_fast_completed_at = 0.0
    c._restore_tracked = lambda: 0
    c._betual_restore_state = lambda: None
    c._deviation = None
    c._start_slow_worker = lambda: None
    c._stop_slow_worker = lambda: None
    c._next_tick_target = 0.0

    class _FakePW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    c._new_session = lambda: object()  # one fake "page"
    c._page_content_timed = lambda p, label="", timeout_s=None: "<html></html>"  # fake page content
    # loop-body + finally-block attributes normally created in __init__
    from collections import deque
    c._prev_fast_started_at = None
    c._browser_started_at = time.monotonic()
    c._tick_stats = {
        "fast_cycle_interval_ms": deque(),
        "sleep": deque(),
        "cycle": deque(),
    }
    c._fast_cycle_overruns = 0
    c._browser = None
    entered_loop = threading.Event()

    def _fake_tick(page):
        entered_loop.set()          # we are inside the fast loop body
        c._running = False          # end start() cleanly after this pass
        return page

    c._tick = _fake_tick
    t = threading.Thread(target=c.start, daemon=True)
    t.start()
    assert entered_loop.wait(10), "start() never reached the fast loop"
    t.join(10)
    # determinism: when the watchdog armed, wait until its thread has run
    # its entry code (it computes _startup_deadline there) so tests that
    # overwrite it cannot be raced by a late thread start.
    if getattr(c, "_watchdog_enabled", False):
        for _ in range(100):
            if getattr(c, "_startup_deadline", 0.0):
                break
            time.sleep(0.02)
        assert getattr(c, "_startup_deadline", 0.0), (
            "watchdog thread never computed _startup_deadline")


def test_ready1_sent_after_successful_init(monkeypatch):
    """READY=1 must be emitted by start() AFTER init succeeds (slow worker
    + browser session), never before and never on failure."""
    c, log = _make_collector(monkeypatch, tick_s=5.0,
                             env={"WATCHDOG_USEC": "90000000", "NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    ready = [s for s in log.states if s == "READY=1"]
    assert len(ready) == 1, f"expected exactly one READY=1, got {log.states!r}"
    c._watchdog_stop.set()


def test_watchdog_arms_only_under_systemd(monkeypatch):
    c, log = _make_collector(monkeypatch, tick_s=5.0,
                             env={"WATCHDOG_USEC": "90000000", "NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    assert c._watchdog_enabled is True
    c._watchdog_stop.set()


def test_watchdog_disarmed_without_notify_socket(monkeypatch):
    c, log = _make_collector(monkeypatch, tick_s=5.0, env={"WATCHDOG_USEC": "90000000"})
    _start(c, monkeypatch)
    assert c._watchdog_enabled is False
    # no WATCHDOG=1 may ever be emitted without NOTIFY_SOCKET
    assert not [s for s in log.states if s.startswith("WATCHDOG=1")]
    c._watchdog_stop.set()


def test_watchdog_disarmed_without_watchdog_usec(monkeypatch):
    c, log = _make_collector(monkeypatch, tick_s=5.0,
                             env={"NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    assert c._watchdog_enabled is False
    assert not [s for s in log.states if s.startswith("WATCHDOG=1")]
    c._watchdog_stop.set()


def test_watchdog_disarmed_for_test_cadence(monkeypatch):
    c, log = _make_collector(monkeypatch, tick_s=0.1,
                             env={"WATCHDOG_USEC": "90000000", "NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    assert c._watchdog_enabled is False
    c._watchdog_stop.set()


def test_watchdog_pets_only_when_fast_cycle_fresh(monkeypatch):
    """Silent when no fast cycle ever completed AND the startup grace has
    expired, silent when stale, WATCHDOG=1 ping when fresh (READY=1
    already sent by start()).

    2026-09-29 contract update: a fresh process now pings through the
    BOUNDED startup grace window (watchdog restart-storm forensics);
    this test therefore pins the POST-grace state, and the grace window
    itself is pinned by test_startup_grace_pings_only_while_bounded.
    """
    c, log = _make_collector(monkeypatch, tick_s=5.0,
                             env={"WATCHDOG_USEC": "90000000", "NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    # _start()'s single fake fast tick completes, which legitimately sets
    # the fresh-completion marker; reset it — this test simulates
    # "no fast cycle has EVER completed" (the true post-restart state).
    c._last_fast_completed_at = 0.0
    # ...and the grace window already expired (post-grace wedge state).
    c._startup_deadline = 0.0
    log.states.clear()  # drop READY=1 and any ping on the fake tick

    # never completed a fast cycle, grace expired → the dog must starve
    time.sleep(colmod.WATCHDOG_POLL_DEFAULT_S + 0.3)
    assert log.states == []

    # fresh completion → pet the dog: every ping is WATCHDOG=1 (what
    # actually resets systemd's WatchdogSec timer) + a STATUS line
    c._last_fast_completed_at = time.monotonic()
    time.sleep(colmod.WATCHDOG_POLL_DEFAULT_S + 0.3)  # heartbeat window
    assert log.states, "watchdog never pinged despite fresh fast cycle"
    assert all(s.startswith("WATCHDOG=1") for s in log.states)
    assert all("STATUS=fast cycle complete" in s for s in log.states)

    # stale beyond FAST_LIVENESS_FACTOR × tick → silence again
    log.states.clear()
    c._last_fast_completed_at = (
        time.monotonic() - colmod.FAST_TICK_S * colmod.FAST_LIVENESS_FACTOR - 1)
    time.sleep(colmod.WATCHDOG_POLL_DEFAULT_S + 0.3)
    assert log.states == []
    c._watchdog_stop.set()


def test_gating_block_in_start_matches_test_replica():
    """The test replica above must mirror start()'s real gating block."""
    import inspect
    src = inspect.getsource(PokerBetCollector.start)
    for token in ("WATCHDOG_USEC", "NOTIFY_SOCKET",
                  "WATCHDOG_POLL_DEFAULT_S", "_watchdog_loop"):
        assert token in src, f"start() gating drift: missing {token}"


# ────────────────────────────────────────────────────────────────────────
# 4. Bounded startup grace (2026-09-29 watchdog restart-storm follow-up)
# ────────────────────────────────────────────────────────────────────────

def test_startup_grace_seconds_bounded_by_watchdogsec():
    """min(60s, WatchdogSec / 2); unknown/invalid values keep the 60s
    default (the caller may only ping MORE, never less)."""
    assert colmod._startup_grace_seconds("90000000") == 45.0   # 90s dog
    assert colmod._startup_grace_seconds("300000000") == 60.0  # 300s dog → un-capped
    assert colmod._startup_grace_seconds(None) == 60.0
    assert colmod._startup_grace_seconds("0") == 60.0
    assert colmod._startup_grace_seconds("garbage") == 60.0


def _wait_until(pred, timeout: float = 12.0) -> bool:
    """Deterministic wait for an asynchronous watchdog-thread effect (the
    thread polls on its own 5s cadence — fixed sleeps race it)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_startup_grace_pings_only_while_bounded(monkeypatch):
    """A fresh process with NO completed fast cycle pings through the
    grace window (STATUS says so), then goes SILENT the moment the
    bounded window expires — steady-state liveness stays cycle-gated."""
    c, log = _make_collector(monkeypatch, tick_s=5.0,
                             env={"WATCHDOG_USEC": "90000000", "NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    # simulate a process whose first fast cycle has not completed yet;
    # the window must span at least one full poll pass
    c._last_fast_completed_at = 0.0
    c._startup_deadline = (
        time.monotonic() + colmod.WATCHDOG_POLL_DEFAULT_S + 3.0)
    log.states.clear()

    assert _wait_until(lambda: log.states), (
        "grace window must keep the dog fed on a slow start")
    assert all(s.startswith("WATCHDOG=1") for s in log.states)
    assert all("STATUS=startup grace" in s for s in log.states)

    # the window is BOUNDED: once expired, silence (the dog starves)
    c._startup_deadline = time.monotonic() - 1.0
    log.states.clear()
    assert _wait_until(lambda: c._starving_now), (
        "an expired grace with no completed cycle must reach starvation")
    assert log.states == []

    # and the first completed cycle ends the grace story for good
    c._last_fast_completed_at = time.monotonic()
    assert _wait_until(
        lambda: any("fast cycle complete" in s for s in log.states))
    c._watchdog_stop.set()


def test_starvation_warning_fires_once_per_episode(monkeypatch, caplog):
    """The STOPPING warning must fire ONCE per starvation episode (not on
    every 5s poll pass) and the resume line must actually be reachable —
    the pre-fix starving flag was never propagated (251 duplicate
    warnings across the two 2026-09-29 storms)."""
    c, log = _make_collector(monkeypatch, tick_s=5.0,
                             env={"WATCHDOG_USEC": "90000000", "NOTIFY_SOCKET": "@blm"})
    _start(c, monkeypatch)
    c._last_fast_completed_at = 0.0
    c._startup_deadline = 0.0  # grace expired → starving
    log.states.clear()

    with caplog.at_level(logging.INFO, logger="blm_v4.collector"):
        # starvation begins: the ONE STOPPING warning of this episode
        # fires on the pass that enters starvation; further passes in
        # the same episode must stay silent
        assert _wait_until(lambda: c._starving_now)
        time.sleep(2 * colmod.WATCHDOG_POLL_DEFAULT_S + 0.5)
        stopping = caplog.text.count("STOPPING WATCHDOG")
        assert stopping == 1, (
            f"expected exactly 1 STOPPING warning per episode, got {stopping}")

        # recovery: the resume line must fire exactly once
        caplog.clear()
        c._last_fast_completed_at = time.monotonic()
        assert _wait_until(lambda: "fresh again" in caplog.text)
        time.sleep(colmod.WATCHDOG_POLL_DEFAULT_S + 0.5)
        assert caplog.text.count("fresh again") == 1, (
            "recovery must log 'fresh again — resuming' exactly once")
    c._watchdog_stop.set()
