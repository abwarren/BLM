"""
BLM V3 — Historical Engine Pydantic Models.

Typed data models for every entity in the historical data pipeline.
All inherit from ``BaseModel`` for runtime validation and serialisation.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── UUID v7 (time-sortable) — compatible with Python < 3.14 ─────────


def _uuid7() -> str:
    """Generate a UUID v7 (time-sortable) as a 32-char hex string.

    Format:
      - Bits 0-47: Unix timestamp in milliseconds (48 bits)
      - Bits 48-51: Version (0b0111 = 7)
      - Bits 52-63: Random (12 bits)
      - Bits 64-65: Variant (0b10)
      - Bits 66-127: Random (62 bits)

    Works on Python 3.9+ without the stdlib ``uuid.uuid7()`` (added 3.14).
    """
    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 48 bits
    random_bytes = os.urandom(10)  # 80 bits of randomness

    # Build the 16-byte UUID
    # Bytes 0-5: timestamp (big-endian)
    # Bytes 6-7: version (4 bits) + random (12 bits)
    # Bytes 8-9: variant (2 bits) + random (14 bits)
    # Bytes 10-15: random (48 bits)
    buf = bytearray(16)
    for i in range(6):
        buf[5 - i] = (timestamp_ms >> (i * 8)) & 0xFF
    # Byte 6: version 7 in top 4 bits, random in bottom 4 bits
    buf[6] = (0x70 | (random_bytes[0] >> 4)) & 0xFF
    # Byte 7: random (remaining part of byte 0 + byte 1)
    buf[7] = ((random_bytes[0] << 4) & 0xF0) | (random_bytes[1] >> 4)
    # Byte 8: variant 0b10 in top 2 bits, random in bottom 6 bits
    buf[8] = (0x80 | (random_bytes[1] & 0x0F)) & 0xFF
    # Byte 9: random
    buf[9] = random_bytes[2]
    # Bytes 10-15: random
    for i in range(6):
        buf[10 + i] = random_bytes[3 + i]

    return buf.hex()


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 with microseconds."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── Enums ────────────────────────────────────────────────────────────


class SignalType(str, Enum):
    """Every signal type the engine can detect."""

    LINE_FREEZE = "line_freeze"
    LINE_JUMP = "line_jump"
    ODDS_COMPRESSION = "odds_compression"
    ODDS_EXPANSION = "odds_expansion"
    SHARP_MOVEMENT = "sharp_movement"
    FAKE_MOVEMENT = "fake_movement"
    TRAP_FORMATION = "trap_formation"
    BULL_TRAP = "bull_trap"
    BEAR_TRAP = "bear_trap"
    MARKET_CORRECTION = "market_correction"
    OVERREACTION = "overreaction"
    REGRESSION = "regression"
    MOMENTUM_SWING = "momentum_swing"
    PACE_COLLAPSE = "pace_collapse"
    INFLATION_SPIKE = "inflation_spike"


class SignalSeverity(str, Enum):
    """Severity level for signals."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"
    CRITICAL = "critical"


class GameStatus(str, Enum):
    """Lifecycle status of a game."""

    PRE = "pre"
    LIVE = "live"
    HALFTIME = "halftime"
    ENDED = "ended"


class ExportType(str, Enum):
    """Supported ML export formats."""

    CSV = "csv"
    PARQUET = "parquet"
    JSON = "json"


# ── Game ─────────────────────────────────────────────────────────────


class GameModel(BaseModel):
    """Game metadata and final results."""

    id: str = Field(default="", description="Game identifier (game_id).")
    league: str = Field(default="Cyber 2K26", description="League name.")
    season: Optional[str] = Field(default=None, description="Season identifier.")
    home_team: str = Field(default="", description="Home team name.")
    away_team: str = Field(default="", description="Away team name.")
    status: GameStatus = Field(default=GameStatus.LIVE, description="Game status.")
    start_time: Optional[str] = Field(default=None, description="ISO 8601 start time.")
    end_time: Optional[str] = Field(default=None, description="ISO 8601 end time.")
    final_home: Optional[int] = Field(default=None, ge=0, description="Final home score.")
    final_away: Optional[int] = Field(default=None, ge=0, description="Final away score.")
    final_total: Optional[int] = Field(default=None, ge=0, description="Final total (home + away).")
    final_margin: Optional[int] = Field(default=None, description="Final margin (home - away).")
    total_snapshots: int = Field(default=0, ge=0, description="Snapshot count.")
    created_at: str = Field(default_factory=_now_iso, description="Created timestamp.")
    updated_at: str = Field(default_factory=_now_iso, description="Updated timestamp.")

    def to_db_dict(self) -> dict[str, Any]:
        """Convert to dict for SQLite INSERT/UPDATE."""
        d = self.model_dump()
        d["status"] = d["status"].value
        return d

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "GameModel":
        """Construct from a SQLite row dict."""
        return cls(**row)


