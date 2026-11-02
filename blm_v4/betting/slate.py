"""THE LIVE SLATE — every live game, its distance to the auto-bet gate.

One deep call — ``build_slate()`` — returns, for every game captured in the
last ``lookback_s``, the numbers the fast lane's own gate reads and the
verdict each leg gives. Nothing here decides or places anything: it is the
engine's evaluation, exposed.

Every leg is computed by ``blm_v4.betting.fast_lane.legs`` — the SAME
function the served ``under_alert_exec_armed`` flag composes — so the table
can never drift from what auto-bet actually enforces.

READ-ONLY: both connections are opened ``mode=ro``.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from blm_v4 import trade_window as TW
from blm_v4.betting.fast_lane import legs, first_refusal

#: rows older than this are not "the slate" any more
DEFAULT_LOOKBACK_S = 120.0
#: how long one built slate is reused (the page polls faster than this)
CACHE_S = 3.0

_cache: dict = {}
_cache_lock = threading.Lock()


def _ro(path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _pace_reference(prod_db: Path) -> dict:
    from blm_v4.live_analytics.competition_pace import competition_pace_reference
    con = _ro(prod_db)
    try:
        return competition_pace_reference(con) or {}
    finally:
        con.close()


def _slugs(prod_db: Path, game_ids: list[str]) -> dict:
    """source_game_id -> competition_slug (the traded/universe key)."""
    if not game_ids:
        return {}
    out: dict = {}
    con = _ro(prod_db)
    try:
        marks = ",".join("?" * len(game_ids))
        for r in con.execute(
                f"""SELECT source_game_id, competition_slug FROM games
                     WHERE source_game_id IN ({marks})""", game_ids):
            out[str(r["source_game_id"])] = r["competition_slug"]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _live_rows(clean_db: Path, lookback_s: float) -> list:
    """The newest row per game captured within the lookback.

    Bounded by id (append-only) like the fast lane's own query, so this stays
    cheap on a 6 GB table while the collector writes into it.
    """
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() - lookback_s))
    con = _ro(clean_db)
    try:
        max_id = con.execute("SELECT MAX(id) FROM clean_observations").fetchone()[0] or 0
        window = 20000          # ~10 min of capture; the lookback filters below
        rows = list(con.execute(
            """SELECT o.source_game_id, o.captured_at, o.total_points,
                      o.progress_pct, o.required_pts_per_min,
                      o.actual_pts_per_min, o.live_total_line, o.under_price,
                      o.remaining_game_minutes, o.classification, o.status,
                      o.terminal, g.home_team, g.away_team, g.status AS game_status
                 FROM clean_observations o
                 LEFT JOIN clean_games g ON g.source_game_id = o.source_game_id
                WHERE o.id >= ?
                  AND o.captured_at >= ?
                  AND o.status = 'VALID'
                  AND (o.terminal IS NULL OR o.terminal = 0)
                ORDER BY o.captured_at DESC
                LIMIT 3000""", (max_id - window, since)))
    finally:
        con.close()
    newest: dict = {}
    for r in rows:                       # newest first -> first wins
        newest.setdefault(r["source_game_id"], r)
    return list(newest.values())


def _band(progress: Optional[float]) -> str:
    if progress is None:
        return "unknown"
    if progress < TW.TRADE_MIN_PROGRESS_PCT:
        return "below"
    if progress > TW.TRADE_MAX_PROGRESS_PCT:
        return "above"
    return "in"


def _eta_to_band_min(progress: Optional[float], left_min: Optional[float]) -> Optional[float]:
    """Minutes until the game REACHES the band, at its own observed rate.

    Progress is elapsed/total, so the remaining fraction of the game * the
    clock still to play is the time to 75 %; only meaningful below the band.
    """
    if progress is None or left_min is None or left_min <= 0:
        return None
    if progress >= TW.TRADE_MIN_PROGRESS_PCT:
        return 0.0
    total = left_min / (1.0 - progress / 100.0) if progress < 100 else left_min
    return round(max(0.0, total) * (TW.TRADE_MIN_PROGRESS_PCT - progress) / 100.0, 1)


def build_slate(prod_db, clean_db, lookback_s: float = DEFAULT_LOOKBACK_S) -> dict:
    prod_db, clean_db = Path(prod_db), Path(clean_db)
    rows = _live_rows(clean_db, lookback_s)
    ids = [r["source_game_id"] for r in rows]
    slugs = _slugs(prod_db, ids)
    pace = _pace_reference(prod_db)
    now = time.time()

    games = []
    for r in rows:
        gid = r["source_game_id"]
        slug = slugs.get(gid)
        avg = (pace.get(slug or "") or {}).get("avg_pace")
        req, act = r["required_pts_per_min"], r["actual_pts_per_min"]
        price = r["under_price"]
        ratio = (float(req) / float(avg)) if (req and avg) else None
        leg = legs(required_pace=req, avg_pace=avg, actual_pace=act,
                   progress_pct=r["progress_pct"], total_points=r["total_points"],
                   under_odds=price, competition_slug=slug,
                   entry_line=r["live_total_line"], open_line=None,
                   league_avg_line=None)
        refusal = first_refusal(leg)
        band = _band(r["progress_pct"])
        # "on track" = the pace/price legs it CAN be judged on already pass, so
        # entering the band is the only thing left.  This is the number the
        # operator watches: a below-band game at ratio 1.09 is a candidate in
        # waiting, not a refusal.
        # "on track" must mean WILL FIRE on band entry, so every leg that does
        # not depend on being in the band has to pass — including shortfall,
        # which a pace-only test let through (a 1.13/0.35 game read as on track
        # and would have been refused the moment it entered).
        on_track = bool(
            ratio is not None and ratio > TW.TRADE_MIN_PACE_RATIO
            and price is not None and float(price) >= TW.TRADE_EXEC_MIN_PRICE
            and not leg.get("competition") and not leg.get("shortfall")
            and not leg.get("price") and not leg.get("score"))
        games.append({
            "game_id": gid,
            "home": r["home_team"], "away": r["away_team"],
            "competition": slug, "classification": r["classification"],
            "captured_at": r["captured_at"],
            "age_s": round(now - time.mktime(time.strptime(
                r["captured_at"][:19], "%Y-%m-%dT%H:%M:%S")) + time.timezone, 1),
            "progress_pct": r["progress_pct"],
            "total_points": r["total_points"],
            "required_pace": req, "actual_pace": act,
            "avg_pace": avg,
            "ratio": round(ratio, 4) if ratio is not None else None,
            "need": round(TW.TRADE_MIN_PACE_RATIO - ratio, 4) if ratio is not None else None,
            "shortfall": (round(float(req) - float(act), 3)
                          if (req is not None and act is not None) else None),
            "price": price,
            "line": r["live_total_line"],
            "left_min": r["remaining_game_minutes"],
            "band": band,
            "eta_band_min": _eta_to_band_min(r["progress_pct"], r["remaining_game_minutes"]),
            "armed": refusal is None,
            "on_track": on_track,
            "refusal": refusal,
            "legs": leg,
        })

    # closest-to-arming first: in band (by pace ratio), then below band (the
    # games that can still enter it, nearest first), then everything else
    def _key(g):
        rank = 0 if g["band"] == "in" else (1 if g["band"] == "below" else 2)
        if rank == 0:
            return (rank, -(g["ratio"] if g["ratio"] is not None else -1))
        if rank == 1:
            return (rank, -(g["progress_pct"] or 0))
        return (rank, -(g["progress_pct"] or 0))

    games.sort(key=_key)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lookback_s": lookback_s,
        "gate": {
            "band": [TW.TRADE_MIN_PROGRESS_PCT, TW.TRADE_MAX_PROGRESS_PCT],
            "min_points": TW.TRADE_MIN_SCORED_POINTS,
            "pace_ratio": TW.TRADE_MIN_PACE_RATIO,
            "shortfall_margin": TW.SHORTFALL_MARGIN,
            "price_floor": TW.TRADE_EXEC_MIN_PRICE,
            "signal_price_floor": TW.TRADE_MIN_UNDER_PRICE,
            "excluded_competitions": list(TW.TRADE_EXCLUDED_COMPETITIONS or []),
        },
        "counts": {
            "games": len(games),
            "in_band": sum(1 for g in games if g["band"] == "in"),
            "below": sum(1 for g in games if g["band"] == "below"),
            "above": sum(1 for g in games if g["band"] == "above"),
            "armed": sum(1 for g in games if g["armed"]),
        },
        "games": games,
    }


def cached_slate(prod_db, clean_db, lookback_s: float = DEFAULT_LOOKBACK_S) -> dict:
    """``build_slate`` with a short TTL so a 2 s page poll cannot stack scans."""
    key = (str(prod_db), str(clean_db), lookback_s)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    data = build_slate(prod_db, clean_db, lookback_s)
    with _cache_lock:
        _cache[key] = (now, data)
    return data
