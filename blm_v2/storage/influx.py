"""InfluxDB implementation of the BLM V2 storage interface.

Uses InfluxDB 3's SQL query capabilities to store and retrieve structured
(non-time-series) data — game metadata and alerts — alongside the time-series
snapshots.  This avoids a separate database while still offering a consistent
interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from blm_v2.storage.base import StorageDB
from blm_v2.timeseries.influx import InfluxDBTimeSeries, InfluxDBConfig

logger = logging.getLogger(__name__)

# ── Measurement names ─────────────────────────────────────────────

_MEASUREMENT_GAMES = "blm_games"
_MEASUREMENT_ALERTS = "blm_alerts"


class InfluxDBStorage(StorageDB):
    """InfluxDB-backed CRUD for games and alerts.

    Stores structured data as InfluxDB points under dedicated measurements.
    Uses the same ``InfluxDBClient`` as the time-series layer (shared via
    dependency injection) so there's only one connection pool.

    Usage::

        ts = InfluxDBTimeSeries(...)
        store = InfluxDBStorage(ts_db=ts)
        await store.save_game({"game_id": "g1", "home_team": "Sharks", ...})
    """

    def __init__(
        self,
        ts_db: InfluxDBTimeSeries,
    ) -> None:
        """Create a storage adapter sharing the TS backend's client.

        Args:
            ts_db: An **already-initialized** ``InfluxDBTimeSeries`` instance.
                Its internal ``InfluxDBClient`` is reused for all storage ops.
        """
        self._ts = ts_db
        self._client = ts_db._client
        self._config = ts_db._config
        self._write_api = None
        self._query_api = None

    @property
    def _write(self):
        if self._write_api is None:
            from influxdb_client.client.write_api import SYNCHRONOUS
            self._write_api = self._client.write_api(write_type=SYNCHRONOUS)
        return self._write_api

    @property
    def _query(self):
        if self._query_api is None:
            self._query_api = self._client.query_api()
        return self._query_api

    # ── Games ─────────────────────────────────────────────────────

    async def save_game(self, game: dict[str, Any]) -> None:
        from influxdb_client import Point, WritePrecision

        point = (
            Point(_MEASUREMENT_GAMES)
            .tag("game_id", game.get("game_id", ""))
            .tag("league", game.get("league", "Cyber 2K26"))
            .field("home_team", game.get("home_team", ""))
            .field("away_team", game.get("away_team", ""))
            .field("status", game.get("status", "live"))
            .field("season", game.get("season", "") or "")
            .field("data_json", json.dumps(game, default=str))
        )
        if game.get("updated_at") or game.get("timestamp"):
            ts = game.get("updated_at") or game["timestamp"]
            point = point.time(self._ts._parse_timestamp(ts), WritePrecision.NS)

        def _write() -> None:
            self._write.write(
                bucket=self._config.bucket,
                org=self._config.org,
                record=point,
            )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def get_game(self, game_id: str) -> Optional[dict[str, Any]]:
        flux = f'''
            from(bucket: "{self._config.bucket}")
                |> range(start: 0)
                |> filter(fn: (r) => r._measurement == "{_MEASUREMENT_GAMES}"
                    and r.game_id == "{game_id}")
                |> last()
                |> limit(n: 1)
        '''

        def _query() -> Optional[dict[str, Any]]:
            tables = self._query.query(flux, org=self._config.org)
            for table in tables:
                for record in table.records:
                    d = self._ts._flux_record_to_dict(record)
                    data_json = d.pop("data_json", None)
                    if data_json and isinstance(data_json, str):
                        try:
                            reconstructed = json.loads(data_json)
                            return reconstructed
                        except (json.JSONDecodeError, TypeError):
                            pass
                    return d
            return None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    async def list_games(self, limit: int = 50) -> list[dict[str, Any]]:
        flux = f'''
            from(bucket: "{self._config.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{_MEASUREMENT_GAMES}")
                |> group(columns: ["game_id"])
                |> last()
                |> limit(n: {limit})
        '''

        def _query() -> list[dict[str, Any]]:
            tables = self._query.query(flux, org=self._config.org)
            results: list[dict[str, Any]] = []
            seen: set[str] = set()
            for table in tables:
                for record in table.records:
                    d = self._ts._flux_record_to_dict(record)
                    gid = d.get("game_id", "")
                    if gid in seen:
                        continue
                    seen.add(gid)
                    data_json = d.pop("data_json", None)
                    if data_json and isinstance(data_json, str):
                        try:
                            reconstructed = json.loads(data_json)
                            results.append(reconstructed)
                            continue
                        except (json.JSONDecodeError, TypeError):
                            pass
                    results.append(d)
            return results[:limit]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)

    # ── Alerts ────────────────────────────────────────────────────

    async def save_alert(self, alert: dict[str, Any]) -> None:
        from influxdb_client import Point, WritePrecision

        point = (
            Point(_MEASUREMENT_ALERTS)
            .tag("game_id", alert.get("game_id", ""))
            .tag("type", alert.get("type", "general"))
            .tag("severity", alert.get("severity", "info"))
            .field("message", alert.get("message", ""))
            .field("data_json", json.dumps(alert, default=str))
        )
        ts = alert.get("timestamp")
        if ts:
            point = point.time(self._ts._parse_timestamp(ts), WritePrecision.NS)

        def _write() -> None:
            self._write.write(
                bucket=self._config.bucket,
                org=self._config.org,
                record=point,
            )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write)

    async def get_alerts(
        self,
        game_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        flux = f'''
            from(bucket: "{self._config.bucket}")
                |> range(start: 0)
                |> filter(fn: (r) => r._measurement == "{_MEASUREMENT_ALERTS}"
                    and r.game_id == "{game_id}")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
        '''

        def _query() -> list[dict[str, Any]]:
            tables = self._query.query(flux, org=self._config.org)
            results: list[dict[str, Any]] = []
            for table in tables:
                for record in table.records:
                    d = self._ts._flux_record_to_dict(record)
                    data_json = d.pop("data_json", None)
                    if data_json and isinstance(data_json, str):
                        try:
                            reconstructed = json.loads(data_json)
                            results.append(reconstructed)
                            continue
                        except (json.JSONDecodeError, TypeError):
                            pass
                    results.append(d)
            return results[:limit]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)
