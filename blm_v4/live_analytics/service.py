"""Pace-Z service — the wiring layer for the PACE Z-SCORE measurement.

Reads the game's own live-state row from the clean metrics DB
(``clean_projections``), resolves its authoritative (provider,
competition) identity from the competition ledger in a writable
SIDECAR database (the production clean DB is opened read-only and
hosts no mutable state), registers the game once, and computes the
T-state pace Z against the strictly-prior historical population.

z = (actual_pace − μ) / σ over (provider, competition, period,
progress bucket) prior observations only.  The game's own row is
excluded by observation id; nothing is ever written back into the
population.  Descriptive statistics only — no probability, edge or
betting semantics.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from blm_v4.live_analytics.benchmark import MIN_BENCHMARK_N, pace_z
from blm_v4.live_analytics.league import (UNKNOWN, canonical_competition,
                                          ensure_league_schema,
                                          register_game_competition)

_PACE_ROW_SQL = """
SELECT id, source_game_id, classification, captured_at, period_label,
       progress_pct, actual_pts_per_min
  FROM clean_projections
 WHERE source_game_id = ? AND status = 'VALID'
   AND actual_pts_per_min IS NOT NULL
 ORDER BY captured_at DESC, id DESC
 LIMIT 1
"""


def _sidecar_connect(clean_path: Path) -> sqlite3.Connection:
    """Sidecar DB next to the clean metrics DB (ledger + benchmark cache
    live here; the clean DB itself is never written)."""
    side = Path(str(clean_path) + ".live_analytics.db")
    conn = sqlite3.connect(str(side), timeout=30)
    ensure_league_schema(conn)
    from blm_v4.live_analytics.benchmark import ensure_schema
    ensure_schema(conn)
    return conn


def ensure_ledger(main_conn: sqlite3.Connection,
                  side: sqlite3.Connection) -> None:
    """Top up the sidecar competition ledger from the authoritative
    ``games`` table so EVERY population member is ledger-classified (the
    population join excludes unregistered games by construction).
    INSERT OR IGNORE: first assignment wins — never a re-classification."""
    n_games = main_conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    n_ledger = side.execute(
        "SELECT COUNT(*) FROM competition_ledger").fetchone()[0]
    if n_ledger >= n_games:
        return
    rows = []
    for gid, cls, slug, cid in main_conn.execute(
            "SELECT source_game_id, classification, competition_slug, "
            "competition_id FROM games").fetchall():
        res = canonical_competition(cls, slug, cid)
        rows.append((str(gid), res.provider or UNKNOWN,
                     res.competition or UNKNOWN, res.competition_id,
                     res.source, res.status, res.reason, ""))
    side.executemany(
        "INSERT OR IGNORE INTO competition_ledger "
        "(source_game_id, provider, competition, competition_id, "
        " source_classification, status, reason, resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    side.commit()


def attach_clean_ro(conn: sqlite3.Connection,
                    clean_path: Path) -> str:
    """ATTACH the production clean DB read-only as ``clean`` (idempotent)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM pragma_database_list WHERE name='clean'"
    ).fetchone()
    if not row or not row[0]:
        conn.execute(
            "ATTACH DATABASE 'file:%s?mode=ro' AS clean" % clean_path)
    return "clean"


