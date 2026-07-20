"""
BLM V2 — UNDER Timing Score

Composite score 0–100 that answers: "Is this the right moment for an UNDER entry?"

Combines signals from:
  - LineTracker (excursion, freeze, burst, divergence)
  - HistoricalEngine (league percentiles, regression rates, under rates)

Weights:
  - Historical Inflation (how rare is this excursion?)       25%
  - Freeze Duration (line frozen while score moves)          20%
  - Burst Confirmation (burst without line following)        15%
  - Excursion Depth (how far from OLV?)                     15%
  - Divergence Quality (score-only movement)                 10%
  - Historical Regression Rate (%)                           10%
  - League Under Rate (%)                                    5%

Output:
  - UNDER Timing Score (0-100)
  - Confidence (0-1)
  - Status: WAIT / WATCH / UNDER READY / PASS
  - Component breakdown
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from blm_v2.analytics.historical import HistoricalEngine
from blm_v2.analytics.line_tracker import (
    DivergenceType,
    FREEZE_THRESHOLD_TICKS,
    LineAnalysis,
)

logger = logging.getLogger(__name__)

# ── Entry status ─────────────────────────────────────────────────────

class UnderStatus(str, Enum):
    PASS = "PASS"
    WAIT = "WAIT"
    WATCH = "WATCH"
    UNDER_READY = "UNDER READY"


# ── Timing result ────────────────────────────────────────────────────

@dataclass
class UnderTimingResult:
    """Complete UNDER timing assessment for one snapshot."""
    score: float = 0.0          # 0-100 composite
    confidence: float = 0.0     # 0-1
    status: UnderStatus = UnderStatus.PASS

    # Component breakdown
    historical_inflation_score: float = 0.0   # 0-25
    freeze_score: float = 0.0                 # 0-20
    burst_score: float = 0.0                  # 0-15
    excursion_score: float = 0.0              # 0-15
    divergence_score: float = 0.0             # 0-10
    regression_score: float = 0.0             # 0-10
    under_rate_score: float = 0.0             # 0-5

    # Context
    signals_met: list[str] = field(default_factory=list)
    signals_missed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "under_timing_score": round(self.score, 1),
            "confidence": round(self.confidence, 3),
            "status": self.status.value,
            "components": {
                "historical_inflation": round(self.historical_inflation_score, 1),
                "freeze": round(self.freeze_score, 1),
                "burst": round(self.burst_score, 1),
                "excursion": round(self.excursion_score, 1),
                "divergence": round(self.divergence_score, 1),
                "regression": round(self.regression_score, 1),
                "under_rate": round(self.under_rate_score, 1),
            },
            "signals_met": self.signals_met,
            "signals_missed": self.signals_missed,
        }


# ── Timing thresholds (configurable) ────────────────────────────────

UNDER_READY_THRESHOLD = 60.0   # Score above this = UNDER READY
WATCH_THRESHOLD = 35.0         # Score above watch but below ready = WATCH
WAIT_THRESHOLD = 15.0          # Score above wait but below watch = WAIT
MIN_CONFIDENCE = 0.3           # Minimum confidence to enter WATCH+
HISTORICAL_MIN_GAMES = 5       # Need at least this many games for historical confidence


# ── UNDER Timing Engine ─────────────────────────────────────────────

class UnderTimingEngine:
    """Computes the UNDER Timing Score per-snapshot.

    Combines live line analysis with league historical data.

    Usage:
        engine = UnderTimingEngine(historical_engine)
        result = engine.evaluate(line_analysis, league="Cyber 2K26")
    """

    def __init__(self, historical_engine: HistoricalEngine):
        self._historical = historical_engine

    def evaluate(
        self,
        analysis: LineAnalysis,
        league: str = "Cyber 2K26",
    ) -> UnderTimingResult:
        """Compute UNDER timing for one snapshot.

        Args:
            analysis: LineAnalysis from the LineTracker for this tick.
            league: League name for historical lookups.

        Returns:
            UnderTimingResult with score, confidence, and status.
        """
        league_profile = self._historical.get_profile(league)
        result = UnderTimingResult()
        met: list[str] = []
        missed: list[str] = []
        raw_score = 0.0

        # ── 1. Historical Inflation (0-25) ─────────────────────────
        # How rare is this excursion in the league's history?
        if analysis.excursion is not None and analysis.excursion > 0 and league_profile.excursion_mean > 0:
            percentile = self._historical.get_excursion_percentile(
                analysis.excursion, league
            )
            if percentile >= 95:
                result.historical_inflation_score = 25.0
                met.append("excursion in top 5% historically")
            elif percentile >= 90:
                result.historical_inflation_score = 20.0
                met.append("excursion in top 10% historically")
            elif percentile >= 75:
                result.historical_inflation_score = 15.0
                met.append("excursion in top 25%")
            elif percentile >= 50:
                result.historical_inflation_score = 10.0
                met.append("excursion above median")
            else:
                result.historical_inflation_score = 5.0
                missed.append("excursion not historically elevated")
        else:
            missed.append("no excursion or line not inflated")

        # ── 2. Freeze Duration (0-20) ─────────────────────────────
        if analysis.freeze_ticks >= FREEZE_THRESHOLD_TICKS and analysis.score_delta > 0:
            freeze_weight = min(20.0, analysis.freeze_ticks * 5.0)
            result.freeze_score = freeze_weight
            met.append(f"line frozen {analysis.freeze_ticks} ticks with scoring")
        elif analysis.freeze_ticks >= 2 and analysis.score_delta > 0:
            result.freeze_score = 10.0
            met.append("line starting to freeze while scoring")
        else:
            missed.append("no freeze or no scoring")

        # ── 3. Burst Confirmation (0-15) ──────────────────────────
        if analysis.is_burst and analysis.divergence == DivergenceType.SCORE_ONLY:
            result.burst_score = 15.0
            met.append("scoring burst with no line movement")
        elif analysis.is_burst and analysis.line_delta == 0:
            result.burst_score = 10.0
            met.append("scoring burst with minimal line movement")
        elif analysis.is_burst:
            result.burst_score = 5.0
            met.append("scoring burst detected")
        else:
            missed.append("no scoring burst")

        # ── 4. Excursion Depth (0-15) ─────────────────────────────
        if analysis.excursion is not None:
            if analysis.excursion >= 10.0:
                result.excursion_score = 15.0
                met.append("deep excursion >= 10 points")
            elif analysis.excursion >= 6.0:
                result.excursion_score = 12.0
                met.append("moderate excursion >= 6 points")
            elif analysis.excursion >= 3.0:
                result.excursion_score = 8.0
                met.append("mild excursion >= 3 points")
            elif analysis.excursion > 0:
                result.excursion_score = 4.0
                met.append("slight excursion above OLV")
        else:
            missed.append("no excursion data")

        # ── 5. Divergence Quality (0-10) ──────────────────────────
        if analysis.divergence == DivergenceType.SCORE_ONLY:
            result.divergence_score = 10.0
            met.append("pure score-only movement (market lagging)")
        elif analysis.divergence == DivergenceType.NEITHER:
            result.divergence_score = 5.0
            met.append("stagnant market")
        elif analysis.divergence == DivergenceType.BOTH:
            result.divergence_score = 3.0
            missed.append("score and line both moving — market reacting")
        else:
            missed.append("line-only movement")

        # ── 6. Historical Regression Rate (0-10) ──────────────────
        if league_profile.total_games >= HISTORICAL_MIN_GAMES:
            reg_rate = league_profile.total_regression_rate
            if reg_rate >= 0.6:
                result.regression_score = 10.0
                met.append(f"high historical regression rate ({reg_rate:.0%})")
            elif reg_rate >= 0.4:
                result.regression_score = 7.0
                met.append(f"moderate regression rate ({reg_rate:.0%})")
            elif reg_rate >= 0.2:
                result.regression_score = 4.0
                met.append(f"low regression rate ({reg_rate:.0%})")
            else:
                missed.append(f"very low regression rate ({reg_rate:.0%})")
        else:
            missed.append(f"insufficient historical data ({league_profile.total_games} games)")

        # ── 7. League Under Rate (0-5) ────────────────────────────
        if league_profile.total_games >= HISTORICAL_MIN_GAMES:
            under_rate = league_profile.under_rate
            if under_rate >= 0.55:
                result.under_rate_score = 5.0
                met.append(f"high league under rate ({under_rate:.0%})")
            elif under_rate >= 0.45:
                result.under_rate_score = 3.0
                met.append(f"neutral under rate ({under_rate:.0%})")
            else:
                result.under_rate_score = 1.0
                missed.append(f"low under rate ({under_rate:.0%})")
        else:
            missed.append(f"insufficient under rate data")

        # ── Composite ─────────────────────────────────────────────
        raw_score = (
            result.historical_inflation_score
            + result.freeze_score
            + result.burst_score
            + result.excursion_score
            + result.divergence_score
            + result.regression_score
            + result.under_rate_score
        )
        result.score = min(100.0, raw_score)

        # ── Confidence based on historical sample size ────────────
        base_confidence = league_profile.confidence
        # Boost if many signals are met
        signal_ratio = len(met) / max(len(met) + len(missed), 1)
        result.confidence = min(1.0, base_confidence * (0.5 + 0.5 * signal_ratio))

        # ── Status ────────────────────────────────────────────────
        if result.score >= UNDER_READY_THRESHOLD and result.confidence >= MIN_CONFIDENCE:
            result.status = UnderStatus.UNDER_READY
        elif result.score >= WATCH_THRESHOLD and result.confidence >= MIN_CONFIDENCE:
            result.status = UnderStatus.WATCH
        elif result.score >= WAIT_THRESHOLD:
            result.status = UnderStatus.WAIT
        else:
            result.status = UnderStatus.PASS

        result.signals_met = met
        result.signals_missed = missed
        return result
