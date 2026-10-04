"""2026-10-04 residual directive — the QUARTER-MARKET WS persistence moved
OFF the Playwright MainThread onto the SAME ``blm-betual-persist`` worker.

PROVEN RESIDUAL ROOT CAUSE (py-spy, live, post-Betual-move): the eu-swarm
``framereceived`` callback ran ``store.insert_quarter_market_observation``
synchronously on Playwright's MainThread (24/25 WS-handler samples) → the fast
tick froze on DB write-lock contention → self-watchdog SIGABRT.

FIX: the existing Betual persist worker now supports two job shapes on one
FIFO queue — a 3-tuple ``(game, obs, period)`` line job and a 4-tuple
``(game, obs, period, qnum)`` quarter job.  ``_ingest_quarter_market_observation``
only enqueues (non-blocking); the worker runs the SAME
``insert_quarter_market_observation`` with the SAME values.  Inline fallback
when the worker is not running preserves the original semantics exactly.

``upsert_market_observation`` (the ALERT-feeding observation path) stays
synchronous — deliberately NOT moved.

The Betual LINE job is covered by test_betual_line_worker_2026_10_04.py; this
harness isolates the QUARTER path (the sibling line enqueue is stubbed out) so
the quarter accounting is unambiguous.  Harness style matches that suite (real
collector methods via ``object.__new__``, no browser).
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


def _mk(store, game, neutralise_line=True):
    """Minimal collector with the quarter WS path wired but no browser.

    ``neutralise_line`` stubs the sibling Betual LINE enqueue so the quarter
    accounting below is unambiguous (that path has its own test suite)."""
    c = PokerBetCollector.__new__(PokerBetCollector)
    c.store = store
    c._instances = {}
    c._tracked = {"CYBER_2K26": {}, "BETUAL_NBA": {}}
    c._track_lock = threading.RLock()
    c._find_tracked = lambda gid: game if gid == game.source_game_id else None
    c._game_db_id = lambda g: store.get_game(g.source_game_id)["id"]
    c._base_id = PokerBetCollector._base_id
    c._q_market_obs = 0
    c._betual_line_sync()          # lazy init of the worker primitives
    if neutralise_line:
        c._enqueue_betual_line = lambda *a, **k: None
    return c


def _qobs(gid="900100", mid="222", name="1st Quarter Total",
          line=38.5, ts="2026-10-04T12:00:00.000000Z"):
    return {
        "source_game_id": gid, "captured_at": ts,
        "market_type": "QuarterTotal", "market_name": name,
        "line_value": line, "home_score": 20, "away_score": 18,
        "period_label": "1st Quarter", "clock": "05:00",
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


def _qrows(store):
    con = store._connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, market_id, market_type, market_period, "
            "period_number, line_value, captured_at "
            "FROM quarter_market_observations ORDER BY id")]
    finally:
        con.close()


def _strip_q(d):
    return {k: v for k, v in d.items() if k != "id"}


# ── 1 + 2. WS ingest enqueues; zero synchronous insert on MainThread ──

def test_quarter_ws_ingest_enqueues_not_writes():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    assert c._betual_line_worker_alive()

    threads = []
    orig = store.insert_quarter_market_observation
    store.insert_quarter_market_observation = (
        lambda o: (threads.append(threading.current_thread().name),
                   orig(o))[1])

    # the WS-handler entry point (on_frame → this) runs on the caller thread
    c._ingest_quarter_market_observation(_qobs())

    assert threads == []                        # req 1+2: no sync SQLite here
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 1)
    assert threads == ["blm-betual-persist"]     # req 3: worker did the write
    assert "MainThread" not in threads
    assert len(_qrows(store)) == 1
    c._stop_betual_line_worker()


# ── worker supports BOTH job shapes (line 3-tuple + quarter 4-tuple) ─

def test_worker_dispatches_both_job_kinds():
    store, game = fresh()
    c = _mk(store, game, neutralise_line=False)
    line_calls, quarter_calls = [], []
    c._persist_betual_line = lambda g, o, p: line_calls.append(p)
    c._persist_quarter_observation = lambda g, o, p, q: quarter_calls.append(
        (p, q))
    c._start_betual_line_worker()

    c._enqueue_betual_line(game, _qobs(), "full_game")          # 3-tuple
    c._enqueue_quarter_observation(game, _qobs(), "Q1", 1)      # 4-tuple
    assert _wait(lambda: line_calls == ["full_game"]
                 and quarter_calls == [("Q1", 1)])

    c._stop_betual_line_worker()


# ── 3 + 5. worker drains N quarter jobs → N persisted rows ───────────

def test_worker_persists_n_quarter_jobs():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    N = 25
    for i in range(N):
        c._enqueue_quarter_observation(
            game, _qobs(mid=str(300 + i), ts=f"2026-10-04T12:00:{i:02d}Z"),
            "Q1", 1)
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == N)
    st = c.betual_line_worker_stats()
    assert st["enqueued"] == N and st["persisted"] == N and st["dropped"] == 0
    assert len(_qrows(store)) == N
    c._stop_betual_line_worker()


# ── 4. persisted quarter args identical to the old (inline) call ─────

def test_persisted_quarter_arguments_identical():
    # inline path (no worker → original synchronous call)
    store_i, game_i = fresh()
    c_i = _mk(store_i, game_i)
    assert not c_i._betual_line_worker_alive()
    c_i._ingest_quarter_market_observation(_qobs())

    # worker path
    store_w, game_w = fresh()
    c_w = _mk(store_w, game_w)
    c_w._start_betual_line_worker()
    c_w._ingest_quarter_market_observation(_qobs())
    assert _wait(lambda: c_w.betual_line_worker_stats()["persisted"] == 1)
    c_w._stop_betual_line_worker()

    assert [_strip_q(r) for r in _qrows(store_i)] == \
           [_strip_q(r) for r in _qrows(store_w)]


# ── 7. queue accounting remains correct ─────────────────────────────

def test_queue_accounting_correct():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    for i in range(10):
        c._enqueue_quarter_observation(
            game, _qobs(mid=str(400 + i), ts=f"2026-10-04T13:00:{i:02d}Z"),
            "Q1", 1)
    assert _wait(lambda: c.betual_line_worker_stats()["enqueued"] == 10)
    assert _wait(lambda: c.betual_line_worker_stats()["queue_depth"] == 0
                 and c.betual_line_worker_stats()["persisted"] == 10)
    st = c.betual_line_worker_stats()
    assert st["enqueued"] == 10 and st["persisted"] == 10
    assert st["dropped"] == 0 and st["failed"] == 0
    assert st["max_queue_depth"] >= 1
    c._stop_betual_line_worker()


# ── 8. persistence failure is observable ────────────────────────────

def test_quarter_persistence_failure_is_observable(caplog):
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    def boom(o):
        raise RuntimeError("simulated DB failure")

    store.insert_quarter_market_observation = boom
    with caplog.at_level(logging.ERROR):
        c._enqueue_quarter_observation(game, _qobs(), "Q1", 1)
        assert _wait(lambda: c.betual_line_worker_stats()["failed"] == 1)
    st = c.betual_line_worker_stats()
    assert st["persisted"] == 0
    assert any("quarter market observation persist failed" in r.message
               for r in caplog.records)
    c._stop_betual_line_worker()


# ── 9. worker failure cannot block the WS callback ──────────────────

def test_worker_failure_cannot_block_ws_callback():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()

    gate = threading.Event()

    def stuck(o):
        gate.wait(timeout=5.0)          # simulate a wedged DB write
        raise RuntimeError("wedged")

    store.insert_quarter_market_observation = stuck
    t0 = time.monotonic()
    for i in range(100):                 # far more than the worker can drain
        c._ingest_quarter_market_observation(
            _qobs(mid=str(500 + i), ts=f"2026-10-04T14:00:{i:02d}Z"))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"WS callback blocked for {elapsed:.3f}s"
    # one WS callback enqueues exactly one quarter job (+ the stubbed line job)
    assert c.betual_line_worker_stats()["enqueued"] == 100
    gate.set()
    c._stop_betual_line_worker()


# ── 11. queue overflow is observable (never a silent drop) ──────────

def test_quarter_queue_overflow_is_observable(caplog):
    store, game = fresh()
    c = _mk(store, game)
    c._betual_line_worker_thread = None
    c._betual_line_worker_alive = lambda: True       # force the enqueue branch
    c._betual_line_queue = queue.Queue(maxsize=1)
    c._betual_line_queue.put_nowait(("game", {}, "full_game"))

    with caplog.at_level(logging.ERROR):
        c._enqueue_quarter_observation(game, _qobs(), "Q1", 1)

    st = c.betual_line_worker_stats()
    assert st["dropped"] == 1 and st["enqueued"] == 0
    assert any("OVERFLOW" in r.message for r in caplog.records)


# ── 12. normal operation → zero dropped ─────────────────────────────

def test_quarter_normal_operation_zero_dropped():
    store, game = fresh()
    c = _mk(store, game)
    c._start_betual_line_worker()
    for i in range(50):
        c._enqueue_quarter_observation(
            game, _qobs(mid=str(600 + i), ts=f"2026-10-04T15:00:{i:02d}Z"),
            "Q1", 1)
    assert _wait(lambda: c.betual_line_worker_stats()["persisted"] == 50)
    st = c.betual_line_worker_stats()
    assert st["dropped"] == 0
    from blm_v4.collector import BETUAL_LINE_QUEUE_MAX
    assert st["max_queue_depth"] < BETUAL_LINE_QUEUE_MAX // 10
    c._stop_betual_line_worker()


# ── 10. no alert-path operation was moved ───────────────────────────

def test_alert_path_upsert_market_observation_not_moved():
    """``upsert_market_observation`` (the alert-feeding observation path) must
    remain a DIRECT synchronous store call — it must NOT be queued to the
    Betual persist worker, and the worker must never touch it."""
    import inspect
    # the worker's quarter job must not reference the alert-path call
    for fn in ("_persist_quarter_observation", "_enqueue_quarter_observation"):
        body = inspect.getsource(getattr(PokerBetCollector, fn))
        assert "upsert_market_observation" not in body
    # and the alert-feeding ingest still performs the synchronous upsert
    ws_body = inspect.getsource(PokerBetCollector._ingest_ws_observation)
    assert "upsert_market_observation" in ws_body
