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
    TRADE_MARKET_HEAT_HISTORY_DAYS=7  days shown by the slate's history strip

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

#: how many settled games a single check looks at (bounds the per-game lookup)
MAX_GAMES = 150
#: one check serves this long; the lane passes every 3 s, settlements are slow
CACHE_S = 120.0

#: the history strip is read from the SLATE payload only, never by the lane:
#: a week of settled games is ~6 000 line lookups, so it is cached separately
#: and for much longer than the verdict it illustrates.
HISTORY_CACHE_S = 600.0
HISTORY_DAYS = 7

_cache: dict = {}
_history_cache: dict = {}
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


def _settled(prod_db: Path, since: str, limit: Optional[int] = MAX_GAMES) -> list:
    """Recently settled games: (game_id, competition, final, result_at).

    ``limit=None`` is for the history strip, which needs the whole window; the
    live verdict keeps its cap so one check stays a bounded read.
    """
    sql = """SELECT r.source_game_id AS game_id, g.competition_slug AS comp,
                      r.final_total AS final, r.result_at AS result_at
                 FROM game_results r
                 LEFT JOIN games g ON g.source_game_id = r.source_game_id
                WHERE r.final_result_status = 'OK'
                  AND r.final_total IS NOT NULL
                  AND r.result_at IS NOT NULL
                  AND r.result_at >= ?
                ORDER BY r.result_at DESC"""
    args: tuple = (since,)
    if limit is not None:
        sql += " LIMIT ?"
        args = (since, int(limit))
    con = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return list(con.execute(sql, args))
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


def samples_by_day(prod_db, clean_db,
                   days: int = HISTORY_DAYS,
                   now: Optional[float] = None) -> list:
    """``[(day, final, last_line), ...]`` over the last ``days`` UTC days."""
    from blm_v4.trade_window import TRADE_EXCLUDED_COMPETITIONS

    prod_db, clean_db = Path(prod_db), Path(clean_db)
    since = _iso((now or time.time()) - max(1, int(days)) * 86400.0)
    out = []
    for r in _settled(prod_db, since, limit=None):
        comp = r["comp"]
        if comp and comp in (TRADE_EXCLUDED_COMPETITIONS or ()):
            continue
        line = _last_line(clean_db, r["game_id"], r["result_at"])
        if line is None:
            continue
        out.append((str(r["result_at"])[:10], float(r["final"]), line))
    return out


def daily(pairs: Iterable[tuple], days: int = HISTORY_DAYS,
          today: Optional[str] = None, threshold: Optional[float] = None,
          min_games_n: Optional[int] = None) -> list:
    """Pure: ``[(day, final, line), ...]`` -> one row per day, OLDEST first.

    Every day in the range gets a row, including ones with no settled game:
    a gap in the feed must look like a gap, not like a shorter axis.  ``share``
    is ``None`` on those days, and ``tripped`` applies the SAME threshold and
    minimum sample the live verdict uses, so the strip cannot disagree with
    the banner it sits next to.
    """
    thr = max_over() if threshold is None else float(threshold)
    floor = min_games() if min_games_n is None else int(min_games_n)
    end = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    keys = [(end_d - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            for i in range(max(1, int(days)))]
    buckets = {k: [0, 0] for k in keys}
    for day, final, line in pairs:
        bucket = buckets.get(day)
        if bucket is None or final is None or line is None:
            continue
        bucket[1] += 1
        if float(final) > float(line):
            bucket[0] += 1
    out = []
    for day in keys:
        over, n = buckets[day]
        share = (over / n) if n else None
        out.append({"day": day, "label": day[5:], "over": over, "n": n,
                    "share": (round(share, 4) if share is not None else None),
                    "today": day == end,
                    "tripped": bool(share is not None and n >= floor
                                    and share >= thr)})
    return out


def history(prod_db=None, clean_db=None, days: Optional[int] = None,
            now: Optional[float] = None, *, refresh: bool = False) -> list:
    """The day-by-day OVER share behind the current verdict (cached).

    Fail-open like ``state``: an unreadable database returns ``[]`` and the
    dashboard hides the strip rather than showing a stale or invented trend.
    """
    root = _data_root()
    prod_db = Path(prod_db) if prod_db else root / "blm_pokerbet.db"
    clean_db = Path(clean_db) if clean_db else root / "blm_metrics_clean.db"
    span = max(1, min(int(days or _env_int("TRADE_MARKET_HEAT_HISTORY_DAYS",
                                           HISTORY_DAYS)), 30))
    now = now or time.time()
    key = (str(prod_db), str(clean_db), span)

    with _cache_lock:
        hit = _history_cache.get(key)
        if hit and not refresh and now - hit[0] < HISTORY_CACHE_S:
            return hit[1]
    try:
        pairs = samples_by_day(prod_db, clean_db, days=span, now=now)
        rows = daily(pairs, days=span, today=_iso(now)[:10])
    except Exception:
        return []
    with _cache_lock:
        _history_cache[key] = (now, rows)
    return rows


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
