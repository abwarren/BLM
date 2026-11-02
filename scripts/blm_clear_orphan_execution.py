#!/usr/bin/env python3
"""Clear orphaned (non-terminal) bet executions — DRY RUN by default.

An execution row is written before the provider call, so a process killed
between claim and result leaves it in SUBMITTING / PENDING / RECONCILING
forever.  Such a row is NOT "a bet in flight" — the process that owned it is
gone — and it inflates the daily exposure count and the ledger's census.

This tool:
  1. backs the database up (``.bak-orphanfix-<utc>``),
  2. flips each matching row to UNKNOWN / ORPHANED_BY_RESTART in ONE
     transaction,
  3. writes the matching ``bet_audit`` result row so provenance survives.

It never re-opens a burned claim (the claim has no unclaim): a cleared row can
only ever be verified against the bookmaker's own history.

    python3 scripts/blm_clear_orphan_execution.py            # dry run
    python3 scripts/blm_clear_orphan_execution.py --apply
    python3 scripts/blm_clear_orphan_execution.py --apply --older-than 2026-11-02T00:00:00
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

NONTERMINAL = ("SUBMITTING", "PENDING", "RECONCILING")
DEFAULT_DB = os.environ.get("BLM_BETTING_DB", "/home/ubuntu/BLM/blm_betting.db")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default: dry run)")
    ap.add_argument("--older-than", default=None,
                    help="only rows created before this ISO/UTC stamp "
                         "(default: every non-terminal row)")
    ap.add_argument("--error-code", default="ORPHANED_BY_RESTART")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"no database at {args.db}")
        return 2

    marks = ",".join("?" * len(NONTERMINAL))
    where = f"status IN ({marks})"
    params: list = list(NONTERMINAL)
    if args.older_than:
        where += " AND created_at < ?"
        params.append(args.older_than)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""SELECT id, execution_id, idempotency_key, game_id, status,
                   stake_amount, price, created_at
              FROM bet_executions WHERE {where} ORDER BY created_at""",
        params).fetchall()
    con.close()

    print(f"db: {args.db}")
    print(f"non-terminal rows matching: {len(rows)}")
    for r in rows:
        print(f"  id={r['id']} {r['created_at']} {r['status']:<12} "
              f"game={r['game_id']} stake={r['stake_amount']} "
              f"exec={r['execution_id']}")

    if not rows:
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing written.  Re-run with --apply to clear.")
        return 0

    backup = f"{args.db}.bak-orphanfix-{_utc_stamp()}"
    shutil.copy2(args.db, backup)
    print(f"\nbackup: {backup}")

    now = _now()
    con = sqlite3.connect(args.db, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        changed = 0
        for r in rows:
            cur = con.execute(
                """UPDATE bet_executions
                      SET status='UNKNOWN', execution_state='UNKNOWN',
                          error_code=?, error_message=?, resolved_at_utc=?
                    WHERE id=? AND status=?""",
                (args.error_code,
                 "cleared by operator tool: process ended before a result "
                 "was recorded; verify against the bookmaker history",
                 now, r["id"], r["status"]))
            if cur.rowcount == 0:
                continue
            con.execute(
                """INSERT INTO bet_audit
                   (execution_id, idempotency_key, event, reason, details)
                   VALUES (?,?,?,?,?)""",
                (r["execution_id"], r["idempotency_key"], "result",
                 "orphan cleared by scripts/blm_clear_orphan_execution.py",
                 f'{{"result": "UNKNOWN", "guard": "orphan_clear", '
                 f'"previous_status": "{r["status"]}", '
                 f'"error_code": "{args.error_code}"}}'))
            changed += 1
        con.commit()
        print(f"cleared: {changed}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
