#!/usr/bin/env python3
"""The daily LADDER curve, WITH the shortfall leg (read-only, 2026-11-02).

The operator's ladder table (24 trading days, 145 bets) was produced without
the shortfall leg.  This rebuilds that table over the SAME population and then
adds the engine's own shortfall leg, one sweep row per margin, so the ladder
curve the operator is reading can be compared leg-for-leg with what the live
gate actually arms.

Ladder (operator directive 2026-10-30, blm_v4/betting/ladder.py):
    rung 1 = 1 unit, factor 1.8, depth 4 -> 1 / 1.8 / 3.24 / 5.832 units
    a WIN advances, a LOSS resets, at depth the ladder BANKS and resets.

Population (identical to scripts/tc_all.py): the FIRST moment in a game that
the trade gate's non-shortfall legs all hold:
    status VALID · progress 75-92 · points >= 70 · pace ratio > 1.04 (strict)
    · under_price >= 1.60 · cyber-2k26 excluded · settled final
Settlement: final < line = W (price - 1), final > line = L (-1), final == line
= push (0).  A game with no OK final but whose captured peak already passed the
line is a decided LOSS (tc_all's rule).

Read-only: both SQLite files are opened mode=ro.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"

SINCE, UNTIL = "2026-10-01", "2026-10-30"
MIN_PROGRESS, MAX_PROGRESS = 75.0, 92.0
MIN_POINTS = 70.0
MIN_PACE_RATIO = 1.04          # TRADE_MIN_PACE_RATIO (live)
MIN_PRICE = 1.60               # TRADE_MIN_UNDER_PRICE (live signal floor)
EXCLUDED = {"cyber-basketball-2k26-matches"}
DEPTH, FACTOR = 4, 1.8
START_CAPITAL = 1.0


def f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def load():
    conn = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    meta = {r["source_game_id"]: r["competition_slug"]
            for r in conn.execute("SELECT source_game_id, competition_slug FROM games")}
    finals = {r["source_game_id"]: r["final_total"] for r in conn.execute(
        "SELECT source_game_id, final_total FROM game_results "
        "WHERE final_result_status='OK' AND final_total IS NOT NULL")}
    pace_ref = competition_pace_reference(conn)
    conn.close()

    cc = sqlite3.connect(f"file:{CLEAN}?mode=ro", uri=True)
    cc.row_factory = sqlite3.Row
    peaks = {r[0]: r[1] for r in cc.execute(
        "SELECT source_game_id, MAX(total_points) FROM clean_observations "
        "WHERE total_points IS NOT NULL GROUP BY source_game_id")}
    rows = cc.execute(
        "SELECT source_game_id, captured_at, total_points, progress_pct, "
        "required_pts_per_min, actual_pts_per_min, live_total_line, under_price "
        "FROM clean_observations WHERE status='VALID' "
        "AND progress_pct BETWEEN ? AND ? AND total_points >= ? "
        "AND required_pts_per_min IS NOT NULL AND actual_pts_per_min IS NOT NULL "
        "AND live_total_line IS NOT NULL AND under_price IS NOT NULL "
        "AND captured_at >= ? AND captured_at <= ? "
        "ORDER BY source_game_id, captured_at",
        (MIN_PROGRESS, MAX_PROGRESS, MIN_POINTS, f"{SINCE}T00:00:00",
         f"{UNTIL}T23:59:59.999")).fetchall()
    cc.close()
    return meta, finals, peaks, rows, pace_ref


def candidates(meta, finals, peaks, rows, pace_ref, shortfall_margin):
    """First qualifying moment per game: the full gate, with one shortfall floor.

    ``shortfall_margin=None`` -> the shortfall leg is OFF (the earlier table).
    """
    out = {}
    for r in rows:
        gid = r["source_game_id"]
        if gid in out:
            continue
        slug = meta.get(gid)
        if not slug or slug in EXCLUDED:
            continue
        req = f(r["required_pts_per_min"])
        act = f(r["actual_pts_per_min"])
        price = f(r["under_price"])
        line = f(r["live_total_line"])
        if None in (req, act, price, line):
            continue
        avg = (pace_ref.get(slug) or {}).get("avg_pace")
        if not avg or avg <= 0 or not (req / avg) > MIN_PACE_RATIO:
            continue
        if price < MIN_PRICE:
            continue
        if shortfall_margin is not None and (req - act) < shortfall_margin:
            continue
        out[gid] = dict(gid=gid, slug=slug, ts=str(r["captured_at"])[:19],
                        prog=f(r["progress_pct"]), pts=f(r["total_points"]),
                        pace=req / avg, short=req - act, price=price, line=line)
    return out


def settle(cand, finals, peaks):
    final = finals.get(cand["gid"])
    if final is None:
        peak = peaks.get(cand["gid"])
        if peak is not None and peak > cand["line"]:
            return "L", -1.0, None
        return "?", 0.0, None
    if final < cand["line"]:
        return "W", cand["price"] - 1.0, final
    if final > cand["line"]:
        return "L", -1.0, final
    return "P", 0.0, final


def run_ladder(cands, finals, peaks):
    daymap = defaultdict(list)
    unknown = 0
    for c in cands.values():
        res, pnl, final = settle(c, finals, peaks)
        if res == "?":
            unknown += 1
            continue
        daymap[c["ts"][:10]].append(dict(c, res=res, pnl=pnl, final=final))

    rung, capital = 1, START_CAPITAL
    banks = brk = 0
    n = w = l = 0
    lad_total = flat_total = 0.0
    top = 0.0
    days = []
    for day in sorted(daymap):
        bets = sorted(daymap[day], key=lambda c: c["ts"])
        lad = flat = 0.0
        dw = dl = dbanks = dbrk = 0
        dtop = 0.0
        for c in bets:
            stake = round(FACTOR ** (rung - 1), 4)
            dtop = max(dtop, stake)
            top = max(top, stake)
            n += 1
            if c["res"] == "W":
                lad += stake * (c["price"] - 1.0)
                flat += c["pnl"]
                dw += 1
                rung = 1 if rung >= DEPTH else rung + 1
                if rung == 1:
                    dbanks += 1
            elif c["res"] == "L":
                lad -= stake
                flat -= 1.0
                dl += 1
                rung = 1
                dbrk += 1
        banks += dbanks
        brk += dbrk
        w += dw
        l += dl
        lad_total += lad
        flat_total += flat
        capital += lad
        days.append(dict(day=day, n=len(bets), w=dw, l=dl,
                         win=(dw / (dw + dl) * 100) if (dw + dl) else 0.0,
                         lad=lad, flat=flat, cap=capital, top=dtop,
                         banks=dbanks, brk=dbrk))
    n_days = len(days)
    up = sum(1 for d in days if d["lad"] > 0)
    return dict(n=n, w=w, l=l, win=(w / max(1, w + l)) * 100, lad=lad_total,
                flat=flat_total, banks=banks, brk=brk, top=top, days=days,
                n_days=n_days, up=up, cap=capital, unknown=unknown,
                best=max((d["lad"] for d in days), default=0.0),
                worst=min((d["lad"] for d in days), default=0.0))


def main() -> int:
    meta, finals, peaks, rows, pace_ref = load()
    print(f"population: {SINCE} → {UNTIL} · progress {MIN_PROGRESS:.0f}-{MAX_PROGRESS:.0f}"
          f" · pts >= {MIN_POINTS:.0f} · pace > {MIN_PACE_RATIO:.2f}"
          f" · price >= {MIN_PRICE:.2f} · cyber excluded · settled finals only")
    print(f"ladder: depth {DEPTH}, factor {FACTOR}   stakes "
          f"{' / '.join(format(FACTOR ** i, '.2f') for i in range(DEPTH))} units   "
          f"capital starts {START_CAPITAL:.2f}u\n")

    variants = [("off", None), (">= 0.00", 0.00), (">= 0.25", 0.25),
                (">= 0.50 (live)", 0.50)]
    results = {}
    print(f"{'shortfall':>15s} {'bets':>5s} {'W':>4s} {'L':>4s} {'win%':>6s} "
          f"{'ladder net':>11s} {'flat net':>9s} {'top':>6s} {'banks':>6s} {'brk':>4s}")
    for label, margin in variants:
        cands = candidates(meta, finals, peaks, rows, pace_ref, margin)
        stats = run_ladder(cands, finals, peaks)
        results[label] = (cands, stats)
        print(f"{label:>15s} {stats['n']:5d} {stats['w']:4d} {stats['l']:4d} "
              f"{stats['win']:6.1f} {stats['lad']:+11.2f} {stats['flat']:+9.2f} "
              f"{stats['top']:6.2f} {stats['banks']:6d} {stats['brk']:4d}")

    for label in ("off", ">= 0.50 (live)"):
        cands, stats = results[label]
        print(f"\n── shortfall {label} — daily ladder (depth {DEPTH}) " + "─" * 26)
        print(f"{'day':11s} {'bets':>4s} {'W':>3s} {'L':>3s} {'win%':>6s} "
              f"{'ladder net':>11s} {'flat net':>9s} {'capital':>8s} "
              f"{'top stake':>10s} {'banks':>6s} {'brk':>4s}")
        for d in stats["days"]:
            print(f"{d['day']:11s} {d['n']:4d} {d['w']:3d} {d['l']:3d} "
                  f"{d['win']:6.1f} {d['lad']:+11.2f} {d['flat']:+9.2f} "
                  f"{d['cap']:8.2f} {d['top']:10.2f} {d['banks']:6d} {d['brk']:4d}")
        print(f"{'TOTAL':11s} {stats['n']:4d} {stats['w']:3d} {stats['l']:3d} "
              f"{stats['win']:6.1f} {stats['lad']:+11.2f} {stats['flat']:+9.2f} "
              f"{stats['cap']:8.2f} {stats['top']:10.2f} {stats['banks']:6d} "
              f"{stats['brk']:4d}")
        print(f"  {stats['n_days']} trading days · {stats['up']} up · "
              f"{stats['n_days'] - stats['up']} down · best {stats['best']:+.2f}u · "
              f"worst {stats['worst']:+.2f}u · top stake never exceeded "
              f"{stats['top']:.2f}u · banks {stats['banks']}/{stats['brk']}"
              + (f" · {stats['unknown']} unsettled excluded" if stats["unknown"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
