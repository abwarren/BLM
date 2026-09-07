"""BLM V4 — Clean-Data Boundary (frontend isolation).

The clean statistical dataset starts at CLEAN_DATA_EPOCH (the value
recorded in blm_metrics_clean.db `clean_meta.clean_metrics_start_at`,
verified live as 2026-09-05T05:40:41.782315Z).

Frontend rule: current/live analytical views (live analysis, pace
projector, market/fair analysis, trajectory, statistical/metrics views,
current game scorecards) may contain ONLY observations captured at-or-
after the epoch.  Pre-epoch (legacy) data remains in the operational DB
for audit/reference and is reachable ONLY through explicitly labeled
legacy paths — never silently mixed with clean data.

Partition key: a game is CLEAN when its first_seen_at >= epoch (all of
its observations are post-epoch by construction) and LEGACY otherwise
(it contains pre-epoch observations).  This binary partition is exact
and consistent across every view.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# Value recorded in blm_metrics_clean.db `clean_meta.clean_metrics_start_at`.
# ISO-8601 UTC, same format as every stored captured_at — string compare is
# a correct chronological compare for this format.
CLEAN_DATA_EPOCH = "2026-09-05T05:40:41.782315Z"

CLEAN = "CLEAN"
LEGACY = "LEGACY"


def epoch() -> str:
    """The clean-data epoch (UTC ISO)."""
    return CLEAN_DATA_EPOCH


def is_clean_ts(iso: Optional[str]) -> bool:
    """True when an ISO timestamp is at-or-after the clean epoch.

    None/malformed values are NOT clean (never silently admitted).
    Boundary is exact: a timestamp equal to the epoch is clean.
    """
    return bool(iso) and iso >= CLEAN_DATA_EPOCH


def clean_rows(rows: Iterable[dict]) -> list[dict]:
    """Keep only rows captured at-or-after the clean epoch."""
    return [r for r in rows if is_clean_ts(r.get("captured_at"))]


def game_data_quality(first_seen_at: Optional[str]) -> str:
    """CLEAN when the game's first observation is at-or-after the epoch,
    else LEGACY (contains pre-epoch observations)."""
    return CLEAN if is_clean_ts(first_seen_at) else LEGACY


def split_games_by_quality(
    games: Iterable[dict],
) -> tuple[list[dict], list[dict]]:
    """(clean, legacy) partition of game records by first_seen_at."""
    clean, legacy = [], []
    for g in games:
        (clean if game_data_quality(g.get("first_seen_at")) == CLEAN
         else legacy).append(g)
    return clean, legacy


# SQL fragment re-used by scorecard/trends aggregation: the set of games
# whose first observation is at-or-after the clean epoch (the CLEAN
# population).  The epoch is a fixed trusted constant, never user input.
def clean_games_sql_alias(alias: str = "g") -> str:
    """``alias.first_seen_at >= '<epoch>'`` predicate for game-level joins."""
    return f"{alias}.first_seen_at >= '{CLEAN_DATA_EPOCH}'"


def clean_games_subq() -> str:
    """Subquery of clean source_game_ids (games started at/after epoch)."""
    return (f"(SELECT source_game_id FROM games "
            f"WHERE first_seen_at >= '{CLEAN_DATA_EPOCH}')")


def clean_games_where(alias: str) -> str:
    """``<alias>.source_game_id IN (clean-games subquery)`` — restrict an
    aggregation table (e.g. prediction_scores) to the CLEAN population.
    The epoch is a fixed trusted constant, never user input."""
    return f"{alias}.source_game_id IN {clean_games_subq()}"


def legacy_games_where(alias: str) -> str:
    """``<alias>.first_seen_at < '<epoch>'`` — the LEGACY (pre-clean)
    partition for game-level joins."""
    return f"{alias}.first_seen_at < '{CLEAN_DATA_EPOCH}'"


def annotate_quality(g: dict[str, Any]) -> dict[str, Any]:
    """Return the game record with its data_quality field set."""
    out = dict(g)
    out["data_quality"] = game_data_quality(g.get("first_seen_at"))
    return out