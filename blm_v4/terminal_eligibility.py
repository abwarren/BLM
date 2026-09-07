"""BLM V4 — Terminal-Checkpoint Eligibility (research-data boundary).

HARD RULE (terminal-checkpoint exclusion directive):

    TERMINAL     = SETTLEMENT / AUDIT ONLY.
    NON-TERMINAL = PREDICTIVE RESEARCH ELIGIBLE.

A checkpoint observation is TERMINAL when it represents the END of the
game.  Terminality is decided from authoritative game-time/state
evidence — NEVER from a checkpoint bucket or percentage label:

  1. the game is ENDED (game status / ended flag — the existing
     authoritative terminal-state logic), OR
  2. the period label is an explicit finished state (Full Time /
     Finished / End of Match / Match Ended), OR
  3. elapsed game time has reached the classification's full regulation
     duration (BETUAL_NBA 40:00, CYBER_2K26 48:00 — the classification
     durations remain authoritative), OR
  4. game progress has reached 1.0, OR
  5. the Q4 period-over clock sentinels (00:00 / 21:00) are showing.

A row at the 100% BUCKET whose game-time fields prove the observation
is NOT at the end of the game (e.g. elapsed 39.25 of 40 = 98.1%) is
NOT terminal: the bucket is a grouping/analysis label only and can
never determine terminal status or the displayed progress.  No
percentage threshold other than the authoritative game time itself is
used anywhere — this is a semantic terminal-state exclusion, not a
performance optimization.

Terminal rows are NEVER deleted.  They stay stored for final
settlement, audit, game reconstruction, market-movement analysis,
historical display and debugging — they carry an explicit state:

    terminal             = 1
    predictive_eligible  = 0
    exclusion_reason     = "TERMINAL CHECKPOINT"

and contribute exactly 0 to predictive accuracy, win rate, calibration,
reliability/freshness bins, directional performance, residual x age
interaction, checkpoint performance, chronological validation blocks,
game-weighted performance, deviation benchmarks and prospective
confirmation populations.  No downstream research statistic may override
this rule.

This module is ENFORCEMENT/ELIGIBILITY ONLY: it never modifies the
projection formula, the directional decision rule, the frozen
prospective confirmation specification, or production betting logic.
"""

from __future__ import annotations

from typing import Optional

from blm_v4.projection import duration_for

# Explicit exclusion state carried by terminal rows (directive section 3).
TERMINAL_EXCLUSION_REASON = "TERMINAL CHECKPOINT"

# Predictive-validation label for a row (directive section 3: the critical
# distinction — a terminal row may read U WIN for settlement while
# simultaneously reading PREDICTIVE VALIDATION: EXCLUDED).
PREDICTIVE_LABEL_ELIGIBLE = "PREDICTIVE VALIDATION: ELIGIBLE"
PREDICTIVE_LABEL_EXCLUDED = "PREDICTIVE VALIDATION: EXCLUDED"

# Period labels that identify the game as over (mirrors the collector's
# authoritative _is_final_state vocabulary and scorecard._is_final_label).
_FINISHED_LABEL_KEYWORDS = (
    "full time", "finished", "end of match", "match ended",
)

# Q4 period-over clock sentinels: 00:00 is the expired regulation clock;
# 21:00 is the BetConstruct panel's period-over sentinel (period length +
# 1 display, observed as the panel's finished marker).
_Q4_OVER_CLOCKS = ("00:00", "0:00", "21:00")

# Game statuses that mean the game is over (collector writes "ended";
# the panel infers "finished"-style statuses from period labels).
_ENDED_STATUSES = ("ended", "finished", "full time", "ft", "complete",
                   "completed")


def _finished_label(period_label: Optional[str]) -> bool:
    p = (period_label or "").lower()
    return any(k in p for k in _FINISHED_LABEL_KEYWORDS)


def _ended_status(game_status: Optional[str]) -> bool:
    s = (game_status or "").lower()
    return s in _ENDED_STATUSES


