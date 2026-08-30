"""Dataset builder — flattens BLM snapshots into ML-ready CSV/Parquet rows.

Each snapshot becomes one sample (features only).  Per-game outcome
targets (winner, margin, final scoreline) are derived from the game's
final snapshot and attached to every sample of that game.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── Feature / target schema (disjoint by design) ──────────────────

FEATURES: List[str] = [
    "quarter",
    "clock",
    "home_score",
    "away_score",
    "total_line",
    "spread",
    "pace",
    "possessions",
    "home_projection",
    "away_projection",
    "win_probability",
    "composite_confidence",
    "momentum_score",
    "momentum_velocity",
    "momentum_acceleration",
    "trap_meter",
    "steam_movement",
    "expected_total",
    "expected_margin",
    "score_total",
]

TARGETS: List[str] = [
    "winner",
    "margin",
    "final_home_score",
    "final_away_score",
    "final_total",
    "final_quarter",
]

_OUTCOME_FROM_LAST = {
    "final_home_score": "home_score",
    "final_away_score": "away_score",
    "final_quarter": "quarter",
}


def _get(snap: Dict[str, Any], *paths: List[str]) -> Any:
    """Read a value from a snapshot, trying flat key then nested paths.

    ``_get(snap, ["pace", "real_pace"])`` reads ``snap["pace"]["real_pace"]``
    (or ``snap["pace"]`` if it is scalar).
    """
    for path in paths:
        # flat
        if path[0] in snap and not isinstance(snap.get(path[0]), dict):
            return snap[path[0]]
        # nested
        node: Any = snap
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok:
            return node
    return None


def _to_scalar(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return None
    return value


class DatasetBuilder:
    """Builds flat CSV/Parquet datasets from the time-series database."""

    def __init__(self, output_dir: str = "datasets"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ───────────────────────────────────────────────

    async def build(
        self,
        game_id: str,
        ts_interface: Any,
        output_format: str = "csv",
    ) -> str:
        """Build one game's dataset; returns the output file path."""
        if output_format not in ("csv", "parquet"):
            raise ValueError(f"Unsupported format: {output_format}")
        snapshots = await ts_interface.query_snapshots(game_id, limit=100000)
        if not snapshots:
            raise ValueError(f"No snapshots found for game {game_id}")
        df = self._build_df(game_id, snapshots)
        return self._write(df, game_id, output_format)

    async def build_all(
        self,
        storage_interface: Any,
        ts_interface: Any,
        output_format: str = "parquet",
    ) -> str:
        """Build one combined dataset from all games."""
        if output_format not in ("csv", "parquet"):
            raise ValueError(f"Unsupported format: {output_format}")
        games = await storage_interface.list_games()
        frames: List[pd.DataFrame] = []
        for game in games:
            gid = game.get("game_id") if isinstance(game, dict) else game
            if not gid:
                continue
            snapshots = await ts_interface.query_snapshots(gid, limit=100000)
            if snapshots:
                frames.append(self._build_df(gid, snapshots))
        if not frames:
            raise ValueError("No snapshots found for any game")
        df = pd.concat(frames, ignore_index=True)
        return self._write(df, "all_games", output_format)

    # ── Internals ────────────────────────────────────────────────

    def _build_df(self, game_id: str, snapshots: List[Dict[str, Any]]) -> pd.DataFrame:
        # The TS interface returns snapshots in chronological order (its
        # contract).  Do NOT re-sort by timestamp string — test data may
        # carry non-ISO timestamps ("t1", "final") that would sort wrong.
        ordered = list(snapshots)
        final = ordered[-1]
        final_margin = int(_get(final, ["game_state", "margin"]) or 0)
        winner = "home" if final_margin > 0 else "away"

        outcome: Dict[str, Any] = {"winner": winner, "margin": final_margin}
        for target, src in _OUTCOME_FROM_LAST.items():
            outcome[target] = _to_scalar(_get(final, [src]))
        if "final_total" in TARGETS:
            fh = outcome.get("final_home_score")
            fa = outcome.get("final_away_score")
            outcome["final_total"] = (
                int(fh) + int(fa) if fh is not None and fa is not None else None
            )

        rows: List[Dict[str, Any]] = []
        for snap in ordered:
            row: Dict[str, Any] = {}
            for feat in FEATURES:
                row[feat] = _to_scalar(_feature_value(snap, feat))
            row.update(outcome)
            rows.append(row)
        return pd.DataFrame(rows)

    def _write(self, df: pd.DataFrame, name: str, output_format: str) -> str:
        path = self.output_dir / f"{name}.{output_format}"
        if output_format == "csv":
            df.to_csv(path, index=False)
        else:
            df.to_parquet(path, index=False)
        logger.info("wrote %s (%d rows)", path, len(df))
        return str(path)


def _feature_value(snap: Dict[str, Any], feat: str) -> Any:
    """Map a feature name to its snapshot value (flat or nested)."""
    nested: Dict[str, List[str]] = {
        "pace": ["pace", "real_pace"],
        "home_projection": ["team_totals", "home_projection"],
        "away_projection": ["team_totals", "away_projection"],
        "win_probability": ["blm", "win_probability"],
        "composite_confidence": ["confidence_inputs", "composite_confidence"],
        "momentum_score": ["momentum", "score"],
        "momentum_velocity": ["momentum", "velocity"],
        "momentum_acceleration": ["momentum", "acceleration"],
        "trap_meter": ["trap_detection", "trap_meter"],
        "steam_movement": ["betting_market", "steam_movement"],
        "expected_total": ["blm", "expected_total"],
        "expected_margin": ["blm", "expected_margin"],
        "spread": ["betting_market", "spread"],
        "total_line": ["betting_market", "total"],
        "score_total": ["game_state", "total"],
    }
    if feat in nested:
        return _get(snap, nested[feat])
    return _get(snap, [feat])
