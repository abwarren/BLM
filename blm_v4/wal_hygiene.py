"""WAL HYGIENE (incident 2026-09-29, open item 1) — periodic passive checkpoints.

On 2026-09-29 the production ``blm_pokerbet.db`` WAL ballooned to 7.1 GB:
repeated collector kills left mid-write transactions in the WAL while the
box's constant write pressure (collector ingest + scorecard sections +
settle/reconcile workers) kept ``wal_autocheckpoint`` starved.  When the
server then restarted, it crawled through the giant WAL and lost the
startup lock races — the full-stack outage recorded in STATUS.md.

This module makes that state impossible to reach: a small daemon runs
``PRAGMA wal_checkpoint(PASSIVE)`` on a fixed cadence and only when the
WAL file is big enough to be worth checkpointing.  PASSIVE is the
operation SQLite documents as never blocking other connections — readers
and writers proceed; the checkpoint copies whatever frames are safely
copyable, lets the next writer RESTART from the copied prefix, and skips
the rest.  So the hygiene pass can never reintroduce the very
``database is locked`` races the 2026-09-29 recovery sequence was needed
to clear: the dangerous restart-time checkpointing is converted into
cheap, always-safe steady-state checkpointing.

NO-STRATEGY (AGENTS.md §19): this worker touches only the WAL file's
physical layout.  It reads no game data, writes no rows, alters no
schema, and consults no alert, gate, threshold, fingerprint or betting
path.  The scorecard (an owner-dirty file — its ``PRAGMA
journal_mode=WAL`` at blm_v4/scorecard.py:1160 is the owners') is not
imported, invoked or modified.  It is the sibling of the settle worker
and the reconcilers: a small, bounded, fail-closed daemon.

Conservation properties:
  * PASSIVE-ONLY — the busy/RESTART/FULL(=TRUNCATE) modes all wait on
    readers; PASSIVE never does, so the pass cannot stall behind (or
    stall) the scorecard's 30-90 s section windows or the collector's
    90 s busy_timeout waits.
  * STAT-GATED — a pass runs only when the WAL file exceeds a size
    threshold (default 256 MB), so a quiet box pays one lstat per tick
    and nothing more.
  * BOUNDED — one checkpoint attempt per tick; a checkpoint that cannot
    finish (busy readers) simply leaves frames for the next tick; there
    is no retry loop, no blocking wait, no deadline to blow.
  * FAIL-CLOSED — every failure is logged and retried on the next
    cadence tick; the worker never dies and never raises out of the
    loop.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("blm_v4.wal_hygiene")

#: hygiene cadence — far slower than any worker interval above and far
#: faster than the hours it takes a neglected WAL to become dangerous.
#: Env-overridable so ops can bound it without a code change (mirrors
#: SETTLE_INTERVAL_S / RESULT_RECONCILE_INTERVAL_S).  0 disables the
#: worker entirely.
DEFAULT_INTERVAL_S = 300.0
#: run a checkpoint only when the WAL is at least this large — below it,
#: the default wal_autocheckpoint (1000 pages) is managing fine on its
#: own and a pass would be pure overhead.  The 2026-09-29 incident WAL
#: reached 7.1 GB, so this bound leaves two orders of magnitude of
#: headroom.
DEFAULT_THRESHOLD_MB = 256.0
#: busy_timeout for the hygiene connection itself: the PRAGMA must
#: return promptly even if it cannot make progress; PASSIVE never waits
#: on readers anyway, this only bounds the connection setup handshake.
DEFAULT_BUSY_TIMEOUT_MS = 5000


def wal_bytes(db_path: Path | str) -> int:
    """Size of ``db_path``'s WAL file (0 when absent/unreadable)."""
    wal = Path(str(db_path) + "-wal")
    try:
        return wal.stat().st_size
    except OSError:
        return 0


def should_checkpoint(db_path: Path | str,
                      threshold_mb: float = DEFAULT_THRESHOLD_MB) -> bool:
    """True when the WAL exceeds ``threshold_mb`` megabytes."""
    return wal_bytes(db_path) >= threshold_mb * 1024 * 1024


