#!/usr/bin/env python3
"""
Result-reconciliation AUDIT (directive 2026-09-24, final step).

Answers, read-only, the directive's closing question:

    "Audit that there are zero legitimately resultable games left
     unresolved."

For every game collected on a given day (default: today, UTC) it reports
game_id, league, teams, collected_at, quarter observations, the 50% / 75%
checkpoint rows, and the final result — then classifies every game that
does NOT hold a valid final:

  RESULTABLE_UNRESOLVED  a page render exists and PASSES validation but is
                         not persisted -> GATE FAILURE (must be zero)
  NOT_YET_ATTEMPTED      no reconciliation attempt recorded yet
  RETRYABLE              last attempt FAILED/rejected, attempts left
  NOT_RESULTABLE         the results page has no result for this game
                         (filters-only template / exhausted attempts)
  EXCLUDED_INVALID       the quality gate marked the game INVALID (excluded
                         from statistics by design, never a "final")

READ-ONLY BY CONSTRUCTION: opens ``mode=ro`` + ``PRAGMA query_only=1`` and
PROVES it by requiring an attempted write to be refused.  It never repairs,
never writes, never deletes.

Usage:
    python3 scripts/result-reconciliation-audit.py                # today
    python3 scripts/result-reconciliation-audit.py --date 2026-09-24
    python3 scripts/result-reconciliation-audit.py --json out.json
    python3 scripts/result-reconciliation-audit.py --all-time
Exit code: 0 when zero RESULTABLE_UNRESOLVED (and zero GATE failures),
1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blm_v4 import result_policy as policy          # noqa: E402
from blm_v4.result_reconciler import STATUS_NEEDS_RECONCILIATION  # noqa: E402

DEFAULT_DB = ROOT / "blm_pokerbet.db"

RETRYABLE_OUTCOMES = ("FAILED_ATTEMPT", "REJECTED", "CONFLICT")


def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=180000")
    try:
        conn.execute("CREATE TABLE __audit_probe (x)")
        raise SystemExit("ABORT: the connection is NOT read-only")
    except sqlite3.OperationalError:
        pass                                     # refused, as required
    return conn


def day_bounds(day: str | None) -> tuple[str, str, str]:
    if day:
        lo = f"{day}T00:00"
        hi = f"{day}T23:59:59.999999Z"
        return day, lo, hi
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return today, f"{today}T00:00", f"{today}T23:59:59.999999Z"


def _quarter_profile(conn, gid: str) -> dict:
    """Quarter observations + the observed state range for one game."""
    rows = conn.execute(
        """SELECT COUNT(*) n, MIN(captured_at) first_at,
                  MAX(captured_at) last_at,
                  MAX(COALESCE(home_score,0)+COALESCE(away_score,0)) max_total,
                  MAX(home_score) max_home, MAX(away_score) max_away,
                  MAX(COALESCE(quarter, 0)) max_quarter
           FROM snapshots WHERE source_game_id = ?""", (gid,)).fetchone()
    per_q = {int(r["q"]): r["n"] for r in conn.execute(
        """SELECT COALESCE(quarter, 0) q, COUNT(*) n FROM snapshots
           WHERE source_game_id = ? GROUP BY 1 ORDER BY 1""", (gid,))}
    return {
        "snapshots": rows["n"],
        "observed_first_at": rows["first_at"],
        "observed_last_at": rows["last_at"],
        "observed_max_total": rows["max_total"],
        "observed_max_home": rows["max_home"],
        "observed_max_away": rows["max_away"],
        "observed_max_quarter": rows["max_quarter"],
        "snapshots_per_quarter": per_q,
    }


def _checkpoints(conn, gid: str) -> dict:
    """The frozen checkpoint rows (50% / 75% are the directive's two)."""
    out: dict[str, dict] = {}
    for r in conn.execute(
            """SELECT checkpoint_pct, quarter, progress, elapsed_minutes,
                      live_market_line, blm_fair_value, checkpoint_timestamp
               FROM checkpoint_market WHERE source_game_id = ?
               ORDER BY checkpoint_pct""", (gid,)):
        pct = int(r["checkpoint_pct"])
        out[f"pct{pct}"] = dict(r)
    return out


def classify(conn, game: sqlite3.Row, page_row: sqlite3.Row | None,
             state_row: sqlite3.Row | None, max_attempts: int = 3) -> tuple[str, str]:
    """(status, reason) for one game that does NOT hold a valid final."""
    if page_row is not None:
        checks = json.loads(page_row["checks_json"] or "{}")
        if page_row["outcome"] == "VERIFIED":
            hist = _quarter_profile(conn, game["source_game_id"])
            rows = [{"home_score": hist["observed_max_home"],
                     "away_score": hist["observed_max_away"]}]
            parsed = {
                "home_score": page_row["final_home"],
                "away_score": page_row["final_away"],
                "quarter_scores": json.loads(page_row["quarter_scores"] or "[]"),
                "parse_quality": checks.get("parse_quality"),
            }
            verdict = policy.validate_page_result(parsed, rows)
            if verdict["passed"]:
                return ("RESULTABLE_UNRESOLVED",
                        "a validated page result exists but is not persisted "
                        "(gate failure)")
            return ("NOT_RESULTABLE",
                    "page result fails validation: "
                    + "; ".join(verdict["failures"]))
        if page_row["outcome"] == "TEMPLATE_FAILED":
            return ("NOT_RESULTABLE",
                    "the results page renders the filters-only template")
    if state_row is not None and state_row["outcome"] in RETRYABLE_OUTCOMES:
        a = state_row["attempt"] or 0
        if a < max_attempts:
            return ("RETRYABLE",
                    f"last attempt {state_row['outcome']} "
                    f"(attempt {a}/{max_attempts}) — will retry")
        return ("NOT_RESULTABLE",
                f"attempts exhausted ({state_row['outcome']} × {a})")
    return ("NOT_YET_ATTEMPTED", "no reconciliation attempt recorded yet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--date", default=None,
                    help="UTC day (YYYY-MM-DD); default today")
    ap.add_argument("--all-time", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    conn = connect_ro(Path(args.db))
    try:
        day, lo, hi = day_bounds(args.date)
        where = "" if args.all_time else (
            "WHERE g.last_seen_at >= ? AND g.last_seen_at <= ?")
        params: list = [] if args.all_time else [lo, hi]

        games = conn.execute(
            f"""SELECT g.source_game_id, g.classification, g.home_team,
                       g.away_team, g.status, g.first_seen_at,
                       g.last_seen_at,
                       COALESCE(r.final_result_status, '<NO ROW>') AS result_status,
                       r.final_home, r.final_away, r.final_total,
                       r.result_at, r.result_source
                FROM games g
                LEFT JOIN game_results r
                       ON r.source_game_id = g.source_game_id
                {where}
                ORDER BY g.last_seen_at DESC""", params).fetchall()

        unresolved = [g for g in games if g["result_status"] != "OK"]
        rows_out = []
        buckets: dict[str, int] = {}
        gate_failures = 0
        for g in unresolved:
            gid = g["source_game_id"]
            page = conn.execute(
                """SELECT * FROM result_reconciliation
                   WHERE source_game_id = ?
                   ORDER BY attempt DESC LIMIT 1""", (gid,)).fetchone()
            state = conn.execute(
                """SELECT * FROM result_reconciliation_state
                   WHERE source_game_id = ?""", (gid,)).fetchone()
            status, reason = classify(conn, g, page, state)
            buckets[status] = buckets.get(status, 0) + 1
            if status == "RESULTABLE_UNRESOLVED":
                gate_failures += 1
            rows_out.append({
                "game_id": gid,
                "league": g["classification"],
                "home_team": g["home_team"], "away_team": g["away_team"],
                "game_status": g["status"],
                "collected_at": g["first_seen_at"],
                "last_seen_at": g["last_seen_at"],
                "result_status": g["result_status"],
                "result_source": g["result_source"],
                "last_attempt_outcome": page["outcome"] if page else None,
                "last_attempt_at": page["fetched_at"] if page else None,
                "attempts": state["attempt"] if state else None,
                **_quarter_profile(conn, gid),
                "checkpoints": _checkpoints(conn, gid),
                "audit_status": status,
                "audit_reason": reason,
            })

        verified = sum(1 for g in games if g["result_status"] == "OK")
        breakdown = {r["result_status"]: r["n"] for r in conn.execute(
            f"""SELECT COALESCE(r.final_result_status, '<NO ROW>')
                       AS result_status, COUNT(*) n
                FROM games g LEFT JOIN game_results r
                       ON r.source_game_id = g.source_game_id
                {where} GROUP BY 1""", params)}

        print(f"RESULT RECONCILIATION AUDIT — {'ALL TIME' if args.all_time else day} "
              f"(read-only)")
        print(f"  database            : {args.db}")
        print(f"  games collected     : {len(games)}")
        print(f"  valid final (OK)    : {verified}")
        print(f"  unresolved          : {len(unresolved)}")
        for k in sorted(buckets):
            print(f"      {k:22s}: {buckets[k]}")
        print(f"  result_status mix   : {breakdown}")
        print(f"  GATE FAILURES (resultable but unresolved): {gate_failures}")
        print(f"  verdict             : "
              f"{'PASS — zero legitimately resultable games unresolved' if gate_failures == 0 else 'FAIL — reconciliation required'}")
        for r in rows_out[: args.limit]:
            print(f"\n  [{r['audit_status']}] {r['game_id']} {r['league']} "
                  f"{r['home_team']} v {r['away_team']}")
            print(f"      collected_at={r['collected_at']} status={r['result_status']} "
                  f"snaps={r['snapshots']} max_total={r['observed_max_total']} "
                  f"max_q={r['observed_max_quarter']}")
            print(f"      quarters={r['snapshots_per_quarter']} "
                  f"checkpoints={sorted(r['checkpoints'])}")
            print(f"      reason: {r['audit_reason']}")

        if args.json:
            Path(args.json).write_text(json.dumps({
                "day": day, "all_time": args.all_time,
                "games": len(games), "verified": verified,
                "unresolved": len(unresolved), "buckets": buckets,
                "gate_failures": gate_failures, "games_detail": rows_out,
            }, indent=1, default=str))
            print(f"\n  json -> {args.json}")
        return 1 if gate_failures else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
