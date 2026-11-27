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

BOUNDED, NOT WHOLE-TABLE (2026-11-27).  Both numbers used to come from ONE
``ROW_NUMBER()`` window over every row of ``clean_observations`` — measured
on production at 1098–1469 s per scan against a 60 s worker interval, i.e. a
read snapshot that was held almost permanently, which pinned the WAL at 3–6 GB
and starved the collector's writes (the 2026-11-27 slowdown).  The population
is now two cheap bounds instead:

  * the GAME SET is the near-term ``games`` population
    (``DEFAULT_LOOKBACK_DAYS`` back from the newest ``last_seen_at``), and
  * each game's first line is ONE indexed seek on ``idx_clean_obs_game_ts``
    (``source_game_id, captured_at``), stopping at the first non-null row, and
    a first line is IMMUTABLE (observations are append-only), so the worker's
    cache means that seek is paid once per game, not once per scan.

Measured on production 2026-11-27: the 14-day population is 12 792 games and
the full seek pass costs 78 s against a 60 s worker interval — so the cache is
what keeps a read snapshot from being held continuously.  After the first pass
a scan seeks only games that newly entered the window (a few hundred a day)
plus the handful that still carry no line, which is sub-second.

Neither bound changes the arithmetic.  The per-competition average was ALWAYS
restricted to games carrying a ``competition_slug`` (a game absent from
``games`` never contributed to any league), so dropping games that are absent
from the recent ``games`` population removes only rows that could not
contribute a slug — the average is the same mean over a current, not an
archival, sample.  What ``open`` loses is first lines for games the pipeline
no longer tracks, none of which can be served or slug-keyed.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Optional

#: How far back the near-term population reaches, measured from the NEWEST
#: ``last_seen_at`` in ``games`` rather than from wall-clock time — so a stalled
#: collector degrades the reference's freshness instead of emptying it (an
#: empty reference fails the move leg CLOSED and would block every trade).
#: 14 days keeps each competition's sample in the hundreds of games while
#: bounding the scan to a per-game index seek instead of a whole-table window.
DEFAULT_LOOKBACK_DAYS = 14.0

#: ONE indexed seek per game: the (source_game_id, captured_at) index satisfies
#: the equality prefix AND the ordering, so SQLite walks forward from the
#: game's earliest capture and stops at the first row that carries a line.
_FIRST_LINE_SQL = (
    "SELECT live_total_line, captured_at FROM clean_observations "
    "WHERE source_game_id = ? AND live_total_line IS NOT NULL "
    "ORDER BY captured_at LIMIT 1")


def game_slug_map(games_conn: sqlite3.Connection,
                  lookback_days: Optional[float] = DEFAULT_LOOKBACK_DAYS
                  ) -> dict[str, str]:
    """``{source_game_id: competition_slug}`` from the RECENT pipeline
    ``games`` population (``lookback_days`` back from the newest
    ``last_seen_at``; ``None``/``0`` reads every game ever seen).  Empty when
    the table cannot be read.  Only rows with a non-empty slug are kept — the
    display name is never used as a key."""
    out: dict[str, str] = {}
    base = ("SELECT source_game_id, competition_slug FROM games "
            "WHERE competition_slug IS NOT NULL AND competition_slug <> ''")
    rows = None
    if lookback_days and lookback_days > 0:
        try:
            # strftime() emits the same 'YYYY-MM-DDTHH:MM:SS' shape the column
            # stores, so the comparison stays a plain string comparison.
            rows = games_conn.execute(
                base + " AND last_seen_at >= strftime('%Y-%m-%dT%H:%M:%S',"
                       " (SELECT MAX(last_seen_at) FROM games), ?)",
                (f"-{float(lookback_days):g} days",)).fetchall()
        except sqlite3.Error:
            # A games table with no ``last_seen_at`` (older schema, test
            # fixtures) is read unbounded rather than yielding nothing.
            rows = None
    if rows is None:
        try:
            rows = games_conn.execute(base).fetchall()
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


