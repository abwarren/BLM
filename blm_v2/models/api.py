"""
BLM V2 — API Request / Response Models

Pydantic schemas for every FastAPI V2 endpoint. Organised by endpoint group:

  - Live / Snapshot endpoints
  - Game history endpoints
  - Prediction endpoints
  - Health / meta endpoints
  - Error responses
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from blm_v2.models.game import GameStatus
from blm_v2.models.predictions import PredictionBundle
from blm_v2.models.snapshot import BlmSnapshot


# ── Live / Snapshot ──────────────────────────────────────────────


class LiveSnapshotResponse(BaseModel):
    """Response for ``GET /api/v2/live`` — current snapshot."""

    snapshot: Optional[BlmSnapshot] = Field(
        default=None,
        description="Current live snapshot, or None if no live game.",
    )
    snapshot_count: int = Field(
        default=0,
        ge=0,
        description="Total snapshots collected for this game session.",
    )
    game_id: Optional[str] = Field(
        default=None,
        description="Game identifier (present when a live game exists).",
    )
    status: str = Field(
        default="no_game",
        description="Response status: 'ok', 'no_game', 'error'.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Human-readable status message.",
    )
    server_time: datetime = Field(
        default_factory=datetime.now,
        description="Server timestamp for the response.",
    )


# ── Snapshot History ─────────────────────────────────────────────


class SnapshotHistoryRequest(BaseModel):
    """Query parameters for ``GET /api/v2/snapshots``."""

    game_id: str = Field(
        ..., description="Game identifier to fetch snapshots for."
    )
    offset: int = Field(
        default=0, ge=0, description="Snapshot offset for pagination."
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=5000,
        description="Maximum snapshots to return.",
    )
    sort: str = Field(
        default="asc",
        description="Sort order: 'asc' (oldest first) or 'desc' (newest first).",
    )
    since: Optional[datetime] = Field(
        default=None,
        description="Only return snapshots after this timestamp.",
    )
    until: Optional[datetime] = Field(
        default=None,
        description="Only return snapshots before this timestamp.",
    )
    quarter: Optional[int] = Field(
        default=None,
        ge=1,
        le=10,
        description="Filter to a specific quarter.",
    )


class SnapshotHistoryResponse(BaseModel):
    """Response for ``GET /api/v2/snapshots``."""

    snapshots: list[BlmSnapshot] = Field(
        default_factory=list,
        description="Requested snapshot list.",
    )
    total: int = Field(
        ..., ge=0, description="Total snapshots matching the query."
    )
    offset: int = Field(..., ge=0, description="Applied pagination offset.")
    limit: int = Field(..., ge=1, description="Applied pagination limit.")
    game_id: str = Field(..., description="Game identifier queried.")
    status: str = Field(
        default="ok",
        description="Response status: 'ok', 'partial', 'empty', 'error'.",
    )


# ── Game List ────────────────────────────────────────────────────


class GameListResponse(BaseModel):
    """Response for ``GET /api/v2/games``."""

    games: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of games with summary fields.",
    )
    total: int = Field(default=0, ge=0, description="Total games matching.")
    limit: int = Field(
        default=20, ge=1, description="Applied pagination limit."
    )
    status: str = Field(default="ok", description="Response status.")


# ── Predictions ──────────────────────────────────────────────────


class PredictionRequest(BaseModel):
    """Request body for ``POST /api/v2/predict``."""

    game_id: str = Field(
        ..., description="Game identifier to generate predictions for."
    )
    snapshot: Optional[BlmSnapshot] = Field(
        default=None,
        description="Optional snapshot to predict from. If omitted, uses latest live snapshot.",
    )
    include_winner: bool = Field(
        default=True, description="Include winner prediction in output."
    )
    include_margin: bool = Field(
        default=True, description="Include margin prediction."
    )
    include_total: bool = Field(
        default=True, description="Include total prediction."
    )
    include_clv: bool = Field(
        default=True, description="Include closing line value estimate."
    )
    force_recompute: bool = Field(
        default=False,
        description="Force recomputation even if cached predictions exist.",
    )


class PredictionResponse(BaseModel):
    """Response for ``POST /api/v2/predict``."""

    game_id: str = Field(
        ..., description="Game identifier predictions were generated for."
    )
    predictions: Optional[PredictionBundle] = Field(
        default=None, description="The generated prediction bundle."
    )
    from_cache: bool = Field(
        default=False,
        description="True if predictions were served from cache.",
    )
    computation_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Milliseconds taken to compute predictions.",
    )
    status: str = Field(
        default="ok",
        description="Response status: 'ok', 'stale_data', 'insufficient_data', 'error'.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Human-readable detail about the prediction outcome.",
    )


# ── Health ───────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Response for ``GET /api/v2/health``."""

    status: str = Field(
        default="ok",
        description="Service health status: 'ok', 'degraded', 'unavailable'.",
    )
    app_name: str = Field(default="BLM V2", description="Application name.")
    app_version: str = Field(default="2.0.0", description="Application version.")
    environment: str = Field(
        default="development", description="Runtime environment."
    )
    uptime_seconds: Optional[float] = Field(
        default=None, ge=0.0, description="Server uptime in seconds."
    )
    database_connected: bool = Field(
        default=False,
        description="Whether the database connection is healthy.",
    )
    collector_running: bool = Field(
        default=False,
        description="Whether the snapshot collector is active.",
    )
    engine_running: bool = Field(
        default=False,
        description="Whether the BLM analysis engine is active.",
    )
    websocket_connections: Optional[int] = Field(
        default=None,
        ge=0,
        description="Current number of active WebSocket connections.",
    )
    events_processed: Optional[int] = Field(
        default=None,
        ge=0,
        description="Total events processed by the event bus.",
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="Server timestamp for the health check.",
    )


# ── Errors ───────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    """A single error detail with field-level information."""

    field: Optional[str] = Field(
        default=None,
        description="The field or parameter that caused the error.",
    )
    message: str = Field(
        ..., description="Human-readable error description."
    )
    code: Optional[str] = Field(
        default=None,
        description="Machine-readable error code for programmatic handling.",
    )


class ErrorResponse(BaseModel):
    """Standard error response returned on 4xx and 5xx errors."""

    status: str = Field(
        default="error",
        description="Always 'error' for error responses.",
    )
    error: str = Field(
        ..., description="Short error type identifier."
    )
    message: str = Field(
        ..., description="Human-readable error description."
    )
    details: Optional[list[ErrorDetail]] = Field(
        default=None,
        description="Detailed field-level error information.",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Unique request identifier for debugging.",
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="Server timestamp for the error.",
    )
