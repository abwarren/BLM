"""InfluxDB 3 implementation of the BLM V2 time series interface.

Writes enriched snapshots as InfluxDB points with tagged metadata and
numerical fields for efficient range queries.  Uses the official
``influxdb-client`` library with connection pooling and exponential-backoff
retry logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore[attr-defined]
from influxdb_client.client.write_api import SYNCHRONOUS

from blm_v2.timeseries.base import SnapshotData, TimeSeriesDB

logger = logging.getLogger(__name__)

# ── Numeric field keys (written as InfluxDB fields) ───────────────

_NUMERIC_FIELDS = frozenset({
    "home_score",
    "away_score",
    "total_line",
    "spread",
    "home_projection",
    "away_projection",
    "pace",
    "possessions",
    "home_win_pct",
    "away_win_pct",
    "projected_margin",
    "confidence",
    "trap_score",
    "clv",
    "momentum",
    "home_implied_total",
    "away_implied_total",
})

# ── Tag keys (written as InfluxDB tags) ──────────────────────────

_TAG_KEYS = frozenset({
    "game_id",
    "league",
    "quarter",
    "status",
})


@dataclass
class InfluxDBConfig:
    """Connection configuration for InfluxDB 3."""

    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "blm"
    bucket: str = "blm_snapshots"
    connect_timeout_s: float = 10.0
    write_timeout_s: float = 10.0
    max_retries: int = 3
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 30.0


class InfluxDBTimeSeries(TimeSeriesDB):
    """Time-series backend backed by InfluxDB 3.

    Usage::

        ts = InfluxDBTimeSeries(config=InfluxDBConfig(url="...", token="..."))
        await ts.write_snapshot({"game_id": "g1", "timestamp": "...", ...})
        snaps = await ts.query_snapshots("g1", limit=100)
    """

    def __init__(
        self,
        config: Optional[InfluxDBConfig] = None,
        *,
        client: Optional[InfluxDBClient] = None,
    ) -> None:
        """Create a new InfluxDB time-series adapter.

        Args:
            config: Connection settings.  Used when *client* is *None*.
            client: An existing, connected ``InfluxDBClient`` instance.
                When provided, *config* is ignored.
        """
        self._config = config or InfluxDBConfig()
        self._client: InfluxDBClient
        self._write_api: Any = None  # influxdb_client WriteApi
        self._query_api: Any = None  # influxdb_client QueryApi
        self._own_client = client is None

        if client is not None:
            self._client = client
        else:
            self._client = InfluxDBClient(
                url=self._config.url,
                token=self._config.token,
                org=self._config.org,
                timeout=int(self._config.connect_timeout_s * 1000),
                enable_gzip=True,
            )
        self._closed = False

    # ── Lifecycle ─────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying InfluxDB client and release resources."""
        if self._closed:
            return
        self._closed = True
        if self._own_client:
            self._client.close()
        logger.info("InfluxDBTimeSeries client closed")

    # ── TimeSeriesDB ──────────────────────────────────────────────

    async def write_snapshot(self, snapshot: SnapshotData) -> None:
        """Write a single snapshot as an InfluxDB point.

        The *timestamp* key (ISO-8601 string) is parsed as the point time.
        String keys in ``_TAG_KEYS`` become InfluxDB tags.
        Numeric keys in ``_NUMERIC_FIELDS`` become fields.
        All other keys are stored in a ``data_json`` field for resilience.
        """
        ts_str = snapshot.get("timestamp")
        timestamp = self._parse_timestamp(ts_str) if ts_str else time.time_ns()

        point = Point("snapshot").time(timestamp, WritePrecision.NS)

        # Tags
        for tag_key in _TAG_KEYS:
            val = snapshot.get(tag_key)
            if val is not None:
                point = point.tag(tag_key, str(val))

        # Numeric fields
        has_numeric = False
        for field_key in _NUMERIC_FIELDS:
            val = snapshot.get(field_key)
            if val is not None and isinstance(val, (int, float)):
                point = point.field(field_key, float(val))
                has_numeric = True

        # Fallback: store the full snapshot as a JSON string field so no data
        # is lost even when a key doesn't map to a dedicated tag/field.
        point = point.field("data_json", json.dumps(snapshot, default=str))

        await self._retry_async("write_snapshot", self._do_write, point)

    async def query_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 500,
    ) -> list[SnapshotData]:
        """Query snapshots via Flux, returning oldest-first results."""
        flux = self._build_range_flux(game_id, from_ts, to_ts, limit)
        return await self._retry_async("query_snapshots", self._do_query, flux)

    async def query_latest(self, game_id: str) -> Optional[SnapshotData]:
        """Return the single most recent snapshot via Flux ``last()``."""
        flux = f'''
            from(bucket: "{self._config.bucket}")
                |> range(start: 0)
                |> filter(fn: (r) => r.game_id == "{game_id}")
                |> last()
                |> limit(n: 1)
        '''
        rows = await self._retry_async("query_latest", self._do_query, flux)
        return rows[0] if rows else None

    async def list_games(self) -> list[str]:
        """List distinct game IDs by probing the ``game_id`` tag values."""
        flux = f'''
            import "influxdata/influxdb/schema"
            schema.tagValues(
                bucket: "{self._config.bucket}",
                tag: "game_id",
                start: -30d,
            )
        '''
        try:
            rows = await self._retry_async("list_games", self._do_query, flux)
            return [r["_value"] for r in rows if r.get("_value")]
        except Exception:
            logger.warning("list_games via schema.tagValues failed, fallback to manual", exc_info=True)
            # Fallback: scan recent data
            flux = f'''
                from(bucket: "{self._config.bucket}")
                    |> range(start: -30d)
                    |> keep(columns: ["game_id"])
                    |> distinct(column: "game_id")
            '''
            rows = await self._retry_async("list_games", self._do_query, flux)
            return list({r["game_id"] for r in rows if r.get("game_id")})

    async def delete_game(self, game_id: str) -> None:
        """Delete all snapshots for a game via InfluxDB's delete API."""
        delete_api = self._client.delete_api()
        # Delete from start of epoch to far future
        import datetime
        start = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        stop = datetime.datetime(2099, 12, 31, tzinfo=datetime.timezone.utc)
        predicate = f'_measurement="snapshot" AND game_id="{game_id}"'
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: delete_api.delete(
                    start, stop, predicate, bucket=self._config.bucket, org=self._config.org,
                ),
            )
            logger.info("Deleted game %s from InfluxDB", game_id)
        except Exception:
            logger.warning("Failed to delete game %s from InfluxDB", game_id, exc_info=True)

    # ── Internals ─────────────────────────────────────────────────

    def _do_write(self, point: Point) -> None:
        """Synchronous write call (runs in executor)."""
        if self._write_api is None:
            self._write_api = self._client.write_api(write_type=SYNCHRONOUS)
        self._write_api.write(
            bucket=self._config.bucket,
            org=self._config.org,
            record=point,
        )

    def _do_query(self, flux: str) -> list[SnapshotData]:
        """Synchronous Flux query (runs in executor)."""
        if self._query_api is None:
            self._query_api = self._client.query_api()
        tables = self._query_api.query(flux, org=self._config.org)
        return [self._flux_record_to_dict(record) for table in tables for record in table.records]

    def _flux_record_to_dict(self, record: Any) -> SnapshotData:
        """Convert an InfluxDB Flux record to a plain dict."""
        d: SnapshotData = {}
        values = record.values if hasattr(record, "values") else {}
        for k, v in values.items():
            if k.startswith("_") and k not in ("_time", "_measurement", "_field", "_value"):
                continue
            if k in ("result", "table"):
                continue
            if v is not None:
                d[k] = v
        # Try to reconstruct the full snapshot from data_json if present
        data_json = d.pop("data_json", None)
        if data_json and isinstance(data_json, str):
            try:
                reconstructed = json.loads(data_json)
                reconstructed.update({k: v for k, v in d.items() if k in ("game_id",)})
                return reconstructed
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _build_range_flux(
        self,
        game_id: str,
        from_ts: Optional[str],
        to_ts: Optional[str],
        limit: int,
    ) -> str:
        """Build a Flux query filtering by game_id and optional time range."""
        if from_ts:
            range_start = f'time(v: "{from_ts}")'
        else:
            range_start = "0"

        if to_ts:
            range_stop = f'time(v: "{to_ts}")'
        else:
            range_stop = "now()"

        return f'''
            from(bucket: "{self._config.bucket}")
                |> range(start: {range_start}, stop: {range_stop})
                |> filter(fn: (r) => r.game_id == "{game_id}")
                |> sort(columns: ["_time"])
                |> limit(n: {limit})
        '''

    @staticmethod
    def _parse_timestamp(ts_str: str) -> int:
        """Parse ISO-8601 timestamp to nanoseconds since epoch."""
        try:
            from datetime import datetime, timezone
            # Handle Z suffix and fractional seconds
            ts_str_clean = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1_000_000_000)
        except Exception:
            return time.time_ns()

    async def _retry_async(
        self,
        operation: str,
        fn: Any,
        *args: Any,
    ) -> Any:
        """Call *fn* synchronously in an executor with exponential-backoff retry."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._config.max_retries + 1):
            try:
                return await asyncio.get_event_loop().run_in_executor(None, fn, *args)
            except Exception as exc:
                last_exc = exc
                if attempt < self._config.max_retries:
                    delay = min(
                        self._config.retry_base_delay_s * (2 ** (attempt - 1)),
                        self._config.retry_max_delay_s,
                    )
                    jitter = delay * 0.1 * (hash(str(attempt)) % 20) / 20  # ~10% jitter
                    logger.warning(
                        "%s attempt %d/%d failed: %s. Retrying in %.2fs",
                        operation, attempt, self._config.max_retries, exc, delay + jitter,
                    )
                    await asyncio.sleep(delay + jitter)
                else:
                    logger.error(
                        "%s failed after %d attempts: %s",
                        operation, self._config.max_retries, exc,
                    )
        raise last_exc  # type: ignore[misc]
