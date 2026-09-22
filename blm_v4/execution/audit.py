"""BLM EXECUTION — AUDIT LOG.

Every state transition, resolution, click, verification and placement
is recorded.  The directive's example log line is the shape:

    16:42:02 RESOLVING Game A TOTAL UNDER
    16:42:02 FOUND UNDER 164.5 @ 1.90
    16:42:04 LEG_CONFIRMED

The store writes these to ``execution_audit`` (credential-free — this
layer holds no credentials by construction).  Best-effort: an audit
failure must never break an execution decision path.
"""
from __future__ import annotations

from typing import Optional


class ExecutionAudit:
    """Thin facade over the store's audit table."""

    def __init__(self, sink=None) -> None:
        """``sink(event, parlay_id, leg_index, reason, details)`` is the
        store's ``audit`` method; ``None`` makes a no-op auditor (used
        by pure-engine unit tests)."""
        self._sink = sink

    def log(self, event: str, *, parlay_id: Optional[str] = None,
            leg_index: Optional[int] = None, reason: Optional[str] = None,
            details: Optional[dict] = None) -> None:
        if self._sink is None:
            return
        try:
            self._sink(event, parlay_id=parlay_id, leg_index=leg_index,
                       reason=reason, details=details or {})
        except Exception:
            pass  # best-effort by contract

    # ── convenience names matching the directive's log vocabulary ──
    def state(self, state: str, parlay_id: str,
              leg_index: Optional[int] = None,
              reason: Optional[str] = None) -> None:
        self.log(state, parlay_id=parlay_id, leg_index=leg_index,
                 reason=reason)

    def resolved(self, parlay_id: str, leg_index: int, event: str,
                 position: str, line, price) -> None:
        self.log("FOUND", parlay_id=parlay_id, leg_index=leg_index,
                 reason=f"{event} {position} line={line} price={price}")

    def leg_confirmed(self, parlay_id: str, leg_index: int,
                      line, price, attempt: int) -> None:
        self.log("LEG_CONFIRMED", parlay_id=parlay_id,
                 leg_index=leg_index,
                 details={"line": line, "price": price,
                          "attempt": attempt})
