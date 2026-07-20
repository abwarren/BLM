"""SQLite implementation of the BLM V2 storage interface.

Stores game metadata and alerts in a SQLite database (``blm.db`` by default).
Reuses the V1 table schema for games and adds an ``alerts`` table.
Thread-safe via ``threading.local`` connections with WAL mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from blm_v2.storage.base import StorageDB

logger = logging.getLogger(__name__)

# ── Default path (same DB as V1 for data continuity) ─────────────

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "blm.db"


# ── Schema ────────────────────────────────────────────────────────

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT UNIQUE NOT NULL,
    league      TEXT NOT NULL DEFAULT 'Cyber 2K26',
    season      TEXT,
    home_team   TEXT NOT NULL,
    away_team   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'live'
                CHECK(status IN ('pre', 'live', 'halftime', 'ended')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    type        TEXT NOT NULL,
    message     TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info'
                CHECK(severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    data_json   TEXT,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_game
    ON alerts(game_id, timestamp);
"""


# ── Thread-safe connection ────────────────────────────────────────

_local = threading.local()


def _get_conn(db_path: Path) -> sqlite3.Connection:
    key = f"storage_conn_{db_path}"
    conn: sqlite3.Connection | None = getattr(_local, key, None)
    if conn is None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        setattr(_local, key, conn)
    return conn


def _init_db(db_path: Path) -> None:
    conn = _get_conn(db_path)
    conn.executescript(_CREATE_TABLES)
    conn.commit()


# ── Implementation ────────────────────────────────────────────────


class SQLiteStorage(StorageDB):
    """SQLite-backed CRUD for games and alerts.

    Uses the same ``blm.db`` database as the V1 codebase for data
    compatibility.  All public methods are async-safe (wrapped via
    ``run_in_executor``).
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    _init_db(self._db_path)
                    self._initialized = True

    # ── Games ─────────────────────────────────────────────────────

    async def save_game(self, game: dict[str, Any]) -> None:
        self._ensure_initialized()

        def _write() -> None:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO games (game_id, league, season, home_team, away_team, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        status       = COALESCE(excluded.status, games.status),
                        season       = COALESCE(excluded.season, games.season),
                        updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (
                    game.get("game_id"),
                    game.get("league", "Cyber 2K26"),
                    game.get("season"),
                    game.get("home_team"),
                    game.get("away_team"),
                    game.get("status", "live"),
                ),
            )
            conn.commit()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def get_game(self, game_id: str) -> Optional[dict[str, Any]]:
        self._ensure_initialized()

        def _read() -> Optional[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            row = conn.execute(
                "SELECT * FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()
            return dict(row) if row else None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read)

    async def list_games(self, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_initialized()

        def _read() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            rows = conn.execute(
                """SELECT g.*
                    FROM games g
                    ORDER BY g.updated_at DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read)

    # ── Alerts ────────────────────────────────────────────────────

    async def save_alert(self, alert: dict[str, Any]) -> None:
        self._ensure_initialized()

        def _write() -> None:
            conn = _get_conn(self._db_path)
            data_json = alert.get("data")
            if data_json is not None and not isinstance(data_json, str):
                data_json = json.dumps(data_json, default=str)
            conn.execute(
                """INSERT INTO alerts (game_id, type, message, severity, data_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    alert.get("game_id"),
                    alert.get("type", "general"),
                    alert.get("message", ""),
                    alert.get("severity", "info"),
                    data_json,
                    alert.get("timestamp"),
                ),
            )
            conn.commit()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def get_alerts(
        self,
        game_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._ensure_initialized()

        def _read() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            rows = conn.execute(
                """SELECT * FROM alerts
                    WHERE game_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                (game_id, limit),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("data_json") and isinstance(d["data_json"], str):
                    try:
                        d["data"] = json.loads(d.pop("data_json"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read)


# ── Module-level init ─────────────────────────────────────────────

_init_db(_DEFAULT_DB_PATH)
