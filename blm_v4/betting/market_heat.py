"""MARKET-HEAT GUARDRAIL — pause auto-bet while unders are being beaten.

Investigation 2026-11-15 (tc-new 70-92 cohort): win rate fell 74.2 % → 59.6 %
over the last three days of the week.  The decay was not a selection shift —
price mix, competition mix, pace ratio, progress and line age were all flat —
it was that totals stopped landing under: the share of games finishing OVER
their last observed line went 26 % → 40 % (and 17 % → 47 % day by day), in
every league except the NBA.  When the whole market runs hot, every leg can
still pass on an individual game while the edge underneath it is gone.

So this is a GLOBAL leg, evaluated once per pass rather than per game: over the
recently SETTLED games, what fraction finished above the last line the feed
saw?  Above the threshold, the lane arms nothing and the dashboard says why.

It is deliberately based on settled finals, not live pace: during the same
days ``actual ÷ league-average pace`` among candidates was flat, so a live
pace proxy would not have seen this at all.

Env (deployment owns the values; code defaults are conservative):
    TRADE_MARKET_HEAT_ENABLED=1       0 turns the guardrail off entirely
    TRADE_MARKET_HEAT_WINDOW_S=21600  settled lookback window (6 h)
    TRADE_MARKET_HEAT_MIN_GAMES=20    below this many settled games, no verdict
    TRADE_MARKET_HEAT_MAX_OVER=0.45   trip at/above this share finishing OVER

The 0.45 default is the break-even of the cohort it protects: at the median
price ~1.80 an under needs ~55.6 % wins, i.e. an OVER share below ~44 %.  The
7-day baseline in this feed runs ~30-40 % OVER, and the two days that sank the
cohort (11-13, 11-14) ran 44 % and 47 %.

READ-ONLY, fail-open on IO errors: a guardrail that cannot read must not be
able to halt trading on a broken database — it reports ``error`` and passes.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

#: how many settled games a single check looks at (bounds the per-game lookup)
MAX_GAMES = 150
#: one check serves this long; the lane passes every 3 s, settlements are slow
CACHE_S = 120.0

_cache: dict = {}
_cache_lock = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return _env_bool("TRADE_MARKET_HEAT_ENABLED", True)


def window_s() -> float:
    return max(0.0, _env_float("TRADE_MARKET_HEAT_WINDOW_S", 21600.0))


def min_games() -> int:
    return max(1, _env_int("TRADE_MARKET_HEAT_MIN_GAMES", 20))


def max_over() -> float:
    return min(1.0, max(0.0, _env_float("TRADE_MARKET_HEAT_MAX_OVER", 0.45)))


def _data_root() -> Path:
    return Path(os.environ.get("BLM_DATA_ROOT", "/home/ubuntu/BLM"))


def overshare(pairs: Iterable[tuple[float, float]]) -> dict:
    """``[(final_total, last_line), ...]`` -> counts and the OVER share.

    A push (final == line) is neither over nor under; it stays in ``n`` because
    it is a settled game the check looked at, but it is not counted as OVER.
    """
    n = over = 0
    for final, line in pairs:
        if final is None or line is None:
            continue
        n += 1
        if float(final) > float(line):
            over += 1
    return {"n": n, "over": over,
            "share": (over / n) if n else None}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _settled(prod_db: Path, since: str) -> list:
    """Recently settled games: (game_id, competition, final, result_at)."""
    con = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return list(con.execute(
            """SELECT r.source_game_id AS game_id, g.competition_slug AS comp,
                      r.final_total AS final, r.result_at AS result_at
                 FROM game_results r
                 LEFT JOIN games g ON g.source_game_id = r.source_game_id
                WHERE r.final_result_status = 'OK'
                  AND r.final_total IS NOT NULL
                  AND r.result_at IS NOT NULL
                  AND r.result_at >= ?
                ORDER BY r.result_at DESC
                LIMIT ?""", (since, MAX_GAMES)))
    finally:
        con.close()


def _last_line(clean_db: Path, game_id: str, before: str) -> Optional[float]:
    """The newest line the feed saw for this game at/before its settlement."""
    con = sqlite3.connect(f"file:{clean_db}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            """SELECT live_total_line FROM clean_observations
                WHERE source_game_id = ?
                  AND captured_at <= ?
                  AND live_total_line IS NOT NULL
                  AND status = 'VALID'
                ORDER BY captured_at DESC LIMIT 1""",
            (str(game_id), str(before))).fetchone()
        return None if row is None else float(row[0])
    finally:
        con.close()


def samples(prod_db, clean_db, lookback_s: Optional[float] = None,
            now: Optional[float] = None) -> list:
    """``[(final, last_line), ...]`` over the settled window (trade universe)."""
    from blm_v4.trade_window import TRADE_EXCLUDED_COMPETITIONS

    prod_db, clean_db = Path(prod_db), Path(clean_db)
    look = window_s() if lookback_s is None else float(lookback_s)
    since = _iso((now or time.time()) - look)
    out = []
    for r in _settled(prod_db, since):
        comp = r["comp"]
        if comp and comp in (TRADE_EXCLUDED_COMPETITIONS or ()):
            continue
        line = _last_line(clean_db, r["game_id"], r["result_at"])
        if line is None:
            continue
        out.append((float(r["final"]), line))
    return out


def state(prod_db=None, clean_db=None, now: Optional[float] = None,
          *, refresh: bool = False) -> dict:
    """The guardrail's current verdict as a payload-ready dict."""
    root = _data_root()
    prod_db = Path(prod_db) if prod_db else root / "blm_pokerbet.db"
    clean_db = Path(clean_db) if clean_db else root / "blm_metrics_clean.db"
    key = (str(prod_db), str(clean_db))
    now = now or time.time()

    if not enabled():
        return {"enabled": False, "tripped": False, "reason": None,
                "overshare": None, "over": 0, "n": 0,
                "threshold": max_over(), "min_games": min_games(),
                "window_s": window_s(), "error": None,
                "text": "guardrail disabled (TRADE_MARKET_HEAT_ENABLED=0)"}

    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < CACHE_S:
            return hit[1]

    result = {"enabled": True, "tripped": False, "reason": None,
              "overshare": None, "over": 0, "n": 0,
              "threshold": max_over(), "min_games": min_games(),
              "window_s": window_s(), "error": None, "text": ""}
    try:
        pairs = samples(prod_db, clean_db, now=now)
        agg = overshare(pairs)
        result.update(over=agg["over"], n=agg["n"],
                      overshare=(round(agg["share"], 4)
                                 if agg["share"] is not None else None))
        if agg["n"] < result["min_games"]:
            result["text"] = (f"insufficient sample: {agg['n']} settled games "
                              f"(need {result['min_games']})")
        elif agg["share"] is not None and agg["share"] >= result["threshold"]:
            result["tripped"] = True
            result["reason"] = (f"unders_beaten_{agg['share']*100:.0f}pct_"
                                f"of_{agg['n']}")
            result["text"] = (f"{agg['over']}/{agg['n']} recent settled games "
                              f"finished OVER the line "
                              f"({agg['share']*100:.0f}% ≥ "
                              f"{result['threshold']*100:.0f}%) — unders are "
                              f"being beaten across the market")
        else:
            result["text"] = (f"{agg['over']}/{agg['n']} recent settled games "
                              f"finished OVER the line "
                              f"({(agg['share'] or 0)*100:.0f}% < "
                              f"{result['threshold']*100:.0f}%)")
    except Exception as e:                # never let a read halt trading
        result["error"] = f"{type(e).__name__}: {e}"[:200]
        result["text"] = f"market-heat check unavailable ({result['error']})"
    result["checked_at"] = _iso(now)
    with _cache_lock:
        _cache[key] = (now, result)
    return result


def tripped(prod_db=None, clean_db=None, now: Optional[float] = None) -> bool:
    return bool(state(prod_db, clean_db, now).get("tripped"))


def refusal(prod_db=None, clean_db=None, now: Optional[float] = None
            ) -> Optional[str]:
    """``"market_heat:<reason>"`` when tripped, else ``None``.

    Shaped like a leg reason so the lane, the slate and the journal can treat
    it exactly like ``line_age:line_stale``.
    """
    st = state(prod_db, clean_db, now)
    if not st.get("tripped"):
        return None
    return f"market_heat:{st.get('reason') or 'unders_beaten'}"
