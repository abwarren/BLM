#!/usr/bin/env python3
"""tc-new — Gate-B candidates: pace > 1.08, progress 70–95, price ≥ 1.60,
shortfall off, fresh lines only (age ≤ 15 s by default).

Same row format as tc-all.  Uses the live engine's competition_pace_reference
and game metadata but applies a NEW gate definition that bypasses the
shortfall leg, widens the progress band, and raises the pace bar.  This is
a READ-ONLY analysis tool — it writes nothing.

Usage:
    tc-new                          last 7 complete days
    tc-new 2026-11-04               that day only
    tc-new 2026-11-04 2026-11-10    that span (inclusive)
    tc-new --since 2026-11-04       open-ended, from a date
    tc-new --until 2026-11-10       open-ended, up to a date
    tc-new --max-age 30             fresh lines ≤ 30 s (default 15)
    tc-new --max-age 0              all lines (no freshness filter)
    tc-new --pace 1.12              alternate pace bar (e.g. Gate A)
    tc-new --price 1.50             alternate price floor
    tc-new --prog-min 75            alternate progress floor
    tc-new --prog-max 92            alternate progress ceiling
    tc-new --csv out.csv            also write machine-readable CSV
    tc-new --summary                counts only, no per-game rows
    tc-new --unsettled              include games with no final result

Gate defaults (Gate B from the 2026-11-11 volume sweep):
    pace     > 1.08
    progress   70 – 95
    price    ≥ 1.60
    shortfall  OFF (no requirement)
    line age ≤ 15 s  (captured_at − market_captured_at)

Settlement: same as tc-all — OK final < live line = WIN (price-1),
> line = LOSS (-1), == line = PUSH (0); no OK final = excluded from win%
(shown as ⏳ unless --unsettled).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("BLM_DATA_ROOT", str(REPO.parent / "BLM"))

from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402

DATA = Path(os.environ["BLM_DATA_ROOT"])
PROD_DB = DATA / "blm_pokerbet.db"
CLEAN_DB = DATA / "blm_metrics_clean.db"

EXCLUDED = {"cyber-basketball-2k26-matches"}

# Gate B defaults
DEFAULT_PACE = 1.08
DEFAULT_PRICE = 1.60
DEFAULT_PROG_MIN = 70.0
DEFAULT_PROG_MAX = 95.0
DEFAULT_MAX_AGE = 15      # seconds; 0 = no freshness filter
DEFAULT_POINTS = 70.0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="tc-new",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("dates", nargs="*", metavar="YYYY-MM-DD",
                   help="since [until] — tc-new 2026-11-04  or  tc-new 2026-11-04 2026-11-10")
    p.add_argument("--since", help="first day, YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="last day, YYYY-MM-DD (inclusive)")
    p.add_argument("--pace", type=float, default=DEFAULT_PACE, metavar="X",
                   help=f"pace ratio floor, strict (default {DEFAULT_PACE})")
    p.add_argument("--price", type=float, default=DEFAULT_PRICE, metavar="X",
                   help=f"under_price floor (default {DEFAULT_PRICE})")
    p.add_argument("--prog-min", type=float, default=DEFAULT_PROG_MIN, metavar="X",
                   help=f"progress lower bound (default {DEFAULT_PROG_MIN})")
    p.add_argument("--prog-max", type=float, default=DEFAULT_PROG_MAX, metavar="X",
                   help=f"progress upper bound (default {DEFAULT_PROG_MAX})")
    p.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE, metavar="SECS",
                   help=f"max line age in seconds (default {DEFAULT_MAX_AGE}; 0 = no filter)")
    p.add_argument("--points", type=float, default=DEFAULT_POINTS, metavar="X",
                   help=f"scored-points floor (default {DEFAULT_POINTS})")
    p.add_argument("--unsettled", action="store_true",
                   help="include games with no final result yet")
    p.add_argument("--summary", action="store_true",
                   help="counts only, no per-game rows")
    p.add_argument("--csv", metavar="PATH", help="write CSV to PATH")
    p.add_argument("--units", type=float, default=1.0, help="stake per candidate")
    return p.parse_args(argv)


def _default_window():
    """Default: last 7 complete days (excludes today)."""
    today = date.today()
    return (today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat()


def main(argv=None) -> int:
    a = parse_args(argv)

    # Resolve date window
    import re as _re
    if a.dates:
        if len(a.dates) > 2:
            print("give at most two dates: since [until]", file=sys.stderr)
            return 2
        for d in a.dates:
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                print(f"bad date {d!r} — use YYYY-MM-DD", file=sys.stderr)
                return 2
        if a.since or a.until:
            print("use either positional dates or --since/--until, not both",
                  file=sys.stderr)
            return 2
        a.since = a.dates[0]
        a.until = a.dates[1] if len(a.dates) == 2 else a.dates[0]

    if not a.since and not a.until:
        a.since, a.until = _default_window()

    # Load game metadata and finals
    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pace_ref = competition_pace_reference(conn)
    games_meta = {
        r["source_game_id"]: (r["competition_slug"], r["home_team"], r["away_team"])
        for r in conn.execute(
            "SELECT source_game_id, competition_slug, home_team, away_team FROM games")
    }
    slugs = {g: m[0] for g, m in games_meta.items()}
    finals = {
        r["source_game_id"]: r["final_total"]
        for r in conn.execute(
            "SELECT source_game_id, final_total FROM game_results "
            "WHERE final_result_status = 'OK' AND final_total IS NOT NULL")
    }
    conn.close()

    # Load peak scores (to flag decided losses with no final result)
    peaks: dict[str, float] = {}
    cc0 = sqlite3.connect(f"file:{CLEAN_DB}?mode=ro", uri=True)
    for gid, peak in cc0.execute(
            "SELECT source_game_id, MAX(total_points) FROM clean_observations "
            "WHERE total_points IS NOT NULL GROUP BY source_game_id"):
        if peak is not None:
            peaks[gid] = peak
    cc0.close()

    # Build observation query
    where = [
        "status = 'VALID'",
        "progress_pct >= ?", "progress_pct <= ?",
        "total_points >= ?",
        "required_pts_per_min IS NOT NULL",
        "actual_pts_per_min IS NOT NULL",
        "under_price >= ?",
        "live_total_line IS NOT NULL",
    ]
    params: list = [a.prog_min, a.prog_max, a.points, a.price]

    if a.since:
        where.append("captured_at >= ?")
        params.append(f"{a.since}T00:00:00")
    if a.until:
        where.append("captured_at <= ?")
        params.append(f"{a.until}T23:59:59.999")

    cc = sqlite3.connect(f"file:{CLEAN_DB}?mode=ro", uri=True)
    cc.row_factory = sqlite3.Row
    rows = cc.execute(
        "SELECT source_game_id, captured_at, total_points, progress_pct, "
        "required_pts_per_min, actual_pts_per_min, live_total_line, "
        "under_price, market_captured_at "
        "FROM clean_observations WHERE " + " AND ".join(where) +
        " ORDER BY source_game_id, captured_at",
        params,
    ).fetchall()
    cc.close()

    # Pick first qualifying observation per game
    pick: dict[str, dict] = {}
    stale_skipped = 0

    for r in rows:
        gid = r["source_game_id"]
        if gid in pick:
            continue

        slug = slugs.get(gid)

        # Exclude cyber / simulated feed
        if slug in EXCLUDED:
            continue

        # Pace bar (strict >)
        avg = (pace_ref.get(slug) or {}).get("avg_pace") if slug else None
        if avg is None or avg <= 0:
            continue
        pace_ratio = r["required_pts_per_min"] / avg
        if pace_ratio <= a.pace:
            continue

        # Shortfall: OFF — no requirement applied here

        # Line freshness filter
        if a.max_age > 0:
            cap = r["captured_at"]
            mkt = r["market_captured_at"]
            if cap and mkt:
                # Both stored as ISO strings: "YYYY-MM-DDTHH:MM:SS.ffffffZ"
                # Parse to seconds-since-epoch difference via datetime
                from datetime import datetime, timezone

                def _parse(s):
                    s = str(s).rstrip("Z").replace(" ", "T")
                    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                        except ValueError:
                            continue
                    return None

                dt_cap = _parse(cap)
                dt_mkt = _parse(mkt)
                if dt_cap is None or dt_mkt is None:
                    stale_skipped += 1
                    continue
                age_s = (dt_cap - dt_mkt).total_seconds()
                if age_s > a.max_age:
                    stale_skipped += 1
                    continue
            else:
                # market_captured_at missing — skip (can't verify freshness)
                stale_skipped += 1
                continue

        final = finals.get(gid)
        slug2, home, away = games_meta.get(gid, (slug, "?", "?"))
        short = r["required_pts_per_min"] - r["actual_pts_per_min"]

        entry = dict(
            gid=gid,
            ts=str(r["captured_at"])[:19].replace("T", " "),
            slug=slug or "-",
            teams=f"{home or '?'} v {away or '?'}",
            prog=r["progress_pct"],
            pts=r["total_points"],
            pace=pace_ratio,
            short=short,
            price=r["under_price"],
            line=r["live_total_line"],
            final=final,
        )

        if final is None:
            peak = peaks.get(gid)
            if peak is not None and peak > r["live_total_line"]:
                entry.update(res="L", pnl=-a.units, contested=True)
            else:
                entry.update(res="?", pnl=0.0, contested=(peak is not None))
        elif final < r["live_total_line"]:
            entry.update(res="W", pnl=(r["under_price"] - 1.0) * a.units)
        elif final > r["live_total_line"]:
            entry.update(res="L", pnl=-a.units)
        else:
            entry.update(res="P", pnl=0.0)

        pick[gid] = entry

    out = sorted(pick.values(), key=lambda e: e["ts"])
    unknown_total = sum(1 for e in out if e["res"] == "?")
    if not a.unsettled:
        out = [e for e in out if e["res"] != "?"]

    # Header
    age_label = f"≤{a.max_age}s" if a.max_age > 0 else "any"
    gate_str = (
        f"gate: pace>{a.pace:.2f} progress{a.prog_min:.0f}-{a.prog_max:.0f} "
        f"price≥{a.price:.2f} shortfall=OFF line-age={age_label} "
        f"pts≥{a.points:.0f}  [{a.since or '?'} → {a.until or '?'}]"
    )

    if a.summary:
        n = len(out)
        w = sum(1 for e in out if e["res"] == "W")
        l = sum(1 for e in out if e["res"] == "L")
        print(gate_str)
        print(f"candidates: {n}  W{w} L{l}"
              + (f"  win% {w / max(1, w+l) * 100:.1f}" if w + l else ""))
        return 0

    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "game", "teams", "competition",
                        "prog", "pts", "pace", "shortfall",
                        "price", "line", "final", "result", "units"])
            for e in out:
                w.writerow([
                    e["ts"], e["gid"], e["teams"], e["slug"],
                    f"{e['prog']:.1f}", e["pts"],
                    f"{e['pace']:.3f}", f"{e['short']:+.2f}",
                    f"{e['price']:.2f}", f"{e['line']:.1f}",
                    e["final"], e["res"], f"{e['pnl']:+.2f}",
                ])
        print(f"wrote {len(out)} candidates -> {a.csv}")

    # Print table
    print(gate_str)
    print()
    print(f"{'when (UTC)':19s} {'game':12s} {'prog':>6s} {'pts':>4s} "
          f"{'pace':>5s} {'short':>6s} {'price':>5s} {'line':>6s} {'final':>5s} "
          f"{'res':>3s} {'units':>6s}  game / competition")
    icon = {"W": "✅", "L": "❌", "P": "➖", "?": "⏳"}
    unit_sum = 0.0
    for e in out:
        unit_sum += e["pnl"]
        print(
            f"{e['ts']:19s} {e['gid']:12s} {e['prog']:6.1f} "
            f"{e['pts']:4.0f} {e['pace']:5.2f} {e['short']:+6.2f} "
            f"{e['price']:5.2f} {e['line']:6.1f} "
            f"{(e['final'] if e['final'] is not None else '-'):>5} "
            f"{icon.get(e['res'], e['res']):>3s} "
            f"{e['pnl']:+6.2f}  {e['teams'][:38]} [{e['slug']}]"
        )

    n = len(out)
    w = sum(1 for e in out if e["res"] == "W")
    l = sum(1 for e in out if e["res"] == "L")
    print(
        f"\n{n} candidates · W{w} L{l} "
        f"{'· win% ' + format(w / max(1, w+l)*100, '.1f') if w+l else ''} "
        f"· {unit_sum:+.2f}u at {a.units:g}u"
    )
    if stale_skipped:
        print(f"   note: {stale_skipped} observations skipped — line age > {a.max_age}s "
              f"or market_captured_at missing")
    if unknown_total:
        print(f"   note: {unknown_total} candidate(s) have no decidable result "
              f"and are NOT in the figures above — see --unsettled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
