"""2026-10-04 directive — Betual persistence THROUGHPUT fix (batched writes).

Measured root cause: every store write did ``open → execute → commit → close``
per row (~150 rows/s, the per-row commit dominated) — below the WS producer
rate, so the bounded worker queue overflowed and DROPPED observations.
``PokerBetStore.batch()`` buffers a thread's writes and flushes them in ONE
transaction (~50k rows/s).  The drain worker wraps its pass in ``batch()``.

These tests prove the batch path is EQUIVALENT to the per-row path and that the
worker now outruns a representative producer.
"""
import os
import queue
import tempfile
import threading
import time

import pytest

import blm_v4.collector as col
from blm_v4.betual_dataset import BetualDataset
from blm_v4.collector import PokerBetCollector
from blm_v4.models import PokerBetGame
from blm_v4.storage import PokerBetStore

MARKET_TS = "2026-10-04T12:00:00.000000Z"


# ── harness ─────────────────────────────────────────────────────────

def fresh(gid="900100"):
    tmp = tempfile.mkdtemp(prefix="hermes-batch-")
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game = PokerBetGame(source_game_id=gid, classification="BETUAL_NBA",
                        competition="Betual NBA", game_family="betual",
                        home_team="A", away_team="B")
    store.upsert_game(game)
    return store, game


def _conn(store):
    return store._connect()


def _count(store, table):
    con = _conn(store)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _rows(store, table, order="rowid"):
    con = _conn(store)
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        con.close()


def line_obs(gid, i, line=None, market="m1", ts=None):
    return {
        "source_game_id": gid, "classification": "BETUAL_NBA",
        "captured_at": ts or f"2026-10-04T12:00:{i % 60:02d}.{i:06d}Z",
        "market_id": market, "market_name": "Total Points",
        "period": "full_game", "line": 155.5 if line is None else line,
        "line_previous": 155.0, "line_change": 0.5,
        "seconds_since_previous_line": 3.0, "line_velocity": 0.16,
        "internal_game_time": 120.0, "internal_elapsed_seconds": 300.0,
        "quarter": 3, "quarter_remaining_seconds": 60.0,
        "home_score": 61, "away_score": 55, "total_score": 116,
        "score_at_observation": 114, "betual_displayed_clock": "05:00",
        "over_price": 1.9, "under_price": 1.9, "raw": {"market_id": market},
    }


def quarter_obs(gid, i, line=None):
    return {
        "game_id": None, "source_game_id": gid, "classification": "BETUAL_NBA",
        "captured_at": f"2026-10-04T12:01:{i % 60:02d}.{i:06d}Z",
        "market_id": f"q{i % 20}", "market_type": "OverUnder",
        "market_name": "Quarter Total", "market_period": "Q3",
        "period_number": 3, "line_value": 55.5 if line is None else line,
        "over_price": 1.85, "under_price": 1.95, "home_score": 30,
        "away_score": 28, "period_label": "3rd Quarter", "clock": "04:30",
        "raw": {"market_id": f"q{i % 20}"},
    }


# ── 1. batch session semantics ──────────────────────────────────────

def test_batch_buffers_until_exit_then_flushes_once():
    store, g = fresh()
    with store.batch():
        store.insert_betual_line_observation(line_obs(g.source_game_id, 0))
        store.upsert_betual_timer(
            source_game_id=g.source_game_id, game_start_wall=1700.0,
            observed_at_wall=1700.0, quarter_seconds=600.0, break_seconds=120.0,
            timer_model="calibrated", last_home=61, last_away=55,
            last_line=155.5, last_line_at=1700.0, last_capture_at=MARKET_TS)
        # buffered — a separate reader (own connection) sees NOTHING yet
        assert _count(store, "betual_line_observations") == 0
        assert _count(store, "betual_game_timers") == 0
    # one flush on exit — both rows now present
    assert _count(store, "betual_line_observations") == 1
    assert _count(store, "betual_game_timers") == 1


def test_nested_batch_shares_one_flush():
    store, g = fresh()
    with store.batch():
        store.insert_betual_line_observation(line_obs(g.source_game_id, 0))
        with store.batch():
            store.insert_betual_line_observation(line_obs(g.source_game_id, 1))
        assert _count(store, "betual_line_observations") == 0  # inner didn't flush
    assert _count(store, "betual_line_observations") == 2      # outer flushed both


