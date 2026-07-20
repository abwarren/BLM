"""
BLM V2 — Game State Models

Typed schemas for everything related to the game itself: teams, scoreboard,
clock, possession, and status. These models describe *what is happening on the
court* as distinct from market pricing (BettingMarket) or analysis (BLMScore).
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────


class GameStatus(str, Enum):
    """Possible states of a basketball game lifecycle."""

    PRE = "pre"
    LIVE = "live"
    HALFTIME = "halftime"
    ENDED = "ended"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class TeamSide(str, Enum):
    """Home or away designation."""

    HOME = "home"
    AWAY = "away"


class Possession(str, Enum):
    """Who currently has the ball."""

    HOME = "home"
    AWAY = "away"
    TIP_OFF = "tip_off"
    FREE_THROW = "free_throw"
    UNKNOWN = "unknown"


# ── Models ────────────────────────────────────────────────────────


class Team(BaseModel):
    """A basketball team participating in a game."""

    name: str = Field(..., description="Full team name.", examples=["Lakers"])
    abbreviation: Optional[str] = Field(
        default=None,
        description="Short team code (e.g. LAL).",
        examples=["LAL"],
    )
    league: Optional[str] = Field(
        default=None,
        description="League identifier.",
        examples=["Cyber 2K26"],
    )
    side: Optional[TeamSide] = Field(
        default=None,
        description="Whether this team is home or away.",
    )


class Clock(BaseModel):
    """Game clock state — quarter and time remaining."""

    quarter: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Current quarter (1-4).",
    )
    time_remaining: Optional[str] = Field(
        default=None,
        description="Clock display string (e.g. '7:32').",
        examples=["7:32", "0:00"],
    )
    seconds_remaining: Optional[int] = Field(
        default=None,
        ge=0,
        le=720,
        description="Seconds remaining in the current quarter (computed).",
    )
    is_overtime: bool = Field(
        default=False,
        description="True if the game is in an overtime period.",
    )

    def to_timedelta(self) -> timedelta | None:
        """Convert clock string to timedelta if available."""
        if self.time_remaining and ":" in self.time_remaining:
            parts = self.time_remaining.split(":")
            try:
                return timedelta(minutes=int(parts[0]), seconds=int(parts[1]))
            except (ValueError, IndexError):
                return None
        return None


class Scoreboard(BaseModel):
    """Current score state of the game."""

    home_score: int = Field(
        default=0,
        ge=0,
        description="Home team's current score.",
    )
    away_score: int = Field(
        default=0,
        ge=0,
        description="Away team's current score.",
    )
    margin: int = Field(
        default=0,
        description="Score margin (home - away). Positive = home leading.",
    )
    total: int = Field(
        default=0,
        ge=0,
        description="Combined score (home + away).",
    )


class GameSummary(BaseModel):
    """High-level game metadata returned in list endpoints."""

    game_id: str = Field(..., description="Unique game identifier.")
    league: str = Field(default="Cyber 2K26", description="League name.")
    season: str | None = Field(default=None, description="Season identifier.")
    home_team: str = Field(..., description="Home team name.")
    away_team: str = Field(..., description="Away team name.")
    status: GameStatus = Field(default=GameStatus.LIVE, description="Game lifecycle status.")
    snapshot_count: int = Field(default=0, description="Number of snapshots collected.")
    last_snapshot_ts: str | None = Field(
        default=None,
        description="ISO 8601 timestamp of most recent snapshot.",
    )
    created_at: str = Field(
        ...,
        description="ISO 8601 timestamp when the game was first recorded.",
    )
    updated_at: str = Field(
        ...,
        description="ISO 8601 timestamp of last update.",
    )


class GameInfo(BaseModel):
    """Full game information including metadata and current state."""

    game_id: str = Field(..., description="Unique game identifier.")
    league: str = Field(default="Cyber 2K26", description="League name.")
    season: str | None = Field(default=None, description="Season identifier.")
    home_team: Team = Field(..., description="Home team details.")
    away_team: Team = Field(..., description="Away team details.")
    status: GameStatus = Field(default=GameStatus.LIVE, description="Game lifecycle status.")
    clock: Clock = Field(default_factory=Clock, description="Current game clock state.")
    scoreboard: Scoreboard = Field(
        default_factory=Scoreboard,
        description="Current score state.",
    )
    possession: Possession = Field(
        default=Possession.UNKNOWN,
        description="Current possession.",
    )
