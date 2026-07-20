"""
BLM V2 — OLV/CLV Line Tracker

Tracks every snapshot's total line movement relative to the Opening Line Value
(OLV), detects divergence between score movement and line movement, and records
burst signatures and freeze durations.

Uses SQLite queries against the existing ``snapshots_v2`` table — no new
storage backend needed.

Exports:
    LineTracker      — Per-snapshot analysis calculator
    LineAnalysis     — One analysis record (dict shape)
    DivergenceType   — SCORE_ONLY, LINE_ONLY, BOTH, NEITHER
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

BURST_THRESHOLD_FACTOR = 1.5
"""Multiplier on the rolling mean score delta to classify a burst."""

FREEZE_THRESHOLD_TICKS = 3
"""Number of ticks without line change to classify as 'frozen'."""

DEFAULT_TICK_S = 20.0
"""Expected seconds between ticks — used for time-based estimates."""


# ── Divergence type ───────────────────────────────────────────────────

class DivergenceType(str, Enum):
    """What moved since the last snapshot."""
    SCORE_ONLY = "score_only"     # score changed, line did not
    LINE_ONLY = "line_only"       # line changed, score did not
    BOTH = "both"                 # both moved
    NEITHER = "neither"           # nothing moved (stale snapshot)


# ── Analysis result data class ───────────────────────────────────────

@dataclass
class LineAnalysis:
    """Line analysis computed for one snapshot point.

    Stored as part of the enriched output and optionally written to a
    dedicated ``line_analysis`` table.
    """
    # Identification
    game_id: str = ""
    timestamp: str = ""

    # Opening anchor
    olv: Optional[float] = None
    """Opening Line Value — the first total_line observed for this game."""

    clv: Optional[float] = None
    """Closing Line Value placeholder — the last line before game end."""

    # Current snapshot
    current_line: Optional[float] = None
    current_score: int = 0
    current_total: int = 0

    # Excursion from opening
    excursion: Optional[float] = None
    """current_line - olv. Positive = line inflated above opening."""

    excursion_percent: Optional[float] = None
    """(excursion / olv) * 100 if olv > 0 else 0."""

    # Score movement since last tick
    score_delta: int = 0
    """Points scored since the previous snapshot (home + away increment)."""

    line_delta: float = 0.0
    """Line change since the previous snapshot."""

    # Divergence detection
    divergence: DivergenceType = DivergenceType.NEITHER
    freeze_ticks: int = 0
    """Consecutive ticks where the line has not moved."""

    # Burst detection
    burst_score: int = 0
    """Score delta in the current interval — used to detect bursts."""

    is_burst: bool = False
    """True if score_delta exceeds the historical rolling average * threshold."""

    # Rolling stats (maintained per-game)
    rolling_mean_score_delta: float = 0.0
    """Exponential rolling average of score deltas for this game."""
    rolling_mean_line_delta: float = 0.0
    """Exponential rolling average of line deltas."""

    # Derived under signal
    under_confidence: float = 0.0
    """Preliminary under confidence from line analysis alone (0–1)."""

    # Serialisation helpers
    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "timestamp": self.timestamp,
            "olv": self.olv,
            "clv": self.clv,
            "current_line": self.current_line,
            "current_score": self.current_score,
            "current_total": self.current_total,
            "excursion": self.excursion,
            "excursion_percent": self.excursion_percent,
            "score_delta": self.score_delta,
            "line_delta": self.line_delta,
            "divergence": self.divergence.value,
            "freeze_ticks": self.freeze_ticks,
            "burst_score": self.burst_score,
            "is_burst": self.is_burst,
            "rolling_mean_score_delta": self.rolling_mean_score_delta,
            "rolling_mean_line_delta": self.rolling_mean_line_delta,
            "under_confidence": self.under_confidence,
        }


# ── Line Tracker ─────────────────────────────────────────────────────

class LineTracker:
    """Tracks line movement per-game using its own rolling state.

    Maintains per-game state dicts for OLV, rolling means, freeze counters,
    and previous scores.  Instantiate once and call ``analyze()`` on every
    tick for every game.

    Usage:
        tracker = LineTracker()
        analysis = tracker.analyze(game_id, snapshot_dict, tick_s=20.0)
    """

    def __init__(self):
        self._games: dict[str, dict[str, Any]] = {}
        """Per-game state: olv, prev_line, prev_total, rolling_mean_score,
        rolling_mean_line, freeze_ticks, snapshots_seen."""

    def reset_game(self, game_id: str) -> None:
        """Clear internal state for a game (e.g. when game ends)."""
        self._games.pop(game_id, None)

    def analyze(
        self,
        game_id: str,
        snapshot: dict[str, Any],
        tick_s: float = DEFAULT_TICK_S,
    ) -> LineAnalysis:
        """Compute line analysis for one snapshot tick.

        Args:
            game_id: The game being tracked.
            snapshot: An enriched snapshot dict from the pipeline.
                Expected keys: timestamp, total_line, home_score, away_score,
                quarter, clock.
            tick_s: Expected tick interval in seconds (for time estimates).

        Returns:
            LineAnalysis with all computed fields.
        """
        ts = snapshot.get("timestamp", "")
        total_line = snapshot.get("total_line")
        home_score = snapshot.get("home_score", 0)
        away_score = snapshot.get("away_score", 0)
        total = (home_score or 0) + (away_score or 0)

        state = self._games.setdefault(game_id, {
            "olv": None,
            "prev_line": None,
            "prev_total": 0,
            "rolling_mean_score": 0.0,
            "rolling_mean_line": 0.0,
            "freeze_ticks": 0,
            "snapshots_seen": 0,
            "prev_timestamp": None,
        })

        # ── OLV: first line seen = Opening Line Value ──────────────
        if state["olv"] is None and total_line is not None:
            state["olv"] = total_line
            logger.info("OLV set for %s: %.1f", game_id, total_line)

        olv = state["olv"]
        prev_line = state["prev_line"]
        prev_total = state["prev_total"]

        # ── Score and line deltas ──────────────────────────────────
        score_delta = total - prev_total
        line_delta = 0.0
        if total_line is not None and prev_line is not None:
            line_delta = total_line - prev_line

        # ── Freeze detection ───────────────────────────────────────
        if abs(line_delta) < 0.01 and state["snapshots_seen"] > 0:
            state["freeze_ticks"] += 1
        else:
            state["freeze_ticks"] = 0

        freeze_ticks = state["freeze_ticks"]

        # ── Divergence type ───────────────────────────────────────
        score_moved = score_delta > 0
        line_moved = abs(line_delta) >= 0.01
        if score_moved and not line_moved:
            divergence = DivergenceType.SCORE_ONLY
        elif not score_moved and line_moved:
            divergence = DivergenceType.LINE_ONLY
        elif score_moved and line_moved:
            divergence = DivergenceType.BOTH
        else:
            divergence = DivergenceType.NEITHER

        # ── Rolling means (EMA) ────────────────────────────────────
        alpha = 0.3
        if state["snapshots_seen"] == 0:
            state["rolling_mean_score"] = float(score_delta)
            state["rolling_mean_line"] = float(abs(line_delta))
        else:
            state["rolling_mean_score"] = (
                alpha * score_delta + (1 - alpha) * state["rolling_mean_score"]
            )
            state["rolling_mean_line"] = (
                alpha * abs(line_delta) + (1 - alpha) * state["rolling_mean_line"]
            )

        rolling_mean_score = state["rolling_mean_score"]
        rolling_mean_line = state["rolling_mean_line"]

        # ── Burst detection ───────────────────────────────────────
        is_burst = False
        if rolling_mean_score > 0 and state["snapshots_seen"] >= 3:
            is_burst = score_delta > rolling_mean_score * BURST_THRESHOLD_FACTOR

        # ── Excursion from OLV ────────────────────────────────────
        excursion = None
        excursion_pct = None
        if olv is not None and total_line is not None:
            excursion = total_line - olv
            if olv > 0:
                excursion_pct = (excursion / olv) * 100.0

        # ── Under confidence from line analysis ────────────────────
        # Scored 0-1 based on: is line inflated?, is it frozen?,
        # did a burst just happen without line following?
        under_conf = 0.0
        signals = 0

        # Signal 1: Line inflated above OLV
        if excursion is not None and excursion > 0:
            under_conf += 0.25
            signals += 1

        # Signal 2: Line frozen (no movement despite scoring)
        if freeze_ticks >= FREEZE_THRESHOLD_TICKS and score_delta > 0:
            under_conf += 0.25
            signals += 1

        # Signal 3: Burst detected (score jumped but line didn't)
        if is_burst and not line_moved:
            under_conf += 0.3
            signals += 1

        # Signal 4: Score-only divergence (classic dead market)
        if divergence == DivergenceType.SCORE_ONLY and freeze_ticks >= 2:
            under_conf += 0.2
            signals += 1

        if signals > 0:
            under_conf = min(1.0, under_conf)

        # ── Advance state ─────────────────────────────────────────
        state["prev_line"] = total_line
        state["prev_total"] = total
        state["snapshots_seen"] += 1
        state["prev_timestamp"] = ts

        return LineAnalysis(
            game_id=game_id,
            timestamp=ts,
            olv=olv,
            clv=None,
            current_line=total_line,
            current_score=total,
            current_total=total,
            excursion=excursion,
            excursion_percent=excursion_pct,
            score_delta=score_delta,
            line_delta=line_delta,
            divergence=divergence,
            freeze_ticks=freeze_ticks,
            burst_score=score_delta,
            is_burst=is_burst,
            rolling_mean_score_delta=rolling_mean_score,
            rolling_mean_line_delta=rolling_mean_line,
            under_confidence=under_conf,
        )
