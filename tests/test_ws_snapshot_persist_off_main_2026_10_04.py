"""2026-10-04 — WS snapshot-bridge persistence MUST run off the Playwright
MainThread.

Root cause (py-spy, live): the eu-swarm frame callback (on_frame →
_ingest_ws_observation) ran a synchronous ``storage.insert_snapshot`` on the
Playwright event-loop thread.  When the DB was write-locked (server scorecard
/ deviation backfill), the entire fast loop froze → the self-watchdog
SIGABRT-restarted the unit every ~2.5 min.  The fix routes the bridge insert
through the dedicated ``blm-ws-persist`` worker; the frame handler only
throttles + enqueues.  These tests pin that split plus the dedup / bounds.

No alert, checkpoint, discovery, or persistence *semantics* change: the row
accepted, ``_ws_snap_last``, the snapshot counter, and ``_record_clean`` all
advance on the SAME acceptance condition as before — only the thread differs.
"""
from __future__ import annotations

import inspect
import queue
import threading
from unittest.mock import MagicMock

from blm_v4 import collector as colmod
from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector, utcnow_iso
from blm_v4.models import PokerBetGame

CYBER = Classification.CYBER_2K26
GID = "31060001"


def _make_collector() -> PokerBetCollector:
    """Minimal collector with every external I/O mocked out."""
    c = PokerBetCollector.__new__(PokerBetCollector)
    c._ws_market_last = {}
    c._ws_snap_last = {}
    c._last_market_at = {}
    c._end_state_terminal = {}
    c._final_capture_gids = set()
    c._instances = {}
    c._ws_lifecycle_suppressed = 0
    c.stats = {"snapshots": 0}
    c.store = MagicMock()
    c.store.insert_snapshot = MagicMock(return_value=True)
    c.clean_metrics = None
    c._record_clean = MagicMock()
    c._game_db_id = lambda g: 7
    c._find_tracked = lambda gid: _GAME if gid == GID else None
    # mirror __init__ queue state
    c._ws_snap_inflight = set()
    c._ws_snap_lock = threading.Lock()
    c._ws_snap_queue = queue.Queue(maxsize=colmod.WS_SNAP_QUEUE_MAX)
    c._ws_snap_wake = threading.Event()
    c._ws_snap_stop = threading.Event()
    c._ws_snap_worker_thread = None
    c._ws_snap_dropped = 0
    return c


_GAME = PokerBetGame(
    source_game_id=GID, home_team="Alpha Cyber", away_team="Beta Cyber",
    classification=CYBER.value, status="live")


def _obs(gid: str = GID, line: float = 205.5) -> dict:
    return {
        "source_game_id": gid, "captured_at": "2026-10-04T00:00:00Z",
        "line_value": line, "market_type": "MatchTotal",
        "market_name": "Match Total", "home_score": 50, "away_score": 48,
        "period_label": "2nd Quarter", "clock": "05:00",
        "over_price": 1.9, "under_price": 1.9, "game_id": 7,
    }


class _AliveThread:
    def is_alive(self):
        return True


def test_ingest_enqueues_instead_of_syncing_when_worker_alive():
    """Worker live ⇒ the frame handler must NOT insert on its own thread."""
    c = _make_collector()
    c._ws_snap_worker_thread = _AliveThread()
    c._ingest_ws_observation(_obs())
    c.store.insert_snapshot.assert_not_called()      # nothing persisted inline
    assert c._ws_snap_queue.qsize() == 1             # handed to the worker
    assert c._ws_snap_inflight == {GID}


def test_drain_persists_and_advances_acceptance_state():
    """The worker drain performs the insert + the same state advance."""
    c = _make_collector()
    c._ws_snap_worker_thread = _AliveThread()
    c._ingest_ws_observation(_obs())
    n = c._drain_ws_snapshot_queue()
    assert n == 1
    c.store.insert_snapshot.assert_called_once()
    assert c._ws_snap_last[GID] == "2026-10-04T00:00:00Z"
    assert c.stats["snapshots"] == 1
    c._record_clean.assert_called_once()
    assert c._ws_snap_inflight == set()              # cleared after persist


def test_inline_fallback_when_worker_absent():
    """No worker (unit tests / pre-start) ⇒ original synchronous behaviour."""
    c = _make_collector()
    c._ingest_ws_observation(_obs())
    c.store.insert_snapshot.assert_called_once()     # persisted inline
    assert c._ws_snap_queue.qsize() == 0
    assert c._ws_snap_last[GID] == "2026-10-04T00:00:00Z"


def test_inflight_prevents_duplicate_enqueue():
    """While a gid is still inflight the handler must not re-enqueue it."""
    c = _make_collector()
    c._ws_snap_worker_thread = _AliveThread()
    c._ingest_ws_observation(_obs())
    c._ingest_ws_observation(_obs())                 # same gid, not yet drained
    assert c._ws_snap_queue.qsize() == 1


def test_dedup_window_blocks_repeat_after_persist():
    """A freshly persisted snapshot throttles the next 30 s (unchanged)."""
    c = _make_collector()
    c._ws_snap_worker_thread = _AliveThread()
    c._ingest_ws_observation(_obs())
    c._drain_ws_snapshot_queue()
    assert c._ws_snap_last[GID]                       # persisted
    c._ws_snap_last[GID] = utcnow_iso()               # make the window fresh
    c._ingest_ws_observation(_obs())                  # within 30 s → throttled
    assert c._ws_snap_queue.qsize() == 0


def test_full_queue_drops_and_counts():
    """A saturated queue drops the newest + counts it (bounded memory)."""
    c = _make_collector()
    c._ws_snap_worker_thread = _AliveThread()
    c._ws_snap_queue = queue.Queue(maxsize=1)
    c._enqueue_ws_snapshot(_GAME, object(), "g1", "t")
    c._enqueue_ws_snapshot(_GAME, object(), "g2", "t")   # full → drop
    assert c._ws_snap_dropped == 1
    assert "g2" not in c._ws_snap_inflight               # rolled back
    assert c._ws_snap_queue.qsize() == 1


def test_worker_persists_on_its_own_thread():
    """End-to-end: the insert runs on `blm-ws-persist`, not the caller."""
    c = _make_collector()
    seen = {}
    caller = threading.get_ident()

    def _record(game_id, snap):
        seen["thread"] = threading.get_ident()
        return True

    c.store.insert_snapshot = _record
    c._start_ws_snap_worker()
    try:
        c._ingest_ws_observation(_obs())
        for _ in range(100):
            if "thread" in seen:
                break
            threading.Event().wait(0.05)
    finally:
        c._stop_ws_snap_worker()
    assert "thread" in seen
    assert seen["thread"] != caller
    assert c.stats["snapshots"] == 1


def test_start_stop_worker_lifecycle():
    c = _make_collector()
    c._start_ws_snap_worker()
    assert c._ws_snap_worker_thread is not None
    assert c._ws_snap_worker_thread.is_alive()
    assert c._ws_snap_worker_thread.name == "blm-ws-persist"
    c._stop_ws_snap_worker()
    assert not c._ws_snap_worker_thread.is_alive()


def test_source_insert_not_in_frame_handler_source():
    """Structural guard: _ingest_ws_observation must not itself call the store
    insert on the calling thread (the whole point of the change)."""
    src = inspect.getsource(PokerBetCollector._ingest_ws_observation)
    assert "insert_snapshot" not in src
    assert "_enqueue_ws_snapshot" in src
