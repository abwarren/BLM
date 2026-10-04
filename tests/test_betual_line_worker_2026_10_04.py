"""2026-10-04 directive — Betual line/timer WS persistence moved OFF the
Playwright MainThread.

PROVEN ROOT CAUSE (py-spy, live): the eu-swarm ``framereceived`` callback
ran the synchronous Betual writes — ``storage.insert_betual_line_observation``
and ``storage.upsert_betual_timer`` — on Playwright's MainThread (28/45
samples).  That froze the fast tick whenever the DB was write-locked →
self-watchdog SIGABRT every ~2.5 min.

FIX: a dedicated ``blm-betual-persist`` worker owns those writes; the frame
handler (``on_frame`` → ``_enqueue_betual_line``) only enqueues (bounded,
non-blocking).  The worker calls the SAME ``_betual_record_line()`` with the
SAME arguments, so ``record_line_observation`` / ``upsert_betual_timer``
receive equivalent inputs and ordering + dedup semantics are unchanged.

These tests exercise the REAL collector methods via ``object.__new__`` (no
browser) — the same harness style as test_betual_collector_integration.py.
"""
import logging
import os
import queue
import tempfile
import threading
import time

import pytest

from blm_v4.collector import PokerBetCollector
from blm_v4.models import PokerBetGame
from blm_v4.storage import PokerBetStore


# ── harness ─────────────────────────────────────────────────────────

def fresh(gid="900100"):
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game = PokerBetGame(source_game_id=gid, classification="BETUAL_NBA",
                        competition="Betual NBA", game_family="betual",
                        home_team="A", away_team="B")
    store.upsert_game(game)
    return store, game


def _mk(store, game):
    """Minimal collector with the Betual WS path wired but no browser."""
    from blm_v4.betual_dataset import BetualDataset
    c = PokerBetCollector.__new__(PokerBetCollector)
    c.store = store
    c.betual = BetualDataset(store)
    c._betual_start_ts = {}
    c._betual_last_quarter = {}
    c._instances = {}
    c._tracked = {"CYBER_2K26": {}, "BETUAL_NBA": {}}
    c._track_lock = threading.RLock()
    c._find_tracked = lambda gid: game if gid == game.source_game_id else None
    c._game_db_id = lambda g: store.get_game(g.source_game_id)["id"]
    c._base_id = PokerBetCollector._base_id
    c._betual_line_sync()          # lazy init of the worker primitives
    return c


def _obs(gid="900100", line=155.5, mid="111",
         ts="2026-10-04T12:00:00.000000Z"):
    return {
        "source_game_id": gid, "captured_at": ts,
        "market_type": "MatchTotal", "market_name": "Total Points",
        "line_value": line, "home_score": 61, "away_score": 55,
        "quarter": 3, "clock": "05:00",
        "over_price": 1.9, "under_price": 1.9,
        "raw": {"market_id": mid},
    }


