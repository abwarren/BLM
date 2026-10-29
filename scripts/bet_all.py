#!/usr/bin/env python3
"""bet-all — what the engine actually did, settled.

One line per EXECUTION (not per candidate): the stake it put on, the price it
took, what the book returned, and the money result against the game's final.

    bet-all                     every execution
    bet-all 2026-10-29          that day
    bet-all 2026-10-27 2026-10-29   that span
    bet-all --live              only what is still open (SUBMITTING/UNKNOWN)
    bet-all --csv /tmp/b.csv    machine-readable
    bet-all --summary           totals only

Settlement: the UNDER wins when the game's final total is below the line taken at
placement.  A game with no accepted result (or a result the collector's own score
contradicts) is left OPEN — never guessed.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

DATA = Path(os.environ.get("BLM_DATA_ROOT", "/home/ubuntu/BLM"))
BET_DB = DATA / "blm_betting.db"
PROD_DB = DATA / "blm_pokerbet.db"
ICON = {"W": "✅", "L": "❌", "P": "➖", "?": "⏳", "-": "·"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bet-all", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dates", nargs="*", metavar="YYYY-MM-DD")
    ap.add_argument("--live", action="store_true", help="only open executions")
    ap.add_argument("--csv", metavar="PATH")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args(argv)

    if len(a.dates) > 2:
        print("give at most two dates", file=sys.stderr)
        return 2
    since = a.dates[0] if a.dates else None
    until = (a.dates[1] if len(a.dates) == 2 else a.dates[0]) if a.dates else None

    conn = sqlite3.connect(f"file:{BET_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE 'file:{PROD_DB}?mode=ro' AS p")
    where, params = ["1=1"], []
    if since:
        where.append("e.day_utc >= ?"); params.append(since)
    if until:
        where.append("e.day_utc <= ?"); params.append(until)
    if a.live:
        where.append("e.status IN ('SUBMITTING','UNKNOWN')")
    rows = conn.execute(f"""
        SELECT e.id, e.game_id, e.status, e.stake_amount, e.price, e.triggered_line,
               e.error_code, e.submitted_at_utc, e.day_utc,
               g.home_team, g.away_team, g.competition_slug,
               r.final_total, r.final_result_status
          FROM bet_executions e
          LEFT JOIN p.games g ON g.source_game_id = e.game_id
          LEFT JOIN p.game_results r ON r.source_game_id = e.game_id
         WHERE {' AND '.join(where)}
         ORDER BY e.id""", params).fetchall()
    conn.close()

    out = []
    for r in rows:
        st = (r["status"] or "").upper()
        stake, price, line = r["stake_amount"] or 0.0, r["price"] or 0.0, r["triggered_line"]
        fin = r["final_total"] if (r["final_result_status"] == "OK") else None
        if st != "SUBMITTED":
            res, pnl = "-", 0.0                    # nothing was placed
        elif fin is None or line is None:
            res, pnl = "?", 0.0                    # placed, not settled yet
        elif fin < line:
            res, pnl = "W", stake * (price - 1)
        elif fin > line:
            res, pnl = "L", -stake
        else:
            res, pnl = "P", 0.0
        out.append(dict(id=r["id"], ts=str(r["submitted_at_utc"] or "")[:19].replace("T", " "),
                        game=r["game_id"], teams=f"{r['home_team'] or '?'} v {r['away_team'] or '?'}",
                        slug=r["competition_slug"] or "-", stake=stake, price=price,
                        line=line, status=st, err=r["error_code"] or "",
                        final=fin, res=res, pnl=pnl))

    placed = [x for x in out if x["status"] == "SUBMITTED"]
    staked = sum(x["stake"] for x in placed)
    ret = sum(x["stake"] * x["price"] for x in placed if x["res"] == "W")
    net = sum(x["pnl"] for x in placed)
    w = sum(1 for x in placed if x["res"] == "W")
    l = sum(1 for x in placed if x["res"] == "L")
    openn = sum(1 for x in placed if x["res"] == "?")
    failed = sum(1 for x in out if x["status"] == "FAILED")
    unknown = sum(1 for x in out if x["status"] == "UNKNOWN")

    if a.summary:
        print(f"{len(out)} executions · placed {len(placed)} (staked R{staked:,.2f}) · "
              f"failed {failed} · ambiguous {unknown}")
        print(f"settled W{w} L{l} · open {openn} · net R{net:+,.2f}"
              + (f" · ROI {net/staked*100:+.1f}%" if staked else ""))
        return 0

    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["id", "submitted_utc", "game", "teams", "competition", "status",
                         "stake", "price", "line", "final", "result", "pnl"])
            for x in out:
                wr.writerow([x["id"], x["ts"], x["game"], x["teams"], x["slug"],
                             x["status"], x["stake"], x["price"], x["line"],
                             x["final"], x["res"], round(x["pnl"], 2)])
        print(f"wrote {len(out)} executions -> {a.csv}")

    print(f"{'id':5s} {'when (UTC)':19s} {'status':10s} {'stake':>7s} {'price':>5s} "
          f"{'line':>6s} {'final':>5s} {'res':>3s} {'P&L':>9s}  teams")
    for x in out:
        print(f"{x['id']:<5d} {x['ts']:19s} {x['status']:10s} {x['stake']:7.2f} "
              f"{x['price']:5.2f} {(x['line'] if x['line'] is not None else 0):6.1f} "
              f"{(x['final'] if x['final'] is not None else '-'):>5} "
              f"{ICON.get(x['res'], x['res']):>3s} {x['pnl']:+9.2f}  "
              f"{x['teams'][:34]}{('  [' + x['err'] + ']') if x['err'] else ''}")

    print(f"\n{len(out)} executions · placed {len(placed)} (R{staked:,.2f}) · "
          f"failed {failed} · ambiguous {unknown}")
    print(f"settled W{w} L{l} · open {openn} · net R{net:+,.2f}"
          + (f" · ROI {net/staked*100:+.1f}%" if staked else ""))

    byday = {}
    for x in placed:
        d = byday.setdefault(x["ts"][:10], [0, 0.0])
        d[0] += 1
        d[1] += x["pnl"]
    if byday:
        print("\nby day (placed, net, running)")
        run = 0.0
        for d in sorted(byday):
            n, u = byday[d]
            run += u
            print(f"  {d}  {n:3d}  {u:+9.2f}  cum {run:+10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
