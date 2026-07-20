"""BLM V2 — Model Stability Analyzer.

Evaluates the stability of BLM model outputs over the course of a game.
Key metrics:

  - Model stability:   Variance in BLM score, confidence volatility.
  - Confidence accuracy: How well confidence correlates with actual prediction accuracy.
  - Projection error:   Error distribution (MAE, RMSE, bias).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class StabilityMetrics:
    """Stability metrics for a single game.

    Attributes:
        blm_score_variance:    Variance of the BLM score over the game.
        confidence_variance:   Variance of the confidence score.
        confidence_volatility: Standard deviation of consecutive confidence changes.
        projection_mae:        Mean absolute projection error (expected_total vs actual).
        projection_rmse:       Root-mean-square projection error.
        projection_bias:       Mean signed error (positive = over-prediction).
        sample_count:          Number of snapshots analysed.
    """

    blm_score_variance: float = 0.0
    confidence_variance: float = 0.0
    confidence_volatility: float = 0.0
    projection_mae: float = 0.0
    projection_rmse: float = 0.0
    projection_bias: float = 0.0
    sample_count: int = 0


@dataclass
class ConfidenceAccuracyMetrics:
    """How well confidence correlates with actual accuracy.

    Attributes:
        correlation:       Pearson correlation between confidence and accuracy.
        avg_conf_correct:  Mean confidence when prediction was correct.
        avg_conf_wrong:    Mean confidence when prediction was wrong.
        calibration_slope: Slope of confidence vs accuracy regression.
        sample_count:      Number of predictions analysed.
    """

    correlation: float = 0.0
    avg_conf_correct: float = 0.0
    avg_conf_wrong: float = 0.0
    calibration_slope: float = 0.0
    sample_count: int = 0


class StabilityAnalyzer:
    """Analyse model stability metrics from historical snapshots.

    Usage::

        analyzer = StabilityAnalyzer()
        metrics = await analyzer.model_stability(snapshots)
        conf_acc = await analyzer.confidence_accuracy(snapshots, actual_winner="home")
        errors = await analyzer.projection_error(snapshots, final_total=185.0)
    """

    # ── Public API ────────────────────────────────────────────────

    def model_stability(
        self,
        snapshots: List[Dict[str, Any]],
    ) -> StabilityMetrics:
        """Compute stability metrics for a game.

        Args:
            snapshots: Chronological list of snapshot dicts.

        Returns:
            StabilityMetrics with variance, volatility, and error stats.
        """
        if not snapshots:
            return StabilityMetrics()

        blm_scores = self._extract_blm_scores(snapshots)
        confs = self._extract_confidence(snapshots)

        blm_var = self._variance(blm_scores) if len(blm_scores) > 1 else 0.0
        conf_var = self._variance(confs) if len(confs) > 1 else 0.0
        conf_vol = self._volatility(confs) if len(confs) > 1 else 0.0

        # Projection error vs final outcome.
        last = snapshots[-1]
        final_total = float(last.get("home_score", 0) + last.get("away_score", 0))
        proj_errors = self._compute_projection_errors(snapshots, final_total)

        mae = (
            sum(abs(e) for e in proj_errors) / len(proj_errors)
            if proj_errors
            else 0.0
        )
        rmse = (
            math.sqrt(sum(e * e for e in proj_errors) / len(proj_errors))
            if proj_errors
            else 0.0
        )
        bias = (
            sum(proj_errors) / len(proj_errors) if proj_errors else 0.0
        )

        return StabilityMetrics(
            blm_score_variance=round(blm_var, 4),
            confidence_variance=round(conf_var, 4),
            confidence_volatility=round(conf_vol, 4),
            projection_mae=round(mae, 4),
            projection_rmse=round(rmse, 4),
            projection_bias=round(bias, 4),
            sample_count=len(snapshots),
        )

    def confidence_accuracy(
        self,
        snapshots: List[Dict[str, Any]],
        actual_winner: str = "",
    ) -> ConfidenceAccuracyMetrics:
        """Analyse how well confidence correlates with actual accuracy.

        Args:
            snapshots:     Chronological snapshot list.
            actual_winner: Name of the actual winning team. If empty, inferred
                           from the final snapshot.

        Returns:
            ConfidenceAccuracyMetrics with correlation and per-group means.
        """
        if not snapshots:
            return ConfidenceAccuracyMetrics()

        if not actual_winner:
            last = snapshots[-1]
            home = last.get("home_score", 0)
            away = last.get("away_score", 0)
            home_team = last.get("home_team", "")
            actual_winner = home_team if home >= away else last.get("away_team", "")

        confs = self._extract_confidence(snapshots)
        winners = self._extract_expected_winner(snapshots)

        n = min(len(confs), len(winners))
        if n < 2:
            return ConfidenceAccuracyMetrics(sample_count=n)

        confs = confs[:n]
        winners = winners[:n]

        # Bin predictions into correct/wrong.
        correct_confs = []
        wrong_confs = []
        accuracies = []
        for i in range(n):
            predicted = winners[i] if i < len(winners) else ""
            is_correct = 1.0 if predicted == actual_winner else 0.0
            accuracies.append(is_correct)
            if is_correct:
                correct_confs.append(confs[i])
            else:
                wrong_confs.append(confs[i])

        correlation = self._pearson(confs, accuracies)
        avg_correct = sum(correct_confs) / len(correct_confs) if correct_confs else 0.0
        avg_wrong = sum(wrong_confs) / len(wrong_confs) if wrong_confs else 0.0

        # Calibration slope: simple linear regression slope.
        slope = self._regression_slope(confs, accuracies)

        return ConfidenceAccuracyMetrics(
            correlation=round(correlation, 4),
            avg_conf_correct=round(avg_correct, 4),
            avg_conf_wrong=round(avg_wrong, 4),
            calibration_slope=round(slope, 4),
            sample_count=n,
        )

    def projection_error(
        self,
        snapshots: List[Dict[str, Any]],
        final_total: Optional[float] = None,
        final_margin: Optional[float] = None,
    ) -> StabilityMetrics:
        """Compute projection error distribution.

        Args:
            snapshots:     Chronological snapshot list.
            final_total:   Actual final total (auto-detected if None).
            final_margin:  Actual final margin (auto-detected if None).

        Returns:
            StabilityMetrics focused on projection error stats.
        """
        if not snapshots:
            return StabilityMetrics()

        last = snapshots[-1]
        if final_total is None:
            final_total = float(last.get("home_score", 0) + last.get("away_score", 0))
        if final_margin is None:
            final_margin = float(last.get("home_score", 0) - last.get("away_score", 0))

        proj_errors = self._compute_projection_errors(snapshots, final_total)
        if not proj_errors:
            return StabilityMetrics()

        mae = sum(abs(e) for e in proj_errors) / len(proj_errors)
        rmse = math.sqrt(sum(e * e for e in proj_errors) / len(proj_errors))
        bias = sum(proj_errors) / len(proj_errors)

        return StabilityMetrics(
            projection_mae=round(mae, 4),
            projection_rmse=round(rmse, 4),
            projection_bias=round(bias, 4),
            sample_count=len(proj_errors),
        )

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _extract_blm_scores(snapshots: List[Dict[str, Any]]) -> List[float]:
        scores = []
        for snap in snapshots:
            val = snap.get("momentum_score")
            if val is None:
                mom = snap.get("momentum", {})
                if isinstance(mom, dict):
                    val = mom.get("momentum_score")
            if val is not None:
                scores.append(float(val))
        return scores

    @staticmethod
    def _extract_confidence(snapshots: List[Dict[str, Any]]) -> List[float]:
        confs = []
        for snap in snapshots:
            val = snap.get("composite_confidence")
            if val is None:
                conf_data = snap.get("confidence", {})
                if isinstance(conf_data, dict):
                    val = conf_data.get("composite_confidence")
            if val is None:
                # Top-level fallback
                val = snap.get("confidence")
            if val is not None:
                confs.append(float(val))
        return confs

    @staticmethod
    def _extract_expected_winner(snapshots: List[Dict[str, Any]]) -> List[str]:
        winners = []
        for snap in snapshots:
            blm = snap.get("blm", {})
            if isinstance(blm, dict):
                winner = blm.get("expected_winner")
            else:
                winner = snap.get("expected_winner")
            if winner:
                winners.append(str(winner))
            else:
                # Fallback: use win_probability to infer
                win_prob = snap.get("win_probability")
                if win_prob is not None and isinstance(blm, dict):
                    winner = blm.get("expected_winner")
                    winners.append(str(winner) if winner else "")
                else:
                    winners.append("")
        return winners

    @staticmethod
    def _compute_projection_errors(
        snapshots: List[Dict[str, Any]],
        final_total: float,
    ) -> List[float]:
        errors = []
        for snap in snapshots:
            blm = snap.get("blm", {})
            if isinstance(blm, dict):
                proj = blm.get("expected_total")
            else:
                proj = snap.get("expected_total")
            if proj is not None:
                errors.append(float(proj) - final_total)
        return errors

    @staticmethod
    def _variance(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / len(values)

    @staticmethod
    def _volatility(values: List[float]) -> float:
        """Standard deviation of consecutive changes."""
        if len(values) < 2:
            return 0.0
        diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
        mean = sum(diffs) / len(diffs)
        variance = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        return math.sqrt(variance)

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> float:
        n = len(x)
        if n < 3:
            return 0.0
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(a * b for a, b in zip(x, y))
        sum_x2 = sum(a * a for a in x)
        sum_y2 = sum(b * b for b in y)
        num = n * sum_xy - sum_x * sum_y
        den = math.sqrt((n * sum_x2 - sum_x * sum_x) * (n * sum_y2 - sum_y * sum_y))
        if den == 0:
            return 0.0
        return max(-1.0, min(1.0, num / den))

    @staticmethod
    def _regression_slope(x: List[float], y: List[float]) -> float:
        """Simple linear regression slope (b in y = a + bx)."""
        n = len(x)
        if n < 2:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        den = sum((x[i] - mean_x) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        return num / den
