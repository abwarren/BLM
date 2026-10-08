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

# Data root — where runtime databases/state live.  Defaults to the code
# location (prior behaviour); set BLM_DATA_ROOT to run THIS checkout against a
# production data directory without shipping the code tree's working files.
_data_root_env = os.environ.get("BLM_DATA_ROOT")
_DATA_ROOT = os.path.abspath(_data_root_env) if _data_root_env else _project_root

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
#: DEDICATED SETTLE WORKER cadence (directive 2026-09-19) — settles newly
#: ended games into game_results every 30–60 s, in its OWN thread, so a
#: final never waits on the scorecard gap.  The 4-hour full-history sweep
#: itself is UNTOUCHED: this worker reuses the sweep's settlement rule
#: (blm_v4/settle_worker.py) and changes no thresholds, gates or logic.
SETTLE_INTERVAL_S = float(
    os.environ.get("BLM_SETTLE_INTERVAL_S", "45"))
#: WAL HYGIENE cadence (incident 2026-09-29, open item 1) — periodic
#: PRAGMA wal_checkpoint(PASSIVE) so the collector's high-churn WAL can
#: never balloon again (it reached 7.1 GB and turned every server restart
#: into lost "database is locked" races).  PASSIVE never blocks readers
#: or writers, so this cannot reintroduce the startup lock races; the
#: checkpoint runs only when the WAL exceeds WAL_HYGIENE_THRESHOLD_MB.
#: 0 disables the worker.
WAL_HYGIENE_INTERVAL_S = float(
    os.environ.get("BLM_WAL_HYGIENE_INTERVAL_S", "300"))
#: run a hygiene checkpoint only when the WAL file is at least this big —
#: below it the default wal_autocheckpoint is managing fine alone.
WAL_HYGIENE_THRESHOLD_MB = float(
    os.environ.get("BLM_WAL_HYGIENE_THRESHOLD_MB", "256"))
#: a live-flagged game with NO snapshot AND NO WS observation for this
#: long has left the source's live board — its authoritative final state
#: exists upstream, so reconcile it to ended.
GAME_SOURCE_EXPIRY_S = float(
    os.environ.get("BLM_GAME_SOURCE_EXPIRY_S", "180"))
#: RESULT RECONCILIATION worker cadence (directive 2026-09-23) — a game
#: that leaves the live market without a verified final is stamped
#: NEEDS_RECONCILIATION and its result is recovered from the PokerBet
#: results page (never treated as 'no result').  0 disables the worker.
RESULT_RECONCILE_INTERVAL_S = float(
    os.environ.get("BLM_RESULT_RECONCILE_INTERVAL_S", "300"))
#: games attempted per reconciliation pass (bounded Playwright work).
RESULT_RECONCILE_BATCH = int(
    os.environ.get("BLM_RESULT_RECONCILE_BATCH", "25"))
#: LIVE ALERT STATS recompute cadence (operator stats tab).  The stats
#: payload is a full-history scan (~10 s cold, dominated by the ~1.76M-row
#: snapshots group-by behind fingerprint_c5.q3_pace_reference), so it runs
#: on its OWN daemon thread and the /api/v4/stats route only ever reads the
#: last completed payload — a poll can never block on the scan.  0 disables
#: the worker (the route then reports 'unconfigured').  Read-only: it opens
#: both databases mode=ro and touches no gate, threshold or model input.
#:
#: The cadence is CHEAP: each tick first runs a ~6 ms change-detector
#: (settled count + last settled timestamp) and skips the scan entirely
#: unless a new game has settled.  The cohort can only move when one does,
#: so on a quiet box this costs ~6 ms/minute instead of ~10 s/minute.
STATS_INTERVAL_S = float(os.environ.get("BLM_STATS_INTERVAL_S", "60"))
#: Backstop age for the stats payload: rescan regardless at least this
#: often, so a change-detector miss can never leave the tab on a frozen
#: payload.  Applies even when no game has settled.
STATS_MAX_AGE_S = float(os.environ.get("BLM_STATS_MAX_AGE_S", "900"))
#: PACE REFERENCE worker cadence (empty-live-data fix, diagnostic
#: 2026-09-25).  Precomputes the competition pace + Q3 pace references on
#: its own thread so /api/v4/live never runs the minutes-long whole-table
#: GROUP BY on the request path.  A cheap MAX(rowid) change-detector
#: skips the scan unless a game/snapshot landed; a backstop age re-scans
#: regardless.  Read-only; no model input is touched.  0 disables the
#: worker (the API then computes inline — the pre-fix behavior).
PACE_REF_INTERVAL_S = float(os.environ.get("BLM_PACE_REF_INTERVAL_S", "60"))
#: Backstop age for the pace-reference payload (same rationale as
#: STATS_MAX_AGE_S).
PACE_REF_MAX_AGE_S = float(os.environ.get("BLM_PACE_REF_MAX_AGE_S", "900"))
#: Use the swarm feed that BACKS the PokerBet results page as the
#: authoritative result source (directive 2026-09-24).  The page renders a
#: list of games and carries no game id at all, so a DOM read can only guess
#: which game a score belongs to; the feed answers get_result_games{game_id}
#: exactly and returns a structured four-quarter score line.  Wired here, at
#: the composition root, so library defaults never open a network socket.
#: Set to 0 to fall back to the results-page DOM path.
RESULT_RECONCILE_SWARM = os.environ.get(
    "BLM_RESULT_RECONCILE_SWARM", "1") not in ("0", "false", "no", "")