def _known_game_ids(metrics_conn: sqlite3.Connection,
                    lookback_days: Optional[float] = DEFAULT_LOOKBACK_DAYS
                    ) -> list[str]:
    """The game list for a caller that has no ``games`` table.

    ``clean_games`` is one row per game (~68k rows, 0.24 s measured), so the
    near-term population comes from there.  Never ``SELECT DISTINCT`` over
    ``clean_observations`` unless ``clean_games`` does not exist at all — that
    distinct is a whole-index scan of every observation (48.9 s on production).
    """
    try:
        if lookback_days and lookback_days > 0:
            rows = metrics_conn.execute(
                "SELECT source_game_id FROM clean_games "
                "WHERE last_seen_at >= strftime('%Y-%m-%dT%H:%M:%S',"
                " (SELECT MAX(last_seen_at) FROM clean_games), ?)",
                (f"-{float(lookback_days):g} days",)).fetchall()
        else:
            rows = metrics_conn.execute(
                "SELECT source_game_id FROM clean_games").fetchall()
        return [row[0] for row in rows if row and row[0]]
    except sqlite3.Error:
        pass
    try:
        rows = metrics_conn.execute(
            "SELECT source_game_id FROM clean_games").fetchall()
        return [row[0] for row in rows if row and row[0]]
    except sqlite3.Error:
        pass
    try:
        return [row[0] for row in metrics_conn.execute(
            "SELECT DISTINCT source_game_id FROM clean_observations")]
    except sqlite3.Error:
        return []


def first_lines(metrics_conn: sqlite3.Connection,
                game_ids: Optional[Iterable[str]] = None
                ) -> dict[str, tuple[float, Optional[str]]]:
    """``{source_game_id: (open_line, first_captured_at)}`` — the FIRST
    non-null ``live_total_line`` captured for each game, from
    ``clean_observations``.  Empty when the population cannot be read.

    ``game_ids`` is the bound: one indexed seek per listed game, never a scan
    of the whole table.  ``None`` (callers with no ``games`` table) uses the
    same near-term population from ``clean_games`` — the small one-row-per-game
    table — through ``DEFAULT_LOOKBACK_DAYS``.  It never falls back to a scan of
    ``clean_observations``: the old ``SELECT DISTINCT source_game_id`` there is
    a whole-index scan measured at 48.9 s on production (8.6M rows), and a
    reader that long is exactly the WAL pin this module exists to remove.  Only
    when ``clean_games`` itself is absent (legacy fixtures) does the id list
    come from the observation index.

    "First" is by CAPTURE TIME (the earliest ``captured_at`` carrying a
    line), so it is the first line the pipeline actually SAW — not the
    market's own open."""
    out: dict[str, tuple[float, Optional[str]]] = {}
    if game_ids is None:
        game_ids = _known_game_ids(metrics_conn)
    try:
        for gid in game_ids:
            if not gid:
                continue
            row = metrics_conn.execute(_FIRST_LINE_SQL, (gid,)).fetchone()
            if row is None:
                continue
            line, captured_at = row[0], row[1]
            if line is None:
                continue
            try:
                value = float(line)
            except (TypeError, ValueError):
                continue
            out[gid] = (value, captured_at)
    except (sqlite3.Error, TypeError):
        return {}
    return out


def build_reference(metrics_conn: sqlite3.Connection,
                    slug_by_game: dict[str, str],
                    first_line_cache: Optional[
                        dict[str, tuple[float, Optional[str]]]] = None
                    ) -> dict[str, Any]:
    """The payload the live layer serves and the trade leg reads.

    ``{"avg": {competition_slug: {"avg_line": float, "games": int}},
       "open": {source_game_id: {"line": float, "at": str|None}}}``

    ``avg`` is the mean of the per-game FIRST-captured lines across each
    ``competition_slug`` (the study's league average); ``open`` is the
    per-game first-captured line the served game's ``entry_line`` is
    compared against.  Both empty when the population cannot be read.

    ``slug_by_game`` is both the league authority AND the bound: the loop
    issues one indexed seek per key, so the caller decides the population
    (``game_slug_map`` bounds it to recent games) and no whole-table scan is
    ever issued.

    ``first_line_cache`` is the worker's durable-by-process cache.  A game's
    first line never changes (append-only observations), so only keys absent
    from the cache are sought; the cache is then pruned to the current
    population.  The average is therefore computed over EXACTLY the same game
    set as an uncached call — caching changes WHEN the seek happens, never
    WHICH games contribute.  ``None`` (the request-path fallback) seeks the
    whole population on every call, which is why the live path must read the
    worker's published payload instead."""
    if first_line_cache is None:
        firsts = first_lines(metrics_conn, slug_by_game.keys())
    else:
        wanted = [gid for gid in slug_by_game if gid]
        missing = [gid for gid in wanted if gid not in first_line_cache]
        if missing:
            first_line_cache.update(first_lines(metrics_conn, missing))
        # Only games the caller can serve stay in memory.
        for gid in [g for g in first_line_cache if g not in slug_by_game]:
            del first_line_cache[gid]
        firsts = {gid: first_line_cache[gid]
                  for gid in wanted if gid in first_line_cache}
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