def passive_checkpoint_once(db_path: Path | str, *,
                            log: logging.Logger | None = None,
                            threshold_mb: float = DEFAULT_THRESHOLD_MB,
                            busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
                            ) -> dict:
    """One stat-gated ``wal_checkpoint(PASSIVE)`` pass on ``db_path``.

    Returns ``{"checked": bool, "checkpointed": bool, "wal_bytes": int,
    "busy": int, "log": int, "checkpointed_frames": int,
    "held_back": int}``:

    * ``checked``  — the WAL was big enough to attempt a checkpoint.
    * ``checkpointed`` — SQLite copied at least one frame back into the
      main DB during this pass.
    * ``busy``     — SQLite's busy flag: 0 = the pass was never blocked;
      non-zero = it hit momentary contention (PASSIVE reports instead
      of waiting — it never waits).
    * ``log``      — total WAL frames at pass time.
    * ``checkpointed_frames`` — frames copied into the main DB.
    * ``held_back`` — ``log - checkpointed_frames``: frames a reader
      pinned below its snapshot; PASSIVE skips them and the next tick
      retries.  A pass under an open reader copies the safe PREFIX and
      skips only the pinned tail — partial progress, zero blocking.

    Never raises for per-pass problems (fail-closed, logged); a locked
    or missing DB logs and reports ``checkpointed=False``.  An absent
    WAL (fresh or fully checkpointed DB) is ``checked=False``.
    """
    log = log or logger
    wal = wal_bytes(db_path)
    if wal < threshold_mb * 1024 * 1024:
        return {"checked": False, "checkpointed": False, "wal_bytes": wal,
                "busy": -1, "log": -1, "checkpointed_frames": -1,
                "held_back": -1}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute(
            f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        row = conn.execute(
            "PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        # PRAGMA result row (sqlite3_wal_checkpoint_v2 semantics):
        #   busy         — 0 unless the pass was blocked by another
        #                  connection (PASSIVE reports, never waits);
        #   log          — total WAL frames at pass time;
        #   checkpointed — frames copied into the main DB this pass.
        busy, log_frames, copied = (int(row[0]), int(row[1]),
                                    int(row[2]))
        held_back = max(0, log_frames - copied)
        if copied:
            log.info(
                "wal_hygiene_checkpoint wal_bytes=%d "
                "checkpointed_frames=%d held_back=%d busy_flag=%d "
                "total_frames=%d", wal, copied, held_back, busy,
                log_frames)
        elif log_frames:
            # frames exist but none could be copied this tick — readers
            # pin even the prefix; PASSIVE skips, the next tick retries.
            # Expected under sustained read pressure, never fatal.
            log.info(
                "wal_hygiene_skipped_busy wal_bytes=%d total_frames=%d "
                "busy_flag=%d (readers pin the WAL; retry next tick)",
                wal, log_frames, busy)
        return {"checked": True, "checkpointed": bool(copied),
                "wal_bytes": wal, "busy": busy, "log": log_frames,
                "checkpointed_frames": copied, "held_back": held_back}
    except sqlite3.Error as exc:
        # fail-closed: locked/unreadable DB — logged, retried next tick
        log.warning("wal_hygiene_failed db=%s error=%s",
                    Path(db_path).name, exc)
        return {"checked": True, "checkpointed": False, "wal_bytes": wal,
                "busy": -1, "log": -1, "checkpointed_frames": -1,
                "held_back": -1}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


class WalHygieneWorker:
    """The WAL-hygiene daemon thread — a daemon, like the settle worker.

    Cadence, threshold and enabling are env-overridable in server.py;
    this class takes plain values so tests can drive it cheaply.
    """

    def __init__(self, db_path: Path | str, *,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 threshold_mb: float = DEFAULT_THRESHOLD_MB,
                 log: logging.Logger | None = None):
        self._db_path = Path(db_path)
        self._interval_s = float(interval_s)
        self._threshold_mb = float(threshold_mb)
        self._log = log or logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict:
        """One stat-gated pass (the worker's whole job)."""
        return passive_checkpoint_once(
            self._db_path, log=self._log,
            threshold_mb=self._threshold_mb,
            busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS)

    def loop(self) -> None:
        import time
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # the WORKER never dies: one bad pass is logged and
                # retried on the next cadence tick
                self._log.exception("wal_hygiene_pass_failed")
            self._stop.wait(self._interval_s)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.loop, name="blm-wal-hygiene-worker", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)


def env_interval_s(environ: dict | None = None) -> float:
    """Resolved cadence from ``BLM_WAL_HYGIENE_INTERVAL_S`` (0 = off)."""
    env = os.environ if environ is None else environ
    try:
        return float(env.get("BLM_WAL_HYGIENE_INTERVAL_S",
                             str(DEFAULT_INTERVAL_S)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


def env_threshold_mb(environ: dict | None = None) -> float:
    """Resolved WAL-size gate from ``BLM_WAL_HYGIENE_THRESHOLD_MB``."""
    env = os.environ if environ is None else environ
    try:
        return float(env.get("BLM_WAL_HYGIENE_THRESHOLD_MB",
                             str(DEFAULT_THRESHOLD_MB)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_MB
