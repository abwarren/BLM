"""BLM V4 — Internal Betual Game Timer (directive 2026-09-22, DATA
COLLECTION ONLY).

Betual has NO timeouts, so the collector must maintain its OWN monotonic
game timer and never adopt the bookmaker's displayed countdown as the
authoritative clock (§2/§3).  Design:

    game_start_wall_time = Betual start evidence (WS start_ts preferred)
    monotonic_anchor     = local time.monotonic() at anchor acceptance
    elapsed_game_seconds = monotonic_now - monotonic_anchor

All timing the dataset publishes (internal_elapsed_seconds,
internal_game_time, quarter_remaining_seconds) derives from THIS model
only.  The bookmaker's displayed clock is an observation/debug field
(betual_displayed_clock + clock_difference) — it must never feed back
into the internal state.

The timer deliberately does NOT assume quarter lengths blindly (§4): a
settle_phase() call calibrates the model from OBSERVED quarter
transitions (Q(k) end -> Q(k+1) start).  Until calibration exists the
construction default (4 x 10:00 = 600s) is used and honestly reported as
model='default'; after calibration model='calibrated'.

Survivability (§13): the anchor is derivable from PERSISTED evidence —
the WS feed's start_ts (a wall-clock epoch seconds) plus the wall time
it was first observed.  A restart reconstructs:
    elapsed_now = (wall_now - game_start_wall) minus break_seconds
 monotonic clock is preferred for the live object; wall-clock anchors
make restart recovery exact and NTP-safe (wall adjustments move BOTH
terms of the difference, so the elapsed delta cannot go backwards for
any restart that is not itself a time machine).

Nothing in this module touches alerts, fingerprints, betting logic,
thresholds or any production decision path.  Pure functions + a small
state object — no I/O, no network, no database.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

# Construction default (§4: used only until observed transitions
# calibrate it — projection.py's BETUAL_NBA basis, 4 x 10 min).
DEFAULT_QUARTER_SECONDS = 600.0
DEFAULT_BREAK_SECONDS = 0.0     # §4: no-timeout model — no hidden pauses
QUARTERS = 4

_CLOCK_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$")


def parse_clock_seconds(clock: Optional[str]) -> Optional[float]:
    """MM:SS (or H:MM:SS) -> seconds; None when absent/unparseable.

    The displayed clock is a DOWN-COUNT within the quarter in the
    BetConstruct presentation ('05:00' = five minutes left).
    """
    if not clock:
        return None
    m = _CLOCK_RE.match(str(clock).strip())
    if not m:
        return None
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    return h * 3600.0 + mm * 60.0 + ss


@dataclass(frozen=True)
class TimerAnchors:
    """The persisted, restart-safe timing identity of one game (§13).

    game_start_wall: epoch seconds of the authoritative game start (from
      Betual start evidence — the WS feed's start_ts).  None until the
      source exposes it.
    observed_at_wall: epoch seconds when that start evidence was first
      captured (the anchor's wall-clock receipt stamp).
    monotonic_anchor: time.monotonic() at the same instant (live-object
      convenience; NOT persisted — wall anchors restart).
    quarter_seconds / break_seconds: the current internal model.
    model: 'default' | 'calibrated' (§4: honest provenance of lengths).
    """

    game_start_wall: Optional[float] = None
    observed_at_wall: Optional[float] = None
    monotonic_anchor: Optional[float] = None
    monotonic_elapsed: Optional[float] = None   # game-elapsed at anchor
    quarter_seconds: float = DEFAULT_QUARTER_SECONDS
    break_seconds: float = DEFAULT_BREAK_SECONDS
    model: str = "default"

    @property
    def anchored(self) -> bool:
        return self.game_start_wall is not None

    def with_calibration(self, quarter_seconds: float,
                         break_seconds: float) -> "TimerAnchors":
        """Same identity, calibrated model (never shortens a quarter)."""
        return replace(
            self,
            quarter_seconds=max(1.0, float(quarter_seconds)),
            break_seconds=max(0.0, float(break_seconds)),
            model="calibrated",
        )


@dataclass(frozen=True)
class PhaseObservation:
    """One OBSERVED state transition used to calibrate/validate the timer
    (§4: record game start, Q1..Q4 start/end, game end from evidence)."""

    phase: int                  # 1..4 = that quarter is now LIVE; 0 = pre;
                                # 5 = post-Q4 (final/ended)
    observed_at_wall: float     # epoch seconds of the observation
    clock: Optional[str] = None         # bookmaker displayed clock (debug only)
    captured_at: str = ""               # collector capture timestamp


@dataclass(frozen=True)
class InternalTime:
    """The timer's derived view of ONE instant (§3)."""

    internal_elapsed_seconds: float
    internal_game_time: str             # MM:SS elapsed presentation
    quarter: Optional[int]              # 1..4, None pre-game
    quarter_elapsed_seconds: Optional[float]
    quarter_remaining_seconds: Optional[float]
    model: str                          # 'default' | 'calibrated'
    anchored: bool
    source: str = "internal"            # never 'betual' — §2 invariant


def format_mmss(seconds: float) -> str:
    """Elapsed seconds -> MM:SS (hours roll into minutes when needed)."""
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def derive_internal_time(
    anchors: TimerAnchors, monotonic_now: Optional[float] = None,
    wall_now: Optional[float] = None,
) -> InternalTime:
    """Derive current quarter + elapsed + remaining from the INTERNAL
    model only (§3 — the displayed countdown is never an input).

    Quarter model with no-timeout continuity (§4): Qk spans
        [(k-1)*q + (k-1)*b,  k*q + (k-1)*b)
    with q = quarter_seconds, b = break_seconds between quarters;
    elapsed beyond the Q4 span is capped at the full-game end (post-Q4
    the game is over; extra real time is NOT invented game time).
    """
    if not anchors.anchored:
        return InternalTime(
            internal_elapsed_seconds=0.0, internal_game_time="00:00",
            quarter=None, quarter_elapsed_seconds=None,
            quarter_remaining_seconds=None, model=anchors.model,
            anchored=False)
    if (monotonic_now is not None and anchors.monotonic_anchor is not None
            and anchors.monotonic_elapsed is not None):
        # Live in-process path: the monotonic clock is authoritative (§3)
        # — wall/NTP adjustments cannot move the game timer.  The
        # elapsed-at-adoption offset keeps a MID-GAME join correct (the
        # anchor is the authoritative start, not the adoption instant).
        elapsed = anchors.monotonic_elapsed + (
            float(monotonic_now) - anchors.monotonic_anchor)
    else:
        # Restart path (§13): the anchor is persisted wall-clock evidence;
        # both terms of the difference move together under NTP, so the
        # elapsed delta cannot go backwards.
        wall = float(wall_now) if wall_now is not None else _wall_now()
        elapsed = wall - float(anchors.game_start_wall)
    elapsed = max(0.0, elapsed)

    q = anchors.quarter_seconds
    b = anchors.break_seconds
    full = QUARTERS * q + (QUARTERS - 1) * b
    shown = min(elapsed, full)          # no invented time after the model's end
    quarter = min(QUARTERS, int(shown // (q + b)) + 1)
    q_start = (quarter - 1) * (q + b)
    q_elapsed = shown - q_start
    return InternalTime(
        internal_elapsed_seconds=round(shown, 3),
        internal_game_time=format_mmss(shown),
        quarter=quarter,
        quarter_elapsed_seconds=round(q_elapsed, 3),
        quarter_remaining_seconds=round(max(0.0, q - q_elapsed), 3),
        model=anchors.model,
        anchored=True,
    )


def settle_phase(
    anchors: Optional[TimerAnchors],
    observations: list[PhaseObservation],
) -> TimerAnchors:
    """Calibrate the timer model from OBSERVED quarter transitions (§4).

    Anchor adoption is NOT this function's job: the caller sets
    ``game_start_wall`` from authoritative start evidence (§2).  This
    function only calibrates the quarter model, which needs NO anchor —
    consecutive phase observations measure elapsed spans directly, so a
    game joined mid-stream (no start_ts yet) still calibrates.

    Each consecutive pair Q(k) -> Q(k+1) measures one quarter + one break:
        q + b = t(Q k+1 observed) - t(Q k observed)
    The calibration takes the MEDIAN of the per-step estimates (robust to
    one laggy transition observation) and attributes ALL of it to the
    quarter with the no-timeout break model (b stays 0: virtual replays
    have no halftime pause in the clock model; breaks, if ever real,
    show up as timer-vs-source discrepancy in the diagnostics, not as a
    silently assumed pause).

    With no usable observations the incoming model is kept unchanged
    (§4: never assume lengths blindly — the default IS reported as
    model='default').
    """
    if anchors is None:
        anchors = TimerAnchors()
    starts = sorted(
        (o for o in observations if 1 <= o.phase <= QUARTERS),
        key=lambda o: (o.observed_at_wall, o.phase))
    estimates: list[float] = []
    prev_at: Optional[float] = None
    prev_phase: Optional[int] = None
    for o in starts:
        if prev_phase is not None and o.phase == prev_phase + 1 \
                and prev_at is not None:
            step = o.observed_at_wall - prev_at
            if step > 0:
                estimates.append(step)
        prev_at, prev_phase = o.observed_at_wall, o.phase
    observed_at_wall = anchors.observed_at_wall \
        or min((o.observed_at_wall for o in observations), default=None)
    if not estimates:
        return replace(anchors, observed_at_wall=observed_at_wall)
    estimates.sort()
    median = estimates[len(estimates) // 2]
    return replace(
        anchors,
        observed_at_wall=observed_at_wall,
        quarter_seconds=max(1.0, median),
        break_seconds=0.0,
        model="calibrated",
    )


# ── Clock validation diagnostics (§14) ───────────────────────────────

BACKWARD_DRIFT_S = 1.0        # displayed clock moved backwards ≥ 1s
IMPOSSIBLE_ELAPSED_S = 30.0   # observed span exceeded the model by > 30s
TRANSITION_GAP_S = 240.0      # quarter boundary observed implausibly late
LARGE_CLOCK_DIFF_S = 90.0     # |internal - displayed| ≥ 90s
DUPLICATE_WINDOW_S = 0.0      # exact-same-timestamp observations


@dataclass(frozen=True)
class ClockDiagnostic:
    kind: str
    detail: str


@dataclass(frozen=True)
class PrevState:
    """The previous observation's state — the diagnostic baseline."""

    phase: Optional[int]
    observed_at_wall: float
    remaining: Optional[float]
    captured_at: str


def clock_diagnostics(
    prev: Optional[PrevState], phase: Optional[int],
    observed_at_wall: float, remaining: Optional[float],
    captured_at: str, quarter: Optional[int],
    quarter_seconds: float = DEFAULT_QUARTER_SECONDS,
    model: str = "default",
) -> list[ClockDiagnostic]:
    """Compare one observation against the previous one — FLAGS ONLY
    (§14: diagnostics must never steer the internal timer).

    Flags: backward displayed-clock movement, impossible elapsed time
    (span longer than the model's quarter+slack within one phase),
    implausible transition gaps, duplicate timestamps, and (only under a
    CALIBRATED model — under 'default' the lag is a known property, not
    an anomaly) internal-phase vs source-quarter mismatch.  A first
    observation (prev=None) yields no diagnostics.
    """
    out: list[ClockDiagnostic] = []
    if prev is None:
        return out
    span = observed_at_wall - prev.observed_at_wall
    if span <= DUPLICATE_WINDOW_S and prev.captured_at == captured_at:
        out.append(ClockDiagnostic(
            "duplicate_timestamp",
            f"two observations share captured_at={captured_at}"))
    if remaining is not None and prev.remaining is not None \
            and prev.phase == phase \
            and remaining > prev.remaining + BACKWARD_DRIFT_S:
        out.append(ClockDiagnostic(
            "backward_clock_movement",
            f"displayed clock moved backwards within Q{phase}: "
            f"{prev.remaining:.0f}s -> {remaining:.0f}s remaining"))
    if span > (quarter_seconds + IMPOSSIBLE_ELAPSED_S) \
            and prev.phase == phase and phase in (1, 2, 3, 4):
        out.append(ClockDiagnostic(
            "impossible_elapsed_time",
            f"{span:.0f}s observed within Q{phase} "
            f"(model quarter {quarter_seconds:.0f}s)"))
    if prev.phase in (1, 2, 3) and phase == prev.phase + 1 \
            and span > TRANSITION_GAP_S:
        out.append(ClockDiagnostic(
            "quarter_transition_anomaly",
            f"Q{prev.phase}->Q{phase} gap {span:.0f}s "
            f"> {TRANSITION_GAP_S:.0f}s"))
    if (model == "calibrated" and quarter is not None
            and phase is not None and quarter != phase):
        out.append(ClockDiagnostic(
            "phase_quarter_mismatch",
            f"internal phase {phase} vs source quarter {quarter} "
            f"(calibrated model)"))
    return out


def _wall_now() -> float:
    import time
    return time.time()
