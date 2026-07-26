"""
BLM V3 — Historical Research API Routes.

FastAPI router with all endpoints under ``/api/v2/historical/``.

Dependencies are injected via ``request.state.historical_db`` (set by middleware)
or by providing ``HistoricalDatabase`` at creation time.

Endpoints:
  GET  /games              — List historical games
  GET  /games/{game_id}    — Single game detail
  GET  /snapshots/{game_id} — Snapshots with time range filtering
  GET  /snapshots/{game_id}/aggregated — Time-interval aggregated data
  GET  /metrics/{game_id}  — Named metric series
  GET  /signals            — Signal query
  GET  /events/{game_id}   — Market events
  GET  /compare            — Multi-game comparative data
  POST /compare/query      — Filter-based comparative query
  GET  /health             — Database health
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.config import DEFAULT_DB_PATH


# ═══════════════════════════════════════════════════════════════════════
# Response Models
# ═══════════════════════════════════════════════════════════════════════


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "3.0.0"
    game_count: int = 0
    snapshot_count: int = 0
    signal_count: int = 0
    db_path: str = ""
    uptime_seconds: float = 0.0


class GameItem(BaseModel):
    game_id: str
    home_team: str
    away_team: str
    status: str
    league: str = "Cyber 2K26"
    start_time: Optional[str] = None
    final_home: Optional[int] = None
    final_away: Optional[int] = None
    final_total: Optional[int] = None
    total_snapshots: int = 0


class GameListResponse(BaseModel):
    total: int
    games: list[GameItem]


class GameDetailResponse(BaseModel):
    game: dict[str, Any]
    snapshot_count: int = 0
    signal_count: int = 0


class SnapshotListResponse(BaseModel):
    game_id: str
    total: int
    limit: int
    offset: int
    snapshots: list[dict[str, Any]]


class AggregatedResponse(BaseModel):
    game_id: str
    interval_seconds: float
    intervals: list[dict[str, Any]]


class MetricsResponse(BaseModel):
    game_id: str
    series: dict[str, list[dict[str, Any]]]


class SignalItem(BaseModel):
    id: str
    signal_type: str
    severity: str
    value: Optional[float] = None
    timestamp: str
    description: Optional[str] = None


class SignalListResponse(BaseModel):
    total: int
    signals: list[SignalItem]


class EventItem(BaseModel):
    id: str
    event_type: str
    timestamp: str
    duration_seconds: Optional[float] = None
    magnitude: Optional[float] = None
    description: Optional[str] = None


class EventListResponse(BaseModel):
    total: int
    events: list[EventItem]


class ComparativeResponse(BaseModel):
    game_ids: list[str]
    metrics: list[str]
    series: dict[str, dict[str, list[dict[str, Any]]]]


class ComparativeQueryRequest(BaseModel):
    trap_min: Optional[float] = Field(default=None, ge=0, le=100)
    trap_max: Optional[float] = Field(default=None, ge=0, le=100)
    inflation_min: Optional[float] = None
    inflation_max: Optional[float] = None
    confidence_min: Optional[float] = Field(default=None, ge=0, le=1)
    confidence_max: Optional[float] = Field(default=None, ge=0, le=1)
    quarter_min: Optional[int] = Field(default=None, ge=1, le=10)
    quarter_max: Optional[int] = Field(default=None, ge=1, le=10)
    league: Optional[str] = None
    result: Optional[str] = Field(default=None, pattern="^(over|under)$")


class ComparativeQueryResponse(BaseModel):
    matched_games: list[dict[str, Any]]
    count: int


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════
# Router Factory
# ═══════════════════════════════════════════════════════════════════════


def create_historical_router(
    db: Optional[HistoricalDatabase] = None,
) -> APIRouter:
    """Create a FastAPI ``APIRouter`` with all historical endpoints.

    Args:
        db: Optional ``HistoricalDatabase`` instance.  If omitted, creates
            one from the default path.

    Returns:
        Configured ``APIRouter``.
    """
    router = APIRouter()
    historical_db = db or HistoricalDatabase()

    @router.on_event("startup")
    async def init_db():
        await historical_db.init()

    # ── Health ────────────────────────────────────────────────────

    @router.get("/health", response_model=HealthResponse)
    async def get_health():
        """Return the health status of the historical database."""
        try:
            info = await historical_db.get_health()
            return HealthResponse(
                status=info.get("status", "error"),
                game_count=info.get("game_count", 0),
                snapshot_count=info.get("snapshot_count", 0),
                signal_count=info.get("signal_count", 0),
                db_path=str(historical_db.db_path),
                uptime_seconds=historical_db.uptime_seconds,
            )
        except Exception as e:
            return HealthResponse(status="error", db_path=str(e))

    # ── Games ─────────────────────────────────────────────────────

    @router.get("/games", response_model=GameListResponse)
    async def list_games(
        league: Optional[str] = Query(None, description="Filter by league"),
        status: Optional[str] = Query(None, description="Filter by status"),
        limit: int = Query(50, ge=1, le=200, description="Max results"),
        offset: int = Query(0, ge=0, description="Result offset"),
    ):
        """List historical games with optional filtering."""
        games = await historical_db.list_games(
            league=league, status=status, limit=limit, offset=offset,
        )
        items = [
            GameItem(
                game_id=g.get("id", ""),
                home_team=g.get("home_team", ""),
                away_team=g.get("away_team", ""),
                status=g.get("status", "unknown"),
                league=g.get("league", "Cyber 2K26"),
                start_time=g.get("start_time"),
                final_home=g.get("final_home"),
                final_away=g.get("final_away"),
                final_total=g.get("final_total"),
                total_snapshots=g.get("total_snapshots", 0),
            )
            for g in games
        ]
        return GameListResponse(total=len(items), games=items)

    @router.get("/games/{game_id}", response_model=GameDetailResponse)
    async def get_game_detail(game_id: str):
        """Return full detail for a single historical game."""
        game = await historical_db.get_game(game_id)
        if not game:
            raise HTTPException(status_code=404,
                                detail=f"Game {game_id!r} not found")
        snap_count = await historical_db.count_snapshots(game_id)
        sigs = await historical_db.query_signals(game_id=game_id)
        return GameDetailResponse(
            game=game,
            snapshot_count=snap_count,
            signal_count=len(sigs),
        )

    # ── Snapshots ────────────────────────────────────────────────

    @router.get("/snapshots/{game_id}", response_model=SnapshotListResponse)
    async def get_snapshots(
        game_id: str,
        from_ts: Optional[str] = Query(None, alias="from", description="Start timestamp"),
        to: Optional[str] = Query(None, alias="to", description="End timestamp"),
        limit: int = Query(5000, ge=1, le=100000, description="Max results"),
        offset: int = Query(0, ge=0, description="Result offset"),
    ):
        """Return historical snapshots for a game."""
        snapshots = await historical_db.query_snapshots(
            game_id=game_id, from_ts=from_ts, to_ts=to,
            limit=limit, offset=offset,
        )
        return SnapshotListResponse(
            game_id=game_id, total=len(snapshots),
            limit=limit, offset=offset,
            snapshots=snapshots,
        )

    @router.get(
        "/snapshots/{game_id}/aggregated",
        response_model=AggregatedResponse,
    )
    async def get_aggregated(
        game_id: str,
        interval: float = Query(30.0, description="Interval in seconds"),
    ):
        """Return time-interval aggregated data."""
        intervals = await historical_db.query_aggregated(
            game_id=game_id, interval_seconds=interval,
        )
        return AggregatedResponse(
            game_id=game_id, interval_seconds=interval,
            intervals=intervals,
        )

    # ── Metrics ──────────────────────────────────────────────────

    @router.get("/metrics/{game_id}", response_model=MetricsResponse)
    async def get_metrics(
        game_id: str,
        metrics: str = Query(
            "total_line,trap_meter,inflation_index,confidence,momentum",
            description="Comma-separated metric names",
        ),
        limit: int = Query(10000, ge=1, le=100000),
    ):
        """Return named metric series for a game."""
        metric_names = [m.strip() for m in metrics.split(",") if m.strip()]
        snapshots = await historical_db.query_snapshots(
            game_id=game_id, limit=limit,
        )
        series: dict[str, list[dict[str, Any]]] = {}
        for m in metric_names:
            series[m] = [
                {"t": s.get("timestamp"), "v": s.get(m)}
                for s in snapshots if s.get(m) is not None
            ]
        return MetricsResponse(game_id=game_id, series=series)

    # ── Signals ──────────────────────────────────────────────────

    @router.get("/signals", response_model=SignalListResponse)
    async def get_signals(
        game_id: Optional[str] = Query(None, description="Filter by game"),
        signal_type: Optional[str] = Query(None, description="Filter by type"),
        severity: Optional[str] = Query(None, description="Filter by severity"),
        limit: int = Query(100, ge=1, le=1000),
    ):
        """Query signals with optional filters."""
        raw = await historical_db.query_signals(
            game_id=game_id, signal_type=signal_type,
            severity=severity, limit=limit,
        )
        signals = [
            SignalItem(
                id=s.get("id", ""),
                signal_type=s.get("signal_type", ""),
                severity=s.get("severity", "mid"),
                value=s.get("value"),
                timestamp=s.get("timestamp", ""),
                description=s.get("description"),
            )
            for s in raw
        ]
        return SignalListResponse(total=len(signals), signals=signals)

    # ── Events ───────────────────────────────────────────────────

    @router.get("/events/{game_id}", response_model=EventListResponse)
    async def get_events(
        game_id: str,
        event_type: Optional[str] = Query(None, description="Filter by type"),
        limit: int = Query(100, ge=1, le=1000),
    ):
        """Return market events for a game."""
        raw = await historical_db.query_market_events(
            game_id=game_id, event_type=event_type, limit=limit,
        )
        events = [
            EventItem(
                id=e.get("id", ""),
                event_type=e.get("event_type", ""),
                timestamp=e.get("timestamp", ""),
                duration_seconds=e.get("duration_seconds"),
                magnitude=e.get("magnitude"),
                description=e.get("description"),
            )
            for e in raw
        ]
        return EventListResponse(total=len(events), events=events)

    # ── Compare ──────────────────────────────────────────────────

    @router.get("/compare", response_model=ComparativeResponse)
    async def compare_games(
        game_ids: str = Query(..., description="Comma-separated game IDs"),
        metrics: str = Query(
            "total_line,trap_meter,inflation_index,confidence",
            description="Comma-separated metric names",
        ),
        limit: int = Query(10000, ge=1, le=100000),
    ):
        """Return multi-game comparative metric series."""
        ids = [g.strip() for g in game_ids.split(",") if g.strip()]
        metric_names = [m.strip() for m in metrics.split(",") if m.strip()]

        series: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for gid in ids:
            snapshots = await historical_db.query_snapshots(
                game_id=gid, limit=limit,
            )
            game_series: dict[str, list[dict[str, Any]]] = {}
            for m in metric_names:
                game_series[m] = [
                    {"t": s.get("timestamp"), "v": s.get(m)}
                    for s in snapshots if s.get(m) is not None
                ]
            series[gid] = game_series

        return ComparativeResponse(
            game_ids=ids, metrics=metric_names, series=series,
        )

    @router.post("/compare/query", response_model=ComparativeQueryResponse)
    async def compare_by_filters(
        req: ComparativeQueryRequest,
    ):
        """Run a filter-based comparative query."""
        filters = {k: v for k, v in req.model_dump().items() if v is not None}
        matched = await historical_db.run_comparative_query(filters)
        return ComparativeQueryResponse(
            matched_games=matched, count=len(matched),
        )

    return router
