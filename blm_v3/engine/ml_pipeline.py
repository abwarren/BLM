"""
BLM V3 — ML Dataset Pipeline.

Converts accumulated historical snapshots into labelled ML training rows.
Each snapshot becomes one training observation with configurable features
and multiple label types.

Label types:
  - ``final_result``: 1 if total > total_line at game end, 0 if < line
  - ``over_under``: 'over' if final score exceeded line, 'under' otherwise
  - ``clv``: Closing Line Value — final line minus opening line (profit potential)
  - ``trap_success``: 1 if trap conditions formed AND final result was a loss for bettors
  - ``model_accuracy``: (reserved for future use)

Usage::

    pipeline = MlPipeline(db)
    rows = await pipeline.build_dataset(
        game_ids=['game-001', 'game-002'],
        features=['total_line', 'trap_meter', 'inflation_index', 'confidence'],
        label='final_result',
        max_rows=100000,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from pathlib import Path

from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.config import (
    ML_DEFAULT_FEATURES,
    ML_DEFAULT_LABEL,
)

logger = logging.getLogger(__name__)


# ── Label computation functions ──────────────────────────────────────


def compute_final_result_label(
    snapshots: list[dict[str, Any]],
) -> Optional[int]:
    """Compute the final result label (1 = over, 0 = under).

    Uses the last snapshot's total_score and the first snapshot's total_line.

    Returns:
        1 if final total > opening line, 0 if <, None if data insufficient.
    """
    if not snapshots:
        return None
    opening_line = _get_opening_line(snapshots)
    final_total = _get_final_total(snapshots)
    if opening_line is None or final_total is None:
        return None
    if final_total > opening_line:
        return 1
    if final_total < opening_line:
        return 0
    return None  # push


def compute_over_under_label(
    snapshots: list[dict[str, Any]],
) -> Optional[str]:
    """Compute the over/under label as a string.

    Returns 'over', 'under', or None if data insufficient.
    """
    result = compute_final_result_label(snapshots)
    if result is None:
        return None
    return 'over' if result == 1 else 'under'


def compute_clv_label(
    snapshots: list[dict[str, Any]],
) -> Optional[float]:
    """Compute Closing Line Value (CLV).

    CLV measures how much the market line moved from open to close::
        CLV = opening_line - closing_line

    Positive CLV = line moved in your favour if you bet over at open.
    Negative CLV = line moved against you.

    Returns:
        CLV as float, or None if data insufficient.
    """
    if not snapshots:
        return None
    opening_line = _get_opening_line(snapshots)
    closing_line = _get_closing_line(snapshots)
    if opening_line is None or closing_line is None:
        return None
    return round(opening_line - closing_line, 2)


def compute_trap_success_label(
    snapshots: list[dict[str, Any]],
) -> Optional[int]:
    """Compute trap success label.

    A trap is 'successful' if:
      - Trap meter exceeded 80 at any point during the game
      - The final result favoured the 'trap' direction

    For a bull trap (inflation positive, line moving up):
      - Trap success = final result was UNDER (bettors who took OVER lost)

    For a bear trap (inflation negative, line moving down):
      - Trap success = final result was OVER (bettors who took UNDER lost)

    Returns:
        1 if trap was successful, 0 if not, None if no trap detected.
    """
    if not snapshots:
        return None

    # Check if trap conditions existed
    max_trap = max((s.get('trap_meter', 0) or 0) for s in snapshots)
    if max_trap < 80:
        return None  # No trap conditions — label is undefined

    # Determine trap direction from early-game inflation
    early_snaps = [s for s in snapshots[:max(len(snapshots)//4, 5)]
                   if s.get('inflation_index') is not None]
    if not early_snaps:
        return None

    avg_early_inflation = sum(s['inflation_index'] for s in early_snaps) / len(early_snaps)
    final_total = _get_final_total(snapshots)
    opening_line = _get_opening_line(snapshots)
    if final_total is None or opening_line is None:
        return None

    if avg_early_inflation > 2.0:
        # Bull trap — line inflated => OVER was attractive but UNDER wins
        return 1 if final_total < opening_line else 0
    elif avg_early_inflation < -2.0:
        # Bear trap — line deflated => UNDER was attractive but OVER wins
        return 1 if final_total > opening_line else 0
    else:
        return 0  # Weak/no trap direction


# ── Internal helpers ─────────────────────────────────────────────────


def _get_opening_line(snapshots: list[dict[str, Any]]) -> Optional[float]:
    """Get the opening total line (first snapshot with a value)."""
    for s in snapshots:
        line = s.get('total_line') or s.get('total_line_raw')
        if line is not None:
            return float(line)
    return None


def _get_closing_line(snapshots: list[dict[str, Any]]) -> Optional[float]:
    """Get the closing total line (last snapshot with a value)."""
    for s in reversed(snapshots):
        line = s.get('total_line') or s.get('total_line_raw')
        if line is not None:
            return float(line)
    return None


def _get_final_total(snapshots: list[dict[str, Any]]) -> Optional[float]:
    """Get the final total score (last snapshot's total_score)."""
    for s in reversed(snapshots):
        ts = s.get('total_score') or (s.get('home_score', 0) + s.get('away_score', 0))
        if ts:
            return float(ts)
    return None


# ── Pipeline ─────────────────────────────────────────────────────────


class MlPipeline:
    """ML dataset pipeline that converts historical snapshots to training rows.

    Usage::

        db = HistoricalDatabase()
        pipeline = MlPipeline(db)
        labels = await pipeline.compute_labels('game-001')

        rows = await pipeline.build_dataset(
            game_ids=['game-001', 'game-002'],
            features=ML_DEFAULT_FEATURES,
            label='final_result',
        )
        # rows is list of dicts: [{feature1: v1, feature2: v2, label: l}, ...]
    """

    def __init__(self, db: HistoricalDatabase):
        self._db = db

    async def compute_labels(
        self,
        game_id: str,
    ) -> dict[str, Any]:
        """Compute all label types for a single game.

        Args:
            game_id: Game to compute labels for.

        Returns:
            Dict with keys: final_result, over_under, clv, trap_success,
            opening_line, closing_line, final_total, max_trap_meter.
        """
        snapshots = await self._db.query_snapshots(
            game_id=game_id, limit=100000,
        )
        if not snapshots:
            return {}

        return {
            'game_id': game_id,
            'final_result': compute_final_result_label(snapshots),
            'over_under': compute_over_under_label(snapshots),
            'clv': compute_clv_label(snapshots),
            'trap_success': compute_trap_success_label(snapshots),
            'opening_line': _get_opening_line(snapshots),
            'closing_line': _get_closing_line(snapshots),
            'final_total': _get_final_total(snapshots),
            'max_trap_meter': max(
                (s.get('trap_meter', 0) or 0) for s in snapshots
            ),
            'snapshot_count': len(snapshots),
        }

    async def build_dataset(
        self,
        game_ids: list[str],
        features: Optional[list[str]] = None,
        label: str = ML_DEFAULT_LABEL,
        max_rows: int = 100000,
    ) -> list[dict[str, Any]]:
        """Build a flat ML dataset from historical snapshots.

        Each historical snapshot becomes one training row.

        Args:
            game_ids: List of game IDs to include.
            features: Feature column names (default: ML_DEFAULT_FEATURES).
            label: Label column name (default: 'final_total').
            max_rows: Maximum total rows across all games.

        Returns:
            List of dicts, each with the requested features + label.
        """
        if features is None:
            features = list(ML_DEFAULT_FEATURES)

        # Filter to only features that exist in snapshots
        rows: list[dict[str, Any]] = []
        rows_per_game = max(1, max_rows // max(len(game_ids), 1))

        for gid in game_ids:
            if len(rows) >= max_rows:
                break

            snapshots = await self._db.query_snapshots(
                game_id=gid, limit=rows_per_game,
            )
            if not snapshots:
                continue

            for snap in snapshots[:rows_per_game]:
                row = {}
                for f in features:
                    v = snap.get(f)
                    if v is not None:
                        row[f] = v

                # Label: use the requested label field
                label_value = snap.get(label)
                if label_value is not None:
                    row[label] = label_value
                elif label == 'final_result':
                    # Compute from the game's full snapshot sequence
                    row[label] = compute_final_result_label(snapshots)
                elif label == 'over_under':
                    row[label] = compute_over_under_label(snapshots)
                elif label == 'clv':
                    row[label] = compute_clv_label(snapshots)
                elif label == 'trap_success':
                    row[label] = compute_trap_success_label(snapshots)

                row['game_id'] = gid
                row['timestamp'] = snap.get('timestamp', '')
                rows.append(row)

        return rows