def pace_z_payload(main_conn: sqlite3.Connection, clean_path: Path,
                   source_game_id: str,
                   history: int = 0) -> dict:
    """Descriptive PACE Z payload for one game.

    ``main_conn`` is the production main DB (games table = authoritative
    competition metadata).  Returns keys: game_id, z, benchmark_key, n,
    mean_pace, std_pace, actual_pace, period, progress_pct, provider,
    competition, benchmark_status, measured_at.  ``z`` is None (honest
    gap) whenever the population is ineligible, degenerate (σ=0) or
    below MIN_BENCHMARK_N — never fabricated.
    """
    side = _sidecar_connect(clean_path)
    try:
        ensure_ledger(main_conn, side)
        ro = sqlite3.connect("file:%s?mode=ro" % clean_path, uri=True,
                             timeout=30)
        ro.row_factory = sqlite3.Row
        try:
            row = ro.execute(_PACE_ROW_SQL, (source_game_id,)).fetchone()
        finally:
            ro.close()
        g = main_conn.execute(
            "SELECT competition_slug, competition_id FROM games "
            "WHERE source_game_id=?", (str(source_game_id),)).fetchone()
        slug, comp_id = (g[0], g[1]) if g else (None, None)
        if row is None:
            return {"game_id": source_game_id, "z": None,
                    "benchmark_status": "no_observations",
                    "benchmark_key": None, "n": 0, "mean_pace": None,
                    "std_pace": None, "actual_pace": None, "period": None,
                    "progress_pct": None, "provider": None,
                    "competition": None, "series": []}
        reg = register_game_competition(
            side, str(row["source_game_id"]), row["classification"],
            slug, comp_id, str(row["captured_at"]))
        attach_clean_ro(side, clean_path)
        res = pace_z(
            side, row["actual_pts_per_min"], reg.provider, reg.competition,
            row["progress_pct"], row["captured_at"],
            cutoff_observation_id=int(row["id"]),
            exclude_game_id=str(row["source_game_id"]),
            period_label=row["period_label"], use_cache=True,
            _qualifier="clean")
        status = ("ok" if res.z is not None
                  else ("insufficient_n" if (res.mean is not None
                                             and res.n < MIN_BENCHMARK_N)
                        else ("no_population" if res.mean is None
                              else "degenerate_std")))
        return {
            "game_id": source_game_id,
            "z": res.z,
            "benchmark_key": res.benchmark_key,
            "n": res.n,
            "mean_pace": res.mean,
            "std_pace": res.std,
            "actual_pace": res.actual_pace,
            "period": row["period_label"],
            "progress_pct": row["progress_pct"],
            "provider": reg.provider,
            "competition": reg.competition,
            "benchmark_status": status,
            "measured_at": row["captured_at"],
            "series": _history(main_conn, side, clean_path, source_game_id,
                               history),
        }
    finally:
        side.close()


def _history(main_conn: sqlite3.Connection, side: sqlite3.Connection,
             clean_path: Path, source_game_id: str,
             limit: int) -> list:
    """T-state pace-Z timeline for the game's last ``limit`` observations.

    Each point is the FULL authoritative computation at its own T:
    strictly-prior population (captured_at < T, own row excluded by id,
    same-game rows excluded), recomputed read-time from immutable rows
    (cache bypassed — no writes, no reuse across points).  The current
    observation can never enter its own benchmark; future observations
    are structurally unreachable (cutoff = the point's own captured_at)."""
    if limit <= 0:
        return []
    g = main_conn.execute(
        "SELECT competition_slug, competition_id FROM games "
        "WHERE source_game_id=?", (str(source_game_id),)).fetchone()
    slug, comp_id = (g[0], g[1]) if g else (None, None)
    ro = sqlite3.connect("file:%s?mode=ro" % clean_path, uri=True,
                         timeout=30)
    ro.row_factory = sqlite3.Row
    try:
        rows = ro.execute(
            """SELECT id, classification, captured_at, period_label,
                      progress_pct, actual_pts_per_min
               FROM clean_projections
               WHERE source_game_id=? AND status='VALID'
                 AND actual_pts_per_min IS NOT NULL
               ORDER BY captured_at DESC, id DESC LIMIT ?""",
            (str(source_game_id), int(limit))).fetchall()
    finally:
        ro.close()
    out = []
    for r in rows:
        reg = register_game_competition(
            side, str(source_game_id), r["classification"],
            slug, comp_id, str(r["captured_at"]))
        res = pace_z(side, r["actual_pts_per_min"], reg.provider,
                     reg.competition, r["progress_pct"], r["captured_at"],
                     cutoff_observation_id=int(r["id"]),
                     exclude_game_id=str(source_game_id),
                     period_label=r["period_label"], use_cache=False,
                     _qualifier="clean")
        out.append({"captured_at": r["captured_at"],
                    "z": res.z, "actual_pace": r["actual_pts_per_min"],
                    "n": res.n, "mean_pace": res.mean, "std_pace": res.std,
                    "benchmark_key": res.benchmark_key})
    out.reverse()  # chronological for charting
    return out
