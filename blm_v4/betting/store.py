"""Betting persistence — blm_betting.db, a SEPARATE database.

Holds the AUTO BETTING kill-switch state, the execution ledger and the
audit log.  Deliberately NOT part of blm_pokerbet.db: the betting layer
has its own failure domain, and the analytics DBs stay untouched.

SECURITY: no credential value is ever written here.  The only
authentication-related column is ``auth_mode`` (a policy label such as
``env``), never a username, password or token.

UNIQUENESS: ``bet_executions.idempotency_key`` carries a UNIQUE index —
the database itself enforces ONE execution per (game, checkpoint, alert)
opportunity across restarts, retries and concurrent workers.  The
INSERT-OR-IGNORE + check pattern below is the atomic claim.

KILL SWITCH: persisted state defaults OFF.  A missing/corrupt row means
OFF — the absence of an explicit enable is never an enable.  The
frontend-visible switch writes here through the API; the server's own
authority (``BETTING_DRY_RUN``, risk limits) lives above it.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS betting_config (
    key               TEXT PRIMARY KEY,
    value             TEXT NOT NULL,
    updated_at_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS betting_game_controls (
    game_id           TEXT PRIMARY KEY,
    enabled           INTEGER NOT NULL CHECK (enabled IN (0,1)),
    updated_at_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bet_executions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id        TEXT NOT NULL UNIQUE,
    idempotency_key     TEXT NOT NULL UNIQUE,
    game_id             TEXT NOT NULL,
    alert_id            TEXT NOT NULL,
    checkpoint          TEXT NOT NULL,
    market              TEXT NOT NULL DEFAULT 'MONEYLINE_TOTAL_UNDER',
    selection           TEXT NOT NULL DEFAULT 'UNDER',
    triggered_line      REAL,
    price               REAL,
    unit_price          REAL,
    stake_units         REAL,
    stake_amount        REAL,
    status              TEXT NOT NULL,
    execution_state     TEXT,
    elapsed_ms          INTEGER,
    provider_ref        TEXT,
    error_code          TEXT,
    error_message       TEXT,
    requested_at_utc    TEXT NOT NULL,
    submitted_at_utc    TEXT,
    resolved_at_utc     TEXT,
    day_utc             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_bet_executions_day
    ON bet_executions(day_utc, status);
CREATE INDEX IF NOT EXISTS idx_bet_executions_game
    ON bet_executions(game_id, checkpoint);

CREATE TABLE IF NOT EXISTS bet_audit (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      TEXT,
    idempotency_key   TEXT,
    event             TEXT NOT NULL,
    reason            TEXT,
    details           TEXT,   -- JSON, credential-free
    at_utc            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_bet_audit_key
    ON bet_audit(idempotency_key);
"""

# Kill-switch keys (betting_config).  Anything absent/corrupt reads OFF.
KEY_AUTO_ENABLED = "auto_betting_enabled"
KEY_UNIT_PRICE = "unit_price"

_STATUSES = ("PENDING", "WOULD_BET", "SUBMITTING", "SUBMITTED",
             "ACCEPTED", "REJECTED", "BLOCKED", "UNKNOWN",
             "RECONCILING", "EXPIRED", "FAILED", "CANCELLED")
_TERMINAL_STATUSES = {"WOULD_BET", "ACCEPTED", "REJECTED", "BLOCKED",
                      "EXPIRED", "FAILED", "CANCELLED"}


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _day() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


