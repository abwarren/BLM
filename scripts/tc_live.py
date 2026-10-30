#!/usr/bin/env python3
"""tc-live — the LIVE cohort's results, with the daily and units curve.

tc-all answers "what did this gate take" but leaves you to read a 250-row table
and add up the days.  This runs the same gate (tc-all adopts the running
engine's TRADE_* env) and prints the summary, the drawdown and the daily line.

    tc-live                   last 14 days
    tc-live 30                last 30 days
    tc-live 2026-10-01 2026-10-31   explicit range
"""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta

TC_ALL = "/home/ubuntu/blm-fix-price-mapping/scripts/tc_all.py"


def main(argv) -> int:
    if len(argv) == 1:
        days = 14
        until = date.today()
        since = until - timedelta(days=days - 1)
    elif len(argv) == 2 and argv[1].isdigit():
        days = int(argv[1])
        until = date.today()
        since = until - timedelta(days=days - 1)
    elif len(argv) == 3:
        since, until = (date.fromisoformat(argv[1]), date.fromisoformat(argv[2]))
    else:
        print(__doc__, file=sys.stderr)
        return 2

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as fh:
        out = fh.name
    # the gate text: tc-all only prints it in --summary mode, and re-running it
    # just for one line costs a minute.  Importing tc_all performs the same
    # env adoption the engine's drop-in provides, so the labels match.
    gate_text = ""
    try:
        sys.path.insert(0, "/home/ubuntu/blm-fix-price-mapping/scripts")
        import tc_all  # noqa: F401  (import adopts the running TRADE_* env)
        from blm_v4 import trade_window as tw
        gate_text = (f"gate: pace>{tw.TRADE_MIN_PACE_RATIO:.2f} · "
                     f"shortfall>={tw.SHORTFALL_MARGIN:+.2f} · "
                     f"progress {tw.TRADE_MIN_PROGRESS_PCT:.0f}-"
                     f"{tw.TRADE_MAX_PROGRESS_PCT:.0f} · revalidate "
                     f"{'on' if __import__('os').environ.get('BET_REVALIDATE') == '1' else 'off'}")
    except Exception:
        pass

    proc = subprocess.run(
        ["/usr/bin/python3", TC_ALL, since.isoformat(), until.isoformat(),
         "--csv", out], capture_output=True, text=True)
    gate = gate_text
    note = next((ln.strip() for ln in proc.stdout.splitlines()
                 if ln.strip().startswith("note:")), "")

    rows = [r for r in csv.DictReader(open(out)) if r["result"] in ("W", "L")]
    undecided = None
    if note:
        for tok in note.split():
            if tok.isdigit():
                undecided = int(tok)
                break

    n = len(rows)
    if n == 0:
        print(gate)
        print(f"no settled candidates {since} → {until}")
        return 0
    w = sum(1 for r in rows if r["result"] == "W")
    l = n - w
    units = sum(float(r["units"]) for r in rows)
    px = sum(float(r["price"]) for r in rows) / n
    be = 100 / px

    cum = peak = dd = 0.0
    dd_at = ""
    for r in rows:
        cum += float(r["units"])
        if cum > peak:
            peak = cum
        elif peak - cum > dd:
            dd, dd_at = peak - cum, r["ts"][:10]

    byday = defaultdict(lambda: [0, 0, 0, 0.0])
    for r in rows:
        d = byday[r["ts"][:10]]
        d[0] += 1
        d[3] += float(r["units"])
        if r["result"] == "W":
            d[1] += 1
        else:
            d[2] += 1

    trading_days = len(byday)
    print(gate)
    print(f"{since} → {until}   {trading_days} trading days\n")
    print(f"  {n} bets · W{w} L{l} · win% {w / n * 100:.1f} · avg price {px:.3f} "
          f"(break-even {be:.1f}%, edge {w / n * 100 - be:+.1f})")
    print(f"  {units:+.2f}u · ROI {units / n * 100:+.1f}% · {units / trading_days:+.2f}u/day "
          f"· max drawdown {dd:.2f}u ({dd_at})"
          + (f" · {undecided} undecided" if undecided else ""))

    print("\n  day          n    W    L   win%    units      cum")
    run = 0.0
    for d in sorted(byday):
        c, ww, ll, u = byday[d]
        run += u
        dec = ww + ll
        print(f"  {d}  {c:3d}  {ww:3d}  {ll:3d}  {ww / dec * 100 if dec else 0:5.1f}  "
              f"{u:+8.2f}  {run:+9.2f}")

    nw = sum(1 for d in byday if byday[d][3] > 0)
    print(f"\n  {nw} up days · {trading_days - nw} down · "
          f"best {max(v[3] for v in byday.values()):+.2f}u · "
          f"worst {min(v[3] for v in byday.values()):+.2f}u")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
