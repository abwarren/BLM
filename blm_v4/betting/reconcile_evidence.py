"""SUBMISSION EVIDENCE — did the book's own balance move?

The browser bridge can only ever answer ``SUBMITTED`` when the BET NOW click
left no ticket: that is honest ("sent, unconfirmed") but it is not the answer
the operator needs.  The one book-side channel this system ALREADY reads every
pass is the signed-in account balance, so:

* immediately BEFORE a submit the balance is pinned on the execution row
  (``BettingStore.set_balance_before``);
* a later pass compares the current balance with that pin and labels the
  submission with the evidence it can actually prove:

  ``PLACED_EVIDENCE``      — at least 90 % of the stake left the account;
                             the slip WAS taken (still no bookmaker reference);
  ``NOT_PLACED_EVIDENCE``  — the adjudication window passed and the balance
                             never fell; the book never took the stake.

These are EVIDENCE labels, never a status change.  ``status`` stays
``SUBMITTED`` so ``has_placed_bet()`` keeps blocking a re-bet (a second click on
an unreadable state could place the bet twice), and no stake/accounting path
consults the labels.  Fail-closed throughout: an unreadable balance, a missing
pin or a stake-less row leaves the submission open rather than guessing.

The measurement that motivates it (2026-11-22, prod): execution 8218 staked
R34.01, the balance stayed at R34.01 for 15+ minutes → the bet was NOT placed,
while other SUBMITTED rows showed the exact stake leaving the account → they
were.  The ledger alone could not tell those apart.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

PLACED_EVIDENCE = "PLACED_EVIDENCE"
NOT_PLACED_EVIDENCE = "NOT_PLACED_EVIDENCE"
EVIDENCE_STATES = (PLACED_EVIDENCE, NOT_PLACED_EVIDENCE)

#: share of the stake that must leave the account to call it placed
PLACED_FRACTION = 0.9
#: a no-drop verdict needs this much elapsed time after the submission
ADJUDICATE_AFTER_S = 600.0
#: how far back a submission is still worth adjudicating
LOOKBACK_MINUTES = 60


def _age_s(row: dict, now: datetime) -> Optional[float]:
    raw = row.get("submitted_at_utc") or row.get("requested_at_utc")
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (now - t).total_seconds()


def reconcile_submitted(store, balance_now, *, now: Optional[datetime] = None,
                        adjudicate_after_s: float = ADJUDICATE_AFTER_S,
                        lookback_minutes: int = LOOKBACK_MINUTES) -> dict:
    """Label every open SUBMITTED submission with the balance evidence."""
    now = now or datetime.now(timezone.utc)
    summary = {"checked": 0, "placed": 0, "not_placed": 0, "open": 0,
               "unpinned": 0}
    if isinstance(balance_now, bool) or not isinstance(balance_now, (int, float)):
        return summary                     # fail closed: no evidence, no verdict
    try:
        rows = store.open_submissions(within_minutes=lookback_minutes)
    except Exception:
        return summary
    for row in rows:
        summary["checked"] += 1
        try:
            before = float(row.get("balance_before"))
            stake = float(row.get("stake_amount") or 0)
        except (TypeError, ValueError):
            summary["unpinned"] += 1
            continue
        drop = round(before - float(balance_now), 2)
        age = _age_s(row, now)
        detail = {"balance_before": before,
                  "balance_now": round(float(balance_now), 2),
                  "drop": drop, "stake": stake,
                  "age_s": None if age is None else round(age, 1),
                  "status_at_adjudication": "SUBMITTED",
                  "authority": "account_balance"}
        try:
            if stake > 0 and drop >= stake * PLACED_FRACTION:
                store.mark_submission_evidence(
                    row["execution_id"], PLACED_EVIDENCE,
                    detail=detail, idempotency_key=row.get("idempotency_key"))
                summary["placed"] += 1
            elif (stake > 0 and age is not None
                  and age >= adjudicate_after_s and drop <= 0):
                detail["note"] = ("balance never fell — the book never took "
                                  "the stake")
                store.mark_submission_evidence(
                    row["execution_id"], NOT_PLACED_EVIDENCE,
                    detail=detail, idempotency_key=row.get("idempotency_key"))
                summary["not_placed"] += 1
            else:
                summary["open"] += 1
        except Exception:
            summary["open"] += 1
    return summary
