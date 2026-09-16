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
import threading
import time
from datetime import datetime, timedelta, timezone

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "262"))
RELOAD = os.environ.get("RELOAD", "false").lower() in ("true", "1", "yes")
ENVIRONMENT = os.environ.get("BLM_ENV", "development")

# ── TEMPORAL-FRESHNESS FIX (audit 2026-09-16) ──────────────────────────
#: gap between scorecard runs.  The scorecard is a minutes-long CPU-bound
#: workload (observed 6,446s); back-to-back runs starved the API event
#: loop and stretched dashboard polls past the 15s freshness window.
#: A full poll-cycle gap between runs is the documented, configurable
#: separation — the workload itself is untouched (no thresholds, no
#: model math).
SCORECARD_INTER_RUN_GAP_S = float(
    os.environ.get("BLM_SCORECARD_INTER_RUN_GAP_S", "300"))
#: game_finished reconciler cadence — reconciliation runs on its OWN
#: thread so a slow scorecard can never delay it (audit §7).  Small,
#: read-mostly, bounded; never a second collector.
RECONCILER_INTERVAL_S = float(
    os.environ.get("BLM_GAME_FINISHED_RECONCILE_S", "30"))
#: a live-flagged game with NO snapshot AND NO WS observation for this
#: long has left the source's live board — its authoritative final state
#: exists upstream, so reconcile it to ended.
GAME_SOURCE_EXPIRY_S = float(
    os.environ.get("BLM_GAME_SOURCE_EXPIRY_S", "180"))


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

    # ── Mount dashboard sub-app ─────────────────────────────
    from blm_v2.dashboard.server import create_dashboard_app
    app.mount("/dashboard", create_dashboard_app())

    # ── BLM V4 PokerBet pipeline API (classification-aware) ──
    from blm_v4.api import router as v4_router
    app.include_router(v4_router)

    # ── Operator dashboard at the root ───────────────────────
    # http://<host>:2262/ is the live analytics terminal;
    # /api/v2/live and /api/v4/* remain the API/debug endpoints.
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    _dashboard_static = root / "blm_v4" / "dashboard" / "static"
    if _dashboard_static.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(_dashboard_static)),
            name="blm_operator_dashboard",
        )

        @app.get("/", include_in_schema=False)
        async def operator_dashboard():
            return FileResponse(str(_dashboard_static / "index.html"))
    else:
        logger.warning("operator dashboard static dir missing: %s", _dashboard_static)

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
        # The scheduler's ticks are LONG SYNCHRONOUS work (SQLite reads,
        # json_extract scans in the historical layer).  Running that
        # coroutine on the uvicorn event loop blocked every HTTP request
        # for seconds per tick (dashboard polls never completing — the
        # page froze on its initial state).  Give it its own loop/thread:
        # the API event loop stays responsive.
        def _run_scheduler():
            asyncio.run(scheduler.run())
        thread = threading.Thread(target=_run_scheduler,
                                  name="blm-scheduler", daemon=True)
        thread.start()
        app.state._scheduler_thread = thread

        # ── Projection accuracy scorecard (persisted, quality-gated) ──
        # The scorecard run is a long synchronous SQLite workload (1-3 min
        # over blm_pokerbet.db, per-section write transactions).  Running
        # it inline on the event loop starved /api/v2/* responses (16-40s+
        # timeouts), the 20s snapshot scheduler (missed ticks), and the V1
        # Playwright thread (GIL), while its long write-lock windows locked
        # the V4 collector out of the same DB ("database is locked" storms
        # that made the collector relaunch healthy browsers).  Run it in a
        # worker thread: the event loop stays responsive and the V4
        # collector's busy-timeout can wait out the lock windows.
        async def _scorecard_loop():
            from blm_v4.scorecard import Scorecard
            sc = Scorecard(root / "blm_pokerbet.db")
            while True:
                try:
                    t0 = time.monotonic()
                    logger.info("scorecard_run_start")
                    stats = await asyncio.to_thread(sc.run)
                    logger.info(
                        "scorecard_run",
                        elapsed_s=round(time.monotonic() - t0, 1),
                        **stats,
                    )
                except Exception:
                    logger.exception("scorecard_run_failed")
                # TEMPORAL-FRESHNESS FIX (audit 2026-09-16 §7): the scorecard
                # is a MINUTES-long synchronous workload (observed 6,446s —
                # 1h47m).  The old 60s sleep made runs back-to-back, holding
                # CPU at load ~17 on 8 cores and stretching /api/v4/live
                # polls past the dashboard's 15s freshness window — the
                # exact mechanism that showed STALE while the collector
                # heartbeat stayed green, and that delayed game_finished
                # reconciliation past game end.  A full poll-cycle gap
                # between runs lets the event loop drain; it does NOT gate
                # reconciliation (that has its own thread below) and it
                # never touches alert thresholds or model inputs.
                await asyncio.sleep(SCORECARD_INTER_RUN_GAP_S)

        app.state._scorecard_task = asyncio.create_task(_scorecard_loop())

        # ── game_finished reconciliation (audit 2026-09-16 §7) ───────────
        # A small, dedicated worker that reconciles the games-table status
        # against the authoritative source evidence INDEPENDENT of the
        # scorecard workload: a game whose WS/DOM stream has gone quiet
        # past the source-expiry bound has left the provider's live board —
        # mark it ended so under_alert_eligibility flips game_finished and
        # ACTIVE alerts close promptly, without waiting minutes for a
        # scorecard gap.  Read-mostly: one small UPDATE per detection, no
        # second collector, no snapshot writes, no re-derivation of any
        # model input.
        def _game_finished_reconciler():
            import sqlite3 as _sq
            import time as _t
            while True:
                try:
                    dbp = root / "blm_pokerbet.db"
                    conn = _sq.connect(f"file:{dbp}", uri=True, timeout=30)
                    conn.row_factory = _sq.Row
                    try:
                        cutoff = (datetime.now(timezone.utc)
                                  - timedelta(seconds=GAME_SOURCE_EXPIRY_S))
                        cut = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
                        rows = conn.execute(
                            """SELECT g.id, g.source_game_id FROM games g
                               WHERE g.status = 'live'
                                 AND g.last_seen_at < ?
                                 AND NOT EXISTS (
                                     SELECT 1 FROM snapshots s
                                     WHERE s.source_game_id = g.source_game_id
                                       AND s.captured_at >= ?)
                                 AND NOT EXISTS (
                                     SELECT 1 FROM market_observations mo
                                     WHERE mo.source_game_id =
                                           g.source_game_id
                                       AND mo.captured_at >= ?)""",
                            (cut, cut, cut)).fetchall()
                        for r in rows:
                            conn.execute(
                                "UPDATE games SET status='ended' WHERE id=?",
                                (r["id"],))
                            logger.info("game_finished_reconciled",
                                        game_id=r["source_game_id"])
                        if rows:
                            conn.commit()
                    finally:
                        conn.close()
                except Exception:
                    logger.exception("game_finished_reconcile_failed")
                _t.sleep(RECONCILER_INTERVAL_S)

        reconciler = threading.Thread(
            target=_game_finished_reconciler,
            name="blm-game-finished-reconciler", daemon=True)
        reconciler.start()
        app.state._reconciler_thread = reconciler
        logger.info("pipeline_started")

    @app.on_event("shutdown")
    async def stop_pipeline():
        await scheduler.stop()
        thread = getattr(app.state, "_scheduler_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=25)
        # the reconciler is daemon=True and its loop sleeps — the join is a
        # formality that keeps shutdown ordering explicit
        recthread = getattr(app.state, "_reconciler_thread", None)
        if recthread and recthread.is_alive():
            recthread.join(timeout=2)
        sct = getattr(app.state, "_scorecard_task", None)
        if sct and not sct.done():
            sct.cancel()
            try: await sct
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
