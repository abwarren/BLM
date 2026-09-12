"""UNDER ALERT final outcome — backend-authoritative settlement block.

Directive "UNDER ALERT HISTORY — FINAL OUTCOME COLORING" (2026-09-12):

When an Under Alert triggers, its trigger snapshot is immutable.  Once the
game finishes, every alert the game raised is settled against the final
result.  The ONLY comparison is::

    final_total  vs  trigger_market_total

    final  < trigger   ->  "under"   (successful UNDER, GREEN)
    final  > trigger   ->  "over"    (OVER, RED)
    final == trigger   ->  "push"    (neutral)

The trigger market total is the live O/U line captured at the moment the
game reached that checkpoint — never the opening line, a later live line,
the closing line or any reconstructed value.  The FINAL total comes from
the backend's game-final data where available (the scorecard's settled
``game_results`` store, ``final_result_status='OK'``), falling back to
the stored terminal observation only when no settled result exists.
This module computes the verdict from stored data only; the frontend
consumes it verbatim and never reconstructs the quantitative condition.
"""
from __future__ import annotations

from typing import Optional

from blm_v4.live_analytics.under_alert import CHECKPOINTS
from blm_v4.projection import (duration_for, period_quarter,
                               row_elapsed_minutes)
from blm_v4.terminal_eligibility import is_terminal_checkpoint


def _f(value) -> Optional[float]:
    """The value as a finite float, else None (booleans excluded)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _row_progress(row: dict, classification: Optional[str]
                  ) -> Optional[float]:
    """Game progress (0..1) for one snapshot row — the same game-time
    authority everywhere else uses (count-down clock + period label)."""
    q_min, full = duration_for(
        row.get("classification") or classification)
    elapsed = row_elapsed_minutes(row, q_min, full)
    if elapsed is None or not full:
        return None
    return min(1.0, max(0.0, elapsed / full))


def _terminal_row(row: dict, classification: Optional[str]) -> bool:
    """True when this row represents the END of the game — the SAME
    terminal authority the research boundary and the alert gate use
    (ended status / finished label / full regulation / progress 1.0 /
    Q4 clock sentinels), never a bucket label."""
    return is_terminal_checkpoint(
        classification=row.get("classification") or classification,
        elapsed_minutes=row_elapsed_minutes(
            row,
            *duration_for(row.get("classification") or classification)),
        progress=_row_progress(row, classification),
        quarter=row.get("quarter"),
        clock=row.get("clock"),
        period_label=row.get("period_label"),
        game_status=row.get("game_status"))


def trigger_market_total(rows: list[dict], checkpoint: int,
                         classification: Optional[str] = None
                         ) -> Optional[float]:
    """The live market total the game carried when it REACHED ``checkpoint``.

    ``rows`` is the game's snapshot list ascending (the same rows the
    projection consumed).  The checkpoint is reached at the FIRST
    observation whose game progress >= checkpoint/100 — phase-based
    attribution, mirroring the alert layer — so the settled line is the
    line the alert fired against.  The market total persists between
    captures (the bookmaker line does not vanish between event-view
    visits), so the line in force at the boundary is the most recent
    line OBSERVED AT OR BEFORE it — exactly the value ``market.total_line``
    (and the WS fallback) supplied the alert.  A later line is never
    used: nothing captured after the boundary can leak in.  If no line
    had been observed by the boundary, the trigger is unprovable and
    stays None — never fabricated, never replaced by the closing line.
    """
    target = checkpoint / 100.0
    last_line: Optional[float] = None
    for row in list(rows):
        line = _f(row.get("total_line"))
        if line is not None:
            last_line = line
        progress = _row_progress(row, classification)
        if progress is None or progress < target:
            continue
        return last_line
    return None


def final_total_for(rows: list[dict],
                    classification: Optional[str] = None,
                    settled: Optional[tuple] = None) -> Optional[float]:
    """The game's final total — backend-authoritative, then observations.

    ``settled`` is ``(final_total, result_at)`` from the scorecard's
    ``game_results`` store (``final_result_status='OK'``): the backend's
    authoritative game-final data, which takes precedence.  Without a
    settled result the last terminal observation's home+away scores are
    used.  Never a fabricated or reconstructed value; None while the
    game has not provably finished either way.
    """
    if settled is not None:
        return _f(settled[0])
    final = None
    for row in list(rows):
        if not _terminal_row(row, classification):
            continue
        h, a = _f(row.get("home_score")), _f(row.get("away_score"))
        if h is None or a is None:
            continue
        final = h + a
    return final


def resolved_at_for(rows: list[dict],
                    classification: Optional[str] = None,
                    settled: Optional[tuple] = None) -> Optional[str]:
    """Settlement moment: the settled ``result_at`` when authoritative,
    else captured_at of the LAST terminal observation."""
    if settled is not None:
        return settled[1]
    resolved_at = None
    for row in list(rows):
        if _terminal_row(row, classification):
            resolved_at = row.get("captured_at")
    return resolved_at


def outcome_status(trigger_total, final_total) -> Optional[str]:
    """Pure rule: trigger vs final — under / over / push / None."""
    t, f = _f(trigger_total), _f(final_total)
    if t is None or f is None:
        return None
    if f < t:
        return "under"
    if f > t:
        return "over"
    return "push"


def under_alert_outcome(rows: list[dict],
                        classification: Optional[str] = None,
                        settled: Optional[tuple] = None) -> dict:
    """The sibling ``under_alert_outcome`` payload for one game.

    One settlement entry per alert checkpoint (by_checkpoint)::

        {"status": "under"|"over"|"push"|None,
         "trigger_total": float|None,
         "final_total": float|None,
         "resolved_at": iso|None}

    ``settled`` is the backend's authoritative game-final record
    ``(final_total, result_at)`` from ``game_results``
    (``final_result_status='OK'``) when one exists; otherwise the final
    total is derived from the stored terminal observation.  ``status``
    is None for a checkpoint whose trigger line or the game's final
    total is not provable (game still live, market not captured at the
    boundary) — an honest gap, never a guessed verdict.  Computed only
    from stored data; the browser never re-derives any of it.
    """
    final = final_total_for(rows, classification, settled)
    resolved_at = resolved_at_for(rows, classification, settled)
    by_checkpoint = {}
    for cp in CHECKPOINTS:
        trig = trigger_market_total(rows, cp, classification)
        by_checkpoint[cp] = {
            "status": outcome_status(trig, final),
            "trigger_total": trig,
            "final_total": final,
            "resolved_at": resolved_at,
        }
    return {
        "status": "resolved" if final is not None else "pending",
        "final_total": final,
        "resolved_at": resolved_at,
        "by_checkpoint": by_checkpoint,
    }
