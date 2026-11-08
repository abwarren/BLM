"""UNKNOWN-submission RECONCILIATION (directive Phase Nine).

An execution whose bookmaker outcome could not be determined (timeout,
connection drop, ambiguous response) is UNKNOWN.  Money MAY be in play.
This worker exists to establish the truth from the bookmaker — never to
place another bet:

  * it NEVER submits, clicks, or re-runs the executor;
  * it never assumes a missing answer means "no bet": without positive
    evidence the record STAYS RECONCILING and its exposure keeps counting;
  * it only advances to ACCEPTED when the provider returns the order the
    idempotency key produced, carrying the bookmaker's own reference;
  * every pass is audited on the execution record.

The provider lookup is optional (`find_order_by_idempotency`).  A
provider that cannot answer leaves the records untouched (and reported
as unresolved) rather than guessing.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Optional

from blm_v4.betting.store import BettingStore


class BettingReconciler:
    """Reconciles UNKNOWN executions against the bookmaker's own ledger."""

    def __init__(self, store: BettingStore, provider,
                 *, poll_interval_s: float = 30.0):
        self.store = store
        self.provider = provider
        self.poll_interval_s = float(poll_interval_s)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="blm-betting-reconciler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                self.reconcile_once()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 — never dies silent
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                traceback.print_exc(limit=2)

    # ── one pass ───────────────────────────────────────────────────────
    def reconcile_once(self, limit: int = 50) -> dict:
        rows = self.store.needs_reconciliation(limit=limit)
        lookup = getattr(self.provider, "find_order_by_idempotency", None)
        summary = {"seen": len(rows), "confirmed": 0, "unresolved": 0,
                   "lookup_available": callable(lookup)}
        if not callable(lookup):
            for row in rows:
                self.store.mark_reconciliation(
                    row["execution_id"], "LOOKUP_UNAVAILABLE",
                    note="provider cannot report orders; UNKNOWN retained")
                summary["unresolved"] += 1
            return summary
        for row in rows:
            execution_id = row["execution_id"]
            key = row.get("idempotency_key")
            try:
                order = lookup(key)
            except Exception as e:  # noqa: BLE001 — unverifiable, stay UNKNOWN
                self.store.mark_reconciliation(
                    execution_id, "LOOKUP_FAILED",
                    note=f"reconciliation lookup failed: {type(e).__name__}")
                summary["unresolved"] += 1
                continue
            if order is None:
                # NO positive evidence.  Absence of an answer is not proof
                # that no bet was placed (the response may have been lost
                # after the bookmaker accepted it).
                self.store.mark_reconciliation(
                    execution_id, "NO_ORDER_FOUND",
                    note="no order returned for this key; UNKNOWN retained")
                self.store.audit(
                    "reconcile", idempotency_key=key,
                    execution_id=execution_id,
                    reason="no order found — UNKNOWN retained, no retry",
                    details={"result": "UNRESOLVED"})
                summary["unresolved"] += 1
                continue
            ref = order.get("provider_ref") or order.get("bet_id")
            if not ref:
                self.store.mark_reconciliation(
                    execution_id, "ORDER_WITHOUT_REFERENCE",
                    note="order found but no bookmaker reference")
                summary["unresolved"] += 1
                continue
            self.store.update_status(
                execution_id, "ACCEPTED", provider_ref=str(ref),
                execution_state="RECONCILED")
            self.store.mark_reconciliation(execution_id, "CONFIRMED",
                                           note=f"reconciled to {ref}")
            self.store.audit(
                "reconcile", idempotency_key=key, execution_id=execution_id,
                reason=f"confirmed by bookmaker order {ref}",
                details={"result": "CONFIRMED", "provider_ref": ref})
            summary["confirmed"] += 1
        return summary
