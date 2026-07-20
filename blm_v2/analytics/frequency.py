"""BLM V2 — Frequency Analyzer.

Analyses how often various market and model events occur during a game:

  - Trap frequency:      How often the trap meter signals a market anomaly.
  - Trap types:          Distribution of detected trap types.
  - Momentum swings:     How often momentum changes direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Counter, Dict, List, Optional


@dataclass
class FrequencyMetrics:
    """Frequency analysis results for a game.

    Attributes:
        traps_per_quarter:       Trap occurrences broken down by quarter.
        total_traps:             Total trap events detected.
        trap_rate:               Traps per snapshot (frequency).
        trap_type_distribution:  Counter of trap type labels.
        momentum_swings:         Number of momentum direction changes.
        momentum_swing_rate:     Momentum swings per snapshot.
        sample_count:            Total snapshots analysed.
    """

    traps_per_quarter: Dict[int, int] = field(default_factory=dict)
    total_traps: int = 0
    trap_rate: float = 0.0
    trap_type_distribution: Dict[str, int] = field(default_factory=dict)
    momentum_swings: int = 0
    momentum_swing_rate: float = 0.0
    sample_count: int = 0


class FrequencyAnalyzer:
    """Analyse event frequencies from historical snapshots.

    Usage::

        analyzer = FrequencyAnalyzer()
        freq = analyzer.trap_frequency(snapshots)
        breakdown = analyzer.trap_types_breakdown(snapshots)
        swings = analyzer.momentum_swing_frequency(snapshots)
    """

    # ── Public API ────────────────────────────────────────────────

    def trap_frequency(
        self,
        snapshots: List[Dict[str, Any]],
    ) -> FrequencyMetrics:
        """Analyse trap occurrence frequency per quarter and overall.

        Args:
            snapshots: Chronological list of snapshot dicts.

        Returns:
            FrequencyMetrics with trap breakdowns.
        """
        if not snapshots:
            return FrequencyMetrics()

        total_traps = 0
        per_quarter: Dict[int, int] = {}
        trap_types: Counter[str] = Counter()
        prev_trap_active = False

        for snap in snapshots:
            quarter = self._get_quarter(snap)
            trap_meter = self._get_trap_meter(snap)
            is_trap = trap_meter > 0.5 if trap_meter is not None else False

            if is_trap:
                if not prev_trap_active:  # Count distinct trap events
                    total_traps += 1
                    per_quarter[quarter] = per_quarter.get(quarter, 0) + 1
                prev_trap_active = True

                # Determine trap type(s) from sub-signals.
                types = self._get_active_trap_types(snap)
                for t in types:
                    trap_types[t] += 1
            else:
                prev_trap_active = False

        trap_rate = total_traps / len(snapshots) if snapshots else 0.0

        return FrequencyMetrics(
            traps_per_quarter=per_quarter,
            total_traps=total_traps,
            trap_rate=round(trap_rate, 4),
            trap_type_distribution=dict(trap_types),
            sample_count=len(snapshots),
        )

    def trap_types_breakdown(
        self,
        snapshots: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Return the distribution of trap types across a game.

        Args:
            snapshots: Chronological list of snapshot dicts.

        Returns:
            Dict mapping trap type label to occurrence count.
        """
        trap_types: Counter[str] = Counter()
        for snap in snapshots:
            types = self._get_active_trap_types(snap)
            for t in types:
                trap_types[t] += 1
        return dict(trap_types)

    def momentum_swing_frequency(
        self,
        snapshots: List[Dict[str, Any]],
    ) -> FrequencyMetrics:
        """Analyse how often momentum changes direction.

        Args:
            snapshots: Chronological list of snapshot dicts.

        Returns:
            FrequencyMetrics with swing counts.
        """
        if not snapshots:
            return FrequencyMetrics()

        directions = self._extract_momentum_directions(snapshots)
        swings = 0
        for i in range(1, len(directions)):
            if directions[i] and directions[i - 1] and directions[i] != directions[i - 1]:
                swings += 1

        swing_rate = swings / len(snapshots) if snapshots else 0.0

        return FrequencyMetrics(
            momentum_swings=swings,
            momentum_swing_rate=round(swing_rate, 4),
            sample_count=len(snapshots),
        )

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _get_quarter(snap: Dict[str, Any]) -> int:
        meta = snap.get("metadata", {})
        if isinstance(meta, dict):
            return meta.get("quarter", 1)
        return snap.get("quarter", 1)

    @staticmethod
    def _get_trap_meter(snap: Dict[str, Any]) -> Optional[float]:
        trap = snap.get("trap_detection", {})
        if isinstance(trap, dict):
            val = trap.get("trap_meter")
            if val is not None:
                return float(val)
        # Top-level key (from enriched fields)
        val = snap.get("trap_meter")
        return float(val) if val is not None else None

    @staticmethod
    def _get_active_trap_types(snap: Dict[str, Any]) -> List[str]:
        """Return trap type labels that are currently active in this snapshot."""
        trap = snap.get("trap_detection", {})
        if not isinstance(trap, dict):
            # Check top-level enriched fields.
            active = []
            for key in [
                "bull_trap_detected", "bear_trap_detected",
                "reverse_bull_trap_detected", "dead_market_detected",
                "false_momentum_detected", "late_trap_detected",
                "sharp_trap_detected",
            ]:
                if snap.get(key):
                    active.append(key.replace("_detected", ""))
            return active

        active = []
        type_keys = {
            "bull_trap": "bull_trap",
            "bear_trap": "bear_trap",
            "reverse_bull_trap": "reverse_bull_trap",
            "dead_market": "dead_market",
            "false_momentum": "false_momentum",
            "late_trap": "late_trap",
            "sharp_trap": "sharp_trap",
        }
        for label, key in type_keys.items():
            signal = trap.get(key, {})
            if isinstance(signal, dict) and signal.get("detected"):
                active.append(label)
        return active

    @staticmethod
    def _extract_momentum_directions(snapshots: List[Dict[str, Any]]) -> List[Optional[str]]:
        directions = []
        for snap in snapshots:
            mom = snap.get("momentum", {})
            if isinstance(mom, dict):
                d = mom.get("momentum_direction")
            else:
                d = snap.get("momentum_direction")
            directions.append(str(d) if d else None)
        return directions
