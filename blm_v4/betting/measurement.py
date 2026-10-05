"""R2 EXACT-TRIGGER EXECUTION MEASUREMENT — instrumentation only.

The question this layer answers is NOT "did BLM produce a good signal" (that is
the trigger layer's job and is frozen) but:

    when the alert fired at trigger_line, did Auto-Bet actually get the money
    down AT THAT LINE?

Nothing here decides, changes or re-derives the model.  The BLM trigger and its
condition are untouched; the EXECUTION policy (``rung.validate_execution``,
whose floor is the one-rung-down bound in ``rung.RUNG_ALLOW_MIN``) is
untouched.  This module only OBSERVES an attempt and classifies it.

REUSE, NOT DUPLICATION
    * the rung arithmetic is the canonical ``blm_v4.betting.rung`` (never a
      second formula, never a hard-coded 0.5 — the tick is DERIVED)
    * the rung size / rungs_moved / trigger_line / current_line come from the
      command's rung verdict
    * trigger_line, executable_line, executable_odds, executed_at,
      provider_ref come from the existing Position
    * the alert identity is the existing ``<game_id>|<checkpoint>``

TWO SEPARATE AXES
    classification  WHICH line/odds disposition the attempt had
                    (exact / tolerance_down_1 / tolerance_up_1 /
                     tolerance_up_2 / miss_unavailable /
                     exact_line_odds_rejected)
    outcome         what actually happened to the money
                    (FILLED / MARKET_MISS / TECHNICAL_FAILURE /
                     ODDS_REFUSED / UNKNOWN / UNMEASURABLE)

    A MARKET AVAILABILITY MISS (the bookmaker no longer offers the line) and a
    TECHNICAL EXECUTION FAILURE (the line was there; we failed) are NEVER
    collapsed into one "miss".  UNKNOWN stays UNKNOWN — it is never promoted to
    a fill for reporting purposes.

DENOMINATOR
    UNIQUE ALERT IDENTITIES ATTEMPTED — never raw attempts, browser clicks,
    retries or positions created.  A retry of ``game|75`` adds no entry.

Sentinels outside the measured vocabulary
    ``unmeasured_rung`` — a QUALIFYING fill at a rung the four measured classes
    do not cover (the execution policy allows unlimited upward movement, but
    the R2 measurement window is -1..+2).  Counted inside TOLERANCE FILLS and
    EXCLUDED from the four measurement rates, so a +3 fill can never be
    silently reported as ``tolerance_up_2``.
    ``UNMEASURABLE`` — the tick could not be proven, so the rung relationship
    is unprovable: fail closed, measured as neither fill nor market miss.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from blm_v4.betting import rung as R

# ── classification vocabulary (mutually exclusive) ────────────────────
CLASS_EXACT = "exact"
CLASS_DOWN_1 = "tolerance_down_1"
CLASS_UP_1 = "tolerance_up_1"
CLASS_UP_2 = "tolerance_up_2"
CLASS_MISS = "miss_unavailable"
CLASS_ODDS_REJECTED = "exact_line_odds_rejected"
#: documented sentinel — see module docstring
CLASS_UNMEASURED = "unmeasured_rung"

CLASSIFICATIONS = (CLASS_EXACT, CLASS_DOWN_1, CLASS_UP_1, CLASS_UP_2,
                   CLASS_MISS, CLASS_ODDS_REJECTED, CLASS_UNMEASURED)

#: rungs_moved -> classification, for a QUALIFYING fill
_RUNG_CLASS = {-1.0: CLASS_DOWN_1, 0.0: CLASS_EXACT, 1.0: CLASS_UP_1,
               2.0: CLASS_UP_2}

# ── outcome vocabulary ────────────────────────────────────────────────
OUTCOME_FILLED = "FILLED"
OUTCOME_MARKET_MISS = "MARKET_MISS"
OUTCOME_TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
OUTCOME_ODDS_REFUSED = "ODDS_REFUSED"
OUTCOME_UNKNOWN = "UNKNOWN"
OUTCOME_UNMEASURABLE = "UNMEASURABLE"

OUTCOMES = (OUTCOME_FILLED, OUTCOME_MARKET_MISS, OUTCOME_TECHNICAL_FAILURE,
            OUTCOME_ODDS_REFUSED, OUTCOME_UNKNOWN, OUTCOME_UNMEASURABLE)

#: the odds refusals the EXISTING price gate raises (precheck vocabulary).
ODDS_REFUSAL_REASONS = ("ODDS_UNAVAILABLE", "ODDS_TOLERANCE_EXCEEDED")
#: the line/aliveness refusals that mean "no qualifying line".
LINE_REFUSAL_REASONS = (R.R_MARKET_MISSING, "LINE_UNAVAILABLE",
                        "LINE_TOLERANCE_EXCEEDED", "MARKET_STATUS_INVALID",
                        "SELECTION_NOT_FOUND")


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def alert_identity(game_id: Any, checkpoint: Any) -> str:
    """The canonical alert identity — the SAME ``<game_id>|<checkpoint>`` the
    command/position/executor already use.  Not a parallel scheme."""
    return f"{game_id}|{checkpoint}"


def classify_attempt(*, trigger_line: Any, available_line: Any,
                     rung_size: Any, odds_ok: Any = True,
                     executed: Any = False, ambiguous: Any = False,
                     refusal_reason: Any = None) -> dict:
    """Classify ONE attempt.  Pure; every input explicit; fail closed.

    Returns ``{"classification", "outcome", "rungs_moved", "reason"}``.
    """
    trig = _f(trigger_line)
    avail = _f(available_line)
    if trig is None or avail is None:
        return {"classification": CLASS_MISS,
                "outcome": OUTCOME_MARKET_MISS, "rungs_moved": None,
                "reason": "line_unavailable"}
    moved = R.rungs_moved(avail, trig, rung_size)      # the canonical formula
    if moved is None:
        # the tick is unprovable -> the rung relationship is unprovable.
        # FAIL CLOSED: not a fill, and not claimed as a market miss either.
        return {"classification": CLASS_MISS,
                "outcome": OUTCOME_UNMEASURABLE, "rungs_moved": None,
                "reason": "rung_size_ambiguous"}
    moved = round(moved, 6)
    # ── odds are INDEPENDENT of the line: an exact line whose price the
    #    existing gate refuses is an ODDS REFUSAL, never a market-price miss.
    if moved == 0.0 and odds_ok is not True:
        return {"classification": CLASS_ODDS_REJECTED,
                "outcome": OUTCOME_ODDS_REFUSED, "rungs_moved": 0.0,
                "reason": refusal_reason or "odds_refused"}
    if moved < R.RUNG_ALLOW_MIN - R.EPS:
        return {"classification": CLASS_MISS, "outcome": OUTCOME_MARKET_MISS,
                "rungs_moved": moved, "reason": "no_qualifying_line"}
    if executed is not True:
        # the line was available (within the window) but nothing filled.
        if ambiguous:
            return {"classification": CLASS_MISS, "outcome": OUTCOME_UNKNOWN,
                    "rungs_moved": moved, "reason": "ambiguous_confirmation"}
        return {"classification": CLASS_MISS,
                "outcome": OUTCOME_TECHNICAL_FAILURE, "rungs_moved": moved,
                "reason": refusal_reason or "execution_failed"}
    cls = _RUNG_CLASS.get(moved)
    if cls is None:
        cls = CLASS_UNMEASURED             # out of the measured window (+3..)
    return {"classification": cls, "outcome": OUTCOME_FILLED,
            "rungs_moved": moved, "reason": "filled"}


@dataclass
class AttemptMeasurement:
    """One measured attempt per alert identity (a VIEW of existing facts)."""
    alert_id: str
    game_id: Optional[str] = None
    checkpoint: Optional[Any] = None
    trigger_line: Optional[float] = None
    available_line: Optional[float] = None
    available_odds: Optional[float] = None
    observed_at: Optional[str] = None
    classification: str = CLASS_MISS
    outcome: str = OUTCOME_UNMEASURABLE
    rungs_moved: Optional[float] = None
    reason: Optional[str] = None
    provider_ref: Optional[str] = None
    attempt_ref: Optional[str] = None
    rung_size: Optional[float] = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def measurement_from_command(command: dict, *,
                             position: Any = None,
                             odds_ok: Any = None,
                             ambiguous: Any = False,
                             observed_at: Optional[str] = None,
                             attempt_ref: Optional[str] = None
                             ) -> Optional[AttemptMeasurement]:
    """Build the measurement for one attempt FROM the existing records.

    ``command`` is the shared producer command (``command.build_command``) —
    its ``rung`` block is the canonical verdict; ``position`` is the existing
    ``Position`` when one was created (a fill).  Nothing is copied that the
    Position already owns: the Position stays the single source for
    trigger_line / executable_line / executable_odds / executed_at /
    provider_ref.  Returns None when the command carries no alert identity.
    """
    if not isinstance(command, dict):
        return None
    verdict = command.get("rung") or {}
    game_id = command.get("game_id") or verdict.get("game_id")
    checkpoint = command.get("checkpoint")
    if checkpoint is None:
        checkpoint = verdict.get("trigger_checkpoint_percent")
    if game_id is None or checkpoint is None:
        return None                       # no identity -> nothing measurable
    alert_id = command.get("alert_id") or verdict.get("alert_id") \
        or alert_identity(game_id, checkpoint)
    reason = command.get("reason")
    if odds_ok is None:
        odds_ok = reason not in ODDS_REFUSAL_REASONS
    executed = (position is not None and
                str(getattr(position, "state", "") or "").upper()
                not in ("REJECTED", "FAILED"))
    if ambiguous is None:
        ambiguous = str(getattr(position, "state", "")).upper() == "UNKNOWN"
    verdict_cls = classify_attempt(
        trigger_line=verdict.get("trigger_line"),
        available_line=verdict.get("current_line"),
        rung_size=verdict.get("rung_size"),
        odds_ok=odds_ok, executed=executed, ambiguous=ambiguous,
        refusal_reason=reason)
    trig = _f(verdict.get("trigger_line"))
    avail = (_f(getattr(position, "executable_line", None))
             if position is not None else None)
    if avail is None:
        avail = _f(verdict.get("current_line"))
    return AttemptMeasurement(
        alert_id=alert_id, game_id=str(game_id), checkpoint=checkpoint,
        # the FROZEN intent — never the live line
        trigger_line=trig,
        # what the bookmaker actually offered/was executed at
        available_line=avail,
        available_odds=_f(getattr(position, "executable_odds", None)),
        observed_at=(observed_at or getattr(position, "executed_at", None)
                     or verdict.get("trigger_timestamp")),
        classification=verdict_cls["classification"],
        outcome=verdict_cls["outcome"],
        rungs_moved=verdict_cls["rungs_moved"],
        reason=verdict_cls["reason"],
        provider_ref=getattr(position, "provider_ref", None),
        attempt_ref=attempt_ref, rung_size=_f(verdict.get("rung_size")))


class MeasurementLedger:
    """Per-alert-identity ledger.  Retries never add a denominator entry."""

    def __init__(self) -> None:
        self._attempts: dict[str, AttemptMeasurement] = {}
        self._signals: set[str] = set()

    # ── the signal side (an alert fired, whether or not it was attempted) ──
    def note_signal(self, alert_id: str) -> bool:
        before = len(self._signals)
        self._signals.add(str(alert_id))
        return len(self._signals) > before

    def note_signals(self, alert_ids) -> int:
        n = 0
        for a in alert_ids or ():
            n += 1 if self.note_signal(a) else 0
        return n

    # ── the attempt side (FIRST measurement per identity wins) ─────────────
    def record(self, m: AttemptMeasurement) -> bool:
        """Record ONE attempt.  Returns False when this alert identity has
        already been measured (a retry, a duplicate, a reconnect) — the
        denominator is unchanged."""
        if m is None or not str(getattr(m, "alert_id", "") or ""):
            return False
        if m.alert_id in self._attempts:
            return False
        self._attempts[m.alert_id] = m
        return True

    def record_all(self, measurements) -> int:
        return sum(1 for m in (measurements or ()) if self.record(m))

    @property
    def attempts(self) -> list:
        return list(self._attempts.values())

    @property
    def signals(self) -> list:
        return sorted(self._signals)

    def get(self, alert_id: str) -> Optional[AttemptMeasurement]:
        return self._attempts.get(str(alert_id))

    # ── the roll-ups ──────────────────────────────────────────────────────
    def report(self) -> dict:
        """The measurement report.  Every rate shares ONE denominator:
        unique alert identities attempted."""
        rows = self.attempts
        denom = len(rows)                       # UNIQUE ALERT IDENTITIES
        counts = {c: 0 for c in CLASSIFICATIONS}
        outcomes = {o: 0 for o in OUTCOMES}
        for m in rows:
            if m.classification in counts:
                counts[m.classification] += 1
            if m.outcome in outcomes:
                outcomes[m.outcome] += 1
        exact = counts[CLASS_EXACT]
        tolerance = (counts[CLASS_DOWN_1] + counts[CLASS_UP_1]
                     + counts[CLASS_UP_2] + counts[CLASS_UNMEASURED])

        def rate(n: int) -> Optional[float]:
            return round(n / denom, 6) if denom else None

        return {
            # ── §9 the execution report ──
            "signals": len(self._signals),
            "attempts": denom,
            "exact_fills": exact,
            "tolerance_fills": tolerance,
            "availability_misses": outcomes[OUTCOME_MARKET_MISS],
            "technical_failures": outcomes[OUTCOME_TECHNICAL_FAILURE],
            "odds_refusals": outcomes[OUTCOME_ODDS_REFUSED],
            "unknown": outcomes[OUTCOME_UNKNOWN],
            "unmeasurable": outcomes[OUTCOME_UNMEASURABLE],
            "unmeasured_rung_fills": counts[CLASS_UNMEASURED],
            # ── §8 the rates (one shared denominator) ──
            "denominator": denom,
            "exact_line_fill_rate": rate(exact),
            "tolerance_down_1_rate": rate(counts[CLASS_DOWN_1]),
            "tolerance_up_1_rate": rate(counts[CLASS_UP_1]),
            "tolerance_up_2_rate": rate(counts[CLASS_UP_2]),
            "availability_miss_rate": rate(outcomes[OUTCOME_MARKET_MISS]),
            "technical_failure_rate": rate(
                outcomes[OUTCOME_TECHNICAL_FAILURE]),
            "exact_line_odds_rejection_rate": rate(
                counts[CLASS_ODDS_REJECTED]),
            "unknown_rate": rate(outcomes[OUTCOME_UNKNOWN]),
            "classifications": counts,
            "outcomes": outcomes,
        }
