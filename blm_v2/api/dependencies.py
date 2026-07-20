"""
BLM V2 — FastAPI Dependency Injection

Defines the abstract interfaces (protocols) for the subsystems that the
V2 REST and WebSocket endpoints depend on, plus FastAPI ``Depends()``
callables that wire them at runtime.

The actual implementations are injected at app startup (see ``server.py``),
keeping the route handlers completely free of business logic.

Interfaces defined:
  - TSInterface         — Timeseries (InfluxDB / Parquet) read operations
  - StorageInterface    — Snapshot / game persistence (SQLite / Postgres)
  - EventBusInterface   — Async pub/sub event bus
  - BLMEngineInterface  — BLM analysis engine
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel


# ── Pydantic models for return values ─────────────────────────────────
# These mirror what the concrete implementations will return.  We define
# them here as light schemas so that the interface methods have typed
# return values without pulling in the full model package.

class LiveGameData(BaseModel):
    """Live game with full BLM enrichment."""
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
    traps: List[Dict[str, Any]] = []
    last_updated: str = ""
    enriched_snapshot: Optional[Dict[str, Any]] = None


class GameListItem(BaseModel):
    """Minimal game summary for listing."""
    game_id: str
    home_team: str
    away_team: str
    status: str
    start_time: str
    home_score: int = 0
    away_score: int = 0
    quarter: int = 0
    snapshot_count: int = 0


class GameDetail(BaseModel):
    """Full game detail including latest snapshot."""
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


class HistoryItem(BaseModel):
    """Single historical snapshot entry."""
    timestamp: str
    home_score: int
    away_score: int
    quarter: int
    clock: str
    blm_score: Optional[float] = None
    confidence: Optional[float] = None
    pace: Optional[float] = None
    home_win_prob: Optional[float] = None
    trap_active: bool = False
    raw: Optional[Dict[str, Any]] = None


class ReplayFrame(BaseModel):
    """A single frame in a game replay sequence."""
    timestamp: str
    home_score: int
    away_score: int
    quarter: int
    clock: str
    blm_score: Optional[float] = None
    confidence: Optional[float] = None
    pace: Optional[float] = None
    momentum: Optional[Dict[str, Any]] = None
    traps: list = []
    markets: Optional[Dict[str, Any]] = None
    raw_snapshot: Optional[Dict[str, Any]] = None


class ChartDataPoint(BaseModel):
    """Aggregated data point optimised for plotting."""
    timestamp: str
    home_score: int = 0
    away_score: int = 0
    blm_score: float = 0.0
    confidence: float = 0.0
    pace: float = 0.0
    home_win_prob: float = 0.5
    trap_intensity: float = 0.0
    quarter: int = 1
    clock_seconds: int = 0


class GameEvent(BaseModel):
    """A single game event."""
    event_id: str
    game_id: str
    event_type: str
    timestamp: str
    quarter: int
    clock: str
    description: str
    data: Dict[str, Any] = {}


class AlertItem(BaseModel):
    """Active alert entry."""
    alert_id: str
    game_id: str
    alert_type: str
    severity: str  # info, warning, critical
    title: str
    message: str
    timestamp: str
    acknowledged: bool = False
    data: Dict[str, Any] = {}


class TrapData(BaseModel):
    """Trap detection data for a game."""
    game_id: str
    active_traps: list = []
    trap_history: list = []
    trap_count: int = 0
    last_trap_time: Optional[str] = None


class BLMModelState(BaseModel):
    """BLM model state and configuration."""
    version: str = "2.0.0"
    status: str = "running"
    uptime_seconds: float = 0.0
    total_snapshots_processed: int = 0
    active_games: int = 0
    engine_config: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}


# ── Abstract interfaces ──────────────────────────────────────────────

@runtime_checkable
class TSInterface(Protocol):
    """Timeseries storage interface (InfluxDB / Parquet)."""

    async def get_latest_snapshot(self, game_id: str) -> Optional[Dict[str, Any]]:
        ...

    async def get_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        ...

    async def get_replay_snapshots(self, game_id: str) -> List[Dict[str, Any]]:
        ...

    async def get_chart_data(self, game_id: str) -> List[Dict[str, Any]]:
        ...

    async def get_live_game(self) -> Optional[Dict[str, Any]]:
        ...

    async def list_games(self) -> List[Dict[str, Any]]:
        ...

    async def get_game_detail(self, game_id: str) -> Optional[Dict[str, Any]]:
        ...

    # ── Line Analysis (OLV/CLV) ───────────────────────────────────

    async def write_line_analysis(self, analysis: Dict[str, Any]) -> None:
        ...

    async def query_line_analysis(
        self,
        game_id: str,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        ...

    async def get_live_line_analysis(self) -> Optional[Dict[str, Any]]:
        ...


@runtime_checkable
class StorageInterface(Protocol):
    """Relational / document storage (SQLite / Postgres)."""

    async def get_game(self, game_id: str) -> Optional[Dict[str, Any]]:
        ...

    async def get_events(self, game_id: str) -> List[Dict[str, Any]]:
        ...

    async def get_alerts(self, game_id: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    async def get_traps(self, game_id: str) -> Dict[str, Any]:
        ...

    async def get_model_state(self) -> Dict[str, Any]:
        ...

    async def list_games(self) -> List[Dict[str, Any]]:
        ...


@runtime_checkable
class EventBusInterface(Protocol):
    """Async pub/sub event bus."""

    async def publish(self, event_type: str, data: Any) -> None:
        ...

    async def subscribe(self, event_type: str, handler) -> Any:
        ...

    async def unsubscribe(self, subscription: Any) -> None:
        ...


@runtime_checkable
class BLMEngineInterface(Protocol):
    """BLM analysis engine interface."""

    async def enrich_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        ...

    async def get_confidence(self, game_id: str) -> Optional[float]:
        ...

    async def get_blm_score(self, game_id: str) -> Optional[float]:
        ...

    async def get_pace(self, game_id: str) -> Optional[float]:
        ...

    async def detect_traps(self, game_id: str) -> List[Dict[str, Any]]:
        ...

    async def get_predictions(self, game_id: str) -> Dict[str, Any]:
        ...

    async def get_config(self) -> Dict[str, Any]:
        ...


# ── Application wiring (set at startup) ──────────────────────────────

class _AppDependencies:
    """Holder for the live dependency instances wired by ``server.py``."""

    def __init__(self) -> None:
        self.ts_interface: Optional[TSInterface] = None
        self.storage_interface: Optional[StorageInterface] = None
        self.event_bus: Optional[EventBusInterface] = None
        self.blm_engine: Optional[BLMEngineInterface] = None


_deps = _AppDependencies()


def wire_dependencies(
    ts_interface: TSInterface,
    storage_interface: StorageInterface,
    event_bus: EventBusInterface,
    blm_engine: BLMEngineInterface,
) -> None:
    """Inject the concrete dependency instances into the app.

    Called once at server startup (see ``server.py``).
    """
    _deps.ts_interface = ts_interface
    _deps.storage_interface = storage_interface
    _deps.event_bus = event_bus
    _deps.blm_engine = blm_engine


# ── FastAPI ``Depends()`` callables ───────────────────────────────────

async def get_ts_interface() -> TSInterface:
    """FastAPI dependency that yields the timeseries interface."""
    if _deps.ts_interface is None:
        raise RuntimeError("TSInterface not wired — call wire_dependencies() at startup")
    return _deps.ts_interface


async def get_storage_interface() -> StorageInterface:
    """FastAPI dependency that yields the storage interface."""
    if _deps.storage_interface is None:
        raise RuntimeError("StorageInterface not wired — call wire_dependencies() at startup")
    return _deps.storage_interface


async def get_event_bus() -> EventBusInterface:
    """FastAPI dependency that yields the event bus."""
    if _deps.event_bus is None:
        raise RuntimeError("EventBusInterface not wired — call wire_dependencies() at startup")
    return _deps.event_bus


async def get_blm_engine() -> BLMEngineInterface:
    """FastAPI dependency that yields the BLM engine."""
    if _deps.blm_engine is None:
        raise RuntimeError("BLMEngineInterface not wired — call wire_dependencies() at startup")
    return _deps.blm_engine


async def get_metrics_collector_dep():
    """FastAPI dependency that yields the metrics collector singleton."""
    from blm_v2.telemetry.metrics import get_metrics_collector
    return get_metrics_collector()