def main() -> None:
    from pathlib import Path
    root = Path(_DATA_ROOT)          # runtime data (DBs/state)
    code_root = Path(_project_root)  # this checkout (code assets only)

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
    from blm_v4.api import configure_stats, configure_pace_reference_worker
    app.include_router(v4_router)

    # ── PACE REFERENCE worker (empty-live-data fix, diagnostic
    # 2026-09-25) ─────────────────────────────────────────────
    # v4_live consumed the competition pace + Q3 pace references INLINE
    # per request; the Q3 reference is a whole-table GROUP BY over the
    # ~2M-row snapshots table (minutes on the production DB), so every
    # cache-miss poll hung the sync worker pool and the dashboard froze
    # on its placeholders.  Mirroring StatsWorker, this daemon thread
    # precomputes BOTH references off the request path and publishes
    # them to blm_v4.api; v4_live reads the last completed payload and
    # never computes one.  Read-only over blm_pokerbet.db; no gate,
    # threshold, alert or model input is touched — only WHERE the same
    # numbers are computed moves.  0 disables it (the API wrappers then
    # fall back to inline computation — the pre-fix behavior).
    pace_ref_worker = None
    if PACE_REF_INTERVAL_S > 0:
        from blm_v4.api import _publish_pace_references
        from blm_v4.live_analytics.pace_reference_worker import (
            PaceReferenceWorker)
        pace_ref_worker = PaceReferenceWorker(
            root / "blm_pokerbet.db",
            interval_s=PACE_REF_INTERVAL_S,
            max_age_s=PACE_REF_MAX_AGE_S,
            log=logger,
            consumer=_publish_pace_references)
        configure_pace_reference_worker(pace_ref_worker)
        app.state._pace_ref_worker = pace_ref_worker

    # ── LIVE ALERT STATS worker (operator stats tab) ─────────
    # Recomputes the stats payload on its own daemon thread so the
    # /api/v4/stats route never scans on the request path.  READ-ONLY over
    # both databases; no gate, threshold, trigger or model input is touched
    # (fingerprints stay RECORDED context, the alert window is REPORTED not
    # enforced).  0 disables it.
    stats_worker = None
    if STATS_INTERVAL_S > 0:
        from blm_v4.live_analytics.fingerprint_stats import StatsWorker
        stats_worker = StatsWorker(
            root / "blm_pokerbet.db",
            root / "blm_metrics_clean.db",
            interval_s=STATS_INTERVAL_S,
            max_age_s=STATS_MAX_AGE_S,
            log=logger)
        configure_stats(stats_worker)
        app.state._stats_worker = stats_worker

    # ── AUTO-BETTING execution layer (directive 2026-09-21) ──
    # DRY_RUN by default; the kill switch persists OFF; credentials
    # are read from the environment at runtime only and are never
    # logged, served or stored.  The worker evaluates the SAME live
    # payload the dashboard consumes and can only ADD an execution
    # ledger — the alert logic itself is untouched.
    from blm_v4.betting.api import configure_betting, router as betting_router
    from blm_v4.betting.config import BettingConfig
    from blm_v4.betting.store import BettingStore
    from blm_v4.betting.worker import BettingWorker
    betting_cfg = BettingConfig.from_env(root)
    betting_store = BettingStore(betting_cfg.db_path)
    configure_betting(betting_store, betting_cfg)
    app.include_router(betting_router)

    # ── BLM ASSISTANT — read-only Q&A over the platform ───────────────
    # Mounted under the same global auth middleware as everything else in
    # /api/v4, so only signed-in dashboard users reach it.  The tools it
    # exposes are read-only by construction (mode=ro SQLite connections).
    from blm_v4.assistant.api import router as assistant_router
    app.include_router(assistant_router)


    # ── Operator dashboard at the root ───────────────────────
    # http://<host>:2262/ is the live analytics terminal;
    # /api/v2/live and /api/v4/* remain the API/debug endpoints.
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    from blm_v4.dashboard.asset_cache import NoCacheStaticFiles, NO_CACHE
    _dashboard_static = code_root / "blm_v4" / "dashboard" / "static"
    if _dashboard_static.is_dir():
        # no-cache (with the ETag) so a deploy can never be masked by a
        # browser reusing a stale bundle — see blm_v4/dashboard/asset_cache.py
        app.mount(
            "/static",
            NoCacheStaticFiles(directory=str(_dashboard_static)),
            name="blm_operator_dashboard",
        )

        @app.get("/", include_in_schema=False)
        async def operator_dashboard():
            return FileResponse(str(_dashboard_static / "index.html"),
                                headers={"Cache-Control": NO_CACHE})
    else:
        logger.warning("operator dashboard static dir missing: %s", _dashboard_static)

    # ── AUTHENTICATION (BLM login + session layer) ───────────
    # ADDITIVE: adds an ASGI guard + the /login page + /api/auth/*.
    # The guard is the single gate for every non-public route (pages, API
    # and the /ws handshake), so protection cannot be bypassed by calling a
    # URL directly.  Sessions live in their own SQLite database
    # (blm_auth.db); no table in any analytics/betting database is touched.
    from blm_v4.auth import install as install_auth
    _auth_state = install_auth(app, root, logger=logger)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Public liveness probe (no data, no internals)."""
        return {"status": "ok",
                "service": "blm",
                "auth": bool(_auth_state.get("enabled")),
                "users": _auth_state.get("users", 0)}

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

        # ── DEDICATED SETTLE WORKER (directive 2026-09-19) ────────────
        # A small dedicated settlement thread, the sibling of the
        # game_finished reconciler above: its ONLY responsibility is to
        # detect newly-ended games and compute/update game_results every
        # 30–60 s, so the resulting UNDER/OVER/PUSH verdict reaches the
        # frontend promptly instead of waiting for the scorecard gap.
        # Settlement semantics are REUSED verbatim from the scorecard
        # (same quality gate, same OK/UNKNOWN rule, same upsert) — no
        # alert threshold, gate, trigger or classification logic is
        # touched, and the 4-hour full-history sweep is UNTOUCHED.
        from blm_v4.settle_worker import SettleWorker
        settle_worker = SettleWorker(
            root / "blm_pokerbet.db", interval_s=SETTLE_INTERVAL_S)
        settle_worker.start()
        app.state._settle_worker = settle_worker
        logger.info("settle_worker_started", interval_s=SETTLE_INTERVAL_S)

        # ── WAL HYGIENE worker (incident 2026-09-29, open item 1) ────
        # The sibling janitor: stat-gates the WAL file and, when it has
        # grown past WAL_HYGIENE_THRESHOLD_MB, runs one
        # wal_checkpoint(PASSIVE) — the only mode that never waits on
        # readers/writers, so a pass can never add lock pressure to the
        # pipeline it is protecting.  No table is read or written; the
        # scorecard is not imported; failure is logged and retried on
        # the next tick.  0 disables the worker.
        if WAL_HYGIENE_INTERVAL_S > 0:
            from blm_v4.wal_hygiene import WalHygieneWorker
            wal_hygiene_worker = WalHygieneWorker(
                root / "blm_pokerbet.db",
                interval_s=WAL_HYGIENE_INTERVAL_S,
                threshold_mb=WAL_HYGIENE_THRESHOLD_MB)
            wal_hygiene_worker.start()
            app.state._wal_hygiene_worker = wal_hygiene_worker
            logger.info("wal_hygiene_worker_started",
                        interval_s=WAL_HYGIENE_INTERVAL_S,
                        threshold_mb=WAL_HYGIENE_THRESHOLD_MB)

        # ── PACE REFERENCE worker (empty-live-data fix) ───────────────
        # Started early: its FIRST scan is the expensive one (the Q3
        # whole-table GROUP BY), and until it lands /live serves empty
        # references (warmup).  Read-only; publishing is a dict swap.
        if pace_ref_worker is not None:
            pace_ref_worker.start()
            logger.info("pace_ref_worker_started",
                        interval_s=PACE_REF_INTERVAL_S)

        # ── RESULT RECONCILIATION WORKER (directive 2026-09-23) ──────
        # A disappearing live market is NOT evidence that a game has no
        # result: ended games without a verified final are stamped
        # NEEDS_RECONCILIATION and their result is recovered from the
        # PokerBet results page (verified against the canonical game
        # record before it is ever written).  Bounded, idempotent, and
        # forbidden from overwriting an OK verdict; conflicts are
        # flagged, never silently overwritten.  0 disables the worker.
        if RESULT_RECONCILE_INTERVAL_S > 0:
            from blm_v4.result_reconciler import ResultReconcilerWorker
            swarm = None
            if RESULT_RECONCILE_SWARM:
                try:
                    from blm_v4.swarm_results import SwarmResultsClient
                    swarm = SwarmResultsClient(log=logger)
                    logger.info("result_reconciler_source",
                                source="swarm_feed")
                except Exception:
                    logger.exception("swarm_client_unavailable")
            result_reconciler = ResultReconcilerWorker(
                root / "blm_pokerbet.db",
                interval_s=RESULT_RECONCILE_INTERVAL_S,
                batch_limit=RESULT_RECONCILE_BATCH,
                swarm=swarm)
            result_reconciler.start()
            app.state._result_reconciler = result_reconciler
            logger.info("result_reconciler_started",
                        interval_s=RESULT_RECONCILE_INTERVAL_S,
                        batch=RESULT_RECONCILE_BATCH,
                        swarm=bool(swarm))

        # ── AUTO-BETTING WORKER (directive 2026-09-21) ─────────────
        # DRY_RUN by default; the persisted kill switch defaults OFF.
        # The worker evaluates the SAME /api/v4/live payload the dashboard
        # consumes and can only ADD execution-ledger rows — the alert
        # logic is untouched.  Credentials are read from the environment
        # at runtime only; nothing is logged or stored.
        _betting_payload_cache = {"at": 0.0, "games": []}
        _betting_payload_lock = threading.Lock()

        def _live_games_for_betting() -> list:
            """The current /api/v4/live games list, computed in-process
            (the same authority the dashboard renders).  Failure-isolated:
            an error yields [] and the worker simply finds no candidates —
            never a stale evaluation. The bounded four-second shared cache
            prevents the UI state endpoint from multiplying full live scans."""
            now = time.monotonic()
            with _betting_payload_lock:
                if now - _betting_payload_cache["at"] < 4.0:
                    return _betting_payload_cache["games"]
            from blm_v4.api import v4_live as _v4_live
            _err = None
            try:
                games = (_v4_live(classification=None).get("games") or [])
            except Exception as _e:
                _err = f"{type(_e).__name__}: {_e}"[:200]
                games = []
            if _err is not None or not games:
                # VISIBILITY (2026-10-08): this list is the betting worker's
                # ONLY input, so a swallowed error or an empty payload here is
                # indistinguishable downstream from "no candidates" — that
                # ambiguity hid a stale/empty feed for hours.  Throttled to
                # once a minute so the 4-second cache cannot spam the journal.
                if now - getattr(_live_games_for_betting, "_last_warn", 0.0) > 60.0:
                    _live_games_for_betting._last_warn = now
                    logger.warning("betting_payload_unusable",
                                   games=len(games), error=_err)
            with _betting_payload_lock:
                _betting_payload_cache.update(at=time.monotonic(), games=games)
            return games

        # Expose the same authoritative in-process game view to the
        # read-only eligibility endpoint and the manual Total validator.
        configure_betting(betting_store, betting_cfg,
                          live_payload_fn=_live_games_for_betting)

        betting_worker = BettingWorker(
            betting_cfg, betting_store, _live_games_for_betting,
            poll_interval_s=5.0)
        betting_worker.start()
        app.state._betting_worker = betting_worker
        logger.info("betting_worker_started",
                    dry_run=betting_cfg.dry_run,
                    enabled=betting_store.is_enabled())

        # ── LIVE ALERT STATS worker ───────────────────────────────────
        # Started LAST of the workers: it is read-only and its first scan is
        # a ~10 s full-history read, so it must never delay the pipeline
        # (collector, scheduler, settlement, reconciliation, betting) coming
        # up.  It computes on its own thread and /api/v4/stats serves the
        # last completed payload, reporting 'warming' until the first lands.
        if stats_worker is not None:
            stats_worker.start()
            logger.info("stats_worker_started", interval_s=STATS_INTERVAL_S)
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
        settle_worker = getattr(app.state, "_settle_worker", None)
        if settle_worker is not None:
            settle_worker.stop(timeout=2)
        wal_hygiene_worker = getattr(app.state, "_wal_hygiene_worker", None)
        if wal_hygiene_worker is not None:
            wal_hygiene_worker.stop(timeout=2)
        result_reconciler = getattr(app.state, "_result_reconciler", None)
        if result_reconciler is not None:
            result_reconciler.stop(timeout=2)
            try:
                result_reconciler.reconciler._fetcher.close()
            except Exception:
                pass
        betting_worker = getattr(app.state, "_betting_worker", None)
        if betting_worker is not None:
            betting_worker.stop(timeout=2)
        stats_worker_obj = getattr(app.state, "_stats_worker", None)
        if stats_worker_obj is not None:
            stats_worker_obj.stop(timeout=2)
        pace_ref_worker = getattr(app.state, "_pace_ref_worker", None)
        if pace_ref_worker is not None:
            pace_ref_worker.stop(timeout=2)
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
