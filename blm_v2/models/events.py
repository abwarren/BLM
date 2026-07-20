"""
BLM V2 — Event System Models

Typed event schemas for the BLM event bus. Each event type represents a specific
domain occurrence: game events, market events, trap events, model events.

Hierarchy:
  - BlmEvent (base):  Every event has a type label, timestamp, and game_id
  - Concrete events:  13+ typed events inheriting from BlmEvent

Event categories:
  - Game events:      ThreePointerMade, Timeout, QuarterStart, QuarterEnd, RotationChange
  - Player events:    Injury
  - Trap events:      TrapTriggered
  - Market events:    MomentumSwing, SharpMoney, MarketMove
  - Model events:     ConfidenceDrop, ModelCorrection
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from blm_v2.models.snapshot import ConfidenceComponent


# ── Event type label registry ────────────────────────────────────


class EventType(str, Enum):
    """Canonical event type labels used for routing and serialisation."""

    THREE_POINTER_MADE = "three_pointer_made"
    TIMEOUT = "timeout"
    QUARTER_START = "quarter_start"
    QUARTER_END = "quarter_end"
    ROTATION_CHANGE = "rotation_change"
    INJURY = "injury"
    TRAP_TRIGGERED = "trap_triggered"
    MOMENTUM_SWING = "momentum_swing"
    SHARP_MONEY = "sharp_money"
    MARKET_MOVE = "market_move"
    CONFIDENCE_DROP = "confidence_drop"
    MODEL_CORRECTION = "model_correction"


# ── Base Event ───────────────────────────────────────────────────


class BlmEvent(BaseModel):
    """Base class for all BLM events.

    Every concrete event inherits from this and carries:
      - event_type:   Discriminator label for routing
      - timestamp:    When the event occurred
      - game_id:      Which game the event pertains to
      - metadata:     Arbitrary extra context (optional)
    """

    event_type: EventType = Field(
        ..., description="Canonical event type discriminator."
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="ISO 8601 timestamp of event occurrence.",
    )
    game_id: str = Field(
        ..., description="Game identifier this event belongs to."
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description="Arbitrary extra context passed with the event.",
    )

    # Runtime-only: not serialised
    _created_at: datetime = datetime.now()

    def __lt__(self, other: "BlmEvent") -> bool:
        """Sort by timestamp for chronological ordering."""
        if not isinstance(other, BlmEvent):
            return NotImplemented
        return self.timestamp < other.timestamp


# ── Game Events ──────────────────────────────────────────────────


class ThreePointerMade(BlmEvent):
    """A three-point shot was made.

    Fired by the collector when it detects a three-pointer on the play-by-play
    or scoreboard.
    """

    event_type: EventType = Field(
        default=EventType.THREE_POINTER_MADE, init=False
    )
    team: str = Field(..., description="Team that made the three.")
    shooter: Optional[str] = Field(
        default=None,
        description="Player who made the shot (if available).",
    )
    assisted_by: Optional[str] = Field(
        default=None,
        description="Player who assisted (if available).",
    )
    score_before: int = Field(
        ..., description="Team score before the three-pointer."
    )
    score_after: int = Field(
        ..., description="Team score after the three-pointer."
    )


class Timeout(BlmEvent):
    """A timeout was called.

    Used to adjust pace calculations and detect tactical pauses.
    """

    event_type: EventType = Field(default=EventType.TIMEOUT, init=False)
    team: str = Field(..., description="Team that called the timeout.")
    timeout_type: Optional[str] = Field(
        default=None,
        description="Type: full, 20-second, TV, official.",
    )
    time_remaining: Optional[str] = Field(
        default=None,
        description="Clock string at timeout.",
    )
    timeouts_remaining: Optional[int] = Field(
        default=None,
        ge=0,
        description="Timeouts remaining for this team after this one.",
    )


class QuarterStart(BlmEvent):
    """A quarter has started (or overtime period began).

    Used to reset period-specific calculations and emit quarter-level signals.
    """

    event_type: EventType = Field(default=EventType.QUARTER_START, init=False)
    quarter: int = Field(..., ge=1, le=10, description="Quarter number starting.")
    home_score: int = Field(..., ge=0, description="Home score at quarter start.")
    away_score: int = Field(..., ge=0, description="Away score at quarter start.")
    is_overtime: bool = Field(
        default=False,
        description="True if this is an overtime period.",
    )


class QuarterEnd(BlmEvent):
    """A quarter has ended (or overtime period ended).

    Used to trigger quarter-level analysis and persist period summaries.
    """

    event_type: EventType = Field(default=EventType.QUARTER_END, init=False)
    quarter: int = Field(..., ge=1, le=10, description="Quarter number ending.")
    home_score: int = Field(..., ge=0, description="Home score at quarter end.")
    away_score: int = Field(..., ge=0, description="Away score at quarter end.")
    period_total: int = Field(
        default=0,
        ge=0,
        description="Points scored in this quarter alone.",
    )
    period_minutes: float = Field(
        default=12.0,
        gt=0.0,
        description="Actual duration of the quarter in minutes.",
    )


class RotationChange(BlmEvent):
    """A player substitution / lineup rotation occurred.

    Used to track lineup changes for fatigue and matchup analysis.
    """

    event_type: EventType = Field(default=EventType.ROTATION_CHANGE, init=False)
    team: str = Field(..., description="Team making the change.")
    player_out: str = Field(..., description="Player leaving the court.")
    player_in: str = Field(..., description="Player entering the court.")
    position_out: Optional[str] = Field(
        default=None,
        description="Position of the player leaving (PG, SG, SF, PF, C).",
    )
    position_in: Optional[str] = Field(
        default=None,
        description="Position of the player entering.",
    )
    quarter: int = Field(..., ge=1, le=10, description="Quarter of the change.")


# ── Player Events ────────────────────────────────────────────────


class Injury(BlmEvent):
    """An injury event — player leaves the game due to injury.

    Triggers confidence recalculation and lineup adjustments.
    """

    event_type: EventType = Field(default=EventType.INJURY, init=False)
    player: str = Field(..., description="Name of the injured player.")
    team: str = Field(..., description="Team of the injured player.")
    injury_type: Optional[str] = Field(
        default=None,
        description="Reported injury type (e.g. ankle, knee, concussion).",
    )
    severity: Optional[str] = Field(
        default=None,
        description="Estimated severity: minor, moderate, severe, unknown.",
    )
    return_probable: Optional[bool] = Field(
        default=None,
        description="Whether the player is expected to return this game.",
    )


# ── Trap Events ──────────────────────────────────────────────────


class TrapTriggered(BlmEvent):
    """The Trap Meter has detected a suspicious market signal.

    This is a high-priority event that may trigger alerts.
    """

    event_type: EventType = Field(default=EventType.TRAP_TRIGGERED, init=False)
    trap_type: str = Field(
        ..., description="Type of trap detected (bull, bear, dead_market, etc.)."
    )
    trap_score: float = Field(
        ..., ge=0.0, le=1.0, description="Trap meter score (0-1)."
    )
    threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Threshold that was exceeded.",
    )
    current_total: Optional[float] = Field(
        default=None,
        description="Current live total when trap was triggered.",
    )
    signal_detail: Optional[str] = Field(
        default=None,
        description="Human-readable detail about the trap signal.",
    )


# ── Market Events ────────────────────────────────────────────────


class MomentumSwing(BlmEvent):
    """A significant change in market or game momentum was detected.

    Momentum swings update the BLM's pace and regression expectations.
    """

    event_type: EventType = Field(default=EventType.MOMENTUM_SWING, init=False)
    direction: str = Field(
        ...,
        description="Direction of the swing: up, down, acceleration, deceleration.",
    )
    magnitude: float = Field(
        ..., description="Magnitude of the momentum change."
    )
    previous_momentum_score: Optional[float] = Field(
        default=None,
        description="Momentum score before the swing.",
    )
    new_momentum_score: float = Field(
        ..., description="Momentum score after the swing."
    )
    contributing_factors: Optional[list[str]] = Field(
        default=None,
        description="Factors that contributed to the swing (e.g. '3pt_run', 'foul_trouble').",
    )


class SharpMoney(BlmEvent):
    """Suspected sharp (professional) money has entered the market.

    Distinguished from public money by pattern: late, large, line-moving bets.
    """

    event_type: EventType = Field(default=EventType.SHARP_MONEY, init=False)
    target_market: str = Field(
        ...,
        description="Market affected (spread, total, moneyline, team_total).",
    )
    direction: str = Field(
        ...,
        description="Direction of the sharp action (over, under, home, away).",
    )
    line_movement: float = Field(
        ..., description="Amount the line moved in response."
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence that this is sharp (not public) money.",
    )
    previous_line: float = Field(
        ..., description="Line before the sharp money arrived."
    )
    new_line: float = Field(
        ..., description="Line after the sharp money arrived."
    )


class MarketMove(BlmEvent):
    """A general market line movement that doesn't meet sharp-money criteria.

    Used to track all line movements for analysis and historical logging.
    """

    event_type: EventType = Field(default=EventType.MARKET_MOVE, init=False)
    market: str = Field(
        ...,
        description="Market that moved (spread, total, moneyline, team_total).",
    )
    previous_value: float = Field(
        ..., description="Previous line / price value."
    )
    new_value: float = Field(..., description="New line / price value.")
    move_type: str = Field(
        default="normal",
        description="Type: normal, steam, reverse, late.",
    )
    seconds_since_last_move: Optional[int] = Field(
        default=None,
        ge=0,
        description="Seconds since the last movement in this market.",
    )


# ── Model Events ─────────────────────────────────────────────────


class ConfidenceDrop(BlmEvent):
    """A significant drop in the BLM's composite confidence score.

    May trigger recalculation, data quality checks, or alerting.
    """

    event_type: EventType = Field(default=EventType.CONFIDENCE_DROP, init=False)
    previous_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence before the drop."
    )
    new_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence after the drop."
    )
    drop_amount: float = Field(
        ..., ge=0.0, le=1.0, description="Absolute drop magnitude."
    )
    reason: str = Field(
        ..., description="Primary reason for the drop (e.g. 'data_gap', 'injury', 'divergence')."
    )
    affected_components: Optional[list[ConfidenceComponent]] = Field(
        default=None,
        description="Which confidence components were most affected.",
    )


class ModelCorrection(BlmEvent):
    """The BLM model self-corrected a previous assessment.

    Fired when new data causes the model to revise a prediction or assessment.
    """

    event_type: EventType = Field(default=EventType.MODEL_CORRECTION, init=False)
    corrected_field: str = Field(
        ...,
        description="The model output field that was corrected (e.g. 'expected_total', 'win_probability').",
    )
    previous_value: Any = Field(
        default=None, description="Previous value before correction."
    )
    new_value: Any = Field(
        default=None, description="Corrected value."
    )
    correction_magnitude: float = Field(
        ..., description="Absolute magnitude of the correction."
    )
    reason: str = Field(
        ..., description="Why the correction was made."
    )
    snapshot_id: Optional[str] = Field(
        default=None,
        description="Snapshot identifier that triggered the correction.",
    )


# ── Event Registry ───────────────────────────────────────────────


# Mapping from EventType to the concrete model class for deserialisation
EVENT_TYPE_MAP: dict[EventType, type[BlmEvent]] = {
    EventType.THREE_POINTER_MADE: ThreePointerMade,
    EventType.TIMEOUT: Timeout,
    EventType.QUARTER_START: QuarterStart,
    EventType.QUARTER_END: QuarterEnd,
    EventType.ROTATION_CHANGE: RotationChange,
    EventType.INJURY: Injury,
    EventType.TRAP_TRIGGERED: TrapTriggered,
    EventType.MOMENTUM_SWING: MomentumSwing,
    EventType.SHARP_MONEY: SharpMoney,
    EventType.MARKET_MOVE: MarketMove,
    EventType.CONFIDENCE_DROP: ConfidenceDrop,
    EventType.MODEL_CORRECTION: ModelCorrection,
}


def event_from_dict(data: dict) -> BlmEvent:
    """Deserialise a dict into the correct concrete event type.

    Uses the ``event_type`` key to look up the correct model class from
    ``EVENT_TYPE_MAP``.

    Raises:
        ValueError: If the event type is unknown or missing.
    """
    event_type_raw = data.get("event_type")
    if not event_type_raw:
        raise ValueError("Missing 'event_type' in event data")

    if isinstance(event_type_raw, str):
        try:
            event_type_enum = EventType(event_type_raw)
        except ValueError:
            raise ValueError(f"Unknown event type string: {event_type_raw!r}")
    elif isinstance(event_type_raw, EventType):
        event_type_enum = event_type_raw
    else:
        raise ValueError(f"Invalid event_type value: {event_type_raw!r}")

    model_cls = EVENT_TYPE_MAP.get(event_type_enum)
    if model_cls is None:
        raise ValueError(f"No model registered for event type: {event_type_enum}")

    return model_cls(**data)
