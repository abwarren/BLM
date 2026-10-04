"""2026-10-04 residual directive — the LAST synchronous Betual SQLite writes
were moved OFF the Playwright MainThread onto the SAME ``blm-betual-persist``
worker (no second worker, one shared FIFO).

Residuals proven by py-spy (post quarter-offload), still on the MainThread:
  B. ``on_frame -> _betual_maybe_start_ts -> _betual_persist_timer``
     -> ``storage.upsert_betual_timer``   (job kind: 1-tuple ``(game,)``)
  C. ``_mark_ended -> _betual_end_game`` -> ``storage.insert_betual_game_end``
     (job kind: 2-tuple ``(game, parsed)``)

The worker RUNS the SAME ``_betual_persist_timer`` / ``_betual_end_game`` with
the SAME arguments, so ``upsert_betual_timer`` / ``insert_betual_game_end``
receive equivalent inputs.  The 3-tuple line job and the 4-tuple quarter job
are UNCHANGED (covered by the sibling suites).

CRITICAL: ``upsert_market_observation`` (the ALERT-feeding observation path) is
DELIBERATELY left synchronous — it must never be enqueued.

These tests use the REAL collector methods via ``object.__new__`` (no browser),
the same harness style as test_betual_line_worker_2026_10_04.py.
"""
import logging
import os
import queue
import tempfile
import threading
import time

import pytest

import blm_v4.collector as col
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
    c._q_market_obs = 0
    c._q_score_obs = 0
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


def _wait(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _timer_rows(store):
    con = store._connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM betual_game_timers ORDER BY source_game_id")]
    finally:
        con.close()


def _end_rows(store):
    con = store._connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM betual_game_ends ORDER BY id")]
    finally:
        con.close()


def _spy_seq(c):
    """Record the ORDER of worker persist calls (one entry per drained job)."""
    seq = []

    def wrap(name):
        orig = getattr(c, name)

        def inner(*a, **k):
            seq.append(name)
            return orig(*a, **k)
        setattr(c, name, inner)

    for n in ("_persist_betual_timer_job", "_persist_betual_line",
              "_persist_betual_end_job", "_persist_quarter_observation"):
        wrap(n)
    return seq


# ── B: start_ts timer persistence is off the MainThread ─────────────

def test_start_ts_timer_persists_off_mainthread():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    assert c._betual_line_worker_alive()

    threads = []
    orig = store.upsert_betual_timer
    store.upsert_betual_timer = lambda *a, **k: (
        threads.append(threading.current_thread().name), orig(*a, **k))[1]

    c._betual_maybe_start_ts(game, 1_700_000_000.0)

    # requirement 3: the WS callback did NOT persist the timer synchronously
    assert threads == []
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] >= 1)
    # requirement 3: the timer persist ran on the worker, never MainThread
    assert threads and "MainThread" not in threads
    assert threads[-1] == "blm-betual-persist"
    assert len(_timer_rows(store)) == 1
    c._stop_betual_line_worker()


# ── C: ended-game persistence is off the MainThread ─────────────────

def test_ended_game_persists_off_mainthread():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    threads = []
    orig = store.insert_betual_game_end
    store.insert_betual_game_end = lambda row: (
        threads.append(threading.current_thread().name), orig(row))[1]

    c._enqueue_betual_end(game)

    assert threads == []                       # not on the calling thread
    assert _wait(lambda: len(_end_rows(store)) == 1)
    assert threads and "MainThread" not in threads
    assert threads[-1] == "blm-betual-persist"
    c._stop_betual_line_worker()


def test_end_row_is_written_before_the_rec_is_discarded():
    """The worker must write the §12 row (which reads the rec's internal-timer
    anchors) BEFORE discarding the rec — a recreated rec would zero the row."""
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    # adopt start evidence so the rec carries real anchors
    c._betual_maybe_start_ts(game, 1_700_000_000.0)
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] >= 1)
    assert game.source_game_id in c.betual._games

    c._enqueue_betual_end(game)
    assert _wait(lambda: len(_end_rows(store)) == 1)
    # row present, and the rec was discarded AFTER the write
    assert game.source_game_id not in c.betual._games
    row = _end_rows(store)[0]
    assert row["source_game_id"] == game.source_game_id
    assert row["end_evidence"] == "disappeared"
    c._stop_betual_line_worker()


# ── 7. ordering across ALL four job kinds ───────────────────────────

def test_mixed_jobs_preserve_fifo_order():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    seq = _spy_seq(c)

    c._enqueue_betual_timer(game)
    c._enqueue_betual_line(game, _obs(line=100.0), "full_game")
    c._enqueue_betual_end(game)

    assert _wait(lambda: len(seq) == 3)
    assert seq == ["_persist_betual_timer_job", "_persist_betual_line",
                   "_persist_betual_end_job"]
    c._stop_betual_line_worker()


# ── 5/6. existing line + quarter kinds still work through the worker ─

