"""BLM V4 — Pace Projector (deterministic trajectory layer).

Answers, from validated clean observations only:

    what HAS happened  → actual pace, recent pace (1/2/3/5 game-min)
    what MUST happen   → required pace to reach the live total line
    the gap between    → pace_gap, required/actual ratio
    where current pace projects → projected final total (trajectory)
    vs the market      → projection vs live line (signed, NOT a probability)
    what followed      → subsequent-observation linkage + trajectory state

The statistical boundary is blm_metrics_clean.db from the clean epoch:
only status='VALID' clean observations feed this layer.  NO z-scores,
NO probabilities, NO ranges, NO calibration, NO signals — purely
deterministic state/trajectory information.

No look-ahead: the metrics at time T (pace, required pace, gap,
projection, recent pace, acceleration) are computed ONLY from
observations at-or-before T.  Future observations are stored ONLY as
outcome fields (subsequent_*, final_settled_total) for later analysis,
and the descriptive trajectory state (A/B/C/D) is itself an outcome
category assigned with hindsight ("what subsequently happened").

Recent pace uses GAME-CLOCK elapsed minutes (clean_observations
elapsed_game_minutes), never wall-clock ingestion time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

MODEL_VERSION = "v4-pace-projector-1"

# Market freshness convention (mirrors the dashboard/API 300s threshold):
# a line observed more than this many seconds before the observation is
# STALE; at/under it is LIVE; absent is MISSING.
FRESH_LINE_SECONDS = 300.0

# |pace_gap| at/above this many Pts/min is treated as "significantly"
# above/below — the deterministic split between trajectory states.
PACE_GAP_SIGNIFICANT_MIN = 1.0

# Recent-pace windows in GAME minutes.
RECENT_WINDOWS_MIN = (1, 2, 3, 5)


def _parse_ts(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(x, n)


# ── Core calculations ─────────────────────────────────────────────────


def pace_gap(required_pts_per_min: Optional[float],
             actual_pts_per_min: Optional[float]) -> Optional[float]:
    """required - actual (signed).

    positive → required future pace is HIGHER than current pace
    negative → required future pace is LOWER than current pace
    zero     → equal
    Neither state is "good"/"bad" and neither implies Over or Under.
    """
    if required_pts_per_min is None or actual_pts_per_min is None:
        return None
    return _r(required_pts_per_min - actual_pts_per_min)


def required_to_actual_ratio(required_pts_per_min: Optional[float],
                             actual_pts_per_min: Optional[float]) -> Optional[float]:
    """required / actual, preserved raw — very large, negative, or
    undefined values are NOT capped or transformed.  NULL when actual is
    zero or either side is undefined."""
    if required_pts_per_min is None or actual_pts_per_min is None:
        return None
    if actual_pts_per_min == 0:
        return None
    return _r(required_pts_per_min / actual_pts_per_min)


def trajectory_projection(current_total_points: Optional[int],
                          actual_pts_per_min: Optional[float],
                          remaining_game_minutes: Optional[float]) -> Optional[float]:
    """Final total implied by the CURRENT observed pace:

        current_total_points + actual_pts_per_min * remaining_game_minutes

    Deterministic.  NULL when actual pace is undefined (incl. zero elapsed)
    or remaining time is zero/invalid — never a fabricated value.
    """
    if current_total_points is None or actual_pts_per_min is None:
        return None
    if remaining_game_minutes is None or remaining_game_minutes <= 0:
        return None
    return _r(current_total_points + actual_pts_per_min * remaining_game_minutes, 1)


def projection_vs_live_line(projected_final_total: Optional[float],
                            live_total_line: Optional[float]) -> Optional[float]:
    """projected_final_total - live_total_line (signed).  Positive → the
    current pace projects ABOVE the live line.  NOT a probability."""
    if projected_final_total is None or live_total_line is None:
        return None
    return _r(projected_final_total - live_total_line, 2)


def market_age_seconds(captured_at: Optional[str],
                       market_captured_at: Optional[str]) -> Optional[float]:
    """Age of the live line at the observation (captured - market ts),
    clamped at >= 0 (a line from the same tick is fresh, not negative)."""
    t = _parse_ts(captured_at)
    m = _parse_ts(market_captured_at)
    if t is None or m is None:
        return None
    return max(0.0, round((t - m).total_seconds(), 2))


def market_status(market_age_seconds: Optional[float]) -> Optional[str]:
    """LIVE (age <= 300s) | STALE (age > 300s) | MISSING (no line time)."""
    if market_age_seconds is None:
        return "MISSING"
    return "LIVE" if market_age_seconds <= FRESH_LINE_SECONDS else "STALE"


def classify_trajectory_state(
    pace_gap_value: Optional[float],
    subsequent_pace_change: Optional[float],
) -> Optional[str]:
    """Descriptive trajectory-state category, assigned with hindsight
    from what subsequently happened (never an assumption about the
    bookmaker, never a prediction):

      A  required pace significantly ABOVE actual, THEN accelerated
      B  required pace significantly ABOVE actual, pace remained low
      C  required pace significantly BELOW actual, THEN decelerated
      D  required pace significantly BELOW actual, pace remained high

    NULL when the gap is insignificant or no subsequent observation
    exists to measure what followed.
    """
    if pace_gap_value is None or subsequent_pace_change is None:
        return None
    if pace_gap_value >= PACE_GAP_SIGNIFICANT_MIN:
        return "A" if subsequent_pace_change > 0 else "B"
    if pace_gap_value <= -PACE_GAP_SIGNIFICANT_MIN:
        return "C" if subsequent_pace_change < 0 else "D"
    return None


# ── Recent pace / acceleration (game-clock windows) ──────────────────


def _window_pace(rows: list[dict], idx: int,
                 window_min: float) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """(pace, span_min, ref_idx) for the trailing game-clock window of
    ``window_min`` minutes ending at rows[idx] (rows ascending by id).

    Reference = the most recent observation at-or-before (elapsed -
    window_min); pace = points gained / ACTUAL span (recorded so the
    exact window is explicit).  Returns all-None when insufficient
    observations exist — never a fabricated value.
    """
    cur = rows[idx]
    e_cur = cur.get("elapsed_game_minutes")
    cur_total = cur.get("total_points")
    if e_cur is None or cur_total is None:
        return (None, None, None)
    target = e_cur - window_min
    for j in range(idx - 1, -1, -1):
        e = rows[j].get("elapsed_game_minutes")
        if e is None or e > target:
            continue
        ref_total = rows[j].get("total_points")
        if ref_total is None:
            return (None, None, None)
        span = e_cur - e
        if span <= 0:
            return (None, None, None)
        pace = (cur_total - ref_total) / span
        return (_r(pace), _r(span, 2), j)
    return (None, None, None)


def recent_pace(rows: list[dict], idx: int,
                window_min: float) -> Optional[float]:
    """Recent pace over the trailing ``window_min`` game-minutes."""
    pace, _, _ = _window_pace(rows, idx, window_min)
    return pace


def pace_acceleration(
    rows: list[dict], idx: int, window_min: float,
) -> tuple[Optional[float], Optional[str]]:
    """(acceleration, window_label): current window pace MINUS the
    PRECEDING window pace of the same length (e.g. last 1 game-min vs the
    1 game-min before it — windows never mixed).  positive → acceleration,
    negative → deceleration, zero → unchanged.  Descriptive, not
    predictive."""
    pace_now, _, ref_idx = _window_pace(rows, idx, window_min)
    if pace_now is None or ref_idx is None:
        return (None, None)
    pace_prev, _, _ = _window_pace(rows, ref_idx, window_min)
    if pace_prev is None:
        return (None, None)
    label = f"{window_min:g}m_vs_prev_{window_min:g}m"
    return (_r(pace_now - pace_prev), label)


# ── Engine ────────────────────────────────────────────────────────────


class PaceProjector:
    """Deterministic trajectory engine over clean observations.

    ``refresh_game(store, source_game_id)`` recomputes the full trajectory
    row set for one game from its VALID clean observations (idempotent,
    all-or-nothing per game).  Called by the collector as observations
    arrive so the subsequent-observation linkage stays current.
    """

    def refresh_game(self, store: Any, source_game_id: str) -> dict[str, Any]:
        rows = store.valid_observations(source_game_id)
        final_total = store.game_final_total(source_game_id)
        projections = [self._project_observation(rows, i, final_total)
                       for i in range(len(rows))]
        store.replace_projections(source_game_id, projections)
        return {"source_game_id": source_game_id, "n": len(projections)}

    def latest_for_game(self, store: Any, source_game_id: str) -> Optional[dict]:
        """Most recent trajectory row — the CURRENT live trajectory."""
        return store.latest_projection(source_game_id)

    def _project_observation(
        self, rows: list[dict], idx: int, final_total: Optional[int],
    ) -> dict[str, Any]:
        obs = rows[idx]
        actual = obs.get("actual_pts_per_min")
        required = obs.get("required_pts_per_min")
        total = obs.get("total_points")
        line = obs.get("live_total_line")
        elapsed = obs.get("elapsed_game_minutes")
        remaining = obs.get("remaining_game_minutes")

        gap = pace_gap(required, actual)
        ratio = required_to_actual_ratio(required, actual)
        projected = trajectory_projection(total, actual, remaining)
        pvl = projection_vs_live_line(projected, line)
        # no line → MISSING (nothing to age); a line at/under the 300s
        # freshness threshold is LIVE, older is STALE
        age = None if line is None else market_age_seconds(
            obs.get("captured_at"), obs.get("market_captured_at"))
        mstatus = "MISSING" if line is None else market_status(age)

        recent: dict[str, Optional[float]] = {}
        for w in RECENT_WINDOWS_MIN:
            pace, span, _ = _window_pace(rows, idx, float(w))
            key = f"recent_pace_{w:g}m"
            recent[key] = pace
            recent[f"recent_span_{w:g}m"] = span

        accel, accel_window = pace_acceleration(rows, idx, 1.0)

        # Subsequent observation — OUTCOME storage only (never used in the
        # row's own state metrics above).
        sub = rows[idx + 1] if idx + 1 < len(rows) else None
        sub_pace = sub.get("actual_pts_per_min") if sub else None
        sub_line = sub.get("live_total_line") if sub else None
        sub_change = _r(sub_pace - actual) \
            if (sub_pace is not None and actual is not None) else None
        sub_line_change = _r(sub_line - line, 2) \
            if (sub_line is not None and line is not None) else None
        state = classify_trajectory_state(gap, sub_change)

        return {
            "observation_id": obs["id"],
            "model_version": MODEL_VERSION,
            "source_game_id": obs["source_game_id"],
            "classification": obs["classification"],
            "captured_at": obs["captured_at"],
            "period_label": obs.get("period_label"),
            "clock": obs.get("clock"),
            "elapsed_game_minutes": elapsed,
            "remaining_game_minutes": remaining,
            "progress_pct": obs.get("progress_pct"),
            "current_total_points": total,
            "live_total_line": line,
            "market_captured_at": obs.get("market_captured_at"),
            "market_age_seconds": age,
            "market_status": mstatus,
            "actual_pts_per_min": actual,
            "required_pts_per_min": required,
            "pace_gap": gap,
            "required_to_actual_ratio": ratio,
            "projected_final_total": projected,
            "projection_vs_live_line": pvl,
            "fair_total": obs.get("fair_total"),
            **recent,
            "pace_acceleration": accel,
            "acceleration_window": accel_window,
            "trajectory_state": state,
            "subsequent_observation_id": sub["id"] if sub else None,
            "subsequent_actual_pace": sub_pace,
            "subsequent_pace_change": sub_change,
            "subsequent_live_line": sub_line,
            "subsequent_live_line_change": sub_line_change,
            "final_settled_total": final_total,
            # terminal eligibility propagated from the clean observation
            # (TERMINAL = SETTLEMENT/AUDIT ONLY — the trajectory row of a
            # terminal frame stays descriptive but is NOT research-eligible)
            "terminal": int(obs.get("terminal") or 0),
            "predictive_eligible": int(obs.get("predictive_eligible") or 0),
            "status": "VALID",
            "computed_at": _now_iso(),
        }