"""
BLM V1 — SQLite Database Layer

Immutable, append-only snapshot storage.
WAL mode for concurrent reads during writes.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "blm.db"

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """Thread-safe connection with WAL mode."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create schema if not exists. Idempotent."""
    conn = get_connection()
    conn.executescript("""
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

        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id         TEXT NOT NULL REFERENCES games(game_id),
            timestamp       TEXT NOT NULL,
            quarter         INTEGER NOT NULL DEFAULT 1,
            clock           TEXT,
            home_score      INTEGER NOT NULL DEFAULT 0,
            away_score      INTEGER NOT NULL DEFAULT 0,
            total_line      REAL,
            spread          REAL,
            total_odds      TEXT,
            spread_odds     TEXT,
            moneyline_home  TEXT,
            moneyline_away  TEXT,
            home_projection REAL,
            away_projection REAL,
            pace            REAL,
            possessions     INTEGER,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_game_ts
            ON snapshots(game_id, timestamp);

        CREATE INDEX IF NOT EXISTS idx_snapshots_game_id
            ON snapshots(game_id);
    """)
    conn.commit()


# ── Queries ──────────────────────────────────────────────────────

def upsert_game(game_id: str, home: str, away: str, league: str = "Cyber 2K26",
                season: Optional[str] = None) -> None:
    conn = get_connection()
    conn.execute("""
        INSERT INTO games (game_id, league, season, home_team, away_team)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    """, (game_id, league, season, home, away))
    conn.commit()


def insert_snapshot(game_id: str, ts: str, quarter: int, clock: Optional[str],
                    home_score: int, away_score: int,
                    total_line: Optional[float] = None,
                    spread: Optional[float] = None,
                    total_odds: Optional[str] = None,
                    spread_odds: Optional[str] = None,
                    moneyline_home: Optional[str] = None,
                    moneyline_away: Optional[str] = None,
                    home_projection: Optional[float] = None,
                    away_projection: Optional[float] = None,
                    pace: Optional[float] = None,
                    possessions: Optional[int] = None) -> None:
    conn = get_connection()
    conn.execute("""
        INSERT INTO snapshots
            (game_id, timestamp, quarter, clock, home_score, away_score,
             total_line, spread, total_odds, spread_odds,
             moneyline_home, moneyline_away,
             home_projection, away_projection, pace, possessions)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (game_id, ts, quarter, clock, home_score, away_score,
          total_line, spread, total_odds, spread_odds,
          moneyline_home, moneyline_away,
          home_projection, away_projection, pace, possessions))
    conn.commit()


def get_live_game() -> Optional[dict]:
    """Return the most recent live game, or None."""
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM games
        WHERE status IN ('live', 'halftime')
        ORDER BY updated_at DESC
        LIMIT 1
    """).fetchone()
    return dict(row) if row else None


def get_snapshots(game_id: str, limit: int = 500) -> list[dict]:
    """Return snapshots for a game, newest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM snapshots
        WHERE game_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (game_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_snapshots_chrono(game_id: str, offset: int = 0, limit: int = 500) -> list[dict]:
    """Return snapshots chronologically (oldest first)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM snapshots
        WHERE game_id = ?
        ORDER BY timestamp ASC
        LIMIT ? OFFSET ?
    """, (game_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def set_game_status(game_id: str, status: str) -> None:
    conn = get_connection()
    conn.execute("""
        UPDATE games SET status = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE game_id = ?
    """, (status, game_id))
    conn.commit()


def get_recent_games(limit: int = 20) -> list[dict]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT g.*, COUNT(s.id) as snapshot_count,
               MAX(s.timestamp) as last_snapshot_ts
        FROM games g
        LEFT JOIN snapshots s ON s.game_id = g.game_id
        GROUP BY g.id
        ORDER BY g.updated_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Initialization ───────────────────────────────────────────────

init_db()
