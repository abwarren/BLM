#!/usr/bin/env python3
"""tc-all — every cohort candidate, in chronological order.

One line per candidate: the FIRST moment in a game that a cohort's legs all
hold (the same rule the engine fires on), labelled with the tightest cohort and
every cohort it belongs to, then settled against the game's final total.

    tc-all                      all cohorts, whole clean era
    tc-all 2026-10-12           that day
    tc-all 2026-10-12 2026-10-14   that span (inclusive)
    tc-all --since 2026-10-22   open-ended, from a date (also --until)
    tc-all --cohort TC-X        only games that satisfy TC-X
    tc-all 2026-10-12 --csv out.csv   machine-readable, one row per candidate
    tc-all --summary            cohort counts only

Cohorts (blm_v4/trade_cohorts.py — imported, never re-implemented):
    TC-W WIDE 1.00 · TC-C CORE 1.04 · TC-S SHARP 1.08 · TC-E EDGE 1.12
    TC-X ELITE 1.16 · TC-D DEEP · TC-C+ CORE+
TC-X ⊂ TC-E ⊂ TC-S ⊂ TC-C ⊂ TC-W: a game labelled TC-S is also a TC-W game.

Read-only. Settlement: final total vs the live line taken at entry; UNDER wins
when final < line, pushes when equal, 1 unit flat.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("BLM_DATA_ROOT", str(REPO.parent / "BLM"))


def _adopt_running_gate() -> dict:
    """Use the LIVE engine's TRADE_* gate, not the code defaults.

    The deployment selects its cohort through systemd drop-ins, so a report run
    from a shell would otherwise describe a different gate than the one firing —
    exactly the mismatch that made a fired bet look "not in the cohort".
    Reads /proc/<server pid>/environ; anything already in the environment wins,
    and any failure is silent (the defaults stay honest).
    """
    import subprocess
    adopted = {}
    try:
        pid = subprocess.run(
            ["systemctl", "--user", "show", "blm-server.service", "-p",
             "MainPID", "--value"], capture_output=True, text=True,
            timeout=5).stdout.strip()
        if not pid or pid == "0":
            return adopted
        with open(f"/proc/{pid}/environ", "rb") as fh:
            for kv in fh.read().split(b"\0"):
                k, _, v = kv.partition(b"=")
                name = k.decode()
                if name.startswith("TRADE_") and name not in os.environ:
                    os.environ[name] = v.decode()
                    adopted[name] = v.decode()
    except Exception:
        pass
    return adopted


_ADOPTED = _adopt_running_gate()

from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402
from blm_v4.trade_cohorts import COHORT_ORDER, resolve_cohort  # noqa: E402
from blm_v4.trade_window import (SHORTFALL_MARGIN, TRADE_MAX_PROGRESS_PCT,
                                 TRADE_MIN_PACE_RATIO, TRADE_MIN_PROGRESS_PCT,
                                 TRADE_MIN_SCORED_POINTS,
                                 exec_shortfall_reason)  # noqa: E402

DATA = Path(os.environ["BLM_DATA_ROOT"])
PROD_DB = DATA / "blm_pokerbet.db"
CLEAN_DB = DATA / "blm_metrics_clean.db"


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="tc-all", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dates", nargs="*", metavar="YYYY-MM-DD",
                   help="since [until] — tc-all 2026-10-12  or  tc-all 2026-10-12 2026-10-14")
    p.add_argument("--since", help="first day, YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="last day, YYYY-MM-DD (inclusive)")
    p.add_argument("--cohort", default=None, metavar="TC-X",
                   help="only candidates that satisfy this cohort")
    p.add_argument("--unsettled", action="store_true",
                   help="include games with no final result yet")
    p.add_argument("--summary", action="store_true", help="cohort counts only")
    p.add_argument("--csv", metavar="PATH", help="write CSV to PATH")
    p.add_argument("--limit", type=int, default=0, help="stop after N candidates")
    p.add_argument("--units", type=float, default=1.0, help="stake per candidate")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    # positional shorthand: tc-all 2026-10-12 [2026-10-14]
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
        # one date = THAT DAY; two dates = the span between them.
        # (Open-ended "from here on" is --since, so it stays unambiguous.)
        a.until = a.dates[1] if len(a.dates) == 2 else a.dates[0]
    if a.cohort and a.cohort not in COHORT_ORDER:
        print(f"unknown cohort {a.cohort!r}; choose from {', '.join(COHORT_ORDER)}",
              file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pace_ref = competition_pace_reference(conn)
    slugs = {r["source_game_id"]: r["competition_slug"]
             for r in conn.execute("SELECT source_game_id, competition_slug FROM games")}
    peaks: dict[str, float] = {}
    cc0 = sqlite3.connect(f"file:{CLEAN_DB}?mode=ro", uri=True)
    try:
        for gid, peak in cc0.execute(
                "SELECT source_game_id, MAX(total_points) FROM clean_observations "
                "WHERE total_points IS NOT NULL GROUP BY source_game_id"):
            peaks[gid] = peak
    finally:
        cc0.close()
    finals = {r["source_game_id"]: r["final_total"] for r in conn.execute(
        "SELECT source_game_id, final_total FROM game_results "
        "WHERE final_result_status = 'OK' AND final_total IS NOT NULL")}
    conn.close()

    where = ["status = 'VALID'",
             "progress_pct >= ?", "progress_pct <= ?",
             "total_points >= ?",
             "required_pts_per_min IS NOT NULL",
             "actual_pts_per_min IS NOT NULL",
             "under_price IS NOT NULL",
             "live_total_line IS NOT NULL"]
    params = [TRADE_MIN_PROGRESS_PCT, TRADE_MAX_PROGRESS_PCT, TRADE_MIN_SCORED_POINTS]
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
        "required_pts_per_min, actual_pts_per_min, live_total_line, under_price "
        "FROM clean_observations WHERE " + " AND ".join(where) +
        " ORDER BY source_game_id, captured_at", params).fetchall()
    cc.close()

    pick = {}
    for r in rows:                       # first qualifying moment per game wins
        gid = r["source_game_id"]
        if gid in pick:
            continue
        slug = slugs.get(gid)
        # the ENGINE's shortfall leg — the cohort labels carry each rung's own
        # floor, which is NOT the live gate.  Without this the tool reported
        # 40 candidates/day where the engine could fire 20.
        if exec_shortfall_reason(r["required_pts_per_min"],
                                 r["actual_pts_per_min"]) is not None:
            continue
        avg = (pace_ref.get(slug) or {}).get("avg_pace") if slug else None
        coh = resolve_cohort(progress_pct=r["progress_pct"],
                            total_points=r["total_points"],
                            required_pace=r["required_pts_per_min"],
                            avg_pace=avg,
                            actual_pace=r["actual_pts_per_min"],
                            under_price=r["under_price"],
                            competition_slug=slug)
        if not coh["codes"]:
            continue
        if a.cohort and a.cohort not in coh["codes"]:
            continue
        final = finals.get(gid)
        entry = dict(gid=gid, ts=str(r["captured_at"])[:19].replace("T", " "),
                     slug=slug or "-", prog=r["progress_pct"], pts=r["total_points"],
                     pace=(r["required_pts_per_min"] / avg) if avg else None,
                     short=r["required_pts_per_min"] - r["actual_pts_per_min"],
                     price=r["under_price"], line=r["live_total_line"],
                     codes=coh["codes"], code=coh["code"],
                     final=final)
        if final is None:
            # contested / not yet settled.  The collector is a second witness:
            # if it already scored MORE than the line, the UNDER lost for
            # certain (final >= peak > line) — that is a decided loss, not a
            # missing result.  Anything else stays genuinely unknown.
            peak = peaks.get(gid)
            if peak is not None and peak > r["live_total_line"]:
                entry.update(res="L", pnl=-a.units, contested=True)
            else:
                entry.update(res="?", pnl=0.0,
                             contested=peak is not None)
        elif final < r["live_total_line"]:
            entry.update(res="W", pnl=(r["under_price"] - 1.0) * a.units)
        elif final > r["live_total_line"]:
            entry.update(res="L", pnl=-a.units)
        else:
            entry.update(res="P", pnl=0.0)
        pick[gid] = entry

    out = sorted(pick.values(), key=lambda e: e["ts"])
    # counted BEFORE the unsettled filter: these are the candidates with no
    # decidable outcome at all, and silently dropping them flatters the record
    unknown_total = sum(1 for e in out if e["res"] == "?")
    if not a.unsettled:
        out = [e for e in out if e["res"] != "?"]
    if a.limit:
        out = out[:a.limit]

    counts = {c: sum(1 for e in out if c in e["codes"]) for c in COHORT_ORDER}
    gate = (f"gate: pace>{TRADE_MIN_PACE_RATIO:.2f} shortfall>="
            f"{SHORTFALL_MARGIN:+.2f} progress<={TRADE_MAX_PROGRESS_PCT:.0f}"
            f"{'  (live engine)' if _ADOPTED else '  (code defaults)'}")
    if a.summary:
        print(gate)
        print(f"candidates: {len(out)}   " +
              "  ".join(f"{c}={n}" for c, n in counts.items() if n))
        return 0

    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "game", "competition", "cohort", "also", "prog",
                        "pts", "pace", "shortfall", "price", "line", "final",
                        "result", "units"])
            for e in out:
                w.writerow([e["ts"], e["gid"], e["slug"], e["code"],
                            "+".join(c for c in e["codes"] if c != e["code"]),
                            f"{e['prog']:.1f}", e["pts"],
                            f"{e['pace']:.3f}" if e["pace"] else "",
                            f"{e['short']:+.2f}", f"{e['price']:.2f}",
                            f"{e['line']:.1f}", e["final"], e["res"],
                            f"{e['pnl']:+.2f}"])
        print(f"wrote {len(out)} candidates -> {a.csv}")

    print(f"{'when (UTC)':19s} {'game':12s} {'cohort':7s} {'prog':>6s} {'pts':>4s} "
          f"{'pace':>5s} {'short':>6s} {'price':>5s} {'line':>6s} {'final':>5s} "
          f"{'res':>3s} {'units':>6s}  competition")
    icon = {"W": "✅", "L": "❌", "P": "➖", "?": "⏳"}
    unit_sum = 0.0
    for e in out:
        unit_sum += e["pnl"]
        also = ",".join(c for c in e["codes"] if c != e["code"])
        print(f"{e['ts']:19s} {e['gid']:12s} {e['code']:7s} {e['prog']:6.1f} "
              f"{e['pts']:4.0f} {(e['pace'] or 0):5.2f} {e['short']:+6.2f} "
              f"{e['price']:5.2f} {e['line']:6.1f} "
              f"{(e['final'] if e['final'] is not None else '-'):>5} "
              f"{icon.get(e['res'], e['res']):>3s} "
              f"{e['pnl']:+6.2f}  {e['slug']}{(' ⊂' + also) if also else ''}")

    n = len(out)
    w = sum(1 for e in out if e["res"] == "W")
    l = sum(1 for e in out if e["res"] == "L")
    unk = sum(1 for e in out if e["res"] == "?")
    con = sum(1 for e in out if e.get("contested"))
    print(f"\n{n} candidates · W{w} L{l} "
          f"{'· win% ' + format(w / max(1, w + l) * 100, '.1f') if w + l else ''} "
          f"· {unit_sum:+.2f}u at {a.units:g}u   " +
          "  ".join(f"{c}={k}" for c, k in counts.items() if k))
    if con or unknown_total:
        print(f"   note: {con} settled as LOSSES on the collector's own score "
              f"(peak passed the line before the feed stopped); "
              f"{unknown_total} candidate(s) have no decidable result and are "
              f"NOT in the figures above — see --unsettled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
