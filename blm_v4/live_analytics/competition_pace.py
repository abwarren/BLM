"""Competition pace reference — the league-specific scoring environment.

The UNDER condition compares a game's REQUIRED pace against its own
competition's normal scoring rate, so that reference has to be
league-specific: one global figure would blend an NBA game with a KBL
game and yield a comparison neither league actually satisfies.  No global
average is computed anywhere in this module.

Definition — the mean over SETTLED games of

    final_total / regulation_minutes

partitioned by the canonical competition slug (``games.competition_slug``,
the authoritative identifier — the display name is unreliable and is never
used as a key).  Regulation minutes come from ``projection.duration_for``,
the same authority the projector uses, so the reference is expressed in
the same units as ``actual_pts_per_min`` and ``required_pts_per_min``.

Population: ``game_results`` joined to ``games`` in the pipeline DB,
positive final totals only.

Read-only and failure-isolated: a missing table or column yields an empty
reference, and a competition absent from it produces NO alert rather than
falling back to a shared number.  The grouped scan is cached in-process so
a per-poll call costs nothing.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

# How long a computed reference is reused.  The underlying population only
# changes when a game settles, so a short TTL keeps the payload honest
# while reducing the cost to a single grouped scan every few minutes.
CACHE_TTL_SECONDS = 300.0

# Cached PER DATABASE: the reference is a property of a population, so a
# second connection to a different file must never read the first one's
# numbers.
_cache: dict[str, Any] = {}


def _db_identity(conn: sqlite3.Connection) -> str:
    """A stable key for the database behind this connection."""
    try:
        for _seq, name, path in conn.execute("PRAGMA database_list"):
            if name == "main":
                return path or ":memory:"
    except sqlite3.Error:
        pass
    return "unknown"


def _regulation_minutes(classification: Optional[str]) -> float:
    """Full-game regulation minutes for a classification — the projector's
    own authority, imported lazily to avoid an import cycle."""
    from blm_v4.projection import duration_for
    _quarter, full = duration_for(classification)
    return float(full or 0.0)


def competition_pace_reference(conn: sqlite3.Connection
                               ) -> dict[str, dict]:
    """{competition_slug: {"avg_pace": float, "games": int}}

    Empty when the population cannot be read.  Never returns a global
    figure and never merges two competitions into one entry."""
    now = time.monotonic()
    identity = _db_identity(conn)
    cached = _cache.get(identity)
    if cached and now - cached["computed_at"] < CACHE_TTL_SECONDS:
        return cached["reference"]

    try:
        rows = conn.execute(
            "SELECT g.competition_slug AS competition, "
            "       g.classification   AS classification, "
            "       gr.final_total     AS final_total "
            "FROM game_results gr "
            "JOIN games g ON g.source_game_id = gr.source_game_id "
            "WHERE gr.final_total IS NOT NULL AND gr.final_total > 0 "
            "  AND g.competition_slug IS NOT NULL "
            "  AND g.competition_slug <> ''"
        ).fetchall()
    except sqlite3.Error:
        return {}

    grouped: dict[str, list[float]] = {}
    for row in rows:
        # POSITIONAL access on purpose: this module must not depend on the
        # caller having set row_factory.  Reading by name against a plain
        # tuple raises, and a silently-empty reference would disable every
        # alert rather than fail loudly.
        try:
            slug = row[0]
            classification = row[1]
            final_total = row[2]
        except (IndexError, TypeError):
            continue
        minutes = _regulation_minutes(classification)
        if not slug or minutes <= 0 or final_total is None:
            continue
        grouped.setdefault(slug, []).append(float(final_total) / minutes)

    reference: dict[str, dict] = {}
    for slug, values in grouped.items():
        if not values:
            continue
        reference[slug] = {
            "avg_pace": round(sum(values) / len(values), 4),
            "games": len(values),
        }

    if reference:
        _cache[identity] = {"computed_at": now, "reference": reference}
    return reference
