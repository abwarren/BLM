"""Settlement feed for the bet LADDER.

The ledger does not settle: `bet_executions` stops at SUBMITTED/ACCEPTED and
nothing ever stamps WON/LOST (checked 2026-10-30: 297 SUBMITTED, 0 SETTLED).
A win-driven ladder therefore has to derive the outcome instead of waiting for
a bookmaker callback that never arrives.

The rule is the same one `scripts/bet_all.py` and the reconciler already use —
the render's final total against the line the bet was taken at, and only when
`game_results.final_result_status = 'OK'`.  Anything else (UNKNOWN, INVALID,
NEEDS_RECONCILIATION, missing) is NOT settled and yields no outcome, so the
ladder holds its rung rather than inventing a win.

Chronology is by `submitted_at_utc`, which is the order the stakes were placed.
Only bets that actually reached the provider count: a FAILED row has no
position, so it cannot move the ladder either way.
"""
from __future__ import annotations

import os
import sqlite3
from typing import List, Optional

from . import ladder as _ladder

#: statuses that represent a real position at the bookmaker
PLACED_STATUSES = ("SUBMITTED", "ACCEPTED", "SETTLED", "PUSHED")

PROD_DB_DEFAULT = "/home/ubuntu/BLM/blm_pokerbet.db"


def _prod_path(prod_db: Optional[str] = None) -> str:
    return prod_db or os.environ.get("BLM_PROD_DB") or PROD_DB_DEFAULT


def settled_outcomes(betting_db: str, prod_db: Optional[str] = None,
                     limit: int = 200,
                     unit_price: Optional[float] = None) -> List[str]:
    """Outcome of each placed bet, oldest first ("WON"/"LOST"/"PUSH").

    `limit` bounds the replay (the rung only needs enough history to reach the
    last non-win; 200 is far beyond any permitted depth).

    `unit_price` scopes the replay to bets staked at the CURRENT unit.  This is
    what keeps the ladder self-funding: its whole premise is that rungs 1-3 won
    the money that funds rung 4, which is only true if those rungs were staked
    at the same unit.  Carrying rungs across a unit change (R20 -> R115) makes
    the ladder jump from R20 bets to a R670 bet with none of the winnings that
    were supposed to pay for it — measured 2026-10-30: three R20 wins worth
    R46.40 were being counted as rungs 1-3 of a R115 ladder.  A unit change
    therefore restarts the progression at rung 1, which is the safe direction.
    """
    prod = _prod_path(prod_db)
    try:
        conn = sqlite3.connect(f"file:{betting_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"ATTACH DATABASE 'file:{prod}?mode=ro' AS p")
        except sqlite3.Error:
            return []
        rows = conn.execute(
            """
            SELECT e.id, e.market, e.selection, e.triggered_line, e.submitted_at_utc,
                   r.final_total, r.final_result_status
              FROM bet_executions e
              LEFT JOIN p.game_results r ON r.source_game_id = e.game_id
             WHERE e.status IN ({ph})
               AND e.triggered_line IS NOT NULL
               {unit_clause}
             ORDER BY COALESCE(e.submitted_at_utc, e.requested_at_utc) ASC, e.id ASC
            """.format(ph=",".join("?" * len(PLACED_STATUSES)),
                       unit_clause=("AND ROUND(e.unit_price, 2) = ROUND(?, 2)"
                                    if unit_price is not None else "")),
            (*PLACED_STATUSES, *((unit_price,) if unit_price is not None else ())),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out: List[str] = []
    for r in rows:
        if (r["final_result_status"] or "") != "OK":
            continue
        outcome = _ladder.settle_outcome(
            r["market"], r["selection"], r["triggered_line"], r["final_total"])
        if outcome is not None:
            out.append(outcome)
    return out[-limit:]


def current_rung(betting_db: str, depth_value, prod_db: Optional[str] = None,
                 unit_price: Optional[float] = None) -> int:
    """The rung the NEXT bet should be staked at.

    1 when the ladder is off, when no history is readable, or when the newest
    settled bet won (a win at the selected depth banks to rung 1).
    """
    d = _ladder.depth(depth_value)
    if d == 0:
        return 1
    try:
        return _ladder.rung_from_history(
            settled_outcomes(betting_db, prod_db, unit_price=unit_price), d)
    except Exception:
        # never stake a ladder rung we cannot justify: rung 1 is the minimum
        return 1
