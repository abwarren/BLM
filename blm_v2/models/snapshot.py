"""
BLM V2 — Snapshot Models

A *snapshot* is the smallest unit of information in the BLM platform — one complete
market + game state at a single point in time. Every snapshot is immutable and
append-only after storage.

This module defines the full schema hierarchy:
  - BlmSnapshot (top-level container)
  - 10 nested sub-models covering every domain described in the BLM Constitution
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Sub-enums used across the snapshot ───────────────────────────


class MomentumDirection(str, Enum):
    """Direction of current momentum."""

    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"
    NEUTRAL = "neutral"


class MomentumStrength(str, Enum):
    """Qualitative strength of momentum."""

    NONE = "none"
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    EXTREME = "extreme"


class ConfidenceComponent(str, Enum):
    """Labels for the individual confidence input components."""

    PACE = "PACE"
    LINE = "LINE"
    INJURY = "INJURY"
    BLOWOUT = "BLOWOUT"
    TEAM_TOTAL = "TEAM_TOTAL"


class TrapType(str, Enum):
    """Types of traps the Trap Meter can detect."""

    BULL = "bull_trap"
    BEAR = "bear_trap"
    REVERSE_BULL = "reverse_bull_trap"
    DEAD_MARKET = "dead_market"
    FALSE_MOMENTUM = "false_momentum"
    LATE = "late_trap"
    SHARP = "sharp_trap"


# ── Snapshot Metadata ────────────────────────────────────────────


class SnapshotMetadata(BaseModel):
    """Identifying and temporal metadata for a single snapshot."""

    game_id: str = Field(
        default="",
        description="Unique game identifier.",
    )
    league: str = Field(default="Cyber 2K26", description="League name.")
    season: Optional[str] = Field(default=None, description="Season identifier.")
    quarter: int = Field(
        default=1,
        ge=0,
        le=10,
        description="Current quarter (0 = pre-game, 5+ = overtime).",
    )
    clock: Optional[str] = Field(
        default=None,
        description="Game clock string (e.g. '7:32').",
        examples=["12:00", "7:32", "0:00"],
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="ISO 8601 timestamp of when this snapshot was captured.",
    )
    snapshot_version: str = Field(
        default="2.0",
        description="Snapshot schema version for forward compatibility.",
    )


# ── Game State ───────────────────────────────────────────────────


class GameState(BaseModel):
    """What is happening on the court — score, margin, possession."""

    home_team: str = Field(..., description="Home team name.")
    away_team: str = Field(..., description="Away team name.")
    possession: Optional[str] = Field(
        default=None,
        description="Current possession indicator (home/away/tip_off).",
    )
    home_score: int = Field(default=0, ge=0, description="Home team score.")
    away_score: int = Field(default=0, ge=0, description="Away team score.")
    margin: int = Field(
        default=0,
        description="Score margin (home - away). Positive = home leads.",
    )
    total: int = Field(default=0, ge=0, description="Combined score (home + away).")


# ── BLM Score ────────────────────────────────────────────────────


class BLMScore(BaseModel):
    """Betting Logic Model's current assessment of the game."""

    expected_winner: Optional[str] = Field(
        default=None,
        description="Team the model expects to win (home/away name).",
    )
    win_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Model's estimated win probability for the expected winner.",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Overall confidence in the BLM assessment (0-1).",
    )
    expected_margin: Optional[float] = Field(
        default=None,
        description="Expected final score margin (home - away).",
    )
    expected_total: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Expected final total combined score.",
    )


# ── Pace Metrics ─────────────────────────────────────────────────


class PaceMetrics(BaseModel):
    """Pace-of-play measurements and projections."""

    real_pace: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Current observed pace (possessions per 48 min).",
    )
    expected_pace: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Expected pace for this league / matchup.",
    )
    possessions: Optional[int] = Field(
        default=None,
        ge=0,
        description="Total possessions taken so far.",
    )
    remaining_possessions: Optional[int] = Field(
        default=None,
        ge=0,
        description="Estimated possessions remaining in the game.",
    )


# ── Betting Market ───────────────────────────────────────────────


