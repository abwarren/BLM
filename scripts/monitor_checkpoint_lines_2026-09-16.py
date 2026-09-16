"""CHECKPOINT-LINE MONITOR — verify frozen checkpoint lines use the
price-selected market line (read-only, re-runnable).

Background: the market-selection fix (commits d9f4458, 99b496a) went
live at the 2026-09-16 22:21:49Z restart of blm-server/blm-collector.
The scorecard's checkpoint_market rows freeze `live_market_line` from
the at-or-before snapshot/WS evidence.  This script classifies every
checkpoint row RECORDED after the cutover:

  policy-current : checkpoint happened after the cutover AND the frozen
                   line equals the price-selector's pick from the
                   at-or-before evidence (snapshot line, else WS batch
                   selector)  -> the fix is working.
  legacy-captured: checkpoint happened before the cutover (data captured
                   under the old positional policy; rows are historical
                   by definition, never failures).
  MISMATCH       : post-cutover checkpoint whose frozen line equals
                   neither the snapshot pick nor the WS-batch selector
                   pick  -> investigate.

Usage:  python3 scripts/monitor_checkpoint_lines_2026-09-16.py [limit]
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")  # run from BLM/

from blm_v4.event_parser import select_total_market

DEPLOY_CUT = "2026-09-16T22:21:49"   # collector+parser restart (UTC)
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot_line_at(conn, gid: str, at_ts: str):
    """Latest line-bearing snapshot at-or-before at_ts -> (line, ts)."""
    r = conn.execute(
        """SELECT total_line, captured_at FROM snapshots
           WHERE source_game_id=? AND total_line IS NOT NULL
             AND captured_at <= ?
           ORDER BY captured_at DESC LIMIT 1""", (gid, at_ts)).fetchone()
    return (float(r["total_line"]), r["captured_at"]) if r else (None, None)


def ws_selected_line(conn, gid: str, at_ts: str):
    """Selector pick over the latest WS batch at-or-before at_ts."""
    rows = conn.execute(
        """SELECT line_value, over_price, under_price, captured_at
           FROM market_observations
           WHERE source_game_id=? AND market_type='MatchTotal'
             AND line_value IS NOT NULL AND captured_at <= ?
             AND captured_at = (
                 SELECT MAX(captured_at) FROM market_observations
                 WHERE source_game_id=? AND market_type='MatchTotal'
                   AND line_value IS NOT NULL AND captured_at <= ?)
           ORDER BY line_value ASC""",
        (gid, at_ts, gid, at_ts)).fetchall()
    if not rows:
        return None
    sel = select_total_market([
        {"line": r["line_value"], "over": r["over_price"],
         "under": r["under_price"]} for r in rows])
    return sel["line"]


def main() -> int:
    conn = _ro("blm_pokerbet.db")
    try:
        rows = conn.execute(
            """SELECT source_game_id, checkpoint_pct, checkpoint_timestamp,
                      live_market_line, recorded_at
               FROM checkpoint_market
               WHERE recorded_at >= ?
               ORDER BY recorded_at DESC LIMIT ?""",
            (DEPLOY_CUT, LIMIT)).fetchall()
    finally:
        conn.close()

    print(f"checkpoint rows recorded since cutover {DEPLOY_CUT}: {len(rows)}")
    if not rows:
        print("nothing recorded yet — re-run after the scorecard run completes")
        return 0

    verdicts: Counter = Counter()
    mismatches = []
    conn = _ro("blm_pokerbet.db")
    try:
        for row in rows:
            gid = row["source_game_id"]
            ck = row["checkpoint_timestamp"]
            line = row["live_market_line"]
            post = (ck or "") >= DEPLOY_CUT
            snap_line, _ = snapshot_line_at(conn, gid, ck)
            if snap_line is not None:
                expect = snap_line
            else:
                expect = ws_selected_line(conn, gid, ck)
            if not post:
                verdicts["legacy-captured"] += 1
            elif line is None and expect is None:
                verdicts["no-line-both-sides"] += 1
            elif line is not None and expect is not None and \
                    abs(float(line) - float(expect)) < 1e-9:
                verdicts["policy-current"] += 1
            else:
                verdicts["MISMATCH"] += 1
                if len(mismatches) < 10:
                    mismatches.append(
                        f"  {gid} {row['checkpoint_pct']} ck={ck} "
                        f"frozen={line} expected={expect}")
    finally:
        conn.close()

    print("verdicts:", dict(verdicts))
    for m in mismatches:
        print(m)
    failed = verdicts["MISMATCH"] > 0
    print("RESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
