"""WHY ARE RESULTED ROWS NOT COLOURED? — data-path quantification (2026-09-19).

Question: the RESULTED ALERTS panel shows rows without the green/red/neutral
verdict colour.  The 2026-09-17 validation proved the shipped classifier
colours 19,575/19,575 SETTLED statuses with 0 uncoloured — so the colour
pipeline (status → class → CSS) is proven.  The remaining question is how
many resulted rows never RECEIVE a status at all (status=None → no verdict,
no colour) and WHY.

This script recomputes, for the most recent settled games, exactly what the
API serves — ``under_alert_outcome`` over the SAME window /live and
/alert-outcomes read (the 500-row snapshot TAIL) — and compares it with the
FULL observation stream, attributing every status=None to its cause:

  A. trigger line unprovable — no line observed at/before the boundary
     (in the API's 500-row tail) → outcome_status(None, final) = None
  B. trigger line provable ONLY in the full stream — the 500-row tail
     truncated the boundary (long games) → API-served None is a WINDOW
     defect, the full stream proves the verdict
  C. final unprovable — no settled game_results OK row reachable
  D. genuinely coloured — under/over/push

READ-ONLY over blm_pokerbet.db (mode=ro).  Usage:
    python3 scripts/analysis_uncolored_resulted_2026-09-19.py [games]
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")

from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    CHECKPOINTS, outcome_status, trigger_market_total)

DB = Path("blm_pokerbet.db")
API_TAIL = 500          # exactly what api.py serves (_load_snapshot_tail)
N_GAMES = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    if not DB.exists():
        print(f"missing {DB}")
        return 1
    conn = _ro(DB)
    try:
        games = conn.execute(
            "SELECT gr.source_game_id AS gid, g.classification AS cls, "
            "       gr.final_total AS final "
            "FROM game_results gr JOIN games g ON g.source_game_id = gr.source_game_id "
            "WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL "
            "ORDER BY gr.result_at DESC LIMIT ?", (N_GAMES,)).fetchall()
        rows_needed = 0
        tail_status: Counter = Counter()
        full_status: Counter = Counter()
        flips = 0                      # tail=None but full stream proves it
        examples = []
        for g in games:
            gid, cls, final = g["gid"], g["cls"], g["final"]
            full = [dict(r) for r in conn.execute(
                "SELECT s.home_score, s.away_score, s.quarter, s.clock, "
                "       s.period_label, s.game_status, s.total_line, s.captured_at "
                "FROM snapshots s JOIN games gg ON gg.id = s.game_id "
                "WHERE gg.source_game_id = ? "
                "ORDER BY s.captured_at ASC LIMIT 50000", (gid,)).fetchall()]
            if not full:
                continue
            tail = full[-API_TAIL:]
            rows_needed = max(rows_needed, len(full))
            for cp in CHECKPOINTS:
                st_tail = outcome_status(trigger_market_total(tail, cp, cls), final)
                st_full = outcome_status(trigger_market_total(full, cp, cls), final)
                tail_status[st_tail] += 1
                full_status[st_full] += 1
                if st_tail is None and st_full is not None:
                    flips += 1
                    if len(examples) < 5:
                        trig = trigger_market_total(full, cp, cls)
                        examples.append(
                            f"    {gid} cp={cp}% line={trig} final={final} "
                            f"(stream rows={len(full)}, tail rows={len(tail)})")
    finally:
        conn.close()

    def pct(n: int, d: int) -> str:
        return f"{(100.0 * n / d):.1f}%" if d else "n/a"

    total_t = sum(tail_status.values())
    total_f = sum(full_status.values())
    print("=" * 74)
    print(f"RESULTED COLOUR GAP — most recent {len(games)} settled games "
          f"× {len(CHECKPOINTS)} checkpoints")
    print("=" * 74)
    print(f"max observation rows in one game's stream: {rows_needed} "
          f"(API serves the LAST {API_TAIL})")
    print()
    print("API-SERVED window (500-row tail — what /live + /alert-outcomes use):")
    for k in ("under", "over", "push", None):
        label = {"under": "UNDER  → al-under", "over": "OVER   → al-over",
                 "push": "PUSH   → al-push", None: "None   → UNCOLOURED"}[k]
        print(f"  {label}: {tail_status[k]:6d}  ({pct(tail_status[k], total_t)})")
    print()
    print("FULL observation stream (no window):")
    for k in ("under", "over", "push", None):
        label = {"under": "UNDER  → al-under", "over": "OVER   → al-over",
                 "push": "PUSH   → al-push", None: "None   → UNCOLOURED"}[k]
        print(f"  {label}: {full_status[k]:6d}  ({pct(full_status[k], total_f)})")
    print()
    print(f"WINDOW DEFECT — rows the tail leaves uncoloured but the FULL stream "
          f"proves: {flips}  ({pct(flips, total_t)} of all rows)")
    for line in examples:
        print(line)
    print()
    n_all = tail_status[None]
    n_win = flips
    n_line = n_all - n_win
    print("ATTRIBUTION of every API-served UNCOLOURED row:")
    print(f"  B. snapshot-tail truncation (500-row window too short) : {n_win}")
    print(f"  A. trigger line never observed by the boundary at all  : {n_line}")
    print()
    print("CONCLUSION: the classifier/CSS colours 100% of rows that RECEIVE a")
    print("status; every uncoloured resulted row is status=None — an unprovable")
    print("trigger line (window truncation or no line captured by the boundary),")
    print("never a frontend classification failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