# ── 2-4. equivalence to the per-row path (all three writers) ────────

def test_line_batch_equivalent_to_per_row():
    a, ga = fresh()
    b, gb = fresh()
    for i in range(50):
        a.insert_betual_line_observation(line_obs(ga.source_game_id, i))
    with b.batch():
        for i in range(50):
            b.insert_betual_line_observation(line_obs(gb.source_game_id, i))
    A = _conn(a); B = _conn(b)
    try:
        sa = A.execute("SELECT * FROM betual_line_observations ORDER BY rowid").fetchall()
        sb = B.execute("SELECT * FROM betual_line_observations ORDER BY rowid").fetchall()
    finally:
        A.close(); B.close()
    assert [tuple(x) for x in sa] == [tuple(x) for x in sb]
    assert len(sa) == 50


def test_quarter_batch_equivalent_to_per_row():
    a, ga = fresh()
    b, gb = fresh()
    for i in range(50):
        a.insert_quarter_market_observation(quarter_obs(ga.source_game_id, i))
    with b.batch():
        for i in range(50):
            b.insert_quarter_market_observation(quarter_obs(gb.source_game_id, i))
    A = _conn(a); B = _conn(b)
    try:
        sa = A.execute("SELECT * FROM quarter_market_observations ORDER BY rowid").fetchall()
        sb = B.execute("SELECT * FROM quarter_market_observations ORDER BY rowid").fetchall()
    finally:
        A.close(); B.close()
    assert [tuple(x) for x in sa] == [tuple(x) for x in sb]
    assert len(sa) == 50


def test_timer_batch_matches_per_row_and_last_write_wins():
    a, ga = fresh()
    b, gb = fresh()
    for i in range(20):                      # repeated upsert, same game
        a.upsert_betual_timer(
            source_game_id=ga.source_game_id, game_start_wall=1700.0 + i,
            observed_at_wall=1700.0, quarter_seconds=600.0, break_seconds=120.0,
            timer_model="calibrated", last_home=None, last_away=None,
            last_line=150.0 + i, last_line_at=1700.0, last_capture_at=MARKET_TS)
    with b.batch():
        for i in range(20):
            b.upsert_betual_timer(
                source_game_id=gb.source_game_id, game_start_wall=1700.0 + i,
                observed_at_wall=1700.0, quarter_seconds=600.0,
                break_seconds=120.0, timer_model="calibrated",
                last_home=None, last_away=None, last_line=150.0 + i,
                last_line_at=1700.0, last_capture_at=MARKET_TS)
    A = _conn(a); B = _conn(b)
    try:
        ra = dict(A.execute("SELECT * FROM betual_game_timers").fetchone())
        rb = dict(B.execute("SELECT * FROM betual_game_timers").fetchone())
    finally:
        A.close(); B.close()
    assert ra["last_line"] == rb["last_line"] == 169.0   # last write wins
    assert _count(a, "betual_game_timers") == 1 and _count(b, "betual_game_timers") == 1


# ── 5. dedup preserved through the batch ────────────────────────────

def test_batch_dedup_identical_rows_collapse():
    store, g = fresh()
    dup = line_obs(g.source_game_id, 0, market="m1", ts=MARKET_TS, line=155.5)
    with store.batch():
        for _ in range(5):
            store.insert_betual_line_observation(dict(dup))   # identical obs
        store.insert_betual_line_observation(
            line_obs(g.source_game_id, 0, market="m1",
                     ts="2026-10-04T12:00:05.000000Z", line=156.0))  # distinct ts
    assert _count(store, "betual_line_observations") == 2   # 5 dups -> 1, +1


# ── 6. FIFO preserved ───────────────────────────────────────────────

def test_batch_fifo_order_preserved():
    store, g = fresh()
    with store.batch():
        for i in range(10):
            store.insert_betual_line_observation(line_obs(g.source_game_id, i))
    rows = _rows(store, "betual_line_observations")
    assert [r["captured_at"] for r in rows] == [
        f"2026-10-04T12:00:{i:02d}.{i:06d}Z" for i in range(10)]


# ── 9. failure isolation: one bad row must not lose the batch ────────

