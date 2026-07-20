"""BLM V2 — Model Calibration Analyzer.

Evaluates how well the BLM model's predicted confidence matches actual accuracy.

Key concepts:
  - Calibration curve: Actual accuracy vs predicted confidence (binned).
  - ECE (Expected Calibration Error):  Mean absolute difference between
    confidence and accuracy across bins.
  - Over/under-confidence: Systematic tendency to be too confident or too cautious.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CalibrationBin:
    """A single bin on the calibration curve.

    Attributes:
        bin_center:      Midpoint of the confidence range.
        bin_lower:       Lower bound of this bin (inclusive).
        bin_upper:       Upper bound of this bin (exclusive).
        count:           Number of predictions in this bin.
        avg_confidence:  Mean predicted confidence in this bin.
        accuracy:        Fraction of correct predictions in this bin.
        confidence_gap:  avg_confidence - accuracy (positive = overconfidence).
    """

    bin_center: float
    bin_lower: float
    bin_upper: float
    count: int
    avg_confidence: float
    accuracy: float
    confidence_gap: float


@dataclass
class CalibrationCurve:
    """Full calibration curve data.

    Attributes:
        bins:           List of CalibrationBin objects.
        ece:            Expected Calibration Error.
        mce:            Maximum Calibration Error.
        n_bins:         Number of bins used.
        total_samples:  Total predictions analysed.
    """

    bins: List[CalibrationBin] = field(default_factory=list)
    ece: float = 0.0
    mce: float = 0.0
    n_bins: int = 0
    total_samples: int = 0


@dataclass
class CalibrationReport:
    """Comprehensive calibration report for a game.

    Attributes:
        calibration_curve:   CalibrationCurve with bin details.
        ece:                 Expected Calibration Error.
        is_overconfident:    True if model is systematically overconfident.
        is_underconfident:   True if model is systematically underconfident.
        overconfidence_magnitude: Mean confidence_gap (positive = overconfident).
        recommendation:      Human-readable recommendation.
    """

    calibration_curve: CalibrationCurve = field(default_factory=CalibrationCurve)
    ece: float = 0.0
    is_overconfident: bool = False
    is_underconfident: bool = False
    overconfidence_magnitude: float = 0.0
    recommendation: str = ""


class CalibrationAnalyzer:
    """Analyse model calibration from historical snapshots.

    Usage::

        analyzer = CalibrationAnalyzer()
        curve = analyzer.calibration_curve(snapshots, actual_winner="Warriors")
        ece = analyzer.calibration_error(snapshots, actual_winner="Warriors")
        report = analyzer.model_calibration_report(snapshots, actual_winner="Warriors")
    """

    # ── Public API ────────────────────────────────────────────────

    def calibration_curve(
        self,
        snapshots: List[Dict[str, Any]],
        actual_winner: str = "",
        n_bins: int = 10,
    ) -> CalibrationCurve:
        """Compute the calibration curve — accuracy vs confidence per bin.

        Args:
            snapshots:     Chronological list of snapshot dicts.
            actual_winner: Actual winning team name. Auto-detected if empty.
            n_bins:        Number of confidence bins (default 10).

        Returns:
            CalibrationCurve with bin-level accuracy and confidence.
        """
        if not snapshots:
            return CalibrationCurve(n_bins=n_bins)

        if not actual_winner:
            actual_winner = self._infer_winner(snapshots)

        pairs = self._extract_confidence_accuracy_pairs(snapshots, actual_winner)
        if len(pairs) < n_bins:
            n_bins = max(2, len(pairs))

        bins = self._bin_pairs(pairs, n_bins)
        ece = self._compute_ece(bins)
        mce = self._compute_mce(bins)

        return CalibrationCurve(
            bins=bins,
            ece=round(ece, 4),
            mce=round(mce, 4),
            n_bins=n_bins,
            total_samples=len(pairs),
        )

    def calibration_error(
        self,
        snapshots: List[Dict[str, Any]],
        actual_winner: str = "",
        n_bins: int = 10,
    ) -> float:
        """Compute Expected Calibration Error (ECE).

        ECE = Σ (|b| / N) × |acc(b) - conf(b)|

        where:
            |b|      = number of predictions in bin b
            N        = total predictions
            acc(b)   = accuracy in bin b
            conf(b)  = mean confidence in bin b

        Args:
            snapshots:     Chronological list of snapshot dicts.
            actual_winner: Actual winning team name. Auto-detected if empty.
            n_bins:        Number of confidence bins (default 10).

        Returns:
            ECE value in [0.0, 1.0].
        """
        curve = self.calibration_curve(snapshots, actual_winner, n_bins)
        return curve.ece

    def model_calibration_report(
        self,
        snapshots: List[Dict[str, Any]],
        actual_winner: str = "",
        n_bins: int = 10,
    ) -> CalibrationReport:
        """Generate a comprehensive calibration report.

        Includes over/under-confidence detection and a recommendation.

        Args:
            snapshots:     Chronological list of snapshot dicts.
            actual_winner: Actual winning team name. Auto-detected if empty.
            n_bins:        Number of confidence bins (default 10).

        Returns:
            CalibrationReport with full analysis.
        """
        curve = self.calibration_curve(snapshots, actual_winner, n_bins)

        if not curve.bins:
            return CalibrationReport(
                calibration_curve=curve,
                ece=curve.ece,
                recommendation="Insufficient data for calibration analysis.",
            )

        # Compute over/under-confidence signal.
        total_gap = sum(b.confidence_gap for b in curve.bins if b.count > 0)
        n_nonempty = sum(1 for b in curve.bins if b.count > 0)
        avg_gap = total_gap / n_nonempty if n_nonempty > 0 else 0.0

        is_over = avg_gap > 0.05
        is_under = avg_gap < -0.05

        if is_over:
            recommendation = (
                f"Model shows systematic overconfidence (mean gap: {avg_gap:.3f}). "
                "Consider adjusting confidence weights downward or increasing "
                "the penalty for high-confidence errors."
            )
        elif is_under:
            recommendation = (
                f"Model shows systematic underconfidence (mean gap: {avg_gap:.3f}). "
                "The model is more accurate than it believes. Consider increasing "
                "confidence calibration or reducing the confidence penalty."
            )
        else:
            recommendation = (
                f"Model calibration is acceptable (mean gap: {avg_gap:.3f}). "
                "No systematic over/under-confidence detected."
            )

        return CalibrationReport(
            calibration_curve=curve,
            ece=curve.ece,
            is_overconfident=is_over,
            is_underconfident=is_under,
            overconfidence_magnitude=round(avg_gap, 4),
            recommendation=recommendation,
        )

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _infer_winner(snapshots: List[Dict[str, Any]]) -> str:
        last = snapshots[-1]
        home = last.get("home_score", 0)
        away = last.get("away_score", 0)
        home_team = last.get("home_team", "")
        away_team = last.get("away_team", "")
        return home_team if home >= away else (away_team or "")

    @staticmethod
    def _extract_confidence_accuracy_pairs(
        snapshots: List[Dict[str, Any]],
        actual_winner: str,
    ) -> List[Tuple[float, bool]]:
        """Extract (confidence, is_correct) pairs from snapshots."""
        pairs = []
        for snap in snapshots:
            conf = None
            conf_data = snap.get("confidence", {})
            if isinstance(conf_data, dict):
                conf = conf_data.get("composite_confidence")
            if conf is None:
                conf = snap.get("composite_confidence")
            if conf is None:
                conf = snap.get("confidence")
            if conf is None:
                continue

            conf = float(conf)

            blm = snap.get("blm", {})
            if isinstance(blm, dict):
                predicted = blm.get("expected_winner", "")
            else:
                predicted = snap.get("expected_winner", "")

            is_correct = bool(predicted) and str(predicted) == actual_winner
            pairs.append((conf, is_correct))

        return pairs

    @staticmethod
    def _bin_pairs(
        pairs: List[Tuple[float, bool]],
        n_bins: int,
    ) -> List[CalibrationBin]:
        """Group (confidence, accuracy) pairs into equal-width bins."""
        if not pairs:
            return []

        bins: List[CalibrationBin] = []
        bin_width = 1.0 / n_bins

        for i in range(n_bins):
            lower = i * bin_width
            upper = (i + 1) * bin_width
            center = lower + bin_width / 2.0

            # Last bin includes 1.0
            if i == n_bins - 1:
                in_bin = [(c, a) for c, a in pairs if lower <= c <= 1.0]
            else:
                in_bin = [(c, a) for c, a in pairs if lower <= c < upper]

            count = len(in_bin)
            if count == 0:
                bins.append(CalibrationBin(
                    bin_center=round(center, 2),
                    bin_lower=round(lower, 2),
                    bin_upper=round(upper, 2),
                    count=0,
                    avg_confidence=0.0,
                    accuracy=0.0,
                    confidence_gap=0.0,
                ))
                continue

            avg_conf = sum(c for c, _ in in_bin) / count
            accuracy = sum(1 for _, a in in_bin if a) / count
            gap = avg_conf - accuracy

            bins.append(CalibrationBin(
                bin_center=round(center, 2),
                bin_lower=round(lower, 2),
                bin_upper=round(upper, 2),
                count=count,
                avg_confidence=round(avg_conf, 4),
                accuracy=round(accuracy, 4),
                confidence_gap=round(gap, 4),
            ))

        return bins

    @staticmethod
    def _compute_ece(bins: List[CalibrationBin]) -> float:
        """Expected Calibration Error.

        .. math::
            ECE = \\sum_{b} \\frac{|b|}{N} \\times |acc(b) - conf(b)|
        """
        total = sum(b.count for b in bins)
        if total == 0:
            return 0.0
        ece = 0.0
        for b in bins:
            if b.count > 0:
                weight = b.count / total
                ece += weight * abs(b.confidence_gap)
        return ece

    @staticmethod
    def _compute_mce(bins: List[CalibrationBin]) -> float:
        """Maximum Calibration Error — the worst-case bin gap."""
        if not bins:
            return 0.0
        return max(abs(b.confidence_gap) for b in bins if b.count > 0)
