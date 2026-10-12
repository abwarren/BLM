"""League AVERAGE LINE reference + the per-game FIRST-CAPTURED line.

The LINE-MOVEMENT VOLATILITY trade leg
(``docs/rag/15_EDGE_FINDINGS.md`` §15.8,
``blm_v4.trade_window.trade_move_reason``) needs two numbers per game:

  * ``open_line``        the FIRST non-null ``live_total_line`` CAPTURED for
                         the game — the first CAPTURED line, NOT the true
                         market open;
  * ``league_avg_line``  the mean of those per-game first-captured lines
                         across the game's ``competition_slug``.

Both come from ``clean_observations`` (the clean metrics DB), joined to the
pipeline ``games`` table for the authoritative ``competition_slug`` — the
clean projections/observations carry only the provider FAMILY
(``classification``), never the league, so the slug must be read from
``games`` (the same authority the rest of the live layer keys on).  This
module is the ONE definition of both; the LineReferenceWorker precomputes
them off the request path.

READ-ONLY and failure-isolated: a missing table or column yields an empty
reference, and a game absent from the ``games`` table contributes to NO
league's average (never borrowed across competitions).  Every read is
POSITIONAL on purpose — this module must not depend on the caller having set
``row_factory`` (reading by name against a plain tuple raises, and a
silently-empty reference would fail every move-leg trade closed).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional


def game_slug_map(games_conn: sqlite3.Connection) -> dict[str, str]:
    """``{source_game_id: competition_slug}`` from the pipeline ``games``
    table.  Empty when the table cannot be read.  Only rows with a
    non-empty slug are kept — the display name is never used as a key."""
    out: dict[str, str] = {}
    try:
        rows = games_conn.execute(
            "SELECT source_game_id, competition_slug FROM games").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        try:
            gid, slug = row[0], row[1]
        except (IndexError, TypeError):
            continue
        if gid and slug:
            out[gid] = slug
    return out


def first_lines(metrics_conn: sqlite3.Connection
                ) -> dict[str, tuple[float, Optional[str]]]:
    """``{source_game_id: (open_line, first_captured_at)}`` — the FIRST
    non-null ``live_total_line`` captured for each game, from
    ``clean_observations``.  Empty when the population cannot be read.

    "First" is by CAPTURE TIME (the earliest ``captured_at`` carrying a
    line), so it is the first line the pipeline actually SAW — not the
    market's own open."""
    out: dict[str, tuple[float, Optional[str]]] = {}
    try:
        rows = metrics_conn.execute(
            "SELECT source_game_id, live_total_line, captured_at FROM ("
            "  SELECT source_game_id, live_total_line, captured_at,"
            "         ROW_NUMBER() OVER (PARTITION BY source_game_id"
            "                            ORDER BY captured_at) AS rn"
            "  FROM clean_observations WHERE live_total_line IS NOT NULL"
            ") WHERE rn = 1").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        try:
            gid, line, captured_at = row[0], row[1], row[2]
        except (IndexError, TypeError):
            continue
        if not gid or line is None:
            continue
        try:
            value = float(line)
        except (TypeError, ValueError):
            continue
        out[gid] = (value, captured_at)
    return out


def build_reference(metrics_conn: sqlite3.Connection,
                    slug_by_game: dict[str, str]) -> dict[str, Any]:
    """The payload the live layer serves and the trade leg reads.

    ``{"avg": {competition_slug: {"avg_line": float, "games": int}},
       "open": {source_game_id: {"line": float, "at": str|None}}}``

    ``avg`` is the mean of the per-game FIRST-captured lines across each
    ``competition_slug`` (the study's league average); ``open`` is the
    per-game first-captured line the served game's ``entry_line`` is
    compared against.  Both empty when the population cannot be read."""
    firsts = first_lines(metrics_conn)
    grouped: dict[str, list[float]] = {}
    openmap: dict[str, dict[str, Any]] = {}
    for gid, (line, captured_at) in firsts.items():
        openmap[gid] = {"line": line, "at": captured_at}
        slug = slug_by_game.get(gid)
        if slug:
            grouped.setdefault(slug, []).append(line)
    avg: dict[str, dict[str, Any]] = {}
    for slug, values in grouped.items():
        if values:
            avg[slug] = {"avg_line": round(sum(values) / len(values), 4),
                         "games": len(values)}
    return {"avg": avg, "open": openmap}
