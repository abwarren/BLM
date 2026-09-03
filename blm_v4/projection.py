"""BLM V4 — Shared Projection Math.

Single source of truth for the projection function that the live
dashboard and the accuracy scorecard both use, so a scored prediction
is exactly what the model would have displayed at that moment.

Pure functions of stored snapshots only — never uses final results.
Mirrors the math in ``blm_v4.api._analyze_game`` (parity is pinned by
tests/test_scorecard.py::test_projection_parity_with_api).
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Optional

QUARTER_MINUTES = 10.0
FULL_GAME_MINUTES = 40.0  # cyber/virtual basketball: 4 × 10 min

# Bump this whenever the projection algorithm changes.  Accuracy
# aggregates are always split by model version — never mixed.
MODEL_VERSION = "v4-pace-1"

_RE_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})'?$")


def quantize_half(x: Optional[float]) -> Optional[float]:
    """Deterministic nearest-0.5 quantization (x.0/x.5 grid), HALF-UP.

    Authoritative BLM predictions must lie on the half-point grid
    {n/2 : n in Z} — never an arbitrary decimal.  Examples:
        174.24 -> 174.0   174.25 -> 174.5   174.74 -> 174.5   174.75 -> 175.0

    NOT ``round(raw * 2) / 2`` — Python's round() is banker's rounding
    (ties-to-even).  This uses ``floor(2x + 0.5 + EPS) / 2``, i.e. ties
    round UP toward +infinity: a value exactly on a .25/.75 boundary
    rounds up to the next half (negative ties likewise move toward
    +inf: -174.25 -> -174.0).  The EPS fuzz (~1e-9 on the scaled value)
    absorbs binary-float error so a true boundary computed as
    174.24999999999997 still rounds up; inputs are 1dp decimals whose
    genuine float error is ~1e-13, so the fuzz can never flip a real
    non-boundary value.  Returns None for None (pass-through).
    """
    if x is None:
        return None
    return math.floor(x * 2.0 + 0.5 + 1e-9) / 2.0


def _ceil_half(x: Optional[float]) -> Optional[float]:
    """Smallest half-grid value >= x (x.0/x.5, ceil on the half grid).

    Used to lift a quantized total above a raw floored sum without
    leaving the authoritative grid.  None passes through.
    """
    if x is None:
        return None
    return math.ceil(x * 2.0 - 1e-9) / 2.0


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _i(v: Any) -> Optional[int]:
    return None if v is None else int(v)


def _parse_ts(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def clock_minutes(quarter: Optional[int], clock: Optional[str]) -> Optional[float]:
    """Elapsed game minutes from period + clock (MM:SS or M')."""
    q = _i(quarter)
    if q is None or q < 1:
        return None
    c = (clock or "").strip()
    m = _RE_CLOCK.match(c)
    if m:
        mm, ss = int(m.group(1)), int(m.group(2))
        if mm > 12:  # MM:SS where MM is actually minutes-of-clock style
            return None
        # BetConstruct virtual clocks display 12:00 at a period start and
        # tick down; a quarter is QUARTER_MINUTES long, so any display of
        # 10:00+ means the period clock has NOT begun counting down
        # (period-start/boundary sentinel — e.g. "12:00", "11:30").
        # Clamping the contribution at 0 instead of letting it go negative
        # fixes the 2-minute undercount that mislabeled checkpoint
        # positions (12:00 -> elapsed (q-1)*10, not (q-1)*10 - 2).
        contrib = max(0.0, QUARTER_MINUTES - mm - ss / 60.0)
        return round((q - 1) * QUARTER_MINUTES + contrib, 2)
    try:
        return round((q - 1) * QUARTER_MINUTES + float(c.rstrip("'`")), 2)
    except Exception:
        return None


def pace_from_snapshots(rows: list[dict]) -> Optional[float]:
    """Points-per-full-game pace from wall-clock deltas (fallback: game clock)."""
    scored = [r for r in rows if r.get("home_score") is not None
              and r.get("away_score") is not None]
    if len(scored) >= 2:
        t0, t1 = _parse_ts(scored[0]["captured_at"]), _parse_ts(scored[-1]["captured_at"])
        if t0 and t1 and (t1 - t0).total_seconds() >= 30:
            span_min = (t1 - t0).total_seconds() / 60.0
            pts = (scored[-1]["home_score"] + scored[-1]["away_score"]
                   - scored[0]["home_score"] - scored[0]["away_score"])
            if pts >= 0 and span_min > 0:
                pace = pts / span_min * FULL_GAME_MINUTES
                if 20 <= pace <= 400:
                    return round(pace, 1)
    last = scored[-1] if scored else None
    if last:
        el = clock_minutes(last.get("quarter"), last.get("clock"))
        if el and el > 0:
            total = (last["home_score"] or 0) + (last["away_score"] or 0)
            pace = total / el * FULL_GAME_MINUTES
            if 20 <= pace <= 400:
                return round(pace, 1)
    return None


def market_snapshot(rows: list[dict]) -> Optional[dict]:
    """Most recent snapshot carrying a market total (bookmaker O/U line).

    Panel/list snapshots are written every tick WITHOUT a market payload;
    the bookmaker line persists between event-view captures, so the last
    non-null line is the current market state — never treat a stub as
    "no market".
    """
    for r in reversed(list(rows)):
        if r.get("total_line") is not None:
            return r
    return None


def opening_snapshot(rows: list[dict]) -> Optional[dict]:
    """FIRST snapshot carrying a market total — the event's opening line.

    Distinct from market_snapshot (latest): the opening line is the first
    verified PokerBet observation for this event and never changes as the
    market moves.  Games captured mid-game report the first line observed
    at capture time (honest: no pre-game line exists for them).
    """
    for r in rows:
        if r.get("total_line") is not None:
            return r
    return None


def closing_snapshot(rows: list[dict], ended: bool = False) -> Optional[dict]:
    """LAST verified market total at-or-before the game's terminal state.

    The closing line only exists once the market/game has CLOSED (game
    ended).  For a still-live game this is None — the latest live line is
    NOT the closing line, regardless of how recent it is.  Once the game
    ends, the last verified observation (captured before close) becomes
    the immutable closing line; ended games receive no further snapshots,
    so it can never change.
    """
    if not ended:
        return None
    last = None
    for r in rows:
        if r.get("total_line") is not None:
            last = r
    return last


def project(rows: list[dict], market_override: Optional[float] = None) -> dict[str, Any]:
    """Full projection for a game from its snapshots (ascending).

    Returns pace, expected_total, expected_margin, home_projection,
    away_projection, elapsed_minutes, progress (0..1).  Any of the
    projections may be None when the input is too sparse.

    ``market_override`` pins the observed O/U line used by the model
    (e.g. a fresher eu-swarm WS observation than the last event-view
    snapshot).  Defaults to the last snapshot-carried line; the model
    NEVER fabricates a line — override is always observed PokerBet data.
    """
    rows = list(rows)
    scored = [r for r in rows if r.get("home_score") is not None
              and r.get("away_score") is not None]
    latest = scored[-1] if scored else None
    home_score = _i(latest["home_score"]) if latest else None
    away_score = _i(latest["away_score"]) if latest else None
    if market_override is not None:
        total_line = market_override
    else:
        mkt = market_snapshot(rows)
        total_line = _f(mkt["total_line"]) if mkt else None

    pace = pace_from_snapshots(rows)
    if pace is None:
        pace = total_line if total_line else 100.0
    expected_total = round(0.7 * pace + 0.3 * total_line, 1) if total_line else pace

    expected_margin = 0.0
    if home_score is not None and away_score is not None:
        el = clock_minutes(latest.get("quarter"), latest.get("clock")) if latest else None
        if el and el > 1:
            expected_margin = round((home_score - away_score) / el * FULL_GAME_MINUTES, 1)
        elif len(scored) >= 2:
            t0, t1 = _parse_ts(scored[0]["captured_at"]), _parse_ts(scored[-1]["captured_at"])
            if t0 and t1 and (t1 - t0).total_seconds() >= 60:
                span_min = (t1 - t0).total_seconds() / 60.0
                expected_margin = round(
                    (home_score - away_score - scored[0]["home_score"]
                     + scored[0]["away_score"]) / span_min * FULL_GAME_MINUTES, 1,
                )

    home_projection = round((expected_total + expected_margin) / 2, 1)
    away_projection = round((expected_total - expected_margin) / 2, 1)

    # Live-score floor: a FINAL projection must never sit below the points
    # already on the board.  Conceptually the rate-based model computes
    #   CURRENT SCORE + MODELLED REMAINING POINTS = PROJECTED FINAL
    # (pace = full-game total at the observed scoring rate), but a short or
    # stale pace window can under-sample the true rate and produce a split
    # that contradicts the scoreboard (observed live: home projection 86.7
    # while the home team had already scored 109).  Floor each team, then
    # lift the total to the sum of the floored teams and re-derive the
    # margin so every consumer (API, dashboard, scorecard) sees one
    # coherent final projection satisfying:
    #   home_projection >= home_score
    #   away_projection >= away_score
    #   expected_total  >= home_projection + away_projection
    if home_score is not None:
        home_projection = max(home_projection, float(home_score))
    if away_score is not None:
        away_projection = max(away_projection, float(away_score))
    expected_total = max(expected_total, round(home_projection + away_projection, 1))
    expected_margin = round(home_projection - away_projection, 1)

    # ── Authoritative x.0/x.5 quantization (model-output invariant) ──
    # BLM's authoritative prediction/fair total MUST lie on the half-point
    # grid {n/2}.  Quantize at THIS final boundary — AFTER the live-score
    # floor — so the floor can never reintroduce an arbitrary decimal and
    # every consumer (checkpoint_market, prediction_scores, predictions,
    # API, dashboard) sees the same x.0/x.5 value.  The away split is
    # re-derived from the quantized total (et_q - home) so the documented
    # consistency home + away == expected_total holds EXACTLY, each team
    # stays at/above its live score (et_q >= floored home+away), and no
    # separate downstream rounding rule exists.  The 70/30 pace/market
    # blend and the floor guard are untouched — only the final
    # granularity tightens from 0.1 to 0.5.
    if (home_projection is not None and away_projection is not None
            and expected_total is not None):
        hp_f = home_projection
        q_et = quantize_half(expected_total)
        c_sum = _ceil_half(hp_f + away_projection)
        assert q_et is not None and c_sum is not None  # inputs non-None above
        et_q = max(q_et, c_sum)
        home_projection = hp_f
        away_projection = et_q - hp_f
        expected_total = et_q
        expected_margin = round(home_projection - away_projection, 1)

    elapsed = clock_minutes(latest.get("quarter"), latest.get("clock")) if latest else None
    progress = None
    if elapsed is not None:
        progress = round(min(1.0, max(0.0, elapsed / FULL_GAME_MINUTES)), 4)

    return {
        "pace": pace,
        "expected_total": expected_total,
        "expected_margin": expected_margin,
        "home_projection": home_projection,
        "away_projection": away_projection,
        "elapsed_minutes": elapsed,
        "progress": progress,
        "home_score": home_score,
        "away_score": away_score,
        "market_total": total_line,
    }
