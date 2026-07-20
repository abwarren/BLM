"""
BLM V2 — Prediction Models

Typed schemas for every output the BLM platform produces. Each prediction includes
confidence, supporting evidence, and metadata for traceability.

Model outputs:
  - WinnerPrediction:     Which team wins and by how much
  - MarginPrediction:     Expected final score margin
  - TotalPrediction:      Expected final total (over/under assessment)
  - ClosingLineValue:     Closing line value (CLV) — how well the model's
                          prediction aligned with the final line
  - TrapAccuracy:         Accuracy of the trap meter's alerts
  - PredictionBundle:     Combined container for all prediction types
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Winner Prediction ────────────────────────────────────────────


class WinnerPrediction(BaseModel):
    """Prediction of which team will win the game."""

    predicted_winner: str = Field(
        ..., description="Name of the predicted winning team."
    )
    win_probability: float = Field(
        ..., ge=0.0, le=1.0, description="Estimated win probability (0-1)."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in this prediction (0-1)."
    )
    home_win_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Implied win probability for the home team.",
    )
    away_win_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Implied win probability for the away team.",
    )
    model_agreement: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Agreement level among ensemble models (0-1).",
    )
    supporting_factors: list[str] = Field(
        default_factory=list,
        description="Evidence and factors supporting this prediction.",
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="When this prediction was generated.",
    )


# ── Margin Prediction ────────────────────────────────────────────


class MarginPrediction(BaseModel):
    """Prediction of the final score margin (home - away)."""

    predicted_margin: float = Field(
        ..., description="Predicted final score margin (home - away)."
    )
    margin_range_low: float = Field(
        ..., description="Lower bound of the margin confidence interval."
    )
    margin_range_high: float = Field(
        ..., description="Upper bound of the margin confidence interval."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the margin range."
    )
    projected_home_score: Optional[float] = Field(
        default=None, ge=0.0, description="Projected final home score."
    )
    projected_away_score: Optional[float] = Field(
        default=None, ge=0.0, description="Projected final away score."
    )
    confidence_interval: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Confidence interval percentage (default 95%).",
    )


# ── Total Prediction ─────────────────────────────────────────────


class TotalPrediction(BaseModel):
    """Prediction of the final total combined score."""

    predicted_total: float = Field(
        ..., ge=0.0, description="Predicted final total score."
    )
    total_range_low: float = Field(
        ..., ge=0.0, description="Lower bound of total confidence interval."
    )
    total_range_high: float = Field(
        ..., ge=0.0, description="Upper bound of total confidence interval."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the total range."
    )
    over_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Probability that total goes OVER the current line.",
    )
    under_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Probability that total goes UNDER the current line.",
    )
    expected_pace: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Expected pace used in the total projection.",
    )
    league_average_total: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="League average total for reference.",
    )
    recommendation: Optional[str] = Field(
        default=None,
        description="Simple recommendation: OVER, UNDER, PASS, WATCH, WAIT.",
    )


# ── Closing Line Value ───────────────────────────────────────────


class ClosingLineValue(BaseModel):
    """Closing line value (CLV) — how well the model predicted vs the final line.

    Positive CLV means the model beat the closing line (desirable).
    """

    market: str = Field(
        ..., description="Market evaluated (spread, total, moneyline)."
    )
    model_prediction: float = Field(
        ..., description="Model's predicted line / value."
    )
    closing_line: float = Field(
        ..., description="Actual closing line at game end."
    )
    difference: float = Field(
        ..., description="Difference: model_prediction - closing_line."
    )
    clv_percentage: Optional[float] = Field(
        default=None,
        description="CLV as a percentage of closing line value.",
    )
    is_beat: bool = Field(
        default=False,
        description="True if the model prediction beat the closing line.",
    )
    confidence_at_prediction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Model confidence when the prediction was made.",
    )


# ── Trap Accuracy ────────────────────────────────────────────────


class TrapAccuracy(BaseModel):
    """How accurate the Trap Meter was in detecting traps for a completed game."""

    total_alerts: int = Field(
        default=0, ge=0, description="Total number of trap alerts fired."
    )
    true_positives: int = Field(
        default=0, ge=0, description="Alerts that correctly identified a trap."
    )
    false_positives: int = Field(
        default=0, ge=0, description="Alerts that were false alarms."
    )
    true_negatives: int = Field(
        default=0, ge=0, description="Correct non-alerts."
    )
    false_negatives: int = Field(
        default=0, ge=0, description="Missed traps (should have alerted)."
    )
    accuracy: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="(TP + TN) / Total — overall trap detection accuracy.",
    )
    precision: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="TP / (TP + FP) — precision of trap alerts.",
    )
    recall: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="TP / (TP + FN) — recall of trap detection.",
    )
    f1_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="F1 = 2 * (precision * recall) / (precision + recall).",
    )


# ── Prediction Bundle ────────────────────────────────────────────


class PredictionBundle(BaseModel):
    """Complete set of predictions for a single game at a point in time.

    This is the primary output container served by the BLM engine.
    """

    game_id: str = Field(
        ..., description="Game identifier these predictions apply to."
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="When this prediction bundle was generated.",
    )
    winner: Optional[WinnerPrediction] = Field(
        default=None, description="Winner prediction."
    )
    margin: Optional[MarginPrediction] = Field(
        default=None, description="Margin prediction."
    )
    total: Optional[TotalPrediction] = Field(
        default=None, description="Total score prediction."
    )
    clv: Optional[list[ClosingLineValue]] = Field(
        default=None, description="Closing line values for tracked markets."
    )
    trap_accuracy: Optional[TrapAccuracy] = Field(
        default=None, description="Trap meter accuracy for completed games."
    )
    snapshot_id: Optional[str] = Field(
        default=None,
        description="Snapshot identifier this bundle was derived from.",
    )
    model_version: str = Field(
        default="2.0.0",
        description="Version of the BLM model that generated these predictions.",
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description="Additional metadata or context.",
    )