class BettingMarket(BaseModel):
    """Current sportsbook market pricing for the game."""

    spread: Optional[float] = Field(
        default=None,
        description="Pre-game point spread (home perspective).",
    )
    live_spread: Optional[float] = Field(
        default=None,
        description="Live (in-game) point spread.",
    )
    total: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Pre-game total line.",
    )
    live_total: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Live (in-game) total line.",
    )
    moneyline: Optional[str] = Field(
        default=None,
        description="Moneyline odds (e.g. '-150/+130').",
    )
    steam_movement: Optional[float] = Field(
        default=None,
        description="Steam movement indicator — rapid line/odds change magnitude.",
    )
    reverse_line_movement: Optional[bool] = Field(
        default=None,
        description="True if line moved opposite to expected public money direction.",
    )


# ── Trap Detection ───────────────────────────────────────────────


class TrapDetection(BaseModel):
    """Trap Meter output — measures whether market behaviour is suspicious."""

    trap_meter: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Composite trap probability (0 = clean, 1 = certain trap).",
    )
    bull_trap: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Bull trap score — false upward breakout signal.",
    )
    bear_trap: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Bear trap score — false downward breakout signal.",
    )
    reverse_bull_trap: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Reverse bull trap score — contrarian bull signal.",
    )
    dead_market: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Dead market indicator — line frozen despite score movement.",
    )
    false_momentum: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="False momentum indicator — apparent momentum not backed by real scoring.",
    )
    late_trap: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Late trap indicator — suspicious line behaviour in final minutes.",
    )
    sharp_trap: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Sharp trap indicator — line move consistent with sharp money entry.",
    )


# ── Momentum Metrics ─────────────────────────────────────────────


class MomentumMetrics(BaseModel):
    """Computed momentum measurements derived from recent snapshots."""

    momentum_score: Optional[float] = Field(
        default=None,
        description="Composite momentum score (positive = upward).",
    )
    momentum_direction: MomentumDirection = Field(
        default=MomentumDirection.NEUTRAL,
        description="Direction of current momentum.",
    )
    momentum_velocity: Optional[float] = Field(
        default=None,
        description="Rate of momentum change per snapshot interval.",
    )
    momentum_acceleration: Optional[float] = Field(
        default=None,
        description="Second derivative — acceleration of momentum change.",
    )
    momentum_strength: MomentumStrength = Field(
        default=MomentumStrength.NONE,
        description="Qualitative strength of the current momentum.",
    )


# ── Team Totals ──────────────────────────────────────────────────


class ExpectedTeamTotal(BaseModel):
    """Expected scoring contributions for one team."""

    team_name: str = Field(..., description="Team name.")
    first_quarter: Optional[float] = Field(default=None, description="Expected Q1 score.")
    second_quarter: Optional[float] = Field(default=None, description="Expected Q2 score.")
    third_quarter: Optional[float] = Field(default=None, description="Expected Q3 score.")
    fourth_quarter: Optional[float] = Field(default=None, description="Expected Q4 score.")
    full_game: float = Field(..., ge=0.0, description="Expected full-game total for this team.")


class TeamTotals(BaseModel):
    """Projected team scoring totals."""

    home_projection: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Projected final home team score.",
    )
    away_projection: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Projected final away team score.",
    )
    expected_team_totals: Optional[list[ExpectedTeamTotal]] = Field(
        default=None,
        description="Per-quarter expected scoring breakdown for each team.",
    )


# ── Confidence Inputs ────────────────────────────────────────────


class ConfidenceInputs(BaseModel):
    """Individual components that feed into the composite confidence score."""

    PACE: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence contributed by pace analysis.",
    )
    LINE: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence contributed by line / market analysis.",
    )
    INJURY: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence contributed by injury / lineup analysis.",
    )
    BLOWOUT: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence contributed by blowout / garbage-time analysis.",
    )
    TEAM_TOTAL: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence contributed by team total projection analysis.",
    )
    composite_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Weighted composite of all confidence inputs.",
    )


# ── Player State ─────────────────────────────────────────────────


class PlayerInjury(BaseModel):
    """Injury information for a single player."""

    player_name: str = Field(..., description="Player name.")
    team: str = Field(..., description="Team name.")
    injury_type: Optional[str] = Field(default=None, description="Type of injury (e.g. ankle, knee).")
    status: str = Field(
        default="active",
        description="Playing status: active, doubtful, questionable, out.",
    )
    minutes_played: Optional[int] = Field(default=None, ge=0, description="Minutes played before injury.")


class PlayerFoul(BaseModel):
    """Foul tracking for a single player."""

    player_name: str = Field(..., description="Player name.")
    team: str = Field(..., description="Team name.")
    personal_fouls: int = Field(default=0, ge=0, le=6, description="Number of personal fouls.")
    fouls_remaining: int = Field(default=6, ge=0, le=6, description="Fouls before disqualification.")