# ── Snapshot ─────────────────────────────────────────────────────────


class HistoricalSnapshot(BaseModel):
    """One complete market observation at a single point in time.

    This is the core data structure — every 250ms/500ms scrape produces one
    of these.  All derived metrics are computed at write time and stored
    alongside the raw data.
    """

    # ── Identity ──
    id: str = Field(default_factory=lambda: _uuid7(), description="UUID v7 (time-sortable).")
    game_id: str = Field(..., description="Game identifier.")
    timestamp: str = Field(default_factory=_now_iso, description="ISO 8601 with microseconds.")

    # ── Game State ──
    quarter: int = Field(default=1, ge=1, le=10, description="Current quarter (5+ = OT).")
    clock: Optional[str] = Field(default=None, description="Game clock (MM:SS).")
    possession: Optional[str] = Field(default=None, description="Current possession (home/away).")

    home_score: int = Field(default=0, ge=0, description="Home team score.")
    away_score: int = Field(default=0, ge=0, description="Away team score.")
    score_difference: int = Field(default=0, description="home - away.")
    total_score: int = Field(default=0, ge=0, description="home + away.")

    # ── Market ──
    total_line: Optional[float] = Field(default=None, description="Live over/under line.")
    spread: Optional[float] = Field(default=None, description="Live spread (home perspective).")
    home_team_total: Optional[float] = Field(default=None, description="Home team total line.")
    away_team_total: Optional[float] = Field(default=None, description="Away team total line.")
    total_line_raw: Optional[float] = Field(default=None, description="Un-smoothed line.")
    spread_raw: Optional[float] = Field(default=None, description="Un-smoothed spread.")

    # ── Odds ──
    over_odds: Optional[float] = Field(default=None, description="Decimal odds: OVER.")
    under_odds: Optional[float] = Field(default=None, description="Decimal odds: UNDER.")
    spread_odds_home: Optional[float] = Field(default=None, description="Decimal odds: home spread.")
    spread_odds_away: Optional[float] = Field(default=None, description="Decimal odds: away spread.")

    # ── Movement Deltas ──
    line_delta: Optional[float] = Field(default=None, description="Total line change since last snapshot.")
    odds_delta: Optional[float] = Field(default=None, description="Over odds change since last snapshot.")
    spread_delta: Optional[float] = Field(default=None, description="Spread change since last snapshot.")

    # ── Pace ──
    possessions: Optional[int] = Field(default=None, ge=0, description="Estimated total possessions.")
    possessions_per_min: Optional[float] = Field(default=None, ge=0.0, description="Possessions per minute.")
    projected_possessions: Optional[float] = Field(default=None, ge=0.0, description="Projected full-game possessions.")
    projected_total: Optional[float] = Field(default=None, ge=0.0, description="Projected final total at current pace.")

    # ── Derived BLM Metrics ──
    trap_meter: Optional[float] = Field(default=None, ge=0.0, le=100.0, description="Composite trap score (0-100).")
    tt_modifier: Optional[float] = Field(default=None, description="Team total modifier.")
    inflation_index: Optional[float] = Field(default=None, description="Market inflation index.")
    compression_index: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Odds compression index.")
    momentum: Optional[float] = Field(default=None, description="Directional momentum.")
    regression_prob: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Regression probability (0-1).")
    fair_total: Optional[float] = Field(default=None, description="Model's fair value total.")
    expected_total: Optional[float] = Field(default=None, description="Expected final total.")
    variance: Optional[float] = Field(default=None, ge=0.0, description="Variance estimate.")
    volatility: Optional[float] = Field(default=None, ge=0.0, description="Volatility estimate.")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Model confidence (0-1).")

    # ── Raw ──
    raw_json: Optional[str] = Field(default=None, description="Full snapshot as JSON (forward compat).")

    def to_db_dict(self) -> dict[str, Any]:
        """Convert to dict for SQLite INSERT."""
        return self.model_dump()

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "HistoricalSnapshot":
        """Construct from a SQLite row dict."""
        return cls(**row)


# ── Signal ───────────────────────────────────────────────────────────