def _wait(pred, timeout=20.0):
    """Wait for pred() — generous, because a drained observation costs TWO
    SQLite commits (line obs + game timer) and disk fsync latency varies."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _lines(store):
    con = store._connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, period, line, market_id, home_score, away_score, "
            "over_price, under_price FROM betual_line_observations "
            "ORDER BY id")]
    finally:
        con.close()


def _timer_rows(store):
    con = store._connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM betual_game_timers ORDER BY source_game_id")]
    finally:
        con.close()


# ── 1 + 2. callback enqueues; zero synchronous MainThread writes ─────

def test_ws_callback_enqueues_not_writes():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    assert c._betual_line_worker_alive()

    line_threads = []
    orig = c._betual_record_line
    c._betual_record_line = lambda g, o, p: (
        line_threads.append(threading.current_thread().name), orig(g, o, p))

    c._enqueue_betual_line(game, _obs(), "full_game")

    # requirement 1: the frame callback did NOT persist synchronously
    assert line_threads == []
    # requirement 3: the queued observation IS persisted by the worker
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 1)
    # requirement 2: every Betual write ran on the worker, never MainThread
    assert line_threads == ["blm-betual-persist"]
    assert "MainThread" not in line_threads
    c._stop_betual_line_worker()


def test_mainthread_performs_zero_synchronous_betual_db_writes():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    timer_threads = []
    sot = store.upsert_betual_timer
    store.upsert_betual_timer = lambda *a, **k: (
        timer_threads.append(threading.current_thread().name), sot(*a, **k))

    c._enqueue_betual_line(game, _obs(line=150.0), "full_game")
    assert timer_threads == []                 # not on the calling thread
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 1)
    assert timer_threads == ["blm-betual-persist"]
    assert "MainThread" not in timer_threads
    c._stop_betual_line_worker()


# ── 3 + 6. worker persists N queued observations ────────────────────

def test_worker_persists_n_queued_observations():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    N = 25
    for i in range(N):
        c._enqueue_betual_line(game, _obs(line=100.0 + i), "full_game")
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == N)
    st = c.betual_line_worker_stats()
    assert st["enqueued"] == N and st["persisted"] == N and st["dropped"] == 0
    assert len(_lines(store)) == N               # requirement 6
    c._stop_betual_line_worker()


# ── 7. ordering preserved ───────────────────────────────────────────

def test_queue_ordering_is_preserved():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    lines = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0]
    for ln in lines:
        c._enqueue_betual_line(game, _obs(line=ln), "full_game")
    assert _wait(lambda: len(_lines(store)) == len(lines))
    persisted = [r["line"] for r in _lines(store)]   # ORDER BY id
    assert persisted == lines
    c._stop_betual_line_worker()


# ── 4 + 5. record_line_observation / upsert_betual_timer equivalence ─

def test_record_line_and_timer_receive_equivalent_inputs():
    # inline path (no worker → falls back to the original sync call)
    store_i, game_i = fresh()
    c_i = _mk(store_i, game_i)
    cap_i = []
    orig_i = c_i._betual_record_line
    c_i._betual_record_line = lambda g, o, p: (
        cap_i.append((g.source_game_id, o.get("line_value"),
                      o.get("captured_at"), p)), orig_i(g, o, p))
    assert not c_i._betual_line_worker_alive()
    c_i._enqueue_betual_line(game_i, _obs(), "full_game")

    # worker path
    store_w, game_w = fresh()
    c_w = _mk(store_w, game_w)
    c_w._start_betual_line_worker()
    cap_w = []
    orig_w = c_w._betual_record_line
    c_w._betual_record_line = lambda g, o, p: (
        cap_w.append((g.source_game_id, o.get("line_value"),
                      o.get("captured_at"), p)), orig_w(g, o, p))
    c_w._enqueue_betual_line(game_w, _obs(), "full_game")
    assert _wait(lambda: c_w.betual_line_worker_stats()["persisted"] == 1)
    c_w._stop_betual_line_worker()

    # requirement 4: identical call inputs (record_line_observation)
    assert cap_i == cap_w
    # requirement 5: identical persisted Betual line rows
    assert _lines(store_i) == _lines(store_w)
    # requirement 5: identical upsert_betual_timer inputs (wall-clock- and
    # id-derived columns are excluded — they are not inputs to the call)
    def _strip(d):
        return {k: v for k, v in d.items()
                if k not in ("id", "updated_at", "last_line_at")}
    assert [_strip(r) for r in _timer_rows(store_i)] \
        == [_strip(r) for r in _timer_rows(store_w)]


# ── 8. dedup semantics unchanged ────────────────────────────────────

def test_dedup_semantics_unchanged():
    """The Betual line path never had application-level dedup: distinct
    observations all persist, while the DB UNIQUE key still collapses
    genuinely identical rows — exactly as before the worker."""
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    c._enqueue_betual_line(game, _obs(line=1.0), "full_game")   # distinct
    c._enqueue_betual_line(game, _obs(line=2.0), "full_game")   # distinct
    c._enqueue_betual_line(game, _obs(line=1.0), "full_game")   # dup of #1
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 3)
    rows = _lines(store)
    # two distinct lines persist; the identical row is collapsed by UNIQUE
    assert sorted(r["line"] for r in rows) == [1.0, 2.0]
    c._stop_betual_line_worker()


# ── 9. persistence failure is observable ────────────────────────────

def test_persistence_failure_is_observable(caplog):
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    def boom(g, o, p):
        raise RuntimeError("simulated DB failure")

    c._betual_record_line = boom
    with caplog.at_level(logging.ERROR):
        c._enqueue_betual_line(game, _obs(line=77.0), "full_game")
        assert _wait(lambda: c.betual_line_worker_stats()["failed"] == 1)
    assert c.betual_line_worker_stats()["persisted"] == 0
    assert any("betual line persist failed" in r.message
               for r in caplog.records)
    c._stop_betual_line_worker()


# ── 10. worker failure cannot block the fast tick ───────────────────

def test_worker_failure_cannot_block_fast_tick():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    gate = threading.Event()

    def stuck(g, o, p):
        gate.wait(timeout=5.0)          # simulate a wedged DB write
        raise RuntimeError("wedged")

    c._betual_record_line = stuck
    t0 = time.monotonic()
    for i in range(100):                # far more than the worker can drain
        c._enqueue_betual_line(game, _obs(line=float(i)), "full_game")
    elapsed = time.monotonic() - t0
    # requirement 10: the enqueues never waited on the stuck worker
    assert elapsed < 0.5, f"enqueue blocked for {elapsed:.3f}s"
    assert c.betual_line_worker_stats()["enqueued"] == 100
    gate.set()
    c._stop_betual_line_worker()


# ── 11. shutdown drains queued work ─────────────────────────────────

def test_shutdown_handles_queued_work():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    orig = c._betual_record_line

    def slow(g, o, p):
        time.sleep(0.1)                 # ensure work is still queued at stop
        orig(g, o, p)

    c._betual_record_line = slow
    for i in range(5):
        c._enqueue_betual_line(game, _obs(line=200.0 + i), "full_game")
    c._stop_betual_line_worker()        # must drain, not discard
    assert len(_lines(store)) == 5


# ── 12. queue overflow is observable ────────────────────────────────

def test_queue_overflow_is_observable(caplog):
    store, game = fresh()
    c = _mk(store, game)
    c._betual_line_worker_thread = None
    # force the "worker active" branch, then occupy a 1-slot queue
    c._betual_line_worker_alive = lambda: True
    c._betual_line_queue = queue.Queue(maxsize=1)
    c._betual_line_queue.put_nowait(("game", {}, "full_game"))

    with caplog.at_level(logging.ERROR):
        c._enqueue_betual_line(game, _obs(line=9.9), "full_game")

    st = c.betual_line_worker_stats()
    assert st["dropped"] == 1 and st["enqueued"] == 0
    assert any("OVERFLOW" in r.message for r in caplog.records)


# ── 13. normal operation → zero dropped ─────────────────────────────

def test_normal_operation_zero_dropped():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    for i in range(50):
        c._enqueue_betual_line(game, _obs(line=300.0 + i), "full_game")
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 50)
    st = c.betual_line_worker_stats()
    assert st["dropped"] == 0
    # a 50-observation burst never approaches the 2000-slot capacity
    from blm_v4.collector import BETUAL_LINE_QUEUE_MAX
    assert st["max_queue_depth"] <= 50
    assert st["max_queue_depth"] < BETUAL_LINE_QUEUE_MAX // 10
    c._stop_betual_line_worker()


# ── 14. inline fallback preserves original semantics ────────────────

def test_inline_fallback_preserves_semantics():
    store, game = fresh()
    c = _mk(store, game)
    assert not c._betual_line_worker_alive()
    # no worker → synchronous inline persist, identical to the old code path
    c._enqueue_betual_line(game, _obs(line=42.0), "full_game")
    rows = _lines(store)
    assert len(rows) == 1 and rows[0]["line"] == 42.0
    assert rows[0]["period"] == "full_game"
