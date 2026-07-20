"""BLM V2 — Prediction Drift Analyzer.

Analyses how BLM predictions drift over the course of a game.  Drift measures
the stability and consistency of the model's projections as new information
arrives.

Metrics:
  - RMS drift:      Root-mean-square deviation of projections over time.
  - Max drift:      Single largest projection change between consecutive snapshots.
  - Drift velocity: Rate of projection change per snapshot interval.
  - Compare:        Projected vs actual values at configurable intervals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DriftMetrics:
    """Prediction drift statistics for a game.

    Attributes:
        rms_drift:        Root-mean-square of the projection differences.
        max_drift:        Largest single-step projection change.
        drift_velocity:   Mean absolute projection change per snapshot interval.
        drift_trend:      "increasing", "decreasing", or "stable".
        projection_count: Number of projections analysed.
    """

    rms_drift: float = 0.0
    max_drift: float = 0.0
    drift_velocity: float = 0.0
    drift_trend: str = "stable"
    projection_count: int = 0


@dataclass
class ProjectionComparison:
    """Comparison of projected vs actual values at a single point.

    Attributes:
        snapshot_index:     Index of the snapshot in the sequence.
        quarter:            Game quarter.
        clock:              Game clock string.
        projected_total:    BLM expected total at this point.
        actual_total:       Actual total at game end.
        projected_margin:   BLM expected margin at this point.
        actual_margin:      Actual margin at game end.
        projection_error:   Difference (actual - projected).
    """

    snapshot_index: int
    quarter: int
    clock: str = ""
    projected_total: Optional[float] = None
    actual_total: float = 0.0
    projected_margin: Optional[float] = None
    actual_margin: float = 0.0
    projection_error: float = 0.0


class DriftAnalyzer:
    """Analyse prediction drift over the course of a game.

    Usage::

        analyzer = DriftAnalyzer()
        snapshots = await ts.query_snapshots("game-123")
        drift = analyzer.compute_prediction_drift(snapshots)
        comparisons = analyzer.compare_projections(snapshots, final_total=185.0)
    """

    # ── Public API ────────────────────────────────────────────────

    def compute_prediction_drift(
        self,
        snapshots: List[Dict[str, Any]],
    ) -> DriftMetrics:
        """Compute drift metrics for a sequence of snapshots.

        Args:
            snapshots: Chronological list of snapshot dicts.  Each should
                       contain a BLM expected_total or expected_margin field.

        Returns:
            DriftMetrics summarising prediction stability.
        """
        projections = self._extract_projections(snapshots)
        if len(projections) < 2:
            return DriftMetrics(projection_count=len(projections))

        return self.drift_score(projections, projections)

    def compare_projections(
        self,
        snapshots: List[Dict[str, Any]],
        final_total: Optional[float] = None,
        final_margin: Optional[float] = None,
        interval: int = 5,
    ) -> List[ProjectionComparison]:
        """Compare projected vs actual values at regular intervals.

        Args:
            snapshots:   Chronological snapshot list.
            final_total:  Actual final total score (auto-detected from last snap if None).
            final_margin: Actual final margin (auto-detected if None).
            interval:     Take every Nth snapshot.

        Returns:
            List of ProjectionComparison at the sampled intervals.
        """
        if not snapshots:
            return []

        last = snapshots[-1]
        if final_total is None:
            final_total = float(last.get("home_score", 0) + last.get("away_score", 0))
        if final_margin is None:
            final_margin = float(last.get("home_score", 0) - last.get("away_score", 0))

        comparisons: List[ProjectionComparison] = []
        for i in range(0, len(snapshots), interval):
            snap = snapshots[i]
            proj_total = self._get_expected_total(snap)
            proj_margin = self._get_expected_margin(snap)
            error = (final_total - proj_total) if proj_total is not None else 0.0

            quarter = self._get_quarter(snap)
            clock = self._get_clock(snap)

            comparisons.append(ProjectionComparison(
                snapshot_index=i,
                quarter=quarter,
                clock=clock or "",
                projected_total=proj_total,
                actual_total=final_total,
                projected_margin=proj_margin,
                actual_margin=final_margin,
                projection_error=round(error, 2),
            ))

        return comparisons

    @staticmethod
    def drift_score(
        projections: List[float],
        actuals: List[float],
    ) -> DriftMetrics:
        """Compute RMS drift, max drift, and drift velocity from sequences.

        Args:
            projections: Sequence of projected values.
            actuals:     Sequence of actual values (or reference values).

        Returns:
            DriftMetrics with RMS, max, velocity, and trend.
        """
        n = min(len(projections), len(actuals))
        if n < 2:
            return DriftMetrics(projection_count=n)

        # Per-step differences.
        diffs = [projections[i] - actuals[i] for i in range(n)]

        # RMS drift
        sq_sum = sum(d * d for d in diffs)
        rms = math.sqrt(sq_sum / n)

        # Max drift (max absolute deviation from actual)
        max_drift = max(abs(d) for d in diffs)

        # Drift velocity: mean absolute change between consecutive diffs.
        velocities = []
        for i in range(1, n):
            velocities.append(abs(diffs[i] - diffs[i - 1]))
        drift_vel = sum(velocities) / len(velocities) if velocities else 0.0

        # Trend
        trend = _compute_trend(velocities)

        return DriftMetrics(
            rms_drift=round(rms, 4),
            max_drift=round(max_drift, 4),
            drift_velocity=round(drift_vel, 4),
            drift_trend=trend,
            projection_count=n,
        )

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _extract_projections(snapshots: List[Dict[str, Any]]) -> List[float]:
        """Extract BLM expected_total from each snapshot."""
        projections = []
        for snap in snapshots:
            val = DriftAnalyzer._get_expected_total(snap)
            if val is not None:
                projections.append(val)
        return projections

    @staticmethod
    def _get_expected_total(snap: Dict[str, Any]) -> Optional[float]:
        """Safely extract expected_total from a snapshot, checking nested fields."""
        blm = snap.get("blm", {})
        if isinstance(blm, dict):
            val = blm.get("expected_total")
            if val is not None:
                return float(val)
        # Top-level fallback
        val = snap.get("expected_total")
        return float(val) if val is not None else None

    @staticmethod
    def _get_expected_margin(snap: Dict[str, Any]) -> Optional[float]:
        blm = snap.get("blm", {})
        if isinstance(blm, dict):
            val = blm.get("expected_margin")
            if val is not None:
                return float(val)
        val = snap.get("expected_margin")
        return float(val) if val is not None else None

    @staticmethod
    def _get_quarter(snap: Dict[str, Any]) -> int:
        meta = snap.get("metadata", {})
        if isinstance(meta, dict):
            return meta.get("quarter", 1)
        return snap.get("quarter", 1)

    @staticmethod
    def _get_clock(snap: Dict[str, Any]) -> Optional[str]:
        meta = snap.get("metadata", {})
        if isinstance(meta, dict):
            return meta.get("clock")
        return snap.get("clock")


def _compute_trend(velocities: List[float]) -> str:
    """Determine if drift velocity is increasing, decreasing, or stable."""
    if len(velocities) < 3:
        return "stable"
    recent = velocities[-3:]
    if all(recent[i] > recent[i - 1] for i in range(1, len(recent))):
        return "increasing"
    if all(recent[i] < recent[i - 1] for i in range(1, len(recent))):
        return "decreasing"
    return "stable"
