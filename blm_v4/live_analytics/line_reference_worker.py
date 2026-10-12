"""LINE REFERENCE WORKER — precomputed league AVERAGE LINE + per-game first
line for ``/api/v4/live``.

The LINE-MOVEMENT VOLATILITY trade leg (``docs/rag/15_EDGE_FINDINGS.md``
§15.8, ``blm_v4.trade_window.trade_move_reason``) needs, per served game, its
FIRST-captured live total line and its competition's average of those.  The
per-competition average is a whole-table scan of ``clean_observations``
(``blm_metrics_clean.db``, ≈4 GB) — far too expensive for the 5 s dashboard
poll, and it would hang the sync worker pool exactly as the Q3 pace scan once
did.  This worker mirrors :class:`blm_v4.live_analytics.pace_reference_worker.PaceReferenceWorker`:

  * a daemon thread computes the reference from OWN read-only connections —
    never a route thread, never the event loop, never the collector's
    connection;
  * ``should_scan`` is a cheap change-detector (MAX(rowid) on
    ``clean_observations`` and on ``games``) so a quiet box skips the scan
    entirely: the reference can only move when a new observation lands or a
    game's competition slug changes;
  * ``v4_live`` reads the last completed payload and NEVER computes one;
    until the first scan lands the route serves an empty reference — the
    trade leg's documented fail-CLOSED behaviour (no reference ⇒ no flat
    proof ⇒ no trade), honest and self-healing within one scan.

READ-ONLY: both databases are opened ``mode=ro`` only, writes nothing, and
touches no gate, threshold or model input — it only moves WHERE the same
numbers are computed, not WHAT they are.  FAIL SOFT: a scan error keeps the
last good payload, is logged, and never raises out of the loop.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from blm_v4.live_analytics.line_reference import (build_reference,
                                                  game_slug_map)

#: How often the worker re-checks for new data.  The scan itself only runs
#: when the change-detector says the population moved.
DEFAULT_SCAN_INTERVAL_S = 60.0

#: A completed payload older than this is re-scanned regardless (backstop
#: against a change-detector miss, mirroring PaceReferenceWorker's max_age).
DEFAULT_MAX_AGE_S = 900.0


class LineReferenceWorker:
    """Precompute the league average line + per-game first line on a daemon
    thread.

    ``consumer`` (optional) is called under the worker's lock with each
    freshly computed ``(avg, open_map)`` pair — the API module registers a
    setter so ``v4_live`` reads the latest completed payload without polling.
    """

    def __init__(self,
                 metrics_db_path: Path,
                 games_db_path: Path,
                 interval_s: float = DEFAULT_SCAN_INTERVAL_S,
                 max_age_s: float = DEFAULT_MAX_AGE_S,
                 log: Optional[Callable[..., None]] = None,
                 consumer: Optional[Callable] = None) -> None:
        self._metrics_db = Path(metrics_db_path)
        self._games_db = Path(games_db_path)
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
    def start(self) -> "LineReferenceWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._loop, name="blm-line-reference-worker", daemon=True)
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
        """(observations_rowid, games_rowid) via MAX(rowid).

        O(1) on SQLite (rowid is the b-tree key), so the worker can tick
        every minute without touching the data pages the scan reads.
        """
        try:
            mconn = sqlite3.connect(
                f"file:{self._metrics_db}?mode=ro", uri=True, timeout=30)
            try:
                obs = mconn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) "
                    "FROM clean_observations").fetchone()[0]
            finally:
                mconn.close()
            gconn = sqlite3.connect(
                f"file:{self._games_db}?mode=ro", uri=True, timeout=30)
            try:
                games = gconn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM games").fetchone()[0]
            finally:
                gconn.close()
            return (obs, games)
        except sqlite3.Error as exc:
            if self._log is not None:
                self._log.warning("line_ref_key_failed",
                                  error=f"{type(exc).__name__}: {exc}")
            return None

    def should_scan(self) -> tuple[bool, str]:
        """(scan?, why).  Cheap: two MAX(rowid) reads (~sub-ms)."""
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
            # A mode=ro connect itself validates the file is a database; a
            # corrupt/garbage path must FAIL the scan, never publish an
            # empty-looking success.
            mconn = sqlite3.connect(
                f"file:{self._metrics_db}?mode=ro", uri=True, timeout=60)
            mconn.execute("SELECT 1").fetchone()   # probe: raises on a non-DB
            try:
                gconn = sqlite3.connect(
                    f"file:{self._games_db}?mode=ro", uri=True, timeout=30)
                gconn.execute("SELECT 1").fetchone()
                try:
                    reference = build_reference(mconn, game_slug_map(gconn))
                finally:
                    gconn.close()
            finally:
                mconn.close()
            # A forced scan with NO per-game lines at all means the engines
            # could not read the population (fail closed, never publish
            # success).
            if not reference["open"] and not reference["avg"]:
                raise RuntimeError(
                    "line reference scan returned no data for any game")
        except Exception as exc:                      # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._log is not None:
                self._log.warning("line_ref_scan_failed",
                                  error=self._last_error)
            return False
        payload = {"avg": reference["avg"], "open": reference["open"]}
        with self._lock:
            self._latest = payload
            self._latest_at = time.monotonic()
            self._last_error = None
            self._last_key = self._change_key()
            self._runs += 1
            latest = dict(payload)
        if self._consumer is not None:
            try:
                self._consumer(latest["avg"], latest["open"])
            except Exception:                          # noqa: BLE001
                # consumer failures must never kill the worker loop
                pass
        if self._log is not None:
            self._log.info(
                "line_ref_scan",
                elapsed_s=round(time.monotonic() - t0, 1),
                reason=why,
                competitions=len(latest["avg"]),
                games=len(latest["open"]))
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
