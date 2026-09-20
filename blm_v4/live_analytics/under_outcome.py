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

Q3 BREAK (directive 2026-09-18): the Q3/Q4 break is an ADDITIONAL
checkpoint identity (the string ``"Q3_BREAK"``), additive to the numeric
checkpoints.  It settles by the SAME rule — final vs the line in force
when the game crossed 75.0% progress, which for the break IS the Q3/Q4
boundary — so it appears in ``by_checkpoint`` under its own key, settled
by the same ``trigger_observation`` authority, never a different one.

CANONICAL TRIGGER-LINE STORE (ruling 2026-09-20): the 218-flip audit found
the snapshots series and the pace projector's ``clean_projections`` store
carrying different lines at the same boundary instant (121 of 179 verified
flips).  ``clean_projections`` is ruled CANONICAL for the trigger line.
``trigger_observation`` therefore accepts an optional ``projection_rows``
feed (the game's clean_projections rows, ascending).  Precedence:

  1. ``projection_rows`` — the canonical store; the boundary line comes
     from the last non-null ``live_total_line`` observed at-or-before the
     boundary (same at-or-before semantics as snapshots).
  2. ``rows`` — the snapshot fallback, unchanged, when the canonical store
     has no line by the boundary (gap tolerance: a missing projector row
     must never unalert a live trigger that the snapshots can prove).
  3. ``None`` — no line anywhere: unprovable, never fabricated.

The FINAL side of every verdict is untouched: ``game_results`` (settled)
and terminal observations remain the final authorities.  Only the LINE
side is re-homed.
"""
from __future__ import annotations

from typing import Optional

from blm_v4.live_analytics.under_alert import (CHECKPOINTS,
                                               Q3_BREAK_CHECKPOINT)
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


def _projection_progress(row: dict) -> Optional[float]:
    """Progress (0..1) for one clean_projections row.  The store carries
    ``progress_pct`` directly (the projector's own game-time computation);
    when absent it is recomputed from ``elapsed_game_minutes`` and the
    classification's regulation length — the same authority every other
    consumer uses.  None when neither is available."""
    p = _f(row.get("progress_pct"))
    if p is not None:
        return min(1.0, max(0.0, p / 100.0))
    q_min, full = duration_for(row.get("classification"))
    elapsed = _f(row.get("elapsed_game_minutes"))
    if elapsed is None or not full:
        return None
    return min(1.0, max(0.0, elapsed / full))


def _projection_line_at(rows: list[dict],
                        max_progress: float) -> Optional[float]:
    """The canonical store's line IN FORCE at ``max_progress`` — the last
    non-null ``live_total_line`` among clean_projections rows whose own
    progress is at-or-before that instant.  A line first observed AFTER
    the boundary can never leak into the frozen trigger (the same rule
    the snapshot authority applies to itself).  Rows without a usable
    progress never extend the line (fail closed against a malformed
    store, never a guessed read).  None when the store yields no line by
    the boundary — the caller falls back to the snapshots series."""
    last_line: Optional[float] = None
    for row in list(rows):
        progress = _projection_progress(row)
        if progress is None or progress > max_progress:
            continue
        line = _f(row.get("live_total_line"))
        if line is not None:
            last_line = line
    return last_line


def trigger_observation(rows: list[dict], checkpoint: int,
                        classification: Optional[str] = None,
                        projection_rows: Optional[list[dict]] = None) -> dict:
    """The observation that REACHED ``checkpoint`` — the authoritative
    triggering observation the live alert and the settlement BOTH read.

    ``rows`` is the game's snapshot list ascending (the same rows the
    projection consumed).  The checkpoint is reached at the FIRST
    observation whose game progress >= checkpoint/100 — phase-based
    attribution, mirroring the alert layer.

    ``projection_rows`` (optional) is the game's ``clean_projections``
    series ascending — the CANONICAL trigger-line store (ruling
    2026-09-20).  When supplied, the canonical line is the store's last
    non-null ``live_total_line`` AT OR BEFORE the boundary instant — the
    line in force when the game reached the checkpoint, per the projector's
    own captures; a line first observed after the boundary never leaks in.
    When the store carries no line by the boundary (missing rows, no line
    observed yet), the snapshot series in ``rows`` is the fallback so a
    projector gap never unproves a live trigger the snapshots can prove.
    The boundary itself (progress, captured_at) stays the SNAPSHOT
    observation's: one game-time geometry for every consumer.

    Returns ``{"total_line", "progress", "captured_at"}``:

      * ``total_line`` — the live market O/U line in force at the boundary:
        the most recent line OBSERVED AT OR BEFORE it (the bookmaker line
        persists between captures), never the opening line, a later live
        line, the closing line or any reconstructed value.  ``None`` when no
        line had been observed by the boundary in either store — the
        trigger is then unprovable, never fabricated.
      * ``progress`` — that boundary observation's own progress (0..1).
      * ``captured_at`` — when that boundary observation was captured.

    This is the ONE definition of the frozen trigger line: the live alert's
    ``under_alert.trigger_line`` and the settled verdict's
    ``by_checkpoint[cp].trigger_total`` are the same value from here, so the
    line on screen and the verdict behind it can never disagree.
    """
    base = _snapshot_trigger(rows, checkpoint, classification)
    if projection_rows and base["progress"] is not None:
        line = _projection_line_at(projection_rows, base["progress"])
        if line is not None:
            return {"total_line": line,
                    "progress": base["progress"],
                    "captured_at": base["captured_at"]}
    return base


def _snapshot_trigger(rows: list[dict], checkpoint: int,
                      classification: Optional[str] = None) -> dict:
    """The original snapshot-series authority, verbatim (the fallback leg
    of the canonical-store precedence)."""
    target = checkpoint / 100.0
    last_line: Optional[float] = None
    for row in list(rows):
        line = _f(row.get("total_line"))
        if line is not None:
            last_line = line
        progress = _row_progress(row, classification)
        if progress is None or progress < target:
            continue
        return {"total_line": last_line, "progress": progress,
                "captured_at": row.get("captured_at")}
    return {"total_line": None, "progress": None, "captured_at": None}


def trigger_market_total(rows: list[dict], checkpoint: int,
                         classification: Optional[str] = None,
                         projection_rows: Optional[list[dict]] = None
                         ) -> Optional[float]:
    """The live market total the game carried when it REACHED ``checkpoint``
    — the ``total_line`` of :func:`trigger_observation`.  A later line is
    never used: nothing captured after the boundary can leak in."""
    return trigger_observation(rows, checkpoint, classification,
                               projection_rows=projection_rows)["total_line"]


def score_at_observation(rows: list[dict], checkpoint: int,
                         classification: Optional[str] = None
                         ) -> Optional[float]:
    """The combined score at the observation that REACHED ``checkpoint`` —
    the same boundary observation :func:`trigger_observation` attributes,
    so the score and the frozen line can never come from different moments.
    Q3 BREAK (directive 2026-09-18) uses this with checkpoint 75: the score
    at the Q3/Q4 break is the Q3-end score.  ``None`` when the boundary is
    never reached or the boundary observation carries no provable score —
    never guessed, never reconstructed."""
    target = checkpoint / 100.0
    for row in list(rows):
        progress = _row_progress(row, classification)
        if progress is None or progress < target:
            continue
        h = _f(row.get("home_score"))
        a = _f(row.get("away_score"))
        return h + a if (h is not None and a is not None) else None
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
                        settled: Optional[tuple] = None,
                        projection_rows: Optional[list[dict]] = None) -> dict:
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

    PROVENANCE (directive RESULTED ALERTS — CONTROLLED FINAL-RESULT
    CORRECTION, 2026-09-13).  The block carries the authority of its own
    final so a settlement can be told apart from a correction::

        final_source   "settled"      — the game-final record of record
                       "observation"  — the game's own terminal row
                       None           — no provable final
        authoritative  final_source == "settled"

    Only ``authoritative`` authorises the controlled correction path
    downstream (a revised final in ``game_results`` after an alert has
    already settled).  A verdict derived from a terminal OBSERVATION is
    not a verified final and can never revise a settlement, however the
    observations move.  Provenance describes the FINAL, not the line, so
    it sits at the top level and leaves the per-checkpoint contract (and
    with it the immutable trigger line) exactly as it was.
    """
    final = final_total_for(rows, classification, settled)
    resolved_at = resolved_at_for(rows, classification, settled)
    authoritative = settled is not None and final is not None
    final_source = ("settled" if authoritative
                    else ("observation" if final is not None else None))
    by_checkpoint = {}
    for cp in CHECKPOINTS:
        trig = trigger_market_total(rows, cp, classification,
                                    projection_rows=projection_rows)
        by_checkpoint[cp] = {
            "status": outcome_status(trig, final),
            "trigger_total": trig,
            "final_total": final,
            "resolved_at": resolved_at,
        }
    # Q3 BREAK (directive 2026-09-18): the Q3/Q4 break sits at exactly 75.0%
    # progress, so its settlement reuses the SAME boundary line as the 75%
    # checkpoint — one authority, one line, one verdict — under its OWN
    # string identity ("Q3_BREAK"), additive to the numeric checkpoints.
    trig_q3b = trigger_market_total(rows, 75, classification,
                                    projection_rows=projection_rows)
    by_checkpoint[Q3_BREAK_CHECKPOINT] = {
        "status": outcome_status(trig_q3b, final),
        "trigger_total": trig_q3b,
        "final_total": final,
        "resolved_at": resolved_at,
    }
    return {
        "status": "resolved" if final is not None else "pending",
        "final_total": final,
        "resolved_at": resolved_at,
        "final_source": final_source,
        "authoritative": authoritative,
        "by_checkpoint": by_checkpoint,
    }
