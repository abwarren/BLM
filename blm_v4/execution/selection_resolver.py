"""BLM EXECUTION — SELECTION RESOLVER (generic).

Resolves a selection identity (event + market + position) against the
adapter's CURRENT bookmaker state.  This is the heart of the directive's
critical design:

    USER SELECTION (TOTAL → UNDER)
        ↓  execution time
    READ BOOKMAKER STATE (via adapter)
        ↓
    CURRENT TOTAL UNDER  →  Line + Price  →  CLICK CURRENT
        ↓
    VERIFY BETSLIP

Every call is a fresh resolution.  Nothing here remembers a previous
line or price: if the bookmaker moved the market, the NEW offer is what
this module finds, hands to the clicker, and the betslip verifier
checks.  A stale line can never be clicked because a stale line is
never read.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from blm_v4.execution.adapter import (
    AdapterUnavailable,
    MarketObservation,
    SelectionResolver,
)
from blm_v4.execution.selection_model import Selection

RESOLUTION_FAILED = "RESOLUTION_FAILED"


@dataclass
class Resolution:
    """The outcome of ONE fresh resolution attempt."""

    ok: bool
    reason: str
    observation: Optional[MarketObservation] = None


def resolve_selection(adapter: SelectionResolver, sel: Selection,
                      *, settle_ms: int = 0) -> Resolution:
    """Resolve (event, market, position) to the CURRENT bookmaker offer.

    Event/market failures are TERMINAL for a job (a wrong event must
    never be forced); a missing POSITION is recoverable — the market
    may be mid-refresh — so the engine re-resolves.  ``settle_ms`` is
    the small pause after navigation/state changes before reading the
    DOM, keeping the read off the rerender boundary.
    """
    if settle_ms:
        time.sleep(settle_ms / 1000.0)
    try:
        if not adapter.find_event(sel.event):
            return Resolution(False, "EVENT_NOT_FOUND")
        if not adapter.find_market(sel.event, sel.market):
            return Resolution(False, "MARKET_NOT_FOUND")
        obs = adapter.find_position(sel.event, sel.market, sel.position)
    except AdapterUnavailable:
        raise  # engine maps → BROWSER_DISCONNECTED (non-recoverable)
    except Exception:
        return Resolution(False, "DOM_CHANGED")  # recoverable: re-resolve
    if obs is None:
        return Resolution(False, "POSITION_NOT_FOUND")
    if obs.suspended:
        return Resolution(False, "POSITION_NOT_FOUND")  # not selectable now
    if obs.line is None or obs.price is None:
        return Resolution(False, "DOM_CHANGED")
    return Resolution(True, "CURRENT_SELECTION_FOUND", obs)
