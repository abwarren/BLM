"""Read-only, evidence-bounded calculations for the DB Explorer.

This module deliberately does not infer historical signal qualification or
Auto-Bet eligibility. Those decisions were not persisted for every signal.
It calculates settlement and performance only from recorded execution rows.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def settlement(selection: Any, line: Any, final_total: Any) -> dict:
    """Return a direction-normalized result using the recorded trigger line."""
    side = str(selection or "").strip().upper()
    line_value, final = _number(line), _number(final_total)
    if side not in {"UNDER", "OVER"} or line_value is None or final is None:
        return {"status": "Unknown" if final is not None else "Pending",
                "margin": None}
    margin = (line_value - final if side == "UNDER" else final - line_value)
    status = "Won" if margin > 0 else "Lost" if margin < 0 else "Push"
    return {"status": status, "margin": round(margin, 4)}


def execution_state(row: dict, audit_events: Iterable[dict] = ()) -> dict:
    """Classify only states proved by the execution ledger/provider audit."""
    events = list(audit_events or ())
    status = str(row.get("status") or row.get("execution_state") or "").upper()
    eligibility = "Unknown"
    eligibility_reason = None
    for event in events:
        reason = str(event.get("reason") or "")
        if event.get("event") == "claimed" and reason == "all ten conditions satisfied":
            eligibility, eligibility_reason = "Eligible", reason
        elif event.get("event") == "rung_reject":
            eligibility = "Rejected"
            eligibility_reason = reason or event.get("reason") or "rung_rejected"
    if status == "ACCEPTED" and row.get("provider_ref") \
            and not str(row.get("provider_ref")).startswith("dryrun-") \
            and str(row.get("execution_state") or "").upper() != "WOULD_BET":
        execution = "Confirmed placed"
    elif status == "REJECTED" and any(
            e.get("event") == "result"
            and (e.get("details_result") == "REJECTED"
                 or e.get("result") == "REJECTED") for e in events):
        execution = "Rejected by bookmaker"
    elif status == "FAILED" and row.get("submitted_at_utc"):
        execution = "Submission failed"
    elif status in {"PENDING", "WOULD_BET", "BLOCKED", "CANCELLED"} \
            or (status == "ACCEPTED" and str(row.get("provider_ref") or "").startswith("dryrun-")):
        execution = "Not attempted"
    elif status in {"SUBMITTING", "SUBMITTED", "UNKNOWN", "RECONCILING"}:
        execution = "Unknown"
    elif status == "FAILED":
        execution = "Submission failed" if row.get("submitted_at_utc") else "Not attempted"
    else:
        execution = "Unknown"
    return {"eligibility": eligibility,
            "eligibility_reason": eligibility_reason,
            "execution": execution}


def performance(rows: Iterable[dict]) -> dict:
    """Summarize the confirmed real-bet subset; never counts dry-run rows."""
    wins = losses = pushes = pending = unknown = not_attempted = confirmed = 0
    total_stake = net_amount = unit_net = 0.0
    settled_stake = 0.0
    missing_price = 0
    odds_sum = 0.0
    odds_count = 0
    incomplete_amount = False
    incomplete_units = False
    for row in rows:
        state = row.get("execution")
        if state == "Unknown":
            unknown += 1
        if state == "Not attempted":
            not_attempted += 1
        if state != "Confirmed placed":
            continue
        confirmed += 1
        stake, odds = _number(row.get("actual_stake", row.get("stake_amount"))), _number(row.get("price"))
        if stake is not None and stake > 0:
            total_stake += stake
        if odds is not None and odds > 1:
            odds_sum += odds
            odds_count += 1
        outcome = row.get("settlement")
        if outcome == "Pending":
            pending += 1
            continue
        if outcome not in {"Won", "Lost", "Push"}:
            unknown += 1
            continue
        if stake is None or stake <= 0:
            incomplete_amount = incomplete_units = True
            continue
        settled_stake += stake
        if outcome == "Won":
            wins += 1
            if odds is None or odds <= 1:
                missing_price += 1
                incomplete_amount = incomplete_units = True
                continue
            profit = stake * (odds - 1)
        elif outcome == "Lost":
            losses += 1
            profit = -stake
        else:
            pushes += 1
            profit = 0.0
        net_amount += profit
        unit_price = _number(row.get("unit_price"))
        if unit_price is not None and unit_price > 0:
            unit_net += profit / unit_price
        else:
            incomplete_units = True
    decided = wins + losses
    return {
        "confirmed_bets": confirmed,
        "wins": wins, "losses": losses, "pushes": pushes,
        "settled_selections": wins + losses + pushes,
        "win_rate_pct": round(100 * wins / decided, 2) if decided else None,
        "win_rate_n": decided,
        "net_amount": round(net_amount, 2) if not incomplete_amount else None,
        "net_units": round(unit_net, 4) if not incomplete_units else None,
        "total_stake": round(total_stake, 2),
        "settled_stake": round(settled_stake, 2),
        "roi_pct": round(100 * net_amount / settled_stake, 2)
        if settled_stake and not incomplete_amount else None,
        "average_odds": round(odds_sum / odds_count, 4) if odds_count else None,
        "pending_executions": pending,
        "unknown_executions": unknown,
        "not_attempted": not_attempted,
        "missing_price_wins": missing_price,
        "net_amount_complete": not incomplete_amount,
        "net_units_complete": not incomplete_units,
    }


def theoretical_unit_result(outcome: str, odds: Any) -> float | None:
    """One-unit flat-stake result; None when recorded odds are not valid."""
    price = _number(odds)
    if price is None or price <= 1:
        return None
    if outcome == "Won":
        return round(price - 1, 6)
    if outcome == "Lost":
        return -1.0
    if outcome in {"Push", "Void"}:
        return 0.0
    return None