def terminal_basis(
    *,
    classification: Optional[str] = None,
    elapsed_minutes: Optional[float] = None,
    progress: Optional[float] = None,
    quarter: Optional[int] = None,
    clock: Optional[str] = None,
    period_label: Optional[str] = None,
    game_status: Optional[str] = None,
    ended: Optional[bool] = None,
) -> Optional[str]:
    """Why the observation is terminal (None when it is not).

    The FIRST matching rule wins.  Per-frame game-time evidence (the
    finished period label, the elapsed/progress game time, the Q4
    period-over clock sentinels) is authoritative over a game-level
    status flag: a mid-game frame of a since-ended game carries
    status='ended' in some datasets, but its own elapsed game time
    proves the observation is NOT the end of the game — and a terminal
    decision must never rest on a flag a game-time field contradicts.
    """
    # 1. explicit finished period label (per-frame evidence).
    if _finished_label(period_label):
        return "FINISHED_LABEL"
    # 2. elapsed game time reached the classification's full duration
    #    (40-minute classifications -> 40:00, 48-minute -> 48:00).
    if elapsed_minutes is not None and classification:
        _, full = duration_for(classification)
        if elapsed_minutes >= full - 1e-9:
            return "ELAPSED_FULL_DURATION"
    # 3. game progress reached 1.0 (duration-independent form of rule 2).
    if progress is not None and progress >= 1.0:
        return "PROGRESS_100"
    # 4. Q4 period-over clock sentinels (per-frame evidence).
    q = int(quarter) if quarter is not None else None
    if q is not None and q >= 4 and (clock or "").strip() in _Q4_OVER_CLOCKS:
        return "Q4_CLOCK_SENTINEL"
    # 5. existing authoritative terminal-state logic (ended flag / game
    #    status) — trusted only when NO per-frame game-time evidence
    #    exists that could contradict it.
    game_time_evidence = (
        elapsed_minutes is not None
        or progress is not None
        or (q is not None and (clock or "").strip() != "")
        or bool((period_label or "").strip())
    )
    if (ended is True or _ended_status(game_status)) and not game_time_evidence:
        return "GAME_ENDED"
    # A checkpoint bucket label (pct100) is NEVER terminal evidence: a
    # percentage/bucket cannot make an observation terminal, and rows
    # with no game-time evidence at all cannot be classified terminal.
    return None


def is_terminal_checkpoint(
    *,
    classification: Optional[str] = None,
    elapsed_minutes: Optional[float] = None,
    progress: Optional[float] = None,
    quarter: Optional[int] = None,
    clock: Optional[str] = None,
    period_label: Optional[str] = None,
    game_status: Optional[str] = None,
    ended: Optional[bool] = None,
) -> bool:
    """True when the checkpoint observation represents the END of the game.

    See the module docstring for the authoritative rule order.  A terminal
    row is SETTLEMENT/AUDIT ONLY: it must never enter predictive
    validation, calibration, directional performance or any derived
    model-performance statistic.
    """
    return terminal_basis(
        classification=classification,
        elapsed_minutes=elapsed_minutes,
        progress=progress,
        quarter=quarter,
        clock=clock,
        period_label=period_label,
        game_status=game_status,
        ended=ended,
    ) is not None


def eligibility_state(**kwargs) -> tuple[bool, bool, Optional[str]]:
    """(terminal, predictive_eligible, exclusion_reason) for a row.

    The explicit state terminal rows must carry (directive section 3):
    terminal=1 rows are retained for settlement/audit with
    predictive_eligible=0 and exclusion_reason="TERMINAL CHECKPOINT".
    """
    terminal = is_terminal_checkpoint(**kwargs)
    return terminal, (0 if terminal else 1), \
        (TERMINAL_EXCLUSION_REASON if terminal else None)


def predictive_validation_label(terminal: bool) -> str:
    """Settlement vs predictive distinction for display (section 3): a
    terminal row may say U WIN for settlement while simultaneously saying
    PREDICTIVE VALIDATION: EXCLUDED / REASON: TERMINAL CHECKPOINT."""
    return PREDICTIVE_LABEL_EXCLUDED if terminal else PREDICTIVE_LABEL_ELIGIBLE


# ── SQL fragments ─────────────────────────────────────────────────────
# Research aggregations apply these so terminal rows are absent from
# every numerator AND denominator.  Rows are never deleted — the flags
# live on the rows and the readers enforce the boundary.


def cm_nonterminal_sql(alias: str = "cm") -> str:
    """checkpoint_market predicate: exclude terminal rows (stamped by the
    writer / migration; NULL = pre-migration legacy, treated per its
    checkpoint_pct backfill — see Scorecard._init)."""
    return f"COALESCE({alias}.terminal, 0) = 0"


def pred_nonterminal_sql(alias: str = "p") -> str:
    """predictions predicate: prefer the stamped terminal column; rows
    predating the stamp are classified by their recorded game progress
    (progress >= 1.0 = the terminal snapshot)."""
    return (f"COALESCE({alias}.terminal, "
            f"CASE WHEN COALESCE({alias}.progress, 0) >= 1.0 "
            f"THEN 1 ELSE 0 END) = 0")
