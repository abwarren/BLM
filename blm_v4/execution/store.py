"""BLM EXECUTION — PERSISTENCE (blm_execution.db).

A SEPARATE database, its own failure domain (the pattern proven by
``blm_betting.db``): jobs, per-leg attempts, order submissions and the
audit trail.  Job state is persisted so a browser/extension reconnect
can recover safely — recovery always starts by INSPECTING the current
betslip, never by blind restart.

SECURITY: no credential value is ever written here — the CDP bridge
holds none (connects to the user's own logged-in browser).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      TEXT NOT NULL,
    parlay_id         TEXT NOT NULL,
    combination_id    TEXT NOT NULL,
    fold_size         INTEGER NOT NULL,
    mode              TEXT NOT NULL,
    stake_amount      REAL,
    status            TEXT NOT NULL,
    current_leg       INTEGER DEFAULT 0,
    retry_count       INTEGER DEFAULT 0,
    legs_json         TEXT NOT NULL,
    last_error        TEXT,
    started_at_utc    TEXT,
    completed_at_utc  TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    game_ids_json     TEXT,
    UNIQUE(execution_id, parlay_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_jobs_run
    ON execution_jobs(execution_id, parlay_id);

CREATE TABLE IF NOT EXISTS leg_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      TEXT NOT NULL,
    parlay_id         TEXT NOT NULL,
    leg_index         INTEGER NOT NULL,
    attempt_no        INTEGER NOT NULL,
    event             TEXT NOT NULL,
    market            TEXT NOT NULL,
    position          TEXT NOT NULL,
    game_id           TEXT,
    discovered_line   REAL,
    discovered_price  REAL,
    clicked_line      REAL,
    clicked_price     REAL,
    outcome           TEXT NOT NULL,
    verified_line     REAL,
    verified_price    REAL,
    error_code        TEXT,
    at_utc            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_leg_attempts
    ON leg_attempts(execution_id, parlay_id, leg_index);

CREATE TABLE IF NOT EXISTS order_submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      TEXT NOT NULL,
    parlay_id         TEXT NOT NULL UNIQUE,
    attempt_no        INTEGER NOT NULL,
    stake_amount      REAL,
    status            TEXT NOT NULL,
    provider_ref      TEXT,
    error_code        TEXT,
    game_ids_json     TEXT,
    at_utc            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS execution_audit (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      TEXT,
    parlay_id         TEXT,
    leg_index         INTEGER,
    event             TEXT NOT NULL,
    reason            TEXT,
    details           TEXT,
    at_utc            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_exec_audit
    ON execution_audit(execution_id, parlay_id);
"""


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _safe_json(d) -> Optional[str]:
    if not d:
        return None
    try:
        return json.dumps(d, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return None


class ExecutionStore:
    """SQLite-backed jobs / attempts / orders / audit."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path))
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    # ── migrations (idempotent; pre-game_id ledgers keep working) ──────
    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the game-id provenance columns to pre-existing ledgers.
        Fresh DBs get them from CREATE TABLE; older files are ALTERed in
        place — no historical data is ever dropped."""
        jobs = {r[1] for r in conn.execute(
            "PRAGMA table_info(execution_jobs)")}
        if "game_ids_json" not in jobs:
            conn.execute(
                "ALTER TABLE execution_jobs ADD COLUMN game_ids_json TEXT")
        attempts = {r[1] for r in conn.execute(
            "PRAGMA table_info(leg_attempts)")}
        if "game_id" not in attempts:
            conn.execute(
                "ALTER TABLE leg_attempts ADD COLUMN game_id TEXT")
        orders = {r[1] for r in conn.execute(
            "PRAGMA table_info(order_submissions)")}
        if "game_ids_json" not in orders:
            conn.execute(
                "ALTER TABLE order_submissions ADD COLUMN game_ids_json TEXT")
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ── jobs ───────────────────────────────────────────────────────────
    def upsert_job(self, execution_id: str, job: dict, mode: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO execution_jobs
                   (execution_id, parlay_id, combination_id, fold_size,
                    mode, stake_amount, status, current_leg, retry_count,
                    legs_json, last_error, started_at_utc, completed_at_utc,
                    game_ids_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(execution_id, parlay_id) DO UPDATE SET
                     status=excluded.status,
                     current_leg=excluded.current_leg,
                     retry_count=excluded.retry_count,
                     last_error=excluded.last_error,
                     stake_amount=excluded.stake_amount,
                     game_ids_json=COALESCE(excluded.game_ids_json,
                                            execution_jobs.game_ids_json),
                     completed_at_utc=COALESCE(
                         excluded.completed_at_utc,
                         execution_jobs.completed_at_utc)""",
                (execution_id, job.get("parlay_id"),
                 job.get("combination_id"), job.get("fold_size"),
                 mode, job.get("stake_amount"), job.get("status"),
                 job.get("current_leg", 0), job.get("retry_count", 0),
                 _safe_json(job.get("legs")), job.get("last_error"),
                 job.get("started_at"), job.get("completed_at"),
                 _safe_json(job.get("game_ids"))))
            c.commit()

    def update_job_status(self, execution_id: str, parlay_id: str,
                          status: str, *, current_leg: Optional[int] = None,
                          retry_count: Optional[int] = None,
                          last_error: Optional[str] = None) -> None:
        with self._lock, self._conn() as c:
            sets, args = ["status=?", "completed_at_utc=CASE WHEN ? IN "
                          "('ORDER_PLACED','COMPLETE','USER_ABORT',"
                          "'NON_RECOVERABLE_ERROR') THEN ? ELSE "
                          "completed_at_utc END"], [status, status, _now()]
            if current_leg is not None:
                sets.append("current_leg=?")
                args.append(current_leg)
            if retry_count is not None:
                sets.append("retry_count=?")
                args.append(retry_count)
            if last_error is not None:
                sets.append("last_error=?")
                args.append(str(last_error)[:500])
            args += [execution_id, parlay_id]
            c.execute(
                f"UPDATE execution_jobs SET {', '.join(sets)} "
                "WHERE execution_id=? AND parlay_id=?", args)
            c.commit()

    def jobs_for_run(self, execution_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM execution_jobs WHERE execution_id=? "
                "ORDER BY id", (execution_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── leg attempts (the directive's execution log fields) ────────────
    def record_attempt(self, execution_id: str, parlay_id: str,
                       leg_index: int, attempt_no: int, sel: dict,
                       discovered_line, discovered_price,
                       clicked_line, clicked_price, outcome: str,
                       verified_line=None, verified_price=None,
                       error_code: Optional[str] = None) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO leg_attempts
                   (execution_id, parlay_id, leg_index, attempt_no, event,
                    market, position, game_id, discovered_line,
                    discovered_price, clicked_line, clicked_price, outcome,
                    verified_line, verified_price, error_code)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (execution_id, parlay_id, leg_index, attempt_no,
                 sel.get("event"), sel.get("market"), sel.get("position"),
                 sel.get("game_id"),
                 discovered_line, discovered_price, clicked_line,
                 clicked_price, outcome, verified_line, verified_price,
                 error_code))
            c.commit()

    # ── orders (idempotent: UNIQUE parlay_id) ──────────────────────────
    def record_order(self, execution_id: str, parlay_id: str,
                     attempt_no: int, stake_amount, status: str,
                     provider_ref: Optional[str] = None,
                     error_code: Optional[str] = None,
                     game_ids: Optional[list] = None) -> bool:
        """Returns False when a submission row ALREADY exists for this
        parlay — the persistence-level guard against duplicate orders
        (the engine must also re-inspect the slip, this is the ledger)."""
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    """INSERT INTO order_submissions
                       (execution_id, parlay_id, attempt_no, stake_amount,
                        status, provider_ref, error_code, game_ids_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (execution_id, parlay_id, attempt_no, stake_amount,
                     status, provider_ref, error_code,
                     _safe_json(game_ids)))
                c.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_order(self, parlay_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM order_submissions WHERE parlay_id=?",
                (parlay_id,)).fetchone()
        return dict(row) if row else None

    # ── audit ──────────────────────────────────────────────────────────
    def audit(self, event: str, *, execution_id: Optional[str] = None,
              parlay_id: Optional[str] = None,
              leg_index: Optional[int] = None,
              reason: Optional[str] = None,
              details: Optional[dict] = None) -> None:
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    """INSERT INTO execution_audit
                       (execution_id, parlay_id, leg_index, event, reason,
                        details) VALUES (?,?,?,?,?,?)""",
                    (execution_id, parlay_id, leg_index, event, reason,
                     _safe_json(details)))
                c.commit()
        except sqlite3.Error:
            pass  # audit is best-effort; never breaks execution
