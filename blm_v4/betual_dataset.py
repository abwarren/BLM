"""BLM V4 — Betual-Only Quarterly Dataset Recorder (directive
2026-09-22, DATA COLLECTION ONLY).

A clean, prospective, BETUAL-ONLY dataset for studying how the market
line responds to SCORE and TIME:

    TIME -> SCORE -> LINE -> LINE MOVEMENT

Every observation carries the internal-timer fields (§1) and the
previous-observation deltas (§6/§7: line_previous, line_change,
seconds_since_previous_line, score_change_since_previous_line,
line_velocity) so the dataset is analytically closed without joins.

Hard invariants:
  - BETUAL GATE (§1): classification != BETUAL_NBA is refused — the
    Betual dataset never mixes Cyber/other populations.
  - INTERNAL TIMER (§2/§3): quarter/elapsed/remaining come from
    betual_timer.derive_internal_time; the bookmaker's displayed clock
    is stored as betual_displayed_clock + clock_difference ONLY.
  - GENUINE MOVEMENTS RETAINED (§10): a line that changed and returned
    is kept — dedup only collapses the exact SAME (market, line) at the
    exact SAME timestamp; distinct timestamps are distinct events.
  - NEVER FABRICATE (§5/§9): a missing score or line stores NULL; an
    unparseable observation stores the raw payload + the parse error.
  - NO STRATEGY (§19): this module writes collection tables only; it is
    never imported by alert, fingerprint, betting or threshold code.
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Optional

from blm_v4.betual_timer import (
    PrevState,
    PhaseObservation,
    TimerAnchors,
    clock_diagnostics,
    derive_internal_time,
    parse_clock_seconds,
    settle_phase,
)

CLASSIFICATION_BETUAL = "BETUAL_NBA"


class BetualDatasetError(Exception):
    """Raised when an observation cannot be recorded as specified — the
    raw payload is still retained by the caller (§9)."""


class BetualGameRecord:
    """In-memory per-game dataset state (timer + previous-observation
    caches).  Rebuilt from the DB on collector restart (§13)."""

    __slots__ = (
        "source_game_id", "anchors", "prev_state", "prev_score", "prev_line",
        "prev_line_at", "prev_score_total", "phase_observations",
        "last_capture_at",
    )

    def __init__(self, source_game_id: str,
                 anchors: Optional[TimerAnchors] = None):
        self.source_game_id = source_game_id
        self.anchors = anchors or TimerAnchors()
        self.prev_state: Optional[PrevState] = None
        self.prev_score: Optional[tuple[int, int]] = None
        self.prev_line: Optional[float] = None
        self.prev_line_at: Optional[float] = None
        self.prev_score_total: Optional[int] = None
        self.phase_observations: list[PhaseObservation] = []
        self.last_capture_at: str = ""

    # ── restart recovery (§13) ───────────────────────────────────

    def adopt_start_evidence(
        self, game_start_wall: float, observed_at_wall: float,
        *, elapsed_at_adoption: Optional[float] = None,
    ) -> None:
        """Anchor the internal clock at the authoritative start time.

        Idempotent: the FIRST authoritative evidence wins — a later
        (possibly degraded SPA) repeat never moves the anchor.

        elapsed_at_adoption: game seconds already elapsed when the start
        evidence was first seen (0 for a true pre-game anchor; > 0 when
        the collector joined mid-game, e.g. after a restart recovery).
        The live object then runs on time.monotonic() from that offset
        (§3) while the wall pair persists for restart recovery (§13).
        """
        if self.anchors.anchored:
            return
        if elapsed_at_adoption is None:
            elapsed_at_adoption = 0.0
        self.anchors = TimerAnchors(
            game_start_wall=float(game_start_wall),
            observed_at_wall=float(observed_at_wall),
            monotonic_anchor=time.monotonic(),
            monotonic_elapsed=float(elapsed_at_adoption),
            quarter_seconds=self.anchors.quarter_seconds,
            break_seconds=self.anchors.break_seconds,
            model=self.anchors.model,
        )

    def is_anchored(self) -> bool:
        return self.anchors.anchored

    def observe_phase(self, phase: int, observed_at_wall: float,
                      clock: Optional[str] = None,
                      captured_at: str = "") -> None:
        """Record one OBSERVED quarter state (§4: transitions calibrate
        the model — Q1 start, Q2 start, ..., post-Q4 end)."""
        phase = int(phase)
        if not 1 <= phase <= 5:
            return
        obs = PhaseObservation(phase=phase, observed_at_wall=observed_at_wall,
                               clock=clock, captured_at=captured_at)
        self.phase_observations.append(obs)
        self.anchors = settle_phase(self.anchors, self.phase_observations)

    def internal_time(self) -> dict[str, Any]:
        """The internal-timer view of NOW (monotonic-first; wall fallback
        makes this exact after restarts too)."""
        it = derive_internal_time(self.anchors, monotonic_now=time.monotonic())
        return {
            "internal_elapsed_seconds": it.internal_elapsed_seconds,
            "internal_game_time": it.internal_game_time,
            "quarter": it.quarter,
            "quarter_elapsed_seconds": it.quarter_elapsed_seconds,
            "quarter_remaining_seconds": it.quarter_remaining_seconds,
            "timer_model": it.model,
            "timer_anchored": it.anchored,
        }


def require_betual(classification: Optional[str]) -> None:
    """§1 gate: the Betual dataset is Betual-only.  Raise otherwise."""
    if classification != CLASSIFICATION_BETUAL:
        raise BetualDatasetError(
            f"non-Betual classification {classification!r} refused "
            f"(dataset is BETUAL-only)")


def clock_difference_s(internal: dict[str, Any],
                       displayed_clock: Optional[str]) -> Optional[float]:
    """internal remaining (current quarter) vs displayed remaining.

    Positive = our internal clock thinks MORE of the quarter remains
    than the displayed clock.  None when either side is unavailable —
    never guessed (§9).
    """
    rem = internal.get("quarter_remaining_seconds")
    disp = parse_clock_seconds(displayed_clock)
    if rem is None or disp is None:
        return None
    return round(float(rem) - float(disp), 3)


def line_movement_fields(
    line: Optional[float], captured_at_wall: float,
    prev: BetualGameRecord,
) -> dict[str, Any]:
    """§6/§7 movement fields vs the previous LINE observation of the game.

    Only genuinely observed values are stored: a NULL line or a missing
    previous observation leaves every delta NULL — never zero, never
    guessed.  line_velocity = line_change / seconds_since_previous_line
    (points per second; line movement per minute is x60).
    """
    out: dict[str, Any] = {
        "line_previous": None, "line_change": None,
        "seconds_since_previous_line": None,
        "line_velocity": None,
    }
    if line is None or prev.prev_line is None or prev.prev_line_at is None:
        return out
    change = round(float(line) - float(prev.prev_line), 3)
    secs = float(captured_at_wall) - float(prev.prev_line_at)
    out["line_previous"] = prev.prev_line
    out["line_change"] = change
    out["seconds_since_previous_line"] = round(secs, 3)
    if secs > 0:
        out["line_velocity"] = round(change / secs, 6)
    return out


def score_movement_fields(
    home: Optional[int], away: Optional[int], prev: BetualGameRecord,
) -> dict[str, Any]:
    """§7 score deltas vs the previous observation of the game."""
    out: dict[str, Any] = {
        "score_previous": None, "score_change": None,
    }
    if home is None or away is None:
        return out
    cur = (int(home), int(away))
    out["score_previous"] = (
        list(prev.prev_score) if prev.prev_score is not None else None)
    if prev.prev_score is not None:
        out["score_change"] = [
            cur[0] - prev.prev_score[0], cur[1] - prev.prev_score[1]]
    return out


class BetualDataset:
    """The recorder the collector calls.  Failure-isolated: recording
    problems raise BetualDatasetError only after the raw payload is
    safe, and the collector logs-and-continues (collection never dies)."""

    def __init__(self, store):
        self.store = store
        self._games: dict[str, BetualGameRecord] = {}

    # ── state ────────────────────────────────────────────────────

    def game(self, source_game_id: str,
             classification: Optional[str] = None) -> Optional[BetualGameRecord]:
        """The per-game record, or None for non-Betual games (gate)."""
        try:
            require_betual(classification)
        except BetualDatasetError:
            return None
        rec = self._games.get(source_game_id)
        if rec is None:
            rec = BetualGameRecord(source_game_id)
            self._games[source_game_id] = rec
        return rec

    def discard(self, source_game_id: str) -> None:
        """A game left tracking — keep the DB state, drop memory only."""
        self._games.pop(source_game_id, None)

    def restore(self, source_game_id: str, classification: Optional[str],
                game_start_wall: Optional[float],
                observed_at_wall: Optional[float],
                quarter_seconds: Optional[float] = None,
                timer_model: Optional[str] = None,
                last_home: Optional[int] = None,
                last_away: Optional[int] = None,
                last_line: Optional[float] = None,
                last_line_at: Optional[float] = None) -> Optional[BetualGameRecord]:
        """Rebuild per-game state after a collector restart (§13).

        The internal clock is reconstructed from the PERSISTED wall
        anchor — the timer is never reset to zero.  Previous score/line
        caches are re-seeded from the game's last persisted observations
        so the first post-restart row still carries movement deltas.
        """
        rec = self.game(source_game_id, classification)
        if rec is None:
            return None
        if quarter_seconds:
            # Calibrated model persisted before the restart: restore it
            # with the wall anchor (monotonic fields unset — the wall
            # fallback path keeps elapsed exact).
            rec.anchors = TimerAnchors(
                game_start_wall=game_start_wall,
                observed_at_wall=observed_at_wall,
                quarter_seconds=float(quarter_seconds),
                break_seconds=rec.anchors.break_seconds,
                model=timer_model or "calibrated",
            )
        elif game_start_wall is not None and observed_at_wall is not None:
            # Mid-game re-adoption after restart: elapsed so far is the
            # wall span from the authoritative start to NOW (§13 — never
            # reset to zero).  adopt_start_evidence anchors the live
            # monotonic clock at that offset.
            elapsed_now = max(0.0, time.time() - float(game_start_wall))
            rec.adopt_start_evidence(
                game_start_wall, observed_at_wall,
                elapsed_at_adoption=elapsed_now)
        if last_home is not None and last_away is not None:
            rec.prev_score = (int(last_home), int(last_away))
            rec.prev_score_total = int(last_home) + int(last_away)
        if last_line is not None:
            rec.prev_line = float(last_line)
            rec.prev_line_at = last_line_at
        return rec

    # ── observation paths (§1/§5/§6/§11/§12) ─────────────────────

    def record_score_observation(
        self, rec: BetualGameRecord, *, source_game_id: str,
        classification: str, captured_at: str, home: Optional[int],
        away: Optional[int], quarter: Optional[int],
        displayed_clock: Optional[str], period_label: Optional[str] = None,
        q_scores: Optional[list[tuple[Any, Any]]] = None,
        raw: Optional[dict] = None,
    ) -> dict[str, Any]:
        """One Betual score observation with internal-timer fields.

        ``quarter``/``displayed_clock`` are the SOURCE's view (stored for
        comparison); the dataset's ``internal_quarter`` derives from the
        timer.  Cumulative -> per-quarter derivation happens here ONLY
        from point-in-time-valid inputs: an earlier quarter is derived
        only when both its own cumulative AND the previous cumulative
        were observed at this instant (§5 — never manufactured from a
        later state; a missing input leaves the value NULL).
        """
        require_betual(classification)
        now_wall = time.time()
        internal = rec.internal_time()
        diff = clock_difference_s(internal, displayed_clock)
        q_scores = q_scores or []
        qh: dict[int, int] = {}
        qa: dict[int, int] = {}
        # §5 derivation: Q1's leg IS its cumulative; Qk's leg derives ONLY
        # from two consecutive observed cumulatives (Qk and Qk-1).  A None
        # hole breaks exactly the legs that would need it — never bridged.
        for i in range(min(4, len(q_scores))):
            cur = q_scores[i]
            if cur is None or cur[0] is None or cur[1] is None:
                continue
            if i == 0:
                qh[1], qa[1] = int(cur[0]), int(cur[1])
            else:
                prev_c = q_scores[i - 1]
                if prev_c is None or prev_c[0] is None or prev_c[1] is None:
                    continue
                qh[i + 1] = int(cur[0]) - int(prev_c[0])
                qa[i + 1] = int(cur[1]) - int(prev_c[1])
        phase = quarter if quarter in (1, 2, 3, 4) else None
        diags: list[dict] = []
        if phase is not None:
            diags = [
                {"kind": d.kind, "detail": d.detail}
                for d in clock_diagnostics(
                    rec.prev_state, phase, now_wall,
                    parse_clock_seconds(displayed_clock), captured_at,
                    internal.get("quarter"),
                    quarter_seconds=rec.anchors.quarter_seconds,
                    model=rec.anchors.model)
            ]
        row: dict[str, Any] = {
            "source_game_id": source_game_id,
            "classification": classification,
            "captured_at": captured_at,
            "source_start_time": (
                rec.anchors.game_start_wall if rec.anchors.anchored else None),
            "internal_game_time": internal["internal_game_time"],
            "internal_elapsed_seconds": internal["internal_elapsed_seconds"],
            "quarter": internal["quarter"],
            "quarter_remaining_seconds": internal["quarter_remaining_seconds"],
            "q1_home_score": qh.get(1), "q1_away_score": qa.get(1),
            "q2_home_score": qh.get(2), "q2_away_score": qa.get(2),
            "q3_home_score": qh.get(3), "q3_away_score": qa.get(3),
            "q4_home_score": qh.get(4), "q4_away_score": qa.get(4),
            "home_score": home, "away_score": away,
            "total_score": (int(home) + int(away)
                            if home is not None and away is not None else None),
            "betual_displayed_clock": displayed_clock,
            "clock_difference": diff,
            "source_quarter": quarter,
            "period_label": period_label,
            "raw": raw or {},
        }
        self.store.insert_betual_time_observation(row)
        # advance phase evidence + caches AFTER a successful persist
        if phase is not None:
            rec.observe_phase(phase, now_wall, displayed_clock, captured_at)
            rec.prev_state = PrevState(
                phase=phase, observed_at_wall=now_wall,
                remaining=parse_clock_seconds(displayed_clock),
                captured_at=captured_at)
        if home is not None and away is not None:
            rec.prev_score = (int(home), int(away))
            rec.prev_score_total = int(home) + int(away)
        rec.last_capture_at = captured_at
        for d in diags:
            self.store.record_betual_clock_diagnostic(
                source_game_id=source_game_id, captured_at=captured_at,
                kind=d["kind"], detail=d["detail"])
        return row

    def record_line_observation(
        self, rec: BetualGameRecord, *, source_game_id: str,
        classification: str, captured_at: str, market_id: str,
        market_name: Optional[str], period: Optional[str],
        line: Optional[float], home: Optional[int], away: Optional[int],
        quarter: Optional[int], displayed_clock: Optional[str] = None,
        over_price: Optional[float] = None,
        under_price: Optional[float] = None,
        raw: Optional[dict] = None,
    ) -> dict[str, Any]:
        """One Betual line observation with movement fields (§6/§7).

        Dedup (§10) collapses only the SAME market at the SAME line at
        the SAME timestamp (a genuine change-and-return across distinct
        timestamps is retained).
        """
        require_betual(classification)
        now_wall = time.time()
        internal = rec.internal_time()
        movement = line_movement_fields(line, now_wall, rec)
        row: dict[str, Any] = {
            "source_game_id": source_game_id,
            "classification": classification,
            "captured_at": captured_at,
            "market_id": market_id,
            "market_name": market_name,
            "period": period,
            "line": line,
            "line_previous": movement["line_previous"],
            "line_change": movement["line_change"],
            "seconds_since_previous_line":
                movement["seconds_since_previous_line"],
            "line_velocity": movement["line_velocity"],
            "internal_game_time": internal["internal_game_time"],
            "internal_elapsed_seconds": internal["internal_elapsed_seconds"],
            "quarter": internal["quarter"],
            "quarter_remaining_seconds": internal["quarter_remaining_seconds"],
            "home_score": home, "away_score": away,
            "total_score": (int(home) + int(away)
                            if home is not None and away is not None else None),
            "score_at_observation": rec.prev_score_total,
            "betual_displayed_clock": displayed_clock,
            "over_price": over_price, "under_price": under_price,
            "raw": raw or {},
        }
        self.store.insert_betual_line_observation(row)
        if line is not None:
            rec.prev_line = float(line)
            rec.prev_line_at = now_wall
        if home is not None and away is not None:
            # frames carry the score alongside the line: advance the
            # score cache so score_at_observation stays point-in-time
            rec.prev_score = (int(home), int(away))
            rec.prev_score_total = int(home) + int(away)
        rec.last_capture_at = captured_at
        return row

    # ── transitions + end (§11/§12) ─────────────────────────────

    def maybe_transition(
        self, rec: BetualGameRecord, *, source_game_id: str,
        classification: str, captured_at: str,
        prev_quarter: Optional[int], new_quarter: Optional[int],
        home: Optional[int], away: Optional[int],
        full_game_line: Optional[float], quarter_line: Optional[float],
        line_previous: Optional[float], displayed_clock: Optional[str],
        q_scores: Optional[list[tuple[Any, Any]]] = None,
        raw: Optional[dict] = None,
    ) -> Optional[dict[str, Any]]:
        """Capture a high-quality snapshot at a QUARTER TRANSITION.

        Fires when the observed quarter moves FORWARD exactly one step
        (Q1->Q2, Q2->Q3, Q3->Q4).  The caller passes the PREVIOUS
        observation's quarter (its own cache — a restart simply passes
        None and no transition is invented).  All §11 fields are stored
        exactly as observed; missing ones are NULL.
        """
        require_betual(classification)
        if prev_quarter not in (1, 2, 3) or new_quarter != prev_quarter + 1:
            return None
        now_wall = time.time()
        internal = rec.internal_time()
        row: dict[str, Any] = {
            "source_game_id": source_game_id,
            "classification": classification,
            "captured_at": captured_at,
            "transition": f"Q{prev_quarter}->Q{new_quarter}",
            "prev_quarter": prev_quarter,
            "new_quarter": new_quarter,
            "internal_game_time": internal["internal_game_time"],
            "internal_elapsed_seconds": internal["internal_elapsed_seconds"],
            "quarter_remaining_seconds":
                internal["quarter_remaining_seconds"],
            "betual_displayed_clock": displayed_clock,
            "clock_difference": clock_difference_s(internal, displayed_clock),
            "prev_quarter_home": None, "prev_quarter_away": None,
            "home_score": home, "away_score": away,
            "total_score": (int(home) + int(away)
                            if home is not None and away is not None else None),
            "full_game_line": full_game_line, "quarter_line": quarter_line,
            "line_previous": line_previous,
            "line_change": (
                round(float(full_game_line) - float(line_previous), 3)
                if full_game_line is not None and line_previous is not None
                else None),
            "raw": raw or {},
        }
        if q_scores and len(q_scores) >= prev_quarter:
            ph, pa = q_scores[prev_quarter - 1]
            if ph is not None and pa is not None:
                row["prev_quarter_home"] = int(ph)
                row["prev_quarter_away"] = int(pa)
        self.store.insert_betual_transition(row)
        return row

    def record_game_end(
        self, rec: BetualGameRecord, *, source_game_id: str,
        classification: str, captured_at: str, home: Optional[int],
        away: Optional[int], quarter: Optional[int],
        displayed_clock: Optional[str] = None,
        full_game_line: Optional[float] = None,
        settlement_state: Optional[str] = None,
        end_evidence: str = "observed_final",
        q_scores: Optional[list[tuple[Any, Any]]] = None,
        raw: Optional[dict] = None,
    ) -> dict[str, Any]:
        """§12 game-end capture: final scores + finalization evidence.

        Final status is whatever the SOURCE proves (settlement_state);
        the internal timer expiring is NEVER recorded as final —
        ``end_evidence`` distinguishes 'observed_final' from
        'disappeared' rows, which is what makes the NO-FINAL diagnosis
        data-driven.
        """
        require_betual(classification)
        now_wall = time.time()
        internal = rec.internal_time()
        row: dict[str, Any] = {
            "source_game_id": source_game_id,
            "classification": classification,
            "captured_at": captured_at,
            "internal_game_time": internal["internal_game_time"],
            "internal_elapsed_seconds": internal["internal_elapsed_seconds"],
            "quarter": internal["quarter"],
            "final_home_score": home, "final_away_score": away,
            "final_total": (int(home) + int(away)
                            if home is not None and away is not None else None),
            "q1_home_score": None, "q1_away_score": None,
            "q2_home_score": None, "q2_away_score": None,
            "q3_home_score": None, "q3_away_score": None,
            "q4_home_score": None, "q4_away_score": None,
            "full_game_line": full_game_line,
            "betual_displayed_clock": displayed_clock,
            "settlement_state": settlement_state,
            "end_evidence": end_evidence,
            "source_quarter": quarter,
            "raw": raw or {},
        }
        if q_scores:
            for i in range(1, 5):
                if len(q_scores) >= i:
                    h, a = q_scores[i - 1]
                    row[f"q{i}_home_score"] = int(h) if h is not None else None
                    row[f"q{i}_away_score"] = int(a) if a is not None else None
        self.store.insert_betual_game_end(row)
        rec.observe_phase(5, now_wall, displayed_clock, captured_at)
        return row


def safe_record(store, fn, *args, **kwargs) -> Any:
    """Run one recording call, isolating failures to a logged error.

    Collection must never die because the dataset layer did (§9 keeps
    raw data in the caller's hands regardless of outcome here)."""
    try:
        return fn(*args, **kwargs)
    except BetualDatasetError:
        raise
    except Exception:
        traceback.print_exc()
        store_record_failure(store)
        return None


def store_record_failure(store) -> None:
    try:
        store.record_betual_parse_failure(
            source="", raw_json="", error="dataset recording failure")
    except Exception:
        pass
