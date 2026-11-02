"""Read-only fixture context for ledger rows: teams, tip-off, final score.

The DB EXPLORER renders up to 200 execution rows per pull.  Resolving the
fixture for each row individually would be 400 round trips across two
databases, so this module answers the whole page with one batched query
per database and hands back a plain ``{game_id: {...}}`` map.

Everything here is READ-ONLY and fail-soft: a missing file, a missing
table or a locked database yields ``{}`` — a context lookup must never
hide the ledger itself.  Fields that do not exist are ``None``, never a
fabricated value.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

#: The join is deliberately shallow: only what a ledger row needs to be
#: readable by a human (who played, when, and how it finished).
CONTEXT_FIELDS = (
    "home_team", "away_team", "competition",
    "fixture_start_utc", "fixture_start_source",
    "final_home", "final_away", "final_total", "final_result_status",
)

_CHUNK = 400          # stay well below SQLite's bound-parameter limit


def base_game_id(game_id) -> str:
    """``31412351#i2`` is the second instance of game 31412351 on the board;
    the fixture record is keyed by the base id."""
    return str(game_id or "").split("#", 1)[0]


def _iso_from_epoch(value) -> Optional[str]:
    try:
        return (datetime.fromtimestamp(float(value), tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"))
    except (TypeError, ValueError, OSError):
        return None


def _connect(path: Path) -> Optional[sqlite3.Connection]:
    if not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _chunks(items: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _load_games(clean_db: Path, ids: list[str]) -> dict[str, dict]:
    """Teams + recorded final, keyed by source_game_id (base ids only)."""
    conn = _connect(clean_db)
    if conn is None:
        return {}
    out: dict[str, dict] = {}
    try:
        for chunk in _chunks(ids):
            rows = conn.execute(
                "SELECT source_game_id, home_team, away_team, classification, "
                "       first_seen_at, final_home, final_away, final_total, "
                "       final_result_status "
                f"FROM clean_games WHERE source_game_id IN ({_placeholders(len(chunk))})",
                chunk).fetchall()
            for r in rows:
                out[r["source_game_id"]] = {
                    "home_team": r["home_team"],
                    "away_team": r["away_team"],
                    "competition": r["classification"],
                    "first_seen_at": r["first_seen_at"],
                    "final_home": r["final_home"],
                    "final_away": r["final_away"],
                    "final_total": r["final_total"],
                    "final_result_status": r["final_result_status"],
                }
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def _load_results(pokerbet_db: Path, ids: list[str]) -> dict[str, dict]:
    """Authoritative recorded final per game (``game_results``, latest row).

    This is the table the result reconciler maintains — 927 of the 935
    games in the ledger carry a result here, against 77 rows anywhere in
    ``clean_games``, so it is the primary stop for a final score.
    """
    conn = _connect(pokerbet_db)
    if conn is None:
        return {}
    out: dict[str, dict] = {}
    try:
        for chunk in _chunks(ids):
            rows = conn.execute(
                "SELECT source_game_id, final_home, final_away, final_total, "
                "       final_result_status, result_at, result_source "
                "FROM game_results "
                f"WHERE source_game_id IN ({_placeholders(len(chunk))}) "
                "ORDER BY id ASC",
                chunk).fetchall()
            for r in rows:                     # later rows win (latest result)
                out[r["source_game_id"]] = {
                    "final_home": r["final_home"],
                    "final_away": r["final_away"],
                    "final_total": r["final_total"],
                    "final_result_status": r["final_result_status"],
                    "result_at": r["result_at"],
                    "result_source": r["result_source"],
                }
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def _load_start_times(pokerbet_db: Path, ids: list[str]) -> dict[str, float]:
    """Board tip-off per game (BETUAL timer model, epoch seconds)."""
    conn = _connect(pokerbet_db)
    if conn is None:
        return {}
    out: dict[str, float] = {}
    try:
        for chunk in _chunks(ids):
            rows = conn.execute(
                "SELECT source_game_id, game_start_wall FROM betual_game_timers "
                f"WHERE source_game_id IN ({_placeholders(len(chunk))})",
                chunk).fetchall()
            for r in rows:
                if r["game_start_wall"] is not None:
                    out[r["source_game_id"]] = r["game_start_wall"]
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def fixture_context(root, game_ids: Iterable[Optional[str]]) -> dict[str, dict]:
    """Map every requested ``game_id`` to its fixture fields (see CONTEXT_FIELDS).

    ``root`` is the deployment directory holding ``blm_metrics_clean.db`` and
    ``blm_pokerbet.db`` (``BLM_POKERBET_DB`` still wins for the latter, as the
    rest of the API does).  Keys of the result are the ids as requested, so a
    caller can index with the raw ledger value.
    """
    requested = [str(g) for g in game_ids if g]
    if not requested:
        return {}
    root = Path(root)
    ids = sorted({base_game_id(g) for g in requested})
    clean_db = root / "blm_metrics_clean.db"
    pokerbet_db = Path(os.environ.get("BLM_POKERBET_DB") or (root / "blm_pokerbet.db"))

    games = _load_games(clean_db, ids)
    results = _load_results(pokerbet_db, ids)
    starts = _load_start_times(pokerbet_db, ids)

    base_ctx: dict[str, dict] = {}
    for gid in ids:
        g = games.get(gid) or {}
        res = results.get(gid) or {}
        start_epoch = starts.get(gid)
        start_iso, source = (None, None)
        if start_epoch is not None:
            start_iso, source = _iso_from_epoch(start_epoch), "betual_timer"
        if start_iso is None and g.get("first_seen_at"):
            # Fallback only: first observation, i.e. the collector's first
            # sight of the board — NOT a scheduled tip-off.
            start_iso, source = g["first_seen_at"], "first_seen"
        # game_results is authoritative; clean_games still holds a final for
        # the two games finalized before the reconciler owned that column.
        final_home = res.get("final_home", g.get("final_home"))
        final_away = res.get("final_away", g.get("final_away"))
        final_total = res.get("final_total", g.get("final_total"))
        if final_total is None and final_home is not None and final_away is not None:
            final_total = int(final_home) + int(final_away)
        base_ctx[gid] = {
            "home_team": g.get("home_team"),
            "away_team": g.get("away_team"),
            "competition": g.get("competition"),
            "fixture_start_utc": start_iso,
            "fixture_start_source": source,
            "final_home": final_home,
            "final_away": final_away,
            "final_total": final_total,
            "final_result_status": (res.get("final_result_status")
                                    or g.get("final_result_status")),
        }
    return {gid: base_ctx.get(base_game_id(gid), dict.fromkeys(CONTEXT_FIELDS))
            for gid in requested}