class BettingStore:
    """SQLite-backed config + execution ledger + audit log."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path))
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._ensure_execution_columns(c)

    @staticmethod
    def _ensure_execution_columns(c: sqlite3.Connection) -> None:
        """Additive migration for execution audit fields; never rebuilds data."""
        existing = {r["name"] for r in c.execute(
            "PRAGMA table_info(bet_executions)").fetchall()}
        additions = {
            "requested_amount": "REAL",
            "accepted_amount": "REAL",
            "simulated_amount": "REAL",
            "unit_limit": "REAL",
            "rejection_reason": "TEXT",
            "execution_state": "TEXT",
            "elapsed_ms": "INTEGER",
        }
        for name, decl in additions.items():
            if name not in existing:
                c.execute(f"ALTER TABLE bet_executions ADD COLUMN {name} {decl}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ── kill switch / config ───────────────────────────────────────────
    def get_config(self, key: str, default: str) -> str:
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT value FROM betting_config WHERE key=?",
                    (key,)).fetchone()
            return row["value"] if row else default
        except sqlite3.Error:
            # database unavailable → fail CLOSED (§9): never report enabled
            return default

    def set_config(self, key: str, value: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO betting_config (key, value, updated_at_utc)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                       updated_at_utc=excluded.updated_at_utc""",
                (key, value, _now()))
            c.commit()

    def is_enabled(self) -> bool:
        """The persisted AUTO BETTING switch.  Defaults OFF; corrupt or
        unexpected values read OFF."""
        return self.get_config(KEY_AUTO_ENABLED, "false").strip().lower() \
            == "true"

    def get_unit_price(self) -> Optional[float]:
        try:
            v = float(self.get_config(KEY_UNIT_PRICE, "0"))
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def set_unit_price(self, value: float) -> None:
        self.set_config(KEY_UNIT_PRICE, repr(float(value)))

    def is_game_enabled(self, game_id: str) -> bool:
        """Per-game override; absent rows inherit the global enabled state."""
        with self._conn() as c:
            row = c.execute(
                "SELECT enabled FROM betting_game_controls WHERE game_id=?",
                (str(game_id),)).fetchone()
        return True if row is None else bool(row["enabled"])

    def set_game_enabled(self, game_id: str, enabled: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO betting_game_controls(game_id, enabled, updated_at_utc)
                   VALUES(?,?,?) ON CONFLICT(game_id) DO UPDATE SET
                   enabled=excluded.enabled, updated_at_utc=excluded.updated_at_utc""",
                (str(game_id), 1 if enabled else 0, _now()))

    def game_controls(self) -> dict[str, bool]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT game_id, enabled FROM betting_game_controls").fetchall()
        return {r["game_id"]: bool(r["enabled"]) for r in rows}

    # ── idempotent claim ───────────────────────────────────────────────
    def claim(self, rec: dict) -> tuple[bool, Optional[dict]]:
        """Atomically claim an execution slot for ``rec``'s idempotency
        key.

        Returns ``(claimed, existing)``.  When the key already exists the
        claim is refused and the EXISTING record is returned — the caller
        must not bet again.  The INSERT OR IGNORE + SELECT pair runs
        inside one transaction so two concurrent workers can never both
        win (§4 / concurrency test).
        """
        ik = rec["idempotency_key"]
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                """INSERT OR IGNORE INTO bet_executions
                   (execution_id, idempotency_key, game_id, alert_id,
                    checkpoint, market, selection, triggered_line, price,
                    unit_price, stake_units, stake_amount, status,
                    execution_state,
                    requested_at_utc, day_utc, requested_amount,
                    accepted_amount, simulated_amount, unit_limit,
                    rejection_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec["execution_id"], ik, rec["game_id"], rec["alert_id"],
                 rec["checkpoint"], rec.get("market") or
                 "MONEYLINE_TOTAL_UNDER", rec.get("selection") or "UNDER",
                 rec.get("triggered_line"), rec.get("price"),
                 rec.get("unit_price"), rec.get("stake_units"),
                 rec.get("stake_amount"), rec.get("status") or "PENDING",
                 rec.get("execution_state") or rec.get("status") or "PENDING",
                 _now(), _day(), rec.get("requested_amount"),
                 rec.get("accepted_amount"), rec.get("simulated_amount"),
                 rec.get("unit_limit"), rec.get("rejection_reason")))
            if cur.rowcount == 0:
                row = c.execute(
                    "SELECT * FROM bet_executions WHERE idempotency_key=?",
                    (ik,)).fetchone()
                c.commit()
                return False, (dict(row) if row else None)
            row = c.execute(
                "SELECT * FROM bet_executions WHERE idempotency_key=?",
                (ik,)).fetchone()
            c.commit()
            return True, (dict(row) if row else None)

    def update_status(self, execution_id: str, status: str,
                      provider_ref: Optional[str] = None,
                      error_code: Optional[str] = None,
                      error_message: Optional[str] = None,
                      accepted_amount: Optional[float] = None,
                      simulated_amount: Optional[float] = None,
                      rejection_reason: Optional[str] = None,
                      execution_state: Optional[str] = None) -> None:
        """Advance a record's state machine (§5 vocabulary only)."""
        assert status in _STATUSES, status
        with self._lock, self._conn() as c:
            current_time = _now()
            resolved_at = current_time if status in _TERMINAL_STATUSES else None
            sets = ["status=?", "execution_state=?", "resolved_at_utc=?",
                    "elapsed_ms=MAX(0, CAST((julianday(?) - "
                    "julianday(requested_at_utc))*86400000 AS INTEGER))"]
            args: list = [status, execution_state or status,
                          resolved_at, current_time]
            if status in ("SUBMITTING", "SUBMITTED"):
                sets.append("submitted_at_utc=?")
                args.append(_now())
            if provider_ref is not None:
                sets.append("provider_ref=?")
                args.append(provider_ref)
            if error_code is not None:
                sets.append("error_code=?")
                args.append(error_code)
            if error_message is not None:
                sets.append("error_message=?")
                args.append(str(error_message)[:500])
            if accepted_amount is not None:
                sets.append("accepted_amount=?")
                args.append(float(accepted_amount))
            if simulated_amount is not None:
                sets.append("simulated_amount=?")
                args.append(float(simulated_amount))
            if rejection_reason is not None:
                sets.append("rejection_reason=?")
                args.append(str(rejection_reason)[:300])
            args.append(execution_id)
            c.execute(f"UPDATE bet_executions SET {', '.join(sets)} "
                      "WHERE execution_id=?", args)
            c.commit()

    def get_execution(self, execution_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM bet_executions WHERE execution_id=?",
                (execution_id,)).fetchone()
        return dict(row) if row else None

    def get_by_idempotency(self, key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM bet_executions WHERE idempotency_key=?",
                (key,)).fetchone()
        return dict(row) if row else None

    def latest_for_game(self, game_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM bet_executions WHERE game_id=? "
                "ORDER BY id DESC LIMIT 1", (str(game_id),)).fetchone()
        return dict(row) if row else None

    def history(self, *, query: str = "", status: str = "",
                game_id: str = "", limit: int = 100) -> list[dict]:
        clauses, args = [], []
        if status:
            clauses.append("status=?")
            args.append(status.upper())
        if game_id:
            clauses.append("game_id=?")
            args.append(game_id)
        if query:
            clauses.append("(game_id LIKE ? OR alert_id LIKE ? OR "
                           "execution_id LIKE ? OR provider_ref LIKE ? OR "
                           "error_code LIKE ? OR error_message LIKE ?)")
            args.extend([f"%{query}%"] * 6)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM bet_executions {where} ORDER BY id DESC LIMIT ?",
                (*args, max(1, min(int(limit), 500)))).fetchall()
        return [dict(r) for r in rows]

    def audit_recent(self, *, execution_id: str = "",
                     limit: int = 100) -> list[dict]:
        where = "WHERE execution_id=?" if execution_id else ""
        args = (execution_id, max(1, min(int(limit), 500))) if execution_id else (
            max(1, min(int(limit), 500)),)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM bet_audit {where} ORDER BY id DESC LIMIT ?",
                args).fetchall()
        return [dict(r) for r in rows]

    def explorer_tables(self) -> dict[str, list[str]]:
        allowed = {
            "bet_executions": ["id", "execution_id", "idempotency_key",
                "game_id", "alert_id", "checkpoint", "market", "selection",
                "triggered_line", "price", "unit_price", "stake_units",
                "stake_amount", "requested_amount", "accepted_amount",
                "simulated_amount", "unit_limit", "status", "execution_state",
                "provider_ref", "error_code", "error_message", "rejection_reason",
                "requested_at_utc", "submitted_at_utc", "resolved_at_utc",
                "elapsed_ms", "day_utc"],
            "bet_audit": ["id", "execution_id", "idempotency_key", "event",
                "reason", "details", "at_utc"],
            "betting_config": ["key", "value", "updated_at_utc"],
            "betting_game_controls": ["game_id", "enabled", "updated_at_utc"],
        }
        return allowed

    def explore(self, table: str, *, query: str = "", page: int = 1,
                page_size: int = 50) -> dict:
        columns_by_table = self.explorer_tables()
        if table not in columns_by_table:
            raise ValueError("table is not available in the betting explorer")
        columns = columns_by_table[table]
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        # Config inspection is restricted to non-secret operational keys.
        where, args = ("WHERE key IN (?,?)", [KEY_AUTO_ENABLED, KEY_UNIT_PRICE]) \
            if table == "betting_config" else ("", [])
        if query:
            searchable = [c for c in columns if c not in ("details",)]
            search = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in searchable)
            where = (where + " AND " if where else "WHERE ") + f"({search})"
            args.extend([f"%{query}%"] * len(searchable))
        projection = ",".join(columns)
        with self._conn() as c:
            total = c.execute(
                f"SELECT COUNT(*) FROM {table} {where}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT {projection} FROM {table} {where} "
                f"ORDER BY 1 DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size)).fetchall()
        return {"table": table, "columns": columns,
                "rows": [dict(r) for r in rows], "page": page,
                "page_size": page_size, "total": int(total)}

    def recent(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM bet_executions ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ── daily aggregates for the risk limits ──────────────────────────
    def today_stats(self) -> dict:
        """Today's ACCEPTED (+SUBMITTED/UNKNOWN — money may be in play)
        execution counts and exposure, in the UTC day the records carry.

        SUBMITTED and UNKNOWN count toward exposure: a submission whose
        outcome is unresolved might still be live money — the risk limit
        must assume the worst (§9)."""
        day = _day()
        counted = ("WOULD_BET", "SUBMITTING", "SUBMITTED", "ACCEPTED",
                   "UNKNOWN", "RECONCILING")
        try:
            with self._conn() as c:
                n = c.execute(
                    f"""SELECT COUNT(*) AS n FROM bet_executions
                        WHERE day_utc=? AND status IN
                        ({','.join('?' * len(counted))})""",
                    (day, *counted)).fetchone()["n"]
                amt = c.execute(
                    f"""SELECT COALESCE(SUM(stake_amount),0) AS amt
                        FROM bet_executions
                        WHERE day_utc=? AND status IN
                        ({','.join('?' * len(counted))})""",
                    (day, *counted)).fetchone()["amt"]
        except sqlite3.Error:
            # database unavailable → the limits cannot be verified → the
            # executor must refuse to bet (§9).  Report as exhausted.
            return {"day": day, "bets": None, "amount": None,
                    "verifiable": False}
        return {"day": day, "bets": int(n), "amount": float(amt or 0.0),
                "verifiable": True}

    # ── audit (credential-free by construction) ────────────────────────
    def audit(self, event: str, idempotency_key: Optional[str] = None,
              execution_id: Optional[str] = None,
              reason: Optional[str] = None,
              details: Optional[dict] = None) -> None:
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    """INSERT INTO bet_audit
                       (execution_id, idempotency_key, event, reason,
                        details) VALUES (?,?,?,?,?)""",
                    (execution_id, idempotency_key, event, reason,
                     _safe_json(details)))
        except sqlite3.Error:
            pass  # audit best-effort; never breaks the decision path


def _safe_json(d: Optional[dict]) -> Optional[str]:
    import json
    if not d:
        return None
    try:
        return json.dumps(d, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return None
