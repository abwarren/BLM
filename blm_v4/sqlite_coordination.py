"""Shared coordination for the pipeline's SQLite database.

The collector and server are separate processes which intentionally share a
WAL database.  SQLite serializes writers, but it does not serialize multiple
processes running DDL at startup.  This module provides that coordination and
the one busy-timeout policy used by the shared pipeline connections.
"""

from __future__ import annotations

import fcntl
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

SQLITE_BUSY_TIMEOUT_MS = 90_000
SQLITE_BUSY_TIMEOUT_S = SQLITE_BUSY_TIMEOUT_MS / 1000
_completed_stages: set[tuple[str, str]] = set()
_completed_stages_lock = threading.Lock()


def connect_pipeline_db(db_path: str | Path, *, row_factory: bool = True
                        ) -> sqlite3.Connection:
    """Open a pipeline DB connection without changing database journal mode."""
    conn = sqlite3.connect(str(db_path), timeout=SQLITE_BUSY_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _schema_file_lock(db_path: str | Path) -> Iterator[None]:
    lock_path = Path(f"{db_path}.schema.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_schema_stage(db_path: str | Path, stage: str,
                     migration: Callable[[sqlite3.Connection], None]) -> bool:
    """Run one idempotent schema stage exactly once, under a process lock.

    The sidecar file lock coordinates server and collector processes; the
    SQLite marker makes completed stages restart-safe.  Migrations must remain
    idempotent because SQLite's executescript may commit individual scripts
    before the marker can be written after a crash.
    """
    key = (str(Path(db_path).resolve()), stage)
    with _completed_stages_lock:
        if key in _completed_stages:
            return False
    with _schema_file_lock(db_path):
        conn = connect_pipeline_db(db_path)
        try:
            # WAL is selected once during coordinated database bootstrap.
            # Every later connection trusts this mode and never attempts to
            # change it while application writers are active.
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS blm_schema_stages (
                stage TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            if conn.execute(
                    "SELECT 1 FROM blm_schema_stages WHERE stage=?",
                    (stage,)).fetchone():
                conn.commit()
                with _completed_stages_lock:
                    _completed_stages.add(key)
                return False
            migration(conn)
            conn.execute(
                "INSERT INTO blm_schema_stages(stage) VALUES (?)", (stage,))
            conn.commit()
            with _completed_stages_lock:
                _completed_stages.add(key)
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
