"""PACE REFERENCE WORKER — precomputed league references for /api/v4/live.

Background (diagnostic 2026-09-25): ``v4_live`` called
``competition_pace_reference`` and ``q3_pace_reference`` inline per request.
On the production database (blm_pokerbet.db, 9.4 GB, snapshots ≈ 2M rows)
the Q3 reference's whole-table GROUP BY takes minutes, so every cache-miss
request hung the sync worker pool and the dashboard froze on its initial
placeholders.  The scan cost is O(whole history) but its output only moves
when a game settles — exactly the profile the StatsWorker already serves
with a background worker + "last completed payload" route.

THE WORKER (this module) mirrors StatsWorker's shape:

  * a daemon thread computes BOTH references from an OWN read-only
    connection — never a route thread, never the event loop, never the
    collector's connection;
  * ``should_scan`` is a cheap change-detector (MAX(rowid)-bounded reads)
    so a quiet box skips the scan entirely: the references can only move
    when a game settles or a new snapshot lands;
  * ``v4_live`` reads the last completed payload and NEVER computes one;
    until the first scan lands the route reports ``warmup: true`` with
    empty references — the alert layer's documented fail-closed behavior
    (no reference ⇒ no alert), honest and self-healing within one scan.

READ-ONLY: this worker opens blm_pokerbet.db with mode=ro only, writes
nothing, and touches no gate, threshold or model input — it only moves
WHERE the same numbers are computed, not WHAT they are.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from blm_v4.live_analytics.competition_pace import competition_pace_reference
from blm_v4.live_analytics.fingerprint_c5 import q3_pace_reference

#: How often the worker re-checks for new data.  The scan itself only runs
#: when the change-detector says the settled population moved.
DEFAULT_SCAN_INTERVAL_S = 60.0

#: A completed payload older than this is re-scanned regardless (backstop
#: against a change-detector miss, mirroring StatsWorker's max_age).
DEFAULT_MAX_AGE_S = 900.0


class PaceReferenceWorker:
    """Precompute both league references on a background thread.

    ``consumer`` (optional) is called under the worker's lock with each
    freshly computed ``(pace_reference, q3_reference)`` pair — the API
    module registers a setter so ``v4_live`` reads the latest completed
    payload without polling.
    """

    def __init__(self,
                 db_path: Path,
                 interval_s: float = DEFAULT_SCAN_INTERVAL_S,
                 max_age_s: float = DEFAULT_MAX_AGE_S,
                 log: Optional[Callable[..., None]] = None,
                 consumer: Optional[Callable] = None) -> None:
        self._db_path = Path(db_path)
        self._interval_s = float(interval_s)
        self._max_age_s = float(max_age_s)
        self._log = log
        self._consumer = consumer
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._latest: Optional[dict] = None
        self._latest_at: float = 0.0
        self._last_key: Optional[tuple] = None
        self._last_error: Optional[str] = None
        self._runs = 0
        self._skips = 0

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> "PaceReferenceWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._loop, name="blm-pace-reference-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        # First pass runs immediately so warmup ends within one scan.
        while not self._stop.is_set():
            self._run_once()
            self._stop.wait(self._interval_s)

    # ── change detection (cheap, indexed, rowid-bounded) ─────────────
    def _change_key(self) -> Optional[tuple]:
        """(games_rowid, results_rowid, snapshots_rowid) via MAX(rowid).

        O(1) on SQLite (rowid is the b-tree key), so the worker can tick
        every minute without touching the data pages the scan reads.
        """
        try:
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=30)
            try:
                games = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM games").fetchone()[0]
                results = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) "
                    "FROM game_results").fetchone()[0]
                snaps = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) "
                    "FROM snapshots").fetchone()[0]
            finally:
                conn.close()
            return (games, results, snaps)
        except sqlite3.Error as exc:
            if self._log is not None:
                self._log.warning("pace_ref_key_failed",
                                  error=f"{type(exc).__name__}: {exc}")
            return None

    def should_scan(self) -> tuple[bool, str]:
        """(scan?, why).  Cheap: three MAX(rowid) reads (~sub-ms)."""
        with self._lock:
            have = self._latest is not None
            age = time.monotonic() - self._latest_at if have else None
            prev = self._last_key
        if not have:
            return True, "first_run"
        if age is not None and age >= self._max_age_s:
            return True, "max_age"
        key = self._change_key()
        if key is None:
            # unreadable key → rescan rather than serve a possibly-stale
            # reference silently forever
            return True, "key_unavailable"
        if key != prev:
            return True, "data_changed"
        return False, "unchanged"

    # ── the scan ─────────────────────────────────────────────────────
    def _run_once(self, force: bool = False) -> bool:
        if not force:
            scan, why = self.should_scan()
            if not scan:
                with self._lock:
                    self._skips += 1
                return False
        else:
            why = "forced"
        t0 = time.monotonic()
        try:
            # mode=ro connect itself validates the file is a database (a
            # corrupt/garbage path must FAIL the scan, never publish an
            # empty-looking success).
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=30)
            conn.execute("SELECT 1").fetchone()   # probe: raises on a non-DB
            try:
                pace = competition_pace_reference(conn)
                q3 = q3_pace_reference(conn)
            finally:
                conn.close()
            # An empty pair on a forced scan means the engines could not
            # read the population (fail closed, never publish success).
            if not pace and not q3:
                raise RuntimeError(
                    "reference scan returned no data for any competition")
        except Exception as exc:                      # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._log is not None:
                self._log.warning("pace_ref_scan_failed",
                                  error=self._last_error)
            return False
        payload = {"pace": pace, "q3": q3}
        with self._lock:
            self._latest = payload
            self._latest_at = time.monotonic()
            self._last_error = None
            self._last_key = self._change_key()
            self._runs += 1
            latest = dict(payload)
        if self._consumer is not None:
            try:
                self._consumer(latest["pace"], latest["q3"])
            except Exception:                          # noqa: BLE001
                # consumer failures must never kill the worker loop
                pass
        if self._log is not None:
            self._log.info(
                "pace_ref_scan",
                elapsed_s=round(time.monotonic() - t0, 1),
                reason=why,
                pace_competitions=len(latest["pace"]),
                q3_competitions=len(latest["q3"]))
        return True

    # ── introspection (status route / tests) ─────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "available": self._latest is not None,
                "runs": self._runs,
                "skips": self._skips,
                "last_error": self._last_error,
                "age_s": (round(time.monotonic() - self._latest_at, 1)
                          if self._latest_at else None),
            }
