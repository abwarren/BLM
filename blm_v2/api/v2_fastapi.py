"""
BLM V2 — FastAPI Application

All V2 REST endpoints are mounted here.  Route handlers contain NO business
logic — they delegate entirely to the service layer injected via FastAPI's
``Depends()``.

Endpoints:
  GET  /api/v2/health            — Health check
  GET  /api/v2/live              — Current live game with BLM enrichment
  GET  /api/v2/game/{game_id}    — Single game details
  GET  /api/v2/history/{game_id} — Historical snapshots (query: from, to, limit, offset)
  GET  /api/v2/replay/{game_id}  — Replay data for a completed game
  GET  /api/v2/chart/{game_id}   — Chart data aggregated for plotting
  GET  /api/v2/events/{game_id}  — Events for a game
  GET  /api/v2/alerts            — Active alerts (optional game_id query)
  GET  /api/v2/traps/{game_id}   — Trap detection data
  GET  /api/v2/model             — BLM model state and configuration
  GET  /api/v2/games             — List all games

Dependencies are injected via ``Depends()``, keeping handlers thin.
All responses use Pydantic models.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from blm_v2.api.dependencies import (
    BLMEngineInterface,
    StorageInterface,
    TSInterface,
    get_blm_engine,
    get_metrics_collector_dep,
    get_storage_interface,
    get_ts_interface,
)
from blm_v2.api.websocket import handle_websocket, get_connection_manager
from blm_v2.telemetry.logging import CorrelationIdMiddleware, get_logger
from blm_v2.telemetry.metrics import MetricsCollector, get_metrics_collector

logger = get_logger(__name__)

API_PREFIX = "/api/v2"


# ═══════════════════════════════════════════════════════════════════════
# Pydantic response models
# ═══════════════════════════════════════════════════════════════════════


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "2.0.0"
    uptime_seconds: float = 0.0
    active_connections: int = 0
    total_requests: int = 0


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


class LiveResponse(BaseModel):
    game_id: str
    status: str
    home_team: str
    away_team: str
    home_score: int = 0
    away_score: int = 0
    clock: str = ""
    quarter: int = 0
    blm_score: Optional[float] = None
    confidence: Optional[float] = None
    pace: Optional[float] = None
    traps: list = []
    enriched_snapshot: Optional[Dict[str, Any]] = None
    last_updated: str = ""


class GameDetailResponse(BaseModel):
    game_id: str
    home_team: str
    away_team: str
    status: str
    start_time: str
    home_score: int
    away_score: int
    quarter: int
    clock: str
    possession: Optional[str] = None
    blm_score: Optional[float] = None
    confidence: Optional[float] = None
    pace: Optional[float] = None
    traps: list = []
    prediction: Optional[Dict[str, Any]] = None
    latest_snapshot: Optional[Dict[str, Any]] = None


class HistoryResponse(BaseModel):
    game_id: str
    total: int
    limit: int
    offset: int
    snapshots: list


class ReplayResponse(BaseModel):
    game_id: str
    total_frames: int
    frames: list


class ChartResponse(BaseModel):
    game_id: str
    data_points: list


class EventsResponse(BaseModel):
    game_id: str
    total: int
    events: list


class AlertsResponse(BaseModel):
    total: int
    alerts: list


class TrapsResponse(BaseModel):
    game_id: str
    active_traps: list = []
    trap_history: list = []
    trap_count: int = 0
    last_trap_time: Optional[str] = None


class ModelResponse(BaseModel):
    version: str
    status: str
    uptime_seconds: float = 0.0
    total_snapshots_processed: int = 0
    active_games: int = 0
    engine_config: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}


class GameListItem(BaseModel):
    game_id: str
    home_team: str
    away_team: str
    status: str
    start_time: str
    home_score: int = 0
    away_score: int = 0


class GamesListResponse(BaseModel):
    total: int
    games: list


# ── Line Analysis / OLV/CLV models ─────────────────────────────────────

class LineAnalysisResponse(BaseModel):
    game_id: str
    total_entries: int = 0
    olv: Optional[float] = None
    entries: list = []
    under_signals: Optional[Dict[str, Any]] = None


class UnderSignalsResponse(BaseModel):
    game_id: str = ""
    league: str = "Cyber 2K26"
    under_timing_score: float = 0.0
    confidence: float = 0.0
    status: str = "PASS"
    components: Dict[str, float] = {}
    signals_met: list = []
    signals_missed: list = []
    last_updated: str = ""


class LearnedRangesResponse(BaseModel):
    league: str = "Cyber 2K26"
    total_games: int = 0
    total_snapshots: int = 0
    confidence: float = 0.0
    olv_distribution: Dict[str, float] = {}
    excursion_distribution: Dict[str, float] = {}
    burst: Dict[str, float] = {}
    freeze: Dict[str, float] = {}
    regression: Dict[str, float] = {}


# ═══════════════════════════════════════════════════════════════════════
# Route handlers
# ═══════════════════════════════════════════════════════════════════════


# ── GET /api/v2/health ────────────────────────────────────────────────

async def health_check(
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return the current health status of the V2 API server."""
    metrics.record_request()
    snap = metrics.snapshot()
    ws_count = get_connection_manager().active_connections
    return HealthResponse(
        status="ok",
        version="2.0.0",
        uptime_seconds=snap.uptime_seconds,
        active_connections=ws_count,
        total_requests=snap.total_requests,
    )


