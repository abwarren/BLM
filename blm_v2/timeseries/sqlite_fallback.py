"""SQLite-based time series backend for BLM V2 — fallback when InfluxDB is unavailable.

Uses a dedicated ``blm_ts.db`` database with a ``snapshots_v2`` table that stores
the full snapshot as a JSON blob alongside indexed scalar columns for efficient
filtering.  Thread-safe via ``threading.local`` connections (WAL mode).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from blm_v2.timeseries.base import SnapshotData, TimeSeriesDB

logger = logging.getLogger(__name__)

# ── Default DB path ───────────────────────────────────────────────

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "blm_ts.db"


# ── Schema ────────────────────────────────────────────────────────

_CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    quarter         INTEGER NOT NULL DEFAULT 1,
    clock           TEXT,
    home_score      INTEGER NOT NULL DEFAULT 0,
    away_score      INTEGER NOT NULL DEFAULT 0,
    total_line      REAL,
    spread          REAL,
    home_projection REAL,
    away_projection REAL,
    pace            REAL,
    possessions     INTEGER,
    data_json       TEXT    NOT NULL,       -- full snapshot as JSON
    ingested_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_sv2_game_ts
    ON snapshots_v2(game_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_sv2_game_id
    ON snapshots_v2(game_id);

CREATE TABLE IF NOT EXISTS line_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    olv             REAL,
    current_line    REAL,
    excursion       REAL,
    excursion_percent REAL,
    score_delta     INTEGER NOT NULL DEFAULT 0,
    line_delta      REAL    NOT NULL DEFAULT 0.0,
    divergence      TEXT    NOT NULL DEFAULT 'neither',
    freeze_ticks    INTEGER NOT NULL DEFAULT 0,
    is_burst        INTEGER NOT NULL DEFAULT 0,
    rolling_mean_score REAL NOT NULL DEFAULT 0.0,
    under_confidence REAL NOT NULL DEFAULT 0.0,
    data_json       TEXT,
    ingested_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_la_game_ts
    ON line_analysis(game_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_la_game_id
    ON line_analysis(game_id);
"""


# ── Helpers ───────────────────────────────────────────────────────


def _scalar(value: Any, key: Optional[str] = None) -> Any:
    """Coerce a possibly-nested value to a scalar for SQLite binding.

    BLM engine snapshots carry nested dicts (pace, betting_market,
    team_totals).  SQLite cannot bind dicts — extract the scalar:
      - dict with the requested key → that value
      - dict without it → None
      - anything else → as-is
    """
    if isinstance(value, dict):
        return value.get(key) if key else None
    return value


def _resolve(snapshot: dict, top_key: str, *nested: tuple) -> Any:
    """Resolve a field from a snapshot, trying top-level then nested paths.

    Engine snapshots put identity in ``metadata`` and state in
    ``game_state``/``betting_market``/``team_totals`` — e.g. game_id lives
    at ``metadata.game_id``.  ``_resolve(snap, "game_id", ("metadata", "game_id"))``
    tries ``snap["game_id"]`` first, then the nested path.
    """
    if top_key in snapshot and snapshot[top_key] is not None:
        return snapshot[top_key]
    for path in nested:
        node: Any = snapshot
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok:
            return node
    return None


def _get_data_dir() -> Path:
    """Return the directory where the TS DB file should live."""
    return _DEFAULT_DB_PATH.parent


# ── Thread-safe connection management ─────────────────────────────