def test_all_job_kinds_persist_n_ops():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    N = 6
    for i in range(N):
        c._enqueue_betual_timer(game)
        c._enqueue_betual_line(game, _obs(line=100.0 + i), "full_game")
        c._enqueue_quarter_observation(game, _obs(line=200.0 + i, mid=str(i)),
                                       "Q1", 1)
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 3 * N)
    st = c.betual_line_worker_stats()
    assert st["enqueued"] == 3 * N and st["dropped"] == 0
    c._stop_betual_line_worker()


# ── 9. failures stay observable ─────────────────────────────────────

def test_timer_and_end_failure_is_observable(caplog):
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    def boom(*a, **k):
        raise RuntimeError("simulated DB failure")

    # break the UNDERLYING persist so the job helper's own try/except fires
    c._betual_persist_timer = boom
    c._betual_end_game = boom
    with caplog.at_level(logging.ERROR):
        c._enqueue_betual_timer(game)
        c._enqueue_betual_end(game)
        assert _wait(lambda: c.betual_line_worker_stats()["failed"] >= 2)
    assert any("betual timer job failed" in r.message for r in caplog.records)
    c._stop_betual_line_worker()


# ── 12. overflow stays observable (never a silent loss) ─────────────

def test_overflow_observable_for_timer_and_end(caplog):
    store, game = fresh()
    c = _mk(store, game)
    c._betual_line_worker_alive = lambda: True
    c._betual_line_queue = queue.Queue(maxsize=1)
    c._betual_line_queue.put_nowait((game,))        # occupy the single slot

    with caplog.at_level(logging.ERROR):
        c._enqueue_betual_timer(game)
        c._enqueue_betual_end(game)

    st = c.betual_line_worker_stats()
    assert st["dropped"] == 2 and st["enqueued"] == 0
    assert sum("OVERFLOW" in r.message for r in caplog.records) >= 2


# ── 11. shutdown drains outstanding timer/end work ──────────────────

def test_shutdown_drains_timer_and_end():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    orig = c._persist_betual_timer_job

    def slow(g):
        time.sleep(0.1)                 # ensure work is still queued at stop
        orig(g)

    c._persist_betual_timer_job = slow
    for _ in range(3):
        c._enqueue_betual_timer(game)
    c._enqueue_betual_end(game)

    c._stop_betual_line_worker()        # must drain, not discard
    assert c.betual_line_worker_stats()["persisted"] == 4
    assert len(_timer_rows(store)) == 1
    assert len(_end_rows(store)) == 1


# ── 14. inline fallback preserves the original semantics ────────────

def test_inline_fallback_timer_and_end_preserve_semantics():
    store, game = fresh()
    c = _mk(store, game)
    assert not c._betual_line_worker_alive()

    c._betual_maybe_start_ts(game, 1_700_000_000.0)   # inline timer persist
    c._enqueue_betual_end(game)                        # inline end persist

    assert len(_timer_rows(store)) == 1
    assert len(_end_rows(store)) == 1
    # end row written, THEN rec discarded — the inline order is preserved
    assert game.source_game_id not in c.betual._games


# ── 13. upsert_market_observation stays SYNCHRONOUS ─────────────────

def test_worker_never_touches_the_alert_path():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    seen = []
    orig = store.upsert_market_observation
    store.upsert_market_observation = lambda o: (
        seen.append(threading.current_thread().name), orig(o))[1]

    c._enqueue_betual_timer(game)
    c._enqueue_betual_line(game, _obs(), "full_game")
    c._enqueue_betual_end(game)
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] >= 3)

    # the ALERT-path persist is never routed through the Betual worker
    assert seen == []
    c._stop_betual_line_worker()


def test_ingest_ws_observation_persists_inline(monkeypatch):
    store, game = fresh()
    c = _mk(store, game)
    # attrs _ingest_ws_observation needs
    c._ws_market_last = {}
    c._end_state_terminal = {}
    c._ws_lifecycle_suppressed = 0
    c._final_capture_gids = set()
    c._last_market_at = {}
    # skip the WS->snapshot bridge so we isolate the observation persist
    monkeypatch.setattr(col, "ws_matchtotal_snapshot", lambda g, o: None)

    threads = []
    orig = store.upsert_market_observation
    store.upsert_market_observation = lambda o: (
        threads.append(threading.current_thread().name), orig(o))[1]

    c._ingest_ws_observation(_obs())

    # requirement 13: still synchronous — it ran on the CALLING thread
    assert threads == [threading.current_thread().name]
    assert len(store.get_snapshots(game.source_game_id, limit=5)) >= 0


# ── 4. the line-worker suite's contract is untouched (guard) ────────

def test_line_job_three_tuple_shape_unchanged():
    """A raw 3-tuple on the queue is still a Betual LINE job (the shape the
    sibling test_betual_line_worker suite relies on)."""
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    calls = []
    orig = c._persist_betual_line
    c._persist_betual_line = lambda g, o, p: (calls.append(p), orig(g, o, p))[1]

    c._betual_line_queue.put_nowait((game, _obs(line=7.0), "full_game"))
    c._betual_line_wake.set()
    assert _wait(lambda: calls == ["full_game"])
    c._stop_betual_line_worker()
