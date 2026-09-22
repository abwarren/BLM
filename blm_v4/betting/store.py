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

_STATUSES = ("PENDING", "SUBMITTED", "ACCEPTED", "REJECTED",
             "FAILED", "CANCELLED", "UNKNOWN")


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
                    requested_at_utc, day_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec["execution_id"], ik, rec["game_id"], rec["alert_id"],
                 rec["checkpoint"], rec.get("market") or
                 "MONEYLINE_TOTAL_UNDER", rec.get("selection") or "UNDER",
                 rec.get("triggered_line"), rec.get("price"),
                 rec.get("unit_price"), rec.get("stake_units"),
                 rec.get("stake_amount"), rec.get("status") or "PENDING",
                 _now(), _day()))
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
                      error_message: Optional[str] = None) -> None:
        """Advance a record's state machine (§5 vocabulary only)."""
        assert status in _STATUSES, status
        with self._lock, self._conn() as c:
            sets = ["status=?", "resolved_at_utc=?"]
            args: list = [status, _now()]
            if status in ("SUBMITTED",):
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
        counted = ("ACCEPTED", "SUBMITTED", "UNKNOWN")
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