# ── GET /api/v2/live ──────────────────────────────────────────────────

async def get_live_game(
    ts: TSInterface = Depends(get_ts_interface),
    engine: BLMEngineInterface = Depends(get_blm_engine),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return the current live game with full BLM enrichment."""
    with metrics.timer("api_response_time"):
        live = await ts.get_live_game()
        if live is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No live game currently in progress",
            )
        enriched = await engine.enrich_snapshot(live)
        return LiveResponse(
            game_id=enriched.get("game_id", live.get("game_id", "")),
            status=enriched.get("status", live.get("status", "unknown")),
            home_team=enriched.get("home_team", live.get("home_team", "")),
            away_team=enriched.get("away_team", live.get("away_team", "")),
            home_score=enriched.get("home_score", live.get("home_score", 0)),
            away_score=enriched.get("away_score", live.get("away_score", 0)),
            clock=enriched.get("clock", live.get("clock", "")),
            quarter=enriched.get("quarter", live.get("quarter", 0)),
            blm_score=enriched.get("blm_score"),
            confidence=enriched.get("confidence"),
            pace=enriched.get("pace"),
            traps=enriched.get("traps", []),
            enriched_snapshot=enriched,
            last_updated=enriched.get("last_updated", live.get("last_updated", "")),
        )


# ── GET /api/v2/game/{game_id} ────────────────────────────────────────

async def get_game_detail(
    game_id: str,
    ts: TSInterface = Depends(get_ts_interface),
    engine: BLMEngineInterface = Depends(get_blm_engine),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return full details for a single game, including the latest snapshot."""
    with metrics.timer("api_response_time"):
        detail = await ts.get_game_detail(game_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Game {game_id!r} not found",
            )
        return GameDetailResponse(**detail)


# ── GET /api/v2/history/{game_id} ─────────────────────────────────────

async def get_game_history(
    game_id: str,
    from_ts: Optional[str] = Query(None, alias="from", description="Start timestamp (ISO8601)"),
    to: Optional[str] = Query(None, alias="to", description="End timestamp (ISO8601)"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Result offset"),
    ts: TSInterface = Depends(get_ts_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return historical snapshots for a game with time-range filtering."""
    with metrics.timer("api_response_time"):
        snapshots = await ts.get_snapshots(
            game_id=game_id,
            from_ts=from_ts,
            to_ts=to,
            limit=limit,
            offset=offset,
        )
        return HistoryResponse(
            game_id=game_id,
            total=len(snapshots),
            limit=limit,
            offset=offset,
            snapshots=snapshots,
        )


# ── GET /api/v2/replay/{game_id} ──────────────────────────────────────

async def get_game_replay(
    game_id: str,
    ts: TSInterface = Depends(get_ts_interface),
    engine: BLMEngineInterface = Depends(get_blm_engine),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return all snapshots for a completed game in replay format."""
    with metrics.timer("replay_frame_time"):
        frames = await ts.get_replay_snapshots(game_id)
        if not frames:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No replay data found for game {game_id!r}",
            )
        return ReplayResponse(
            game_id=game_id,
            total_frames=len(frames),
            frames=frames,
        )


# ── GET /api/v2/chart/{game_id} ───────────────────────────────────────

async def get_chart_data(
    game_id: str,
    ts: TSInterface = Depends(get_ts_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return chart data (aggregated / optimised for plotting)."""
    with metrics.timer("api_response_time"):
        data = await ts.get_chart_data(game_id)
        if not data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No chart data found for game {game_id!r}",
            )
        return ChartResponse(
            game_id=game_id,
            data_points=data,
        )


# ── GET /api/v2/events/{game_id} ──────────────────────────────────────

async def get_game_events(
    game_id: str,
    storage: StorageInterface = Depends(get_storage_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return all recorded events for a game."""
    with metrics.timer("api_response_time"):
        events = await storage.get_events(game_id)
        return EventsResponse(
            game_id=game_id,
            total=len(events),
            events=events,
        )


# ── GET /api/v2/alerts ────────────────────────────────────────────────

async def get_alerts(
    game_id: Optional[str] = Query(None, description="Filter by game ID"),
    storage: StorageInterface = Depends(get_storage_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return active alerts, optionally filtered by game_id."""
    with metrics.timer("api_response_time"):
        alerts = await storage.get_alerts(game_id=game_id)
        return AlertsResponse(
            total=len(alerts),
            alerts=alerts,
        )


# ── GET /api/v2/traps/{game_id} ───────────────────────────────────────

async def get_traps(
    game_id: str,
    engine: BLMEngineInterface = Depends(get_blm_engine),
    storage: StorageInterface = Depends(get_storage_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return trap detection data for a game."""
    with metrics.timer("api_response_time"):
        traps = await engine.detect_traps(game_id)
        stored = await storage.get_traps(game_id)
        return TrapsResponse(
            game_id=game_id,
            active_traps=traps,
            trap_history=stored.get("trap_history", []),
            trap_count=len(traps) + len(stored.get("trap_history", [])),
            last_trap_time=stored.get("last_trap_time"),
        )


# ── GET /api/v2/model ─────────────────────────────────────────────────

async def get_model_state(
    engine: BLMEngineInterface = Depends(get_blm_engine),
    storage: StorageInterface = Depends(get_storage_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return the current BLM model state, configuration, and engine metrics."""
    with metrics.timer("api_response_time"):
        config = await engine.get_config()
        state = await storage.get_model_state()
        snap = metrics.snapshot()
        return ModelResponse(
            version=state.get("version", "2.0.0"),
            status=state.get("status", "running"),
            uptime_seconds=snap.uptime_seconds,
            total_snapshots_processed=state.get("total_snapshots_processed", 0),
            active_games=state.get("active_games", 0),
            engine_config=config,
            metrics={
                name: {"min": m.min, "max": m.max, "avg": m.avg, "count": m.count}
                for name, m in snap.metrics.items()
            },
        )


# ── GET /api/v2/games ─────────────────────────────────────────────────

async def list_games(
    ts: TSInterface = Depends(get_ts_interface),
    storage: StorageInterface = Depends(get_storage_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return all known games (merged from TS and storage)."""
    with metrics.timer("api_response_time"):
        ts_games = await ts.list_games()
        storage_games = await storage.list_games()

        # Merge: index by game_id, storage overrides for metadata
        merged = {}
        for g in ts_games:
            merged[g["game_id"]] = g
        for g in storage_games:
            merged[g["game_id"]] = g  # storage wins

        items = [
            GameListItem(
                game_id=meta["game_id"],
                home_team=meta.get("home_team", ""),
                away_team=meta.get("away_team", ""),
                status=meta.get("status", "unknown"),
                start_time=meta.get("start_time", ""),
                home_score=meta.get("home_score", 0),
                away_score=meta.get("away_score", 0),
            )
            for meta in merged.values()
        ]
        items.sort(key=lambda x: x.start_time, reverse=True)

        return GamesListResponse(total=len(items), games=[m.model_dump() for m in items])


# ── GET /api/v2/line-analysis/{game_id} ────────────────────────────────

async def get_line_analysis(
    game_id: str,
    ts: TSInterface = Depends(get_ts_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return OLV/CLV line analysis history for a game."""
    with metrics.timer("api_response_time"):
        entries = await ts.query_line_analysis(game_id=game_id, limit=1000)
        if not entries:
            # Try live analysis as fallback
            live = await ts.get_live_line_analysis()
            if live and live.get("game_id") == game_id:
                entries = [live]
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No line analysis data for game {game_id!r}",
                )
        olv = next((e.get("olv") for e in entries if e.get("olv") is not None), None)
        return LineAnalysisResponse(
            game_id=game_id,
            total_entries=len(entries),
            olv=olv,
            entries=entries[-200:],  # last 200 data points
        )


# ── GET /api/v2/under-signals ──────────────────────────────────────────

async def get_under_signals(
    ts: TSInterface = Depends(get_ts_interface),
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return current UNDER timing signal for the live game."""
    with metrics.timer("api_response_time"):
        import json
        live_analysis = await ts.get_live_line_analysis()
        if not live_analysis:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No live line analysis available",
            )
        # UnderTimingEngine is wired globally; read its last result from
        # the analysis data_json which stores the full under_timing result
        data_str = live_analysis.get("data_json")
        under_result = None
        if data_str and isinstance(data_str, str):
            try:
                parsed = json.loads(data_str)
                under_result = parsed.get("under_timing_result")
            except (json.JSONDecodeError, TypeError):
                pass

        return UnderSignalsResponse(
            game_id=live_analysis.get("game_id", ""),
            league="Cyber 2K26",
            under_timing_score=live_analysis.get("under_timing_score", 0.0),
            confidence=live_analysis.get("under_timing_confidence", 0.0),
            status=live_analysis.get("under_timing_status", "PASS"),
            components=live_analysis.get("under_components", {}),
            signals_met=live_analysis.get("signals_met", []),
            signals_missed=live_analysis.get("signals_missed", []),
            last_updated=live_analysis.get("timestamp", ""),
        )


# ── GET /api/v2/learned-ranges ─────────────────────────────────────────

async def get_learned_ranges(
    league: str = "Cyber 2K26",
    metrics: MetricsCollector = Depends(get_metrics_collector_dep),
):
    """Return the historical learned distributions for a league."""
    with metrics.timer("api_response_time"):
        from blm_v2.analytics.historical import HistoricalEngine
        engine = HistoricalEngine()
        profile = engine.get_profile(league)
        return LearnedRangesResponse(
            league=league,
            total_games=profile.total_games,
            total_snapshots=profile.total_snapshots,
            confidence=profile.confidence,
            olv_distribution=profile.to_dict().get("olv_distribution", {}),
            excursion_distribution=profile.to_dict().get("excursion_distribution", {}),
            burst=profile.to_dict().get("burst", {}),
            freeze=profile.to_dict().get("freeze", {}),
            regression=profile.to_dict().get("regression", {}),
        )


# ═══════════════════════════════════════════════════════════════════════
# App factory
# ═══════════════════════════════════════════════════════════════════════


_ROUTES: list[tuple[str, str, Any]] = [
    ("GET", "/health", health_check),
    ("GET", "/live", get_live_game),
    ("GET", "/game/{game_id}", get_game_detail),
    ("GET", "/history/{game_id}", get_game_history),
    ("GET", "/replay/{game_id}", get_game_replay),
    ("GET", "/chart/{game_id}", get_chart_data),
    ("GET", "/events/{game_id}", get_game_events),
    ("GET", "/alerts", get_alerts),
    ("GET", "/traps/{game_id}", get_traps),
    ("GET", "/model", get_model_state),
    ("GET", "/games", list_games),
    ("GET", "/line-analysis/{game_id}", get_line_analysis),
    ("GET", "/under-signals", get_under_signals),
    ("GET", "/learned-ranges", get_learned_ranges),
]

_RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "/health": HealthResponse,
    "/live": LiveResponse,
    "/game/{game_id}": GameDetailResponse,
    "/history/{game_id}": HistoryResponse,
    "/replay/{game_id}": ReplayResponse,
    "/chart/{game_id}": ChartResponse,
    "/events/{game_id}": EventsResponse,
    "/alerts": AlertsResponse,
    "/traps/{game_id}": TrapsResponse,
    "/model": ModelResponse,
    "/games": GamesListResponse,
    "/line-analysis/{game_id}": LineAnalysisResponse,
    "/under-signals": UnderSignalsResponse,
    "/learned-ranges": LearnedRangesResponse,
}


def create_v2_app() -> FastAPI:
    """Build and return the configured FastAPI V2 application.

    CORS is enabled for dashboard access.  Middleware stack includes
    correlation ID injection.
    """
    app = FastAPI(
        title="BLM V2 API",
        version="2.0.0",
        description="Betting Logic Model — V2 Platform API",
        docs_url="/api/v2/docs",
        redoc_url="/api/v2/redoc",
        openapi_url="/api/v2/openapi.json",
    )

    # ── CORS ───────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Correlation ID middleware ───────────────────────────────
    app.add_middleware(CorrelationIdMiddleware)

    # ── Exception handlers ─────────────────────────────────────
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error=exc.detail, detail=str(exc)).model_dump(),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request, exc: Exception):
        logger.exception("unhandled_error", path=str(request.url))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                error="Internal server error",
                detail=str(exc),
            ).model_dump(),
        )

    # ── Register routes ─────────────────────────────────────────
    for method, path, handler in _ROUTES:
        full_path = f"{API_PREFIX}{path}"
        app.add_api_route(
            full_path,
            handler,
            methods=[method],
            response_model=_RESPONSE_MODELS[path],
            summary=handler.__doc__.strip().split("\n")[0] if handler.__doc__ else None,
        )

    # ── Prometheus /metrics endpoint ────────────────────────────
    @app.get("/metrics")
    async def prometheus_metrics():
        """Prometheus metrics endpoint — refreshed on each scrape."""
        from blm_v2.api.prometheus_metrics import refresh_metrics, render_metrics
        from blm_v2.api.dependencies import get_ts_interface, get_storage_interface
        from fastapi.responses import Response

        ts = await get_ts_interface()
        storage = await get_storage_interface()
        await refresh_metrics(ts, storage)

        return Response(
            content=render_metrics(),
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # ── WebSocket endpoint ──────────────────────────────────────
    @app.websocket("/ws")
    async def websocket_endpoint(websocket):
        ts = await get_ts_interface()
        storage = await get_storage_interface()
        engine = await get_blm_engine()
        metrics_instance = get_metrics_collector()
        await handle_websocket(websocket, ts, storage, engine, metrics_instance)

    # ── Mount dashboard sub-app ────────────────────────────────
    from blm_v2.dashboard.server import create_dashboard_app
    app.mount("/dashboard", create_dashboard_app())

    # ── Startup / shutdown events ───────────────────────────────
    @app.on_event("startup")
    async def on_startup():
        logger.info("v2_app_started", version="2.0.0")

    @app.on_event("shutdown")
    async def on_shutdown():
        logger.info("v2_app_shutting_down")

    return app