def test_batch_flush_failure_isolates_bad_row():
    store, g = fresh()
    good = [line_obs(g.source_game_id, i) for i in range(5)]
    with store.batch() as buf:
        for o in good:
            store.insert_betual_line_observation(o)
        # a row that violates NOT NULL at the DB (fails only during the flush)
        buf.append((
            "INSERT INTO betual_line_observations "
            "(source_game_id, classification, captured_at, market_id) "
            "VALUES (?, ?, ?, ?)", (None, "BETUAL_NBA", MARKET_TS, "mX")))
    # whole-batch commit fails -> per-row fallback saves all 5 good rows
    assert _count(store, "betual_line_observations") == 5


# ── 11. alert path stays synchronous / unbuffered ───────────────────

def test_alert_path_upsert_market_observation_not_buffered():
    store, g = fresh()
    obs = {"source_game_id": g.source_game_id, "game_id": None,
           "captured_at": MARKET_TS, "market_type": "MatchTotal",
           "market_name": "Total Points", "line_value": 155.5,
           "home_score": 61, "away_score": 55, "over_price": 1.9,
           "under_price": 1.9, "raw": {}}
    with store.batch():
        store.upsert_market_observation(obs)
        # committed IMMEDIATELY — independent of the batch (alert data intact)
        assert _count(store, "market_observations") == 1
    assert _count(store, "market_observations") == 1


# ── worker-level: batching wired in + throughput ────────────────────

def _mk_collector(store, game):
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
    c._betual_line_sync()
    return c


def _wait(pred, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_controlled_burst_drains_to_zero_and_outruns_producer():
    """A burst within the queue capacity drains to an empty queue with zero
    drops, and the worker's drain rate far exceeds the production producer."""
    store, game = fresh()
    c = _mk_collector(store, game)
    c._start_betual_line_worker()
    N = 1500                       # < BETUAL_LINE_QUEUE_MAX (2000): no backpressure
    t0 = time.time()
    for i in range(N):
        c._enqueue_betual_line(game, line_obs(game.source_game_id, i),
                               "full_game")
    assert _wait(lambda: c.betual_line_worker_stats()["queue_depth"] == 0
                 and c.betual_line_worker_stats()["persisted"] >= N - 5)
    dt = max(time.time() - t0, 1e-6)
    st = c.betual_line_worker_stats()
    c._stop_betual_line_worker()
    rate = N / dt
    assert st["dropped"] == 0 and st["failed"] == 0, st
    # per-row ceiling was ~75 line-jobs/s; the batch path must dwarf a
    # representative producer (~67/s observed peak).
    assert rate > 500, f"worker drain {rate:.0f} jobs/s must exceed producer"
    assert _count(store, "betual_line_observations") >= N - 5


def test_worker_outruns_representative_paced_producer_zero_drops():
    """Model the real feed: a paced producer (~120/s, above the observed ~67/s
    peak) for 3s while the worker persists — zero drops, queue drains to 0."""
    store, game = fresh()
    c = _mk_collector(store, game)
    c._start_betual_line_worker()
    n = 0
    t0 = time.time()
    while time.time() - t0 < 3.0:
        c._enqueue_betual_line(game, line_obs(game.source_game_id, n),
                               "full_game")
        n += 1
        time.sleep(1.0 / 120)
    _wait(lambda: c.betual_line_worker_stats()["queue_depth"] == 0, timeout=15)
    st = c.betual_line_worker_stats()
    c._stop_betual_line_worker()
    assert st["enqueued"] >= 300, st          # ~120/s * 3s
    assert st["dropped"] == 0, st             # producer never outran the worker
    assert st["queue_depth"] == 0, st         # drained after the burst
    assert _count(store, "betual_line_observations") >= st["persisted"] - 5


def test_shutdown_drains_pending_jobs():
    store, game = fresh()
    c = _mk_collector(store, game)
    c._start_betual_line_worker()
    orig = c._persist_betual_line

    def slow(g, o, p):
        time.sleep(0.05)
        orig(g, o, p)

    c._persist_betual_line = slow
    for i in range(20):
        c._enqueue_betual_line(game, line_obs(game.source_game_id, i),
                               "full_game")
    c._stop_betual_line_worker()                # must drain, not discard
    assert c.betual_line_worker_stats()["persisted"] == 20
    assert _count(store, "betual_line_observations") == 20
