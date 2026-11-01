"""Placed-bet result priority — the win ladder's own read path.

The win ladder does not store a rung: it REPLAYS settled outcomes
(``blm_v4/betting/settlement.py``), and an outcome only exists once
``game_results`` carries ``final_result_status='OK'`` for the game the bet was
placed on.  Both result workers select candidates by game recency alone
(``ORDER BY last_seen_at``), so a bet's game waits behind every other unresolved
game in the window.  Measured 2026-11-01 over the last 12 placed bets, game end
-> OK final: 13.1 / 14.4 / 24.4 / 31.7 min on four of them and 515 min / 1,960
min on two, while the next rung could not be known and the engine re-derived
rung 1 in the meantime.

This module answers ONE question, read-only and cheaply: which games carry a
PLACED bet that is still waiting on an authoritative result?  The settle worker
and the result reconciler sort those ids FIRST in their candidate query, so a
bet's game is reconciled on the next pass instead of behind the backlog.

It is a PRIORITY, never a filter.  The candidate predicate, the quality gate,
the upsert, the batch bound and every settlement rule are untouched, and an
empty answer (no ledger, unreadable ledger, no recent placed bets) reproduces
the previous ordering exactly.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

#: A bet placed longer ago than this cannot be waiting on a live rung: the
#: ladder's own replay is unit-scoped and outcome-ordered, and an unresolved bet
#: from an older session is the general backlog's job.  Bounded so one ancient
#: unresolved row can never seize the priority slot forever.
DEFAULT_LOOKBACK_H = 24.0

#: The id list goes into an ``IN (...)`` clause — keep it small and fixed.
DEFAULT_LIMIT = 32

#: Statuses that mean "a real position exists at the bookmaker, awaiting a
#: result".  The ledger never writes SETTLED/PUSHED (documented in
#: betting/settlement.py), so these two are the whole placed set in practice.
_AWAITING_STATUSES = ("SUBMITTED", "ACCEPTED")


def _awaiting_statuses() -> Sequence[str]:
    """The ONE definition of "placed", when the settlement authority is
    importable; the local pair otherwise (identical values)."""
    try:
        from blm_v4.betting.settlement import PLACED_STATUSES
        return tuple(PLACED_STATUSES)
    except Exception:            # pragma: no cover - import-path edge
        return _AWAITING_STATUSES


def pending_bet_game_ids(
        betting_db_path: Optional[Path | str], *,
        lookback_h: float = DEFAULT_LOOKBACK_H,
        limit: int = DEFAULT_LIMIT) -> tuple[str, ...]:
    """Game ids holding a PLACED bet placed within ``lookback_h`` hours.

    Newest bet first, deduped, capped at ``limit``.  NEVER raises: a missing,
    locked or schema-less ledger returns ``()``, which makes every caller's
    ordering identical to the pre-priority behaviour (fail-soft, because a
    priority hint must never be able to break the settlement path it serves).
    """
    if not betting_db_path or float(lookback_h) <= 0:
        return ()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(lookback_h))
              ).strftime("%Y-%m-%dT%H:%M:%S")
    statuses = tuple(_awaiting_statuses())
    placeholders = ",".join("?" * len(statuses))
    try:
        conn = sqlite3.connect(
            "file:%s?mode=ro" % str(betting_db_path), uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT game_id FROM bet_executions"
                " WHERE status IN (%s)"
                "   AND COALESCE(requested_at_utc, created_at) >= ?"
                " GROUP BY game_id"
                " ORDER BY MAX(id) DESC"
                " LIMIT ?" % placeholders,
                (*statuses, cutoff, max(1, int(limit)))).fetchall()
        finally:
            conn.close()
    except Exception as exc:          # fail-soft by contract
        logger.debug("bet_result_priority_unavailable path=%s err=%s",
                     betting_db_path, exc)
        return ()
    seen: dict[str, None] = {}
    for (gid,) in rows:
        if gid:
            seen.setdefault(str(gid), None)
    return tuple(seen)


def priority_order_sql(id_column: str, order_by: str,
                       priority_ids: Sequence[str]) -> tuple[str, list]:
    """``(<order by clause>, params)`` that sorts ``id_column IN (ids)`` first.

    ``order_by`` is the caller's existing ORDER BY expression and is preserved
    verbatim as the tie-break, so with an empty id set the clause returned is the
    previous one — byte-identical, no params.
    """
    ids = tuple(dict.fromkeys(str(g) for g in (priority_ids or ()) if g))
    if not ids:
        return order_by, []
    return ("CASE WHEN %s IN (%s) THEN 0 ELSE 1 END, %s"
            % (id_column, ",".join("?" * len(ids)), order_by), list(ids))
