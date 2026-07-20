"""
BLM V2 — Pydantic Models Package

Typed schemas for every data domain in the BLM platform.
All models use Pydantic V2 for serialisation, validation, and schema generation.

Domains:
  - snapshot:  Complete market snapshots (game state + BLM analysis + bets + traps)
  - game:      Game state models (teams, score, clock, possession)
  - events:    Typed event system models for the async event bus
  - predictions: Prediction models (winner, margin, CLV, trap accuracy)
  - api:       API request/response schemas for V2 endpoints
"""

from blm_v2.models.snapshot import (
    SnapshotMetadata,
    GameState,
    BLMScore,
    PaceMetrics,
    BettingMarket,
    TrapDetection,
    MomentumMetrics,
    TeamTotals,
    ConfidenceInputs,
    PlayerState,
    BlmSnapshot,
    SnapshotList,
)
from blm_v2.models.game import (
    Team,
    GameStatus,
    GameInfo,
    GameSummary,
    Possession,
    Clock,
    Scoreboard,
)
from blm_v2.models.events import (
    BlmEvent,
    ThreePointerMade,
    Timeout,
    QuarterStart,
    QuarterEnd,
    RotationChange,
    Injury,
    TrapTriggered,
    MomentumSwing,
    SharpMoney,
    MarketMove,
    ConfidenceDrop,
    ModelCorrection,
)
from blm_v2.models.predictions import (
    WinnerPrediction,
    MarginPrediction,
    TotalPrediction,
    ClosingLineValue,
    TrapAccuracy,
    PredictionBundle,
)
from blm_v2.models.api import (
    LiveSnapshotResponse,
    SnapshotHistoryRequest,
    SnapshotHistoryResponse,
    GameListResponse,
    PredictionRequest,
    PredictionResponse,
    HealthResponse,
    ErrorResponse,
)

__all__ = [
    # Snapshot
    "SnapshotMetadata",
    "GameState",
    "BLMScore",
    "PaceMetrics",
    "BettingMarket",
    "TrapDetection",
    "MomentumMetrics",
    "TeamTotals",
    "ConfidenceInputs",
    "PlayerState",
    "BlmSnapshot",
    "SnapshotList",
    # Game
    "Team",
    "GameStatus",
    "GameInfo",
    "GameSummary",
    "Possession",
    "Clock",
    "Scoreboard",
    # Events
    "BlmEvent",
    "ThreePointerMade",
    "Timeout",
    "QuarterStart",
    "QuarterEnd",
    "RotationChange",
    "Injury",
    "TrapTriggered",
    "MomentumSwing",
    "SharpMoney",
    "MarketMove",
    "ConfidenceDrop",
    "ModelCorrection",
    # Predictions
    "WinnerPrediction",
    "MarginPrediction",
    "TotalPrediction",
    "ClosingLineValue",
    "TrapAccuracy",
    "PredictionBundle",
    # API
    "LiveSnapshotResponse",
    "SnapshotHistoryRequest",
    "SnapshotHistoryResponse",
    "GameListResponse",
    "PredictionRequest",
    "PredictionResponse",
    "HealthResponse",
    "ErrorResponse",
]
