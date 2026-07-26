"""
BLM V3 — Historical Database Layer.

Thread-safe SQLite database with WAL mode for concurrent reads during writes.
Manages 7 tables: games, snapshots, signals, market_events, predictions,
comparative_queries, ml_exports.

All public methods are async-safe (wrapped via ``run_in_executor``).
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

from blm_v3.historical.config import DEFAULT_DB_PATH
from blm_v3.historical.schema import FULL_DDL, get_table_names

logger = logging.getLogger(__name__)

# ── Thread-local connection management ───────────────────────────────

_local: threading.local = threading.local()


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Get a thread-local connection to the historical database."""
    key = f"historical_conn_{db_path}"
    conn: sqlite3.Connection | None = getattr(_local, key, None)
    if conn is None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-16384")  # 16 MB cache
        conn.execute("PRAGMA foreign_keys=ON")
        setattr(_local, key, conn)
    return conn


def _init_db(db_path: Path) -> None:
    """Create all tables and indexes if missing. Idempotent."""
    conn = _get_conn(db_path)
    conn.executescript(FULL_DDL)
    conn.commit()


# ── Database Class ───────────────────────────────────────────────────


class HistoricalDatabase:
    """Thread-safe, async-compatible wrapper around ``blm_historical.db``.

    Usage::

        db = HistoricalDatabase()
        await db.init()
        await db.save_game(game_data)
        await db.insert_snapshot(snapshot_data)
        snapshots = await db.query_snapshots("game-001")
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path: Path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialized = False
        self._start_time: float = time.monotonic()

    # ── Lifecycle ─────────────────────────────────────────────────

    def ensure_initialized(self) -> None:
        """Create tables if not already done. Safe to call multiple times."""
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    _init_db(self._db_path)
                    self._initialized = True

    async def init(self) -> None:
        """Async initialisation (alias for ``ensure_initialized``)."""
        self.ensure_initialized()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    # ── Table existence check ─────────────────────────────────────

    def get_table_info(self) -> dict[str, bool]:
        """Return a dict of {table_name: exists} for all managed tables."""
        conn = _get_conn(self._db_path)
        existing = set()
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            existing.add(row["name"])
        return {t: t in existing for t in get_table_names()}

    # ── Games CRUD ────────────────────────────────────────────────

    async def save_game(self, game: dict[str, Any]) -> None:
        """Insert or update a game record."""
        self.ensure_initialized()

        def _write() -> None:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO games
                    (id, league, season, home_team, away_team, status,
                     start_time, end_time, final_home, final_away,
                     final_total, final_margin, total_snapshots)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status         = COALESCE(excluded.status, games.status),
                        end_time       = COALESCE(excluded.end_time, games.end_time),
                        final_home     = COALESCE(excluded.final_home, games.final_home),
                        final_away     = COALESCE(excluded.final_away, games.final_away),
                        final_total    = COALESCE(excluded.final_total, games.final_total),
                        final_margin   = COALESCE(excluded.final_margin, games.final_margin),
                        total_snapshots= COALESCE(excluded.total_snapshots, games.total_snapshots),
                        updated_at     = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (
                    game.get("id"),
                    game.get("league", "Cyber 2K26"),
                    game.get("season"),
                    game.get("home_team"),
                    game.get("away_team"),
                    game.get("status", "live"),
                    game.get("start_time"),
                    game.get("end_time"),
                    game.get("final_home"),
                    game.get("final_away"),
                    game.get("final_total"),
                    game.get("final_margin"),
                    game.get("total_snapshots", 0),
                ),
            )
            conn.commit()

        await self._run_in_executor(_write)

    async def get_game(self, game_id: str) -> Optional[dict[str, Any]]:
        """Return a game record, or None."""
        self.ensure_initialized()

        def _read() -> Optional[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            row = conn.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()
            return dict(row) if row else None

        return await self._run_in_executor(_read)

    async def list_games(
        self,
        league: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List games with optional filtering."""
        self.ensure_initialized()

        def _read() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            clauses: list[str] = []
            params: list[Any] = []
            if league:
                clauses.append("league = ?")
                params.append(league)
            if status:
                clauses.append("status = ?")
                params.append(status)
            where = " AND ".join(clauses) if clauses else "1=1"
            rows = conn.execute(
                f"SELECT * FROM games WHERE {where} ORDER BY start_time DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run_in_executor(_read)

    # ── Snapshots CRUD ────────────────────────────────────────────

    async def insert_snapshot(self, snapshot: dict[str, Any]) -> str:
        """Insert a snapshot row. Returns the snapshot ID."""
        self.ensure_initialized()

        def _write() -> str:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO snapshots
                    (id, game_id, timestamp, quarter, clock, possession,
                     home_score, away_score, score_difference, total_score,
                     total_line, spread, home_team_total, away_team_total,
                     total_line_raw, spread_raw,
                     over_odds, under_odds, spread_odds_home, spread_odds_away,
                     line_delta, odds_delta, spread_delta,
                     possessions, possessions_per_min,
                     projected_possessions, projected_total,
                     trap_meter, tt_modifier, inflation_index, compression_index,
                     momentum, regression_prob, fair_total, expected_total,
                     variance, volatility, confidence,
                     raw_json)
                    VALUES (?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?,
                            ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            ?)""",
                (
                    snapshot.get("id"),
                    snapshot.get("game_id"),
                    snapshot.get("timestamp"),
                    snapshot.get("quarter", 1),
                    snapshot.get("clock"),
                    snapshot.get("possession"),
                    snapshot.get("home_score", 0),
                    snapshot.get("away_score", 0),
                    snapshot.get("score_difference", 0),
                    snapshot.get("total_score", 0),
                    snapshot.get("total_line"),
                    snapshot.get("spread"),
                    snapshot.get("home_team_total"),
                    snapshot.get("away_team_total"),
                    snapshot.get("total_line_raw"),
                    snapshot.get("spread_raw"),
                    snapshot.get("over_odds"),
                    snapshot.get("under_odds"),
                    snapshot.get("spread_odds_home"),
                    snapshot.get("spread_odds_away"),
                    snapshot.get("line_delta"),
                    snapshot.get("odds_delta"),
                    snapshot.get("spread_delta"),
                    snapshot.get("possessions"),
                    snapshot.get("possessions_per_min"),
                    snapshot.get("projected_possessions"),
                    snapshot.get("projected_total"),
                    snapshot.get("trap_meter"),
                    snapshot.get("tt_modifier"),
                    snapshot.get("inflation_index"),
                    snapshot.get("compression_index"),
                    snapshot.get("momentum"),
                    snapshot.get("regression_prob"),
                    snapshot.get("fair_total"),
                    snapshot.get("expected_total"),
                    snapshot.get("variance"),
                    snapshot.get("volatility"),
                    snapshot.get("confidence"),
                    snapshot.get("raw_json"),
                ),
            )
            conn.commit()
            return snapshot.get("id", "")

        return await self._run_in_executor(_write)

    async def query_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return snapshots for a game, oldest-first, with optional time range."""
        self.ensure_initialized()

        def _query() -> list[dict[str, Any]]:
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
                f"SELECT * FROM snapshots WHERE {where} ORDER BY timestamp ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run_in_executor(_query)

    async def query_latest_snapshot(
        self, game_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the most recent snapshot for a game."""
        self.ensure_initialized()

        def _query() -> Optional[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            row = conn.execute(
                "SELECT * FROM snapshots WHERE game_id = ? ORDER BY timestamp DESC LIMIT 1",
                (game_id,),
            ).fetchone()
            return dict(row) if row else None

        return await self._run_in_executor(_query)

    async def count_snapshots(self, game_id: str) -> int:
        """Return the total number of snapshots for a game."""
        self.ensure_initialized()

        def _count() -> int:
            conn = _get_conn(self._db_path)
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM snapshots WHERE game_id = ?",
                (game_id,),
            ).fetchone()
            return row["cnt"] if row else 0

        return await self._run_in_executor(_count)

    async def delete_game_snapshots(self, game_id: str) -> int:
        """Delete all snapshots for a game. Returns count of rows deleted."""
        self.ensure_initialized()

        def _delete() -> int:
            conn = _get_conn(self._db_path)
            conn.execute("DELETE FROM signals WHERE game_id = ?", (game_id,))
            conn.execute("DELETE FROM market_events WHERE game_id = ?", (game_id,))
            conn.execute("DELETE FROM predictions WHERE game_id = ?", (game_id,))
            cur = conn.execute("DELETE FROM snapshots WHERE game_id = ?", (game_id,))
            conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
            conn.commit()
            return cur.rowcount

        return await self._run_in_executor(_delete)

    # ── Aggregated Queries ────────────────────────────────────────

    async def query_aggregated(
        self,
        game_id: str,
        interval_seconds: float = 30.0,
        metrics: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return time-interval aggregated data for a game.

        Groups snapshots into time bins and computes AVG for each requested
        metric.  Useful for overview/zoomed-out views.

        Args:
            game_id: Game to aggregate.
            interval_seconds: Width of each time bin (e.g. 30.0 = 30s intervals).
            metrics: Metric columns to average. Defaults to common ones.

        Returns:
            List of { bin_start, bin_end, avg_metric1, avg_metric2, ... }
        """
        self.ensure_initialized()
        if metrics is None:
            metrics = [
                "total_line", "trap_meter", "inflation_index",
                "compression_index", "momentum", "confidence",
                "projected_total", "possessions_per_min",
                "regression_prob", "variance", "volatility",
            ]

        # SQLite trick: convert ISO timestamp to Unix epoch for binning
        select_avgs = ", ".join(
            f"AVG({m}) AS avg_{m}" for m in metrics
        )
        sql = f"""
            SELECT
                (strftime('%%s', timestamp) / {int(interval_seconds)}) * {int(interval_seconds)} AS bin_start,
                ((strftime('%%s', timestamp) / {int(interval_seconds)}) + 1) * {int(interval_seconds)} AS bin_end,
                {select_avgs}
            FROM snapshots
            WHERE game_id = ?
            GROUP BY bin_start
            ORDER BY bin_start ASC
        """

        def _query() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            rows = conn.execute(sql, (game_id,)).fetchall()
            return [dict(r) for r in rows]

        return await self._run_in_executor(_query)

    # ── Signals ───────────────────────────────────────────────────

    async def insert_signal(self, signal: dict[str, Any]) -> str:
        """Insert a signal record. Returns signal ID."""
        self.ensure_initialized()

        def _write() -> str:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO signals
                    (id, game_id, snapshot_id, timestamp, signal_type,
                     severity, value, threshold, description, related_json, confirmed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.get("id"),
                    signal.get("game_id"),
                    signal.get("snapshot_id"),
                    signal.get("timestamp"),
                    signal.get("signal_type"),
                    signal.get("severity", "mid"),
                    signal.get("value"),
                    signal.get("threshold"),
                    signal.get("description"),
                    signal.get("related_json"),
                    1 if signal.get("confirmed") else 0,
                ),
            )
            conn.commit()
            return signal.get("id", "")

        return await self._run_in_executor(_write)

    async def query_signals(
        self,
        game_id: Optional[str] = None,
        signal_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query signals with optional filters."""
        self.ensure_initialized()

        def _query() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            clauses: list[str] = []
            params: list[Any] = []
            if game_id:
                clauses.append("game_id = ?")
                params.append(game_id)
            if signal_type:
                clauses.append("signal_type = ?")
                params.append(signal_type)
            if severity:
                clauses.append("severity = ?")
                params.append(severity)
            where = " AND ".join(clauses) if clauses else "1=1"
            rows = conn.execute(
                f"SELECT * FROM signals WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run_in_executor(_query)

    # ── Market Events ─────────────────────────────────────────────

    async def insert_market_event(self, event: dict[str, Any]) -> str:
        """Insert a market event record. Returns event ID."""
        self.ensure_initialized()

        def _write() -> str:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO market_events
                    (id, game_id, snapshot_id, timestamp, event_type,
                     duration_seconds, magnitude, description, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.get("id"),
                    event.get("game_id"),
                    event.get("snapshot_id"),
                    event.get("timestamp"),
                    event.get("event_type"),
                    event.get("duration_seconds"),
                    event.get("magnitude"),
                    event.get("description"),
                    event.get("data_json"),
                ),
            )
            conn.commit()
            return event.get("id", "")

        return await self._run_in_executor(_write)

    async def query_market_events(
        self,
        game_id: str,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query market events for a game."""
        self.ensure_initialized()

        def _query() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            if event_type:
                rows = conn.execute(
                    """SELECT * FROM market_events
                        WHERE game_id = ? AND event_type = ?
                        ORDER BY timestamp DESC LIMIT ?""",
                    (game_id, event_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM market_events
                        WHERE game_id = ?
                        ORDER BY timestamp DESC LIMIT ?""",
                    (game_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run_in_executor(_query)

    # ── Predictions ───────────────────────────────────────────────

    async def insert_prediction(self, prediction: dict[str, Any]) -> str:
        """Insert a prediction record. Returns prediction ID."""
        self.ensure_initialized()

        def _write() -> str:
            conn = _get_conn(self._db_path)
            conn.execute(
                """INSERT INTO predictions
                    (id, game_id, snapshot_id, timestamp,
                     predicted_total, predicted_margin, predicted_winner,
                     win_probability, confidence, fair_total, expected_pace,
                     model_version, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prediction.get("id"),
                    prediction.get("game_id"),
                    prediction.get("snapshot_id"),
                    prediction.get("timestamp"),
                    prediction.get("predicted_total"),
                    prediction.get("predicted_margin"),
                    prediction.get("predicted_winner"),
                    prediction.get("win_probability"),
                    prediction.get("confidence"),
                    prediction.get("fair_total"),
                    prediction.get("expected_pace"),
                    prediction.get("model_version"),
                    prediction.get("data_json"),
                ),
            )
            conn.commit()
            return prediction.get("id", "")

        return await self._run_in_executor(_write)

    # ── Comparative Queries CRUD ──────────────────────────────────

    async def save_comparative_query(
        self, query: dict[str, Any]
    ) -> str:
        """Save a comparative query. Returns query ID."""
        self.ensure_initialized()

        def _write() -> str:
            conn = _get_conn(self._db_path)
            filters_json = query.get("filters_json")
            if isinstance(filters_json, dict):
                filters_json = json.dumps(filters_json)
            game_ids_json = query.get("game_ids_json")
            if isinstance(game_ids_json, list):
                game_ids_json = json.dumps(game_ids_json)
            conn.execute(
                """INSERT INTO comparative_queries
                    (id, name, description, filters_json,
                     game_ids_json, result_count, last_run_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    query.get("id"),
                    query.get("name"),
                    query.get("description"),
                    filters_json,
                    game_ids_json,
                    query.get("result_count", 0),
                    query.get("last_run_at"),
                ),
            )
            conn.commit()
            return query.get("id", "")

        return await self._run_in_executor(_write)

    async def list_comparative_queries(
        self, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List saved comparative queries."""
        self.ensure_initialized()

        def _read() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)
            rows = conn.execute(
                "SELECT * FROM comparative_queries ORDER BY last_run_at DESC NULLS LAST, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("filters_json"), str):
                    try:
                        d["filters"] = json.loads(d.pop("filters_json"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                if isinstance(d.get("game_ids_json"), str):
                    try:
                        d["game_ids"] = json.loads(d.pop("game_ids_json"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result

        return await self._run_in_executor(_read)

    # ── ML Exports ────────────────────────────────────────────────

    async def save_ml_export(self, export: dict[str, Any]) -> str:
        """Record an ML export. Returns export ID."""
        self.ensure_initialized()

        def _write() -> str:
            conn = _get_conn(self._db_path)
            game_ids_json = export.get("game_ids_json")
            if isinstance(game_ids_json, list):
                game_ids_json = json.dumps(game_ids_json)
            conn.execute(
                """INSERT INTO ml_exports
                    (id, export_type, game_ids_json, row_count,
                     file_path, file_size_bytes, feature_list, label_column,
                     model_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    export.get("id"),
                    export.get("export_type", "csv"),
                    game_ids_json,
                    export.get("row_count", 0),
                    export.get("file_path"),
                    export.get("file_size_bytes"),
                    export.get("feature_list"),
                    export.get("label_column"),
                    export.get("model_version"),
                ),
            )
            conn.commit()
            return export.get("id", "")

        return await self._run_in_executor(_write)

    # ── Comparative Query Runtime ─────────────────────────────────

    async def run_comparative_query(
        self, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Run a comparative query against the historical database.

        Filters can include:
        - ``trap_min`` / ``trap_max``: trap_meter range
        - ``inflation_min`` / ``inflation_max``: inflation_index range
        - ``confidence_min`` / ``confidence_max``: confidence range
        - ``result``: "over" or "under" (matches final outcome)
        - ``league``: league name filter
        - ``quarter_min`` / ``quarter_max``: quarter range
        - ``date_from`` / ``date_to``: game start date range

        Returns matching game summaries.
        """
        self.ensure_initialized()

        def _query() -> list[dict[str, Any]]:
            conn = _get_conn(self._db_path)

            # Build a query that finds games with snapshots matching filter criteria
            clauses: list[str] = ["s.game_id = g.id"]
            params: list[Any] = []

            if "trap_min" in filters:
                clauses.append("s.trap_meter >= ?")
                params.append(filters["trap_min"])
            if "trap_max" in filters:
                clauses.append("s.trap_meter <= ?")
                params.append(filters["trap_max"])
            if "inflation_min" in filters:
                clauses.append("s.inflation_index >= ?")
                params.append(filters["inflation_min"])
            if "inflation_max" in filters:
                clauses.append("s.inflation_index <= ?")
                params.append(filters["inflation_max"])
            if "confidence_min" in filters:
                clauses.append("s.confidence >= ?")
                params.append(filters["confidence_min"])
            if "confidence_max" in filters:
                clauses.append("s.confidence <= ?")
                params.append(filters["confidence_max"])
            if "quarter_min" in filters:
                clauses.append("s.quarter >= ?")
                params.append(filters["quarter_min"])
            if "quarter_max" in filters:
                clauses.append("s.quarter <= ?")
                params.append(filters["quarter_max"])
            if "league" in filters:
                clauses.append("g.league = ?")
                params.append(filters["league"])
            if "result" in filters:
                r = filters["result"].lower()
                # Compare final_total against the first snapshot's total_line
                # using a subquery to get the opening line
                if r == "over":
                    clauses.append("""(g.final_home + g.final_away) > (
                        SELECT s2.total_line FROM snapshots s2
                        WHERE s2.game_id = g.id
                        ORDER BY s2.timestamp ASC LIMIT 1
                    )""")
                elif r == "under":
                    clauses.append("""(g.final_home + g.final_away) < (
                        SELECT s2.total_line FROM snapshots s2
                        WHERE s2.game_id = g.id
                        ORDER BY s2.timestamp ASC LIMIT 1
                    )""")

            where = " AND ".join(clauses)
            sql = f"""
                SELECT DISTINCT g.id, g.home_team, g.away_team, g.status,
                       g.final_home, g.final_away, g.final_total, g.final_margin,
                       g.total_snapshots, g.start_time,
                       (SELECT AVG(s3.trap_meter) FROM snapshots s3
                        WHERE s3.game_id = g.id AND s3.trap_meter IS NOT NULL) AS avg_trap_meter
                FROM games g, snapshots s
                WHERE {where}
                ORDER BY g.start_time DESC
                LIMIT 50
            """
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

        return await self._run_in_executor(_query)

    # ── Internal ──────────────────────────────────────────────────

    async def _run_in_executor(self, fn, *args, **kwargs):
        """Run a synchronous function in the default executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args, **kwargs)

    # ── Health ────────────────────────────────────────────────────

    async def get_health(self) -> dict[str, Any]:
        """Return database health status."""
        self.ensure_initialized()

        def _check() -> dict[str, Any]:
            conn = _get_conn(self._db_path)
            try:
                row = conn.execute("SELECT COUNT(*) as cnt FROM games").fetchone()
                game_count = row["cnt"]
                snap_row = conn.execute("SELECT COUNT(*) as cnt FROM snapshots").fetchone()
                snap_count = snap_row["cnt"]
                signal_row = conn.execute("SELECT COUNT(*) as cnt FROM signals").fetchone()
                signal_count = signal_row["cnt"]
                return {
                    "status": "ok",
                    "game_count": game_count,
                    "snapshot_count": snap_count,
                    "signal_count": signal_count,
                    "db_path": str(self._db_path),
                    "uptime_seconds": self.uptime_seconds,
                }
            except Exception as e:
                return {"status": "error", "detail": str(e)}

        return await self._run_in_executor(_check)


# ── Module-level init guard ─────────────────────────────────────────

def ensure_global_init(db_path: Optional[Path] = None) -> None:
    """Idempotent initialisation — ensures all tables exist."""
    hdb = HistoricalDatabase(db_path=db_path)
    hdb.ensure_initialized()