_local = threading.local()


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Get a thread-local connection to the TS SQLite database."""
    key = f"ts_conn_{db_path}"
    conn: sqlite3.Connection | None = getattr(_local, key, None)
    if conn is None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-8192")  # 8 MB
        setattr(_local, key, conn)
    return conn


def _init_db(db_path: Path) -> None:
    """Create schema if missing.  Idempotent.  Called once per process."""
    conn = _get_conn(db_path)
    conn.executescript(_CREATE_SNAPSHOTS_TABLE)
    conn.commit()


# ── Implementation ────────────────────────────────────────────────


class SQLiteTimeSeries(TimeSeriesDB):
    """Time-series backend backed by SQLite (``blm_ts.db``).

    Designed as a lightweight fallback when InfluxDB is not available.
    Write latency is typically <5 ms (local SSD / WAL mode).

    Usage::

        ts = SQLiteTimeSeries()
        await ts.write_snapshot({"game_id": "g1", "timestamp": "...", ...})
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        """Open (or create) the SQLite TS database.

        Args:
            db_path: Full path to the SQLite file.  Defaults to
                ``<project-root>/blm_ts.db``.
        """
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # protects DB file creation
        self._initialized = False

    # ── Lifecycle ─────────────────────────────────────────────────

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    _init_db(self._db_path)
                    self._initialized = True

    # ── TimeSeriesDB ──────────────────────────────────────────────

    async def write_snapshot(self, snapshot: SnapshotData) -> None:
        """Persist a snapshot to the ``snapshots_v2`` table.

        Write latency is minimised by using a prepared INSERT and keeping the
        JSON serialisation outside the transaction.
        """
        self._ensure_initialized()

        game_id = _resolve(
            snapshot, "game_id", ("metadata", "game_id"),
        ) or "unknown"
        ts = _resolve(snapshot, "timestamp", ("metadata", "timestamp")) or ""
        quarter = _resolve(snapshot, "quarter", ("metadata", "quarter")) or 1
        clock = _resolve(snapshot, "clock", ("metadata", "clock"))
        home_score = _resolve(
            snapshot, "home_score", ("game_state", "home_score"), ("metadata", "home_score"),
        ) or 0
        away_score = _resolve(
            snapshot, "away_score", ("game_state", "away_score"), ("metadata", "away_score"),
        ) or 0
        total_line = _scalar(_resolve(
            snapshot, "total_line", ("betting_market", "total"),
        ))
        spread = _scalar(_resolve(
            snapshot, "spread", ("betting_market", "spread"),
        ))
        home_proj = _scalar(_resolve(
            snapshot, "home_projection", ("team_totals", "home_projection"),
        ))
        away_proj = _scalar(_resolve(
            snapshot, "away_projection", ("team_totals", "away_projection"),
        ))
        pace = _scalar(_resolve(
            snapshot, "pace", ("pace", "real_pace"),
        ), "real_pace")
        possessions = _scalar(_resolve(
            snapshot, "possessions", ("pace", "possessions"),
        ))
        data_json_str = json.dumps(snapshot, default=str)

        def _write() -> None:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO snapshots_v2
                    (game_id, timestamp, quarter, clock,
                     home_score, away_score, total_line, spread,
                     home_projection, away_projection, pace, possessions,
                     data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    game_id, ts, quarter, clock,
                    home_score, away_score, total_line, spread,
                    home_proj, away_proj, pace, possessions,
                    data_json_str,
                ),
            )
            conn.commit()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def query_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 500,
    ) -> list[SnapshotData]:
        """Query snapshots over an optional time range, oldest-first."""
        self._ensure_initialized()

        def _query() -> list[SnapshotData]:
            conn = _get_conn(self._db_path)
            params: list[Any] = [game_id]
            clauses = ["game_id = ?"]

            if from_ts:
                clauses.append("timestamp >= ?")
                params.append(from_ts)
            if to_ts:
                clauses.append("timestamp <= ?")
                params.append(to_ts)

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM snapshots_v2 WHERE {where} ORDER BY timestamp ASC LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [self._row_to_snapshot(r) for r in rows]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def query_latest(self, game_id: str) -> Optional[SnapshotData]:
        """Return the most recent snapshot for *game_id*."""
        self._ensure_initialized()

        def _query() -> Optional[SnapshotData]:
            conn = _get_conn(self._db_path)
            row = conn.execute(
                """SELECT * FROM snapshots_v2
                    WHERE game_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 1""",
                (game_id,),
            ).fetchone()
            return self._row_to_snapshot(row) if row else None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def list_games(self) -> list[str]:
        """Return distinct game IDs with at least one snapshot."""
        self._ensure_initialized()

        def _query() -> list[str]:
            conn = _get_conn(self._db_path)
            rows = conn.execute(
                "SELECT DISTINCT game_id FROM snapshots_v2 ORDER BY game_id"
            ).fetchall()
            return [r["game_id"] for r in rows]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def delete_game(self, game_id: str) -> None:
        """Delete all snapshots for a game."""
        self._ensure_initialized()

        def _delete() -> None:
            conn = _get_conn(self._db_path)
            conn.execute("DELETE FROM snapshots_v2 WHERE game_id = ?", (game_id,))
            conn.commit()
            logger.info("Deleted %s from blm_ts.db", game_id)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _delete)

    # ── TSInterface adapter methods ────────────────────────────────

    async def get_latest_snapshot(self, game_id: str) -> Optional[SnapshotData]:
        """Alias for query_latest — satisfies TSInterface."""
        return await self.query_latest(game_id)

    async def get_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SnapshotData]:
        """Query snapshots with offset support — satisfies TSInterface."""
        self._ensure_initialized()

        def _query() -> list[SnapshotData]:
            conn = _get_conn(self._db_path)
            params: list[Any] = [game_id]
            clauses = ["game_id = ?"]

            if from_ts:
                clauses.append("timestamp >= ?")
                params.append(from_ts)
            if to_ts:
                clauses.append("timestamp <= ?")
                params.append(to_ts)

            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM snapshots_v2 WHERE {where} ORDER BY timestamp ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return [self._row_to_snapshot(r) for r in rows]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def get_replay_snapshots(self, game_id: str) -> list[SnapshotData]:
        """Return all snapshots for a game in chronological order for replay."""
        return await self.get_snapshots(game_id, limit=10000)

    async def get_chart_data(self, game_id: str) -> list[SnapshotData]:
        """Return snapshots formatted for charting (aggregated data points)."""
        snapshots = await self.get_snapshots(game_id, limit=2000)
        return [
            {
                "timestamp": s.get("timestamp", ""),
                "home_score": s.get("home_score", 0),
                "away_score": s.get("away_score", 0),
                "total_line": s.get("total_line"),
                "spread": s.get("spread"),
                "pace": s.get("pace"),
                "quarter": s.get("quarter", 1),
                "clock": s.get("clock", ""),
                "blm_score": s.get("blm_score"),
                "confidence": s.get("confidence"),
            }
            for s in snapshots
        ]

    async def get_game_detail(self, game_id: str) -> Optional[dict]:
        """Return full details for a single game from the latest snapshot."""
        latest = await self.query_latest(game_id)
        if latest is None:
            return None
        return {
            "game_id": latest.get("game_id", game_id),
            "home_team": latest.get("home_team", ""),
            "away_team": latest.get("away_team", ""),
            "status": latest.get("status", "unknown"),
            "start_time": latest.get("start_time", latest.get("timestamp", "")),
            "home_score": latest.get("home_score", 0),
            "away_score": latest.get("away_score", 0),
            "quarter": latest.get("quarter", 0),
            "clock": latest.get("clock", ""),
            "blm_score": latest.get("blm_score"),
            "confidence": latest.get("confidence"),
            "pace": latest.get("pace"),
            "latest_snapshot": latest,
        }

    # ── Internals ─────────────────────────────────────────────────

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> SnapshotData:
        """Convert a SQLite row back to a full snapshot dict.

        Prioritises ``data_json`` reconstruction and overlays the indexed
        scalar columns on top so that callers always see the full payload.
        """
        d = dict(row)

        # Reconstruct the full snapshot from JSON blob
        data_json_str = d.pop("data_json", None)
        if data_json_str:
            try:
                parsed = json.loads(data_json_str)
                # Overlay with the indexed columns (which are authoritative)
                parsed.update({
                    k: d[k] for k in (
                        "game_id", "timestamp", "quarter", "clock",
                        "home_score", "away_score", "total_line", "spread",
                        "home_projection", "away_projection", "pace", "possessions",
                    ) if k in d and d[k] is not None
                })
                return parsed
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: return the row dict minus internal fields
        d.pop("id", None)
        d.pop("ingested_at", None)
        return d

    # ── Line Analysis Methods ─────────────────────────────────────

    async def write_line_analysis(self, analysis: SnapshotData) -> None:
        """Persist a line analysis record to the ``line_analysis`` table."""
        self._ensure_initialized()

        game_id = analysis.get("game_id", "unknown")
        ts = analysis.get("timestamp", "")
        olv = analysis.get("olv")
        current_line = analysis.get("current_line")
        excursion = analysis.get("excursion")
        excursion_pct = analysis.get("excursion_percent")
        score_delta = analysis.get("score_delta", 0)
        line_delta = analysis.get("line_delta", 0.0)
        divergence = analysis.get("divergence", "neither")
        freeze_ticks = analysis.get("freeze_ticks", 0)
        is_burst = 1 if analysis.get("is_burst", False) else 0
        rolling_mean = analysis.get("rolling_mean_score_delta", 0.0)
        under_conf = analysis.get("under_confidence", 0.0)
        data_json_str = json.dumps(analysis, default=str)

        def _write() -> None:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO line_analysis
                    (game_id, timestamp, olv, current_line, excursion,
                     excursion_percent, score_delta, line_delta, divergence,
                     freeze_ticks, is_burst, rolling_mean_score,
                     under_confidence, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    game_id, ts, olv, current_line, excursion,
                    excursion_pct, score_delta, line_delta, divergence,
                    freeze_ticks, is_burst, rolling_mean,
                    under_conf, data_json_str,
                ),
            )
            conn.commit()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def query_line_analysis(
        self,
        game_id: str,
        limit: int = 500,
    ) -> list[SnapshotData]:
        """Return line analysis records for a game, oldest-first."""
        self._ensure_initialized()

        def _query() -> list[SnapshotData]:
            conn = _get_conn(self._db_path)
            rows = conn.execute(
                """SELECT * FROM line_analysis
                    WHERE game_id = ?
                    ORDER BY timestamp ASC
                    LIMIT ?""",
                (game_id, limit),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                data_json_str = d.pop("data_json", None)
                if data_json_str:
                    try:
                        parsed = json.loads(data_json_str)
                        d.update(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass
                d.pop("id", None)
                d.pop("ingested_at", None)
                results.append(d)
            return results

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def get_live_line_analysis(self) -> Optional[SnapshotData]:
        """Return the most recent line analysis for any live game."""
        self._ensure_initialized()

        def _query() -> Optional[SnapshotData]:
            conn = _get_conn(self._db_path)
            # Find the most recent line analysis across all games
            row = conn.execute(
                """SELECT * FROM line_analysis
                    ORDER BY timestamp DESC
                    LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            data_json_str = d.pop("data_json", None)
            if data_json_str:
                try:
                    parsed = json.loads(data_json_str)
                    d.update(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            d.pop("id", None)
            d.pop("ingested_at", None)
            return d

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def get_live_game(self) -> Optional[SnapshotData]:
        """Return the most recent snapshot across all games (live game)."""
        self._ensure_initialized()

        def _query() -> Optional[SnapshotData]:
            conn = _get_conn(self._db_path)
            row = conn.execute(
                """SELECT * FROM snapshots_v2
                    ORDER BY timestamp DESC
                    LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            data_json_str = d.pop("data_json", None)
            if data_json_str:
                try:
                    parsed = json.loads(data_json_str)
                    d.update(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            d.pop("id", None)
            d.pop("ingested_at", None)
            return d

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)


# ── Module-level initialisation guard ─────────────────────────────

_init_db(_DEFAULT_DB_PATH)