class LineupSlot(BaseModel):
    """One player in the current lineup."""

    player_name: str = Field(..., description="Player name.")
    position: Optional[str] = Field(default=None, description="Position (PG, SG, SF, PF, C).")
    minutes_played: int = Field(default=0, ge=0, description="Minutes played in the game.")


class RotationChange(BaseModel):
    """A recorded lineup rotation event."""

    timestamp: str = Field(..., description="ISO 8601 timestamp of the rotation.")
    team: str = Field(..., description="Team making the change.")
    player_out: str = Field(..., description="Player leaving the court.")
    player_in: str = Field(..., description="Player entering the court.")
    quarter: int = Field(..., ge=1, le=10, description="Quarter the change occurred in.")


class PlayerState(BaseModel):
    """Current player-level state for both teams."""

    home_lineup: Optional[list[LineupSlot]] = Field(
        default=None,
        description="Current home team lineup on the court.",
    )
    away_lineup: Optional[list[LineupSlot]] = Field(
        default=None,
        description="Current away team lineup on the court.",
    )
    injuries: Optional[list[PlayerInjury]] = Field(
        default=None,
        description="Active injury list.",
    )
    fouls: Optional[list[PlayerFoul]] = Field(
        default=None,
        description="Player foul tracking.",
    )
    fatigue: Optional[dict[str, float]] = Field(
        default=None,
        description="Fatigue levels keyed by player name (0.0 = fresh, 1.0 = exhausted).",
    )
    rotation_changes: Optional[list[RotationChange]] = Field(
        default=None,
        description="Rotation events logged this game.",
    )


# ── Top-Level Snapshot ───────────────────────────────────────────


class BlmSnapshot(BaseModel):
    """Complete BLM snapshot — one full market + game state at a point in time.

    This is the top-level data structure used throughout the platform for
    collection, storage, analysis, and API responses.
    """

    metadata: SnapshotMetadata = Field(
        default_factory=lambda: SnapshotMetadata(),
        description="Snapshot metadata (game_id, league, season, quarter, clock, timestamp).",
    )
    game_state: GameState = Field(
        ...,
        description="Current game state (teams, score, margin, total, possession).",
    )
    blm: BLMScore = Field(
        default_factory=BLMScore,
        description="BLM model assessment (expected winner, probability, margin, total).",
    )
    pace: PaceMetrics = Field(
        default_factory=PaceMetrics,
        description="Pace-of-play metrics (real, expected, possessions remaining).",
    )
    betting_market: BettingMarket = Field(
        default_factory=BettingMarket,
        description="Sportsbook market pricing (spread, total, moneyline, steam).",
    )
    trap_detection: TrapDetection = Field(
        default_factory=TrapDetection,
        description="Trap Meter output (composite + 7 trap subtypes).",
    )
    momentum: MomentumMetrics = Field(
        default_factory=MomentumMetrics,
        description="Market momentum metrics (score, velocity, acceleration, strength).",
    )
    team_totals: TeamTotals = Field(
        default_factory=TeamTotals,
        description="Team total projections and per-quarter breakdowns.",
    )
    confidence: ConfidenceInputs = Field(
        default_factory=ConfidenceInputs,
        description="Confidence component breakdown and composite score.",
    )
    player_state: PlayerState = Field(
        default_factory=PlayerState,
        description="Player-level state (lineups, injuries, fouls, fatigue).",
    )

    def model_dump_flat(self) -> dict:
        """Flatten the nested snapshot to a single-level dict for DB storage.

        Keys are dotted paths (e.g. ``metadata.quarter``, ``game_state.margin``).
        """
        flat: dict = {}
        for field_name in self.model_fields:
            sub = getattr(self, field_name)
            if isinstance(sub, BaseModel):
                for sub_field in sub.model_fields:
                    flat[f"{field_name}.{sub_field}"] = getattr(sub, sub_field)
            else:
                flat[field_name] = sub
        return flat


class SnapshotList(BaseModel):
    """Wrapper for returning multiple snapshots."""

    snapshots: list[BlmSnapshot] = Field(
        default_factory=list,
        description="List of snapshot objects.",
    )
    total: int = Field(
        default=0,
        ge=0,
        description="Total number of snapshots available (for pagination).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Pagination offset.",
    )
    limit: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Pagination limit.",
    )