class MarketSignal(BaseModel):
    """A threshold-crossing detection — something interesting happened."""

    id: str = Field(default_factory=lambda: _uuid7(), description="UUID v7.")
    game_id: str = Field(..., description="Game identifier.")
    snapshot_id: Optional[str] = Field(default=None, description="Triggering snapshot ID.")
    timestamp: str = Field(default_factory=_now_iso, description="ISO 8601.")
    signal_type: SignalType = Field(..., description="Type of signal.")
    severity: SignalSeverity = Field(default=SignalSeverity.MID, description="Severity level.")
    value: Optional[float] = Field(default=None, description="Metric value that triggered.")
    threshold: Optional[float] = Field(default=None, description="Threshold crossed.")
    description: Optional[str] = Field(default=None, description="Human-readable explanation.")
    related_json: Optional[str] = Field(default=None, description="Surrounding context as JSON.")
    confirmed: bool = Field(default=False, description="Post-hoc validation.")

    def to_db_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["signal_type"] = d["signal_type"].value
        d["severity"] = d["severity"].value
        d["confirmed"] = 1 if d["confirmed"] else 0
        return d

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "MarketSignal":
        d = dict(row)
        d["confirmed"] = bool(d["confirmed"])
        return cls(**d)


# ── Market Event ─────────────────────────────────────────────────────


class MarketEvent(BaseModel):
    """A higher-level event that groups related signals."""

    id: str = Field(default_factory=lambda: _uuid7(), description="UUID v7.")
    game_id: str = Field(..., description="Game identifier.")
    snapshot_id: Optional[str] = Field(default=None, description="Triggering snapshot ID.")
    timestamp: str = Field(default_factory=_now_iso, description="ISO 8601.")
    event_type: str = Field(..., description="Event type label.")
    duration_seconds: Optional[float] = Field(default=None, ge=0.0, description="Event duration.")
    magnitude: Optional[float] = Field(default=None, description="Event significance.")
    description: Optional[str] = Field(default=None, description="Human-readable explanation.")
    data_json: Optional[str] = Field(default=None, description="Arbitrary event data as JSON.")

    def to_db_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "MarketEvent":
        return cls(**row)


# ── Prediction ───────────────────────────────────────────────────────


class SnapshotPrediction(BaseModel):
    """BLM model prediction at a snapshot point."""

    id: str = Field(default_factory=lambda: _uuid7(), description="UUID v7.")
    game_id: str = Field(..., description="Game identifier.")
    snapshot_id: Optional[str] = Field(default=None, description="Associated snapshot ID.")
    timestamp: str = Field(default_factory=_now_iso, description="ISO 8601.")
    predicted_total: Optional[float] = Field(default=None, description="Predicted final total.")
    predicted_margin: Optional[float] = Field(default=None, description="Predicted final margin.")
    predicted_winner: Optional[str] = Field(default=None, description="Predicted winner team name.")
    win_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Win probability (0-1).")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Prediction confidence (0-1).")
    fair_total: Optional[float] = Field(default=None, description="Fair value total.")
    expected_pace: Optional[float] = Field(default=None, description="Expected pace.")
    model_version: Optional[str] = Field(default=None, description="BLM model version.")
    data_json: Optional[str] = Field(default=None, description="Extra prediction data.")

    def to_db_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "SnapshotPrediction":
        return cls(**row)


# ── Comparative Query ────────────────────────────────────────────────


class ComparativeQuery(BaseModel):
    """A saved analytical query that can be re-run."""

    id: str = Field(default_factory=lambda: _uuid7(), description="UUID v7.")
    name: Optional[str] = Field(default=None, description="Query name.")
    description: Optional[str] = Field(default=None, description="Query description.")
    filters_json: str = Field(default="{}", description="Filter specification as JSON.")
    game_ids_json: Optional[str] = Field(default=None, description="Explicit game list as JSON.")
    result_count: int = Field(default=0, ge=0, description="Number of games matched.")
    created_at: str = Field(default_factory=_now_iso, description="Created timestamp.")
    last_run_at: Optional[str] = Field(default=None, description="Last execution timestamp.")

    def to_db_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "ComparativeQuery":
        return cls(**row)


# ── ML Export ────────────────────────────────────────────────────────


class MlExport(BaseModel):
    """Tracking record for an ML dataset export."""

    id: str = Field(default_factory=lambda: _uuid7(), description="UUID v7.")
    export_type: ExportType = Field(..., description="Export format.")
    game_ids_json: Optional[str] = Field(default=None, description="Game list as JSON.")
    row_count: int = Field(default=0, ge=0, description="Number of rows exported.")
    file_path: Optional[str] = Field(default=None, description="Output file path.")
    file_size_bytes: Optional[int] = Field(default=None, ge=0, description="File size.")
    feature_list: Optional[str] = Field(default=None, description="Comma-separated feature names.")
    label_column: Optional[str] = Field(default=None, description="Target label column.")
    model_version: Optional[str] = Field(default=None, description="BLM model version.")
    created_at: str = Field(default_factory=_now_iso, description="Created timestamp.")

    def to_db_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["export_type"] = d["export_type"].value
        return d

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "MlExport":
        return cls(**row)
