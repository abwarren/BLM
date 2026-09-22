"""BLM EXECUTION — BETSLIP VERIFIER (generic).

A click is never proof.  A leg is confirmed ONLY when the bookmaker's
betslip — re-read fresh from the DOM — actually contains the selection
that was clicked, with the line and price captured at click time.

Verification policy (the race between click and slip update):

  1. exact match on event + market + position AND line AND price
     → confirmed;
  2. same identity but the slip shows a DIFFERENT current line/price —
     the bookmaker re-rendered the slip mid-flight (the classic
     "changed between click and verify" race).  The verifier asks the
     adapter for the CURRENT offer: if the slip's values equal the
     bookmaker's CURRENT offer, the slip is showing the live state and
     the leg is confirmed WITH the new values (the engine proceeds —
     the position is what the user asked for); if they differ, the slip
     is inconsistent → BETSLIP_NOT_UPDATED (recoverable re-resolve);
  3. leg absent → BETSLIP_NOT_UPDATED;
  4. slip unreadable (adapter returned no parse) → BETSLIP_NOT_UPDATED.
     A leg is NEVER assumed present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from blm_v4.execution.adapter import SelectionResolver
from blm_v4.execution.selection_model import Selection

VERIFIED = "VERIFIED"
BETSLIP_NOT_UPDATED = "BETSLIP_NOT_UPDATED"
BETSLIP_UNREADABLE = "BETSLIP_UNREADABLE"
BETSLIP_DUPLICATE = "BETSLIP_DUPLICATE"


@dataclass
class BetslipCheck:
    """The outcome of verifying one leg against the CURRENT slip."""

    outcome: str
    matched: Optional[dict] = None       # the slip entry that matched
    slip_entries: list = field(default_factory=list)


def _same_identity(entry: dict, sel: Selection) -> bool:
    """Identity match tolerant of the bookmaker's RENDERING (case, team
    order, event-name suffixes) but EXACT on the betting semantics:
    position must be the requested position — an UNDER request is never
    satisfied by an OVER entry on the same event."""
    pos = str(entry.get("position") or "").strip().upper()
    mkt = str(entry.get("market") or "").strip().upper()
    ev = str(entry.get("event") or "").strip().upper()
    want_ev = str(sel.event).strip().upper()
    ev_ok = ev == want_ev or (
        all(t in ev for t in want_ev.split(" VS ")) if " VS " in want_ev
        else want_ev in ev or ev in want_ev)
    return pos == sel.position.upper() and "TOTAL" in mkt and ev_ok


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def verify_leg_in_betslip(adapter: SelectionResolver, sel: Selection,
                          line_at_click: Optional[float],
                          price_at_click: Optional[float]) -> BetslipCheck:
    """Re-read the CURRENT betslip and verify ``sel`` per the policy.

    ``line_at_click=None`` means the PRE-CLICK idempotency probe: the
    click has not happened in this attempt yet, so a slip entry for the
    same event+position must be a DIFFERENT leg (e.g. the opposing
    position on the same event) — it is NOT the leg we are about to
    add and must never satisfy the probe."""
    try:
        entries = adapter.read_betslip()
    except Exception:
        return BetslipCheck(BETSLIP_UNREADABLE)
    if not entries and entries != []:
        return BetslipCheck(BETSLIP_UNREADABLE)

    matches = [e for e in entries if _same_identity(e, sel)]
    if line_at_click is None:
        # pre-click idempotency probe: with position-exact identity, a
        # match means THIS leg is already in the slip (added by an
        # earlier attempt whose response was lost, or a reconnect) —
        # never add it again
        if matches:
            return BetslipCheck(BETSLIP_DUPLICATE, matched=matches[0],
                                slip_entries=entries)
        return BetslipCheck(BETSLIP_NOT_UPDATED, slip_entries=entries)
    if not matches:
        return BetslipCheck(BETSLIP_NOT_UPDATED, slip_entries=entries)
    if len(matches) > 1:
        # Duplicate protection: the requested leg is present MORE THAN
        # once.  Report it — the engine's pre-retry idempotency check
        # makes this unreachable in normal flow.
        return BetslipCheck(BETSLIP_DUPLICATE, matched=matches[0],
                            slip_entries=entries)

    m = matches[0]
    slip_line, slip_price = _num(m.get("line")), _num(m.get("price"))
    if (slip_line == _num(line_at_click)
            and slip_price == _num(price_at_click)):
        return BetslipCheck(VERIFIED, matched=m, slip_entries=entries)

    # Values differ from click time → the race path: is the slip showing
    # the bookmaker's CURRENT offer for this identity?
    try:
        obs = adapter.find_position(sel.event, sel.market, sel.position)
    except Exception:
        obs = None
    if (obs is not None and slip_line is not None
            and slip_price is not None
            and slip_line == obs.line and slip_price == obs.price):
        return BetslipCheck(VERIFIED, matched=m, slip_entries=entries)
    return BetslipCheck(BETSLIP_NOT_UPDATED, matched=m,
                        slip_entries=entries)


def verify_full_betslip(adapter: SelectionResolver,
                        legs: list[Selection],
                        lines_at_click: list[Optional[float]],
                        prices_at_click: list[Optional[float]],
                        ) -> dict:
    """Pre-placement gate: EVERY requested leg must be present in the
    CURRENT slip, exactly once.  Returns
    ``{"ok": bool, "missing": [...], "problems": [...]}``."""
    try:
        entries = adapter.read_betslip()
    except Exception:
        entries = None
    if entries is None:
        return {"ok": False, "missing": [], "problems": ["UNREADABLE"]}
    missing, problems = [], []
    for i, sel in enumerate(legs):
        matches = [e for e in entries if _same_identity(e, sel)]
        if not matches:
            missing.append(sel.to_dict())
        elif len(matches) > 1:
            problems.append({"leg": sel.to_dict(),
                             "reason": "DUPLICATE_IN_SLIP"})
        else:
            m = matches[0]
            slip_line, slip_price = _num(m.get("line")), _num(m.get("price"))
            cur = None
            if (slip_line != _num(lines_at_click[i])
                    or slip_price != _num(prices_at_click[i])):
                try:
                    cur = adapter.find_position(
                        sel.event, sel.market, sel.position)
                except Exception:
                    cur = None
                if not (cur is not None and slip_line == cur.line
                        and slip_price == cur.price):
                    problems.append({"leg": sel.to_dict(),
                                     "reason": "STALE_SLIP_VALUES"})
    return {"ok": not missing and not problems,
            "missing": missing, "problems": problems}
