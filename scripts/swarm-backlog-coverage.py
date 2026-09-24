#!/usr/bin/env python3
"""How much of the UNRESOLVED backlog can the authoritative feed recover?

The directive's final question is "are any legitimately-resultable games
left unresolved?".  A game is legitimately resultable if the feed that backs
the results page holds a result for it — so this asks the feed directly,
for every game production currently cannot resolve.

READ-ONLY on the source DB (mode=ro + query_only, and a write attempt is
proven to fail before any query).  Nothing is written anywhere: the feed is
read, the DB is read, a report is printed.

    python3 scripts/swarm-backlog-coverage.py --date 2026-09-24 [--json out]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blm_v4.swarm_results import SwarmResultsClient  # noqa: E402

DEFAULT_DB = Path("/home/ubuntu/BLM/blm_pokerbet.db")


def load_unresolved(db: Path, day: str) -> list[dict]:
    """Games production cannot yet resolve, for ``day`` (read-only)."""
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA query_only=1")
    try:
        src.execute("CREATE TABLE __probe (x)")
        raise SystemExit("ABORT: source connection is NOT read-only")
    except sqlite3.OperationalError:
        pass
    print(f"source: read-only PROVEN (write refused)  {db}")
    try:
        # cheap first: games + their stored result only (games is ~1e3 rows;
        # per-game snapshot aggregates here would scan the whole table
        # through a correlated CASE/instr and never return on a live DB)
        rows = [dict(r) for r in src.execute(
            """
            SELECT g.source_game_id gid, g.classification,
                   COALESCE(r.final_result_status, '<NO ROW>') status,
                   r.final_home, r.final_away, r.result_source
              FROM games g
              LEFT JOIN game_results r
                ON r.source_game_id = g.source_game_id
             WHERE date(g.last_seen_at) = ?
               AND (r.final_result_status IS NULL
                    OR r.final_result_status != 'OK')
             ORDER BY g.source_game_id""", (day,))]
    finally:
        src.close()
    print(f"unresolved games on {day}: {len(rows)}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--date", default="2026-09-24")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    rows = load_unresolved(Path(args.db), args.date)
    if not rows:
        print("\nRESULT: nothing unresolved — 0 games outstanding")
        return 0

    ids = [r["gid"] for r in rows]
    print(f"asking the feed for all {len(ids)} …")
    with SwarmResultsClient() as client:
        found = client.fetch_results(ids)

    recoverable, absent = [], []
    for r in rows:
        res = found.get(r["gid"])
        (recoverable if res else absent).append((r, res))

    print(f"\n=== COVERAGE on {args.date} ===")
    print(f"  unresolved          : {len(rows)}")
    print(f"  feed HAS a result   : {len(recoverable)}  <-- recoverable now")
    print(f"  feed has NO result  : {len(absent)}")

    why = Counter()
    for r, _ in absent:
        if r["status"] == "<NO ROW>":
            why["no game_results row at all"] += 1
        elif r["status"] == "NEEDS_RECONCILIATION":
            why["stamped NEEDS_RECONCILIATION, feed holds no result yet"] += 1
        else:
            why[f"status={r['status']}, feed holds no result yet"] += 1
    print("\n  absent reasons:")
    for k, v in why.most_common():
        print(f"    {v:5d}  {k}")

    if recoverable:
        print("\n  sample recoverable (feed result vs currently stored):")
        for r, res in recoverable[:8]:
            stored = f"({r['final_home']},{r['final_away']})"
            print(f"    {r['gid']}  {r['classification']:12s} "
                  f"stored={stored:12s} feed={res['home_score']}:"
                  f"{res['away_score']}  {res['home_team'][:22]}")

    out = {"date": args.date, "unresolved": len(rows),
           "recoverable": len(recoverable), "absent": len(absent),
           "absent_reasons": dict(why),
           "recoverable_ids": [r["gid"] for r, _ in recoverable]}
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2))
        print(f"\njson -> {args.json}")
    print(f"\nRESULT: {len(recoverable)}/{len(rows)} unresolved games are "
          f"legitimately resultable from the feed "
          f"({100 * len(recoverable) / len(rows):.0f}%) — each one was a "
          f"game we failed to resolve, not a game without a result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
