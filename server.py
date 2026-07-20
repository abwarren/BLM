#!/usr/bin/env python3
"""BLM V2 — Server Entry Point

Starts the full pipeline:
  V1 Playwright collector → V2 BLM Engine → SQLite TS → WebSocket push

Usage:  python server.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "262"))
RELOAD = os.environ.get("RELOAD", "false").lower() in ("true", "1", "yes")
ENVIRONMENT = os.environ.get("BLM_ENV", "development")


def main() -> None:
    from pathlib import Path
    root = Path(_project_root)

    from blm_v2.telemetry.logging import setup_logging, get_logger
    setup_logging(environment=ENVIRONMENT)
    logger = get_logger("server")
    logger.info("blm_v2_initializing", environment=ENVIRONMENT, host=HOST, port=PORT)

    # ── Dependencies ─────────────────────────────────────────
    from blm_v2.timeseries.sqlite_fallback import SQLiteTimeSeries
    from blm_v2.storage.sqlite import SQLiteStorage
    from blm_v2.events.bus import EventBus
    from blm_v2.engine.blm_engine import BLMEngine as CoreEngine
    from blm_v2.engine.adapter import BlmEngineAdapter
    from blm_v2.collector.v1_adapter import V1CollectorAdapter
    from blm_v2.collector.scheduler import SnapshotScheduler
    from blm_v2.alerts.manager import AlertManager, Alert
    from blm_v2.analytics.line_tracker import LineTracker
    from blm_v2.analytics.historical import HistoricalEngine
    from blm_v2.analytics.under_timing import UnderTimingEngine

    ts = SQLiteTimeSeries(db_path=root / "blm_ts.db")
    storage = SQLiteStorage(db_path=root / "blm_v2.db")
    event_bus = EventBus()
    alerts = AlertManager()

    core_engine = CoreEngine()
    engine_adapter = BlmEngineAdapter(core_engine)
    collector = V1CollectorAdapter(headless=True)

    # ── OLV/CLV + Historical + UNDER timing ──────────────────────
    line_tracker = LineTracker()
    historical_engine = HistoricalEngine(db_path=root / "blm_ts.db")
    under_timing_engine = UnderTimingEngine(historical_engine)

    # Adapter: scheduler calls emit(type_str, dict), EventBus expects BlmEvent
    class _SchedulerEventBus:
        async def emit(self, event_type: str, data: dict) -> None:
            logger.debug("scheduler_event", type=event_type)

    scheduler = SnapshotScheduler(
        collector=collector,
        ts_db=ts,
        engine=engine_adapter,
        event_bus=_SchedulerEventBus(),
        tick_s=20.0,
        line_tracker=line_tracker,
        under_timing_engine=under_timing_engine,
    )

    # ── Register event handlers ──────────────────────────────
    from blm_v2.models.events import BlmEvent

    # ── Create FastAPI app ───────────────────────────────────
    from blm_v2.api.v2_fastapi import create_v2_app
    app = create_v2_app()

    # ── Wire deps into global state for API endpoints ────────
    from blm_v2.api.dependencies import wire_dependencies
    wire_dependencies(
        ts_interface=ts,
        storage_interface=storage,
        event_bus=event_bus,
        blm_engine=engine_adapter,
    )

    # ── Lifespan handlers ────────────────────────────────────
    @app.on_event("startup")
    async def start_pipeline():
        logger.info("collector_starting")
        collector.start()
        logger.info("scheduler_starting")
        task = asyncio.create_task(scheduler.run())
        app.state._scheduler_task = task
        logger.info("pipeline_started")

    @app.on_event("shutdown")
    async def stop_pipeline():
        task = getattr(app.state, "_scheduler_task", None)
        if task and not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        collector.stop()
        logger.info("pipeline_stopped")

    # ── Start uvicorn ────────────────────────────────────────
    import uvicorn
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        reload=RELOAD,
        log_level="info" if ENVIRONMENT == "development" else "warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
