"""
BLM V4 — PokerBet Pipeline API (dashboard data source).

Serves the classification-aware live view the operator dashboard renders:

    GET /api/v4/status   — collector heartbeat + DB freshness (per class)
    GET /api/v4/live     — every live/recent CLEAN game with latest market
                            state, observed scoring-rate analytics and the
                            snapshot history needed for charts
    GET /api/v4/games    — all known games (summary)
    GET /api/v4/history/{game_id} — full snapshot history for one game
    GET /api/v4/game/{game_id}    — single-game detail (live + history +
                            timeline events)

Everything is read from the SAME ``blm_pokerbet.db`` the collector writes
(read-only URI connection, WAL-safe).  No new pipeline, no duplicated
storage — classification, identity and snapshots are the collector's own.

Live-state objects are DESCRIPTIVE ONLY (prediction generation frozen
during the clean-data accumulation phase): observed score/clock/period,
market total line + freshness, Pts/min + required Pts/min + pace gap,
recent pace and acceleration from the clean trajectory layer, and the
momentum scoring-rate index.  No model expected-total / edge /
probability / confidence / signal / trap fields are emitted from the
live view; historical prediction and market-vs-fair records remain
reachable only through the explicit audit sections.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from blm_v4.clean_boundary import (CLEAN, CLEAN_DATA_EPOCH, LEGACY,
                                   game_data_quality, is_clean_ts)
from blm_v4.live_analytics.league import PROVIDERS
from blm_v4.live_analytics.historical_context import (
    ANALYTICAL_MIN_REMAINING_MINUTES)
from blm_v4.live_analytics.under_alert import (
    under_alert_eligibility,
    under_alert_state,
)
from blm_v4.projection import (clock_minutes, closing_snapshot, duration_for,
                               opening_snapshot, period_quarter, project)
from blm_v4.terminal_eligibility import (is_terminal_checkpoint,
                                         predictive_validation_label,
                                         TERMINAL_EXCLUSION_REASON)

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

DEFAULT_DB = Path(__file__).resolve().parent.parent / "blm_pokerbet.db"
STATE_FILE = Path(__file__).resolve().parent / "state" / "collector_state.json"

# A game is considered LIVE if its latest snapshot is fresher than this.
LIVE_AGE_S = 15 * 60

# ── Operational ALERT gate ──────────────────────────────────────────────
# The display's LIVE window (LIVE_AGE_S) is deliberately loose — it keeps a
# just-finished game visible while its last observation is still useful.
# ALERTING must be far stricter: a game may raise an UNDER alert ONLY when
# its latest authoritative observation is concurrently
#
#   1. non-terminal      (authoritative game-time evidence, never a label)
#   2. not a finished game (status)
#   3. CURRENT           (observation age <= ALERT_MAX_OBS_AGE_S)
#   4. remaining_game_minutes >= 2.5  (the analytical floor, inclusive)
#
# The gate is evaluated HERE and consumed verbatim by the dashboard: the
# browser must never re-derive it, so a stale stored observation or a
# finished game can never alert merely because its old state still
# satisfies the condition.  The freshness bound reuses the project's
# existing LIVE/STALE 300s market boundary (pace_projector.
# FRESH_LINE_SECONDS == calibration.MARKET_STALE_SECONDS) rather than
# inventing a second notion of freshness.
ALERT_MAX_OBS_AGE_S = 300.0
ALERT_MIN_REMAINING_MINUTES = ANALYTICAL_MIN_REMAINING_MINUTES  # 2.5, inclusive


def _db_path() -> Path:
    return Path(os.environ.get("BLM_POKERBET_DB") or DEFAULT_DB)


def _connect() -> sqlite3.Connection:
    """Read-only connection — the API never writes to the pipeline DB."""
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_s(iso: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    dt = _parse_ts(iso)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds()


def _f(v: Any) -> Optional[float]:
    """SQLite NULL-safe float coercion."""
    return None if v is None else float(v)


def _market_snapshot(rows: list[dict]) -> Optional[dict]:
    """Most recent snapshot carrying a market total (bookmaker O/U line).

    Panel/list snapshots are written every tick without a market payload;
    the line persists between event-view captures, so the last non-null
    line is the current market state — never treat a stub as "no market".
    """
    for r in reversed(list(rows)):
        if r.get("total_line") is not None:
            return r
    return None


def _i(v: Any) -> Optional[int]:
    return None if v is None else int(v)


def _checkpoint_label(cp: Optional[str], pct: Optional[float]) -> str:
    """Human label for a stored checkpoint key (q1..q4, final, pctNN)."""
    if cp and cp.startswith("pct") and cp[3:].isdigit():
        return f"{int(cp[3:])}%"
    if cp == "final":
        return "Final"
    if cp and cp.startswith("q") and cp[1:].isdigit():
        return f"Q{cp[1:]}"
    if pct is not None:
        return f"{round(pct * 100)}%"
    return cp or "–"


def _pred_terminal_sql(conn: sqlite3.Connection) -> str:
    """Terminal classifier for the predictions table at the DB's actual
    schema state (defensive, same semantics as
    ``terminal_eligibility.pred_nonterminal_sql``): prefer the stamped
    ``terminal`` column (written by the scorecard migration); rows
    predating the stamp are classified by their recorded game progress
    (progress >= 1.0 = the terminal snapshot).  Never the bucket label."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(predictions)")}
    if "terminal" in cols:
        return ("COALESCE(terminal, CASE WHEN COALESCE(progress, 0) >= 1.0 "
                "THEN 1 ELSE 0 END)")
    if "progress" in cols:
        return "CASE WHEN COALESCE(progress, 0) >= 1.0 THEN 1 ELSE 0 END"
    return "0"


def _game_checkpoints(conn: sqlite3.Connection,
                      source_game_id: str) -> list[dict]:
    """Historical checkpoint rows for the game-detail table.

    Each row carries the market line FROZEN at capture time (stored
    ``predictions.market_total`` — the last verified observation at-or-before
    the checkpoint's snapshot, never a later line, never the closing line,
    never reconstructed).  Missing markets stay NULL.  The final result and
    the per-checkpoint error are attached only when a verified result
    exists — no result, no error.

    PREDICTIVE SET (directive): only NON-TERMINAL observations —
    ``COALESCE(terminal, progress>=1.0) = 0`` (the shared
    ``pred_nonterminal_sql`` predicate; the COALESCE keeps pre-stamp rows
    classified by their recorded game progress).  The terminal row (the
    game's end state, e.g. 40.00/40.00 = 100.0%) is SETTLEMENT/AUDIT
    data: it is served separately in ``checkpoints_settlement`` and is
    NEVER deleted from storage.
    """
    # A DB the scorecard has never touched has no checkpoint rows yet.
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='predictions'"
    ).fetchone()
    if not has:
        return []
    tsql = _pred_terminal_sql(conn)
    rows = conn.execute(
        f"""SELECT checkpoint, checkpoint_percent, quarter, predicted_at,
                   source_snapshot_at, projected_total, market_total
            FROM predictions
            WHERE source_game_id = ? AND {tsql} = 0
            ORDER BY source_snapshot_at ASC, checkpoint ASC""",
        (source_game_id,),
    ).fetchall()
    res = conn.execute(
        "SELECT final_total FROM game_results WHERE source_game_id = ?",
        (source_game_id,),
    ).fetchone()
    actual = _f(res["final_total"]) if res else None
    out: list[dict] = []
    for r in rows:
        blm = _f(r["projected_total"])
        mkt = _f(r["market_total"])
        out.append({
            "check": r["checkpoint"],
            "checkpoint_percent": r["checkpoint_percent"],
            "label": _checkpoint_label(r["checkpoint"], r["checkpoint_percent"]),
            "quarter": r["quarter"],
            "predicted_at": r["predicted_at"],
            "source_snapshot_at": r["source_snapshot_at"],
            "blm_prediction": blm,
            "market_at_checkpoint": mkt,
            "edge": (round(blm - mkt, 2)
                     if blm is not None and mkt is not None else None),
            "actual_final": actual,
            "error": (round(blm - actual, 2)
                      if blm is not None and actual is not None else None),
        })
    return out


def _prediction_settlement_rows(conn: sqlite3.Connection,
                                source_game_id: str) -> list[dict]:
    """Terminal prediction rows, SETTLEMENT/AUDIT ONLY (directive).

    Same storage as ``_game_checkpoints`` — the rows are never deleted,
    never rewritten — but served through this separate collection so no
    consumer can mistake the game's end state for a predictive
    checkpoint.  Every row carries its explicit exclusion state
    (terminal=1, predictive_eligible=0, reason).
    """
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='predictions'"
    ).fetchone()
    if not has:
        return []
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(predictions)")}
    stamped = [c for c in ("terminal", "predictive_eligible",
                           "exclusion_reason") if c in cols]
    extra = (", " + ", ".join(stamped)) if stamped else ""
    tsql = _pred_terminal_sql(conn)
    rows = conn.execute(
        f"""SELECT checkpoint, checkpoint_percent, quarter, predicted_at,
                   source_snapshot_at, elapsed_minutes, progress,
                   projected_total, market_total{extra}
            FROM predictions
            WHERE source_game_id = ? AND {tsql} = 1
            ORDER BY source_snapshot_at ASC, checkpoint ASC""",
        (source_game_id,),
    ).fetchall()
    res = conn.execute(
        "SELECT final_total FROM game_results WHERE source_game_id = ?",
        (source_game_id,),
    ).fetchone()
    actual = _f(res["final_total"]) if res else None
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["check"] = d["checkpoint"]
        d["label"] = _checkpoint_label(d["checkpoint"], d["checkpoint_percent"])
        d["market_at_checkpoint"] = _f(d.get("market_total"))
        d["actual_final"] = actual
        term = bool(d.get("terminal"))
        d["terminal"] = 1 if term else 0
        d["predictive_eligible"] = 0
        d["predictive_validation"] = predictive_validation_label(True)
        d["exclusion_reason"] = (d.get("exclusion_reason")
                                 or TERMINAL_EXCLUSION_REASON)
        out.append(d)
    return out


# ────────────────────────────────────────────────────────────────────────
# Analytics (pure functions of snapshot lists)
# ────────────────────────────────────────────────────────────────────────

def _implied_win(home_odds: Optional[float], away_odds: Optional[float]) -> float:
    if home_odds and away_odds and home_odds > 1 and away_odds > 1:
        ih, ia = 1.0 / home_odds, 1.0 / away_odds
        return round(ih / (ih + ia), 4)
    return 0.5


def _confidence(snap_count: int, has_line: bool, has_spread: bool,
                has_odds: bool, fresh: bool) -> float:
    c = 0.45
    if snap_count >= 5:
        c += 0.15
    if snap_count >= 15:
        c += 0.10
    if has_line:
        c += 0.10
    if has_spread:
        c += 0.10
    if has_odds:
        c += 0.10
    if fresh:
        c += 0.05
    return round(min(c, 0.95), 4)


def _velocity(rows: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(velocity pts/min, acceleration pts/min²) over the last 3 snapshots.

    Repeated observations of ONE source state — consecutive rows with the
    SAME home/away score, period label AND clock (the ~10s poll catching a
    1s-tick virtual clock mid-dwell) — collapse to the run's first row, so
    velocity measures change across DISTINCT source states using the
    wall-clock between their first observations.  A legitimate no-basket
    interval where the clock keeps advancing is a distinct state per row
    (clock differs) and its 0-delta stays analytically valid.  Prefix-only:
    reads only the rows it is given (callers pass rows[:idx+1]).
    """
    scored = [r for r in rows if r.get("home_score") is not None
              and r.get("away_score") is not None]
    if len(scored) < 2:
        return None, None
    # Collapse runs of identical source state to their first observation.
    states: list[dict] = []
    for r in scored:
        if (states
                and r.get("home_score") == states[-1].get("home_score")
                and r.get("away_score") == states[-1].get("away_score")
                and (r.get("period_label") or "") == (states[-1].get("period_label") or "")
                and (r.get("clock") or "") == (states[-1].get("clock") or "")):
            continue
        states.append(r)
    times = [_parse_ts(r["captured_at"]) for r in states]
    vals = [r["home_score"] + r["away_score"] for r in states]
    deltas: list[float] = []
    for i in range(1, len(states)):
        if times[i - 1] and times[i]:
            dt = (times[i] - times[i - 1]).total_seconds() / 60.0
            if dt >= 1 / 60:
                deltas.append((vals[i] - vals[i - 1]) / max(dt, 1 / 60))
    if not deltas:
        return None, None
    window = deltas[-3:]
    vel = sum(window) / len(window)
    accel = None
    if len(window) >= 2:
        accel = window[-1] - window[-2]
    return round(vel, 3), (round(accel, 3) if accel is not None else None)


def _momentum(rows: list[dict]) -> dict:
    vel, accel = _velocity(rows)
    if vel is None:
        return {
            "score": 50.0, "direction": "flat", "velocity": 0.0,
            "acceleration": 0.0, "strength": 0.0, "strength_label": "none",
        }
    score = 50.0 + vel * 8.0 + (accel or 0) * 4.0
    score = max(0.0, min(100.0, score))
    direction = "up" if vel > 0.15 else ("down" if vel < -0.15 else "flat")
    dev = abs(score - 50.0)
    if dev < 5:
        strength, label = 0.0, "weak"
    elif dev < 15:
        strength, label = 1.0, "moderate"
    elif dev < 30:
        strength, label = 2.0, "strong"
    else:
        strength, label = 3.0, "extreme"
    return {
        "score": round(score, 1), "direction": direction,
        "velocity": vel, "acceleration": accel or 0.0,
        "strength": strength, "strength_label": label,
    }


def _signal(active: bool, confidence: float) -> dict:
    return {"active": bool(active), "confidence": round(float(confidence), 4)}


def _detect_signals(rows: list[dict]) -> dict:
    """Heuristic trap/signal detection from line-vs-score dynamics.

    Pure function of the snapshot history — honest, data-backed signals:
      dead_market        line static while score keeps moving
      false_momentum     score burst with no line response
      bull_trap          line up while scoring has stalled
      bear_trap          line down while scoring accelerates
      late_trap          line moved in the most recent tick
      sharp_trap         big line move without score movement
      reverse_bull_trap  line down while score surges
    """
    out = {
        "bull_trap": _signal(False, 0.0), "bear_trap": _signal(False, 0.0),
        "reverse_bull_trap": _signal(False, 0.0), "dead_market": _signal(False, 0.0),
        "false_momentum": _signal(False, 0.0), "late_trap": _signal(False, 0.0),
        "sharp_trap": _signal(False, 0.0),
    }
    rows = [r for r in rows if r.get("home_score") is not None
            and r.get("away_score") is not None]
    if len(rows) < 3:
        return out
    lines = [(_f(r["total_line"]), _parse_ts(r["captured_at"])) for r in rows]
    last_ts = _parse_ts(rows[-1]["captured_at"])
    last_age = _age_s(last_ts.isoformat()) if last_ts else None
    fresh = last_age is not None and last_age <= 120
    scores = [r["home_score"] + r["away_score"] for r in rows]
    score_moved = scores[-1] - scores[0]

    def _line_series() -> list[Optional[float]]:
        return [l for l, _ in lines]

    series = _line_series()
    line_moved = any(l is not None and l != series[0] for l in series)
    last_interval_line = (
        series[-1] - series[-2] if series[-1] is not None
        and series[-2] is not None else 0.0
    )
    last_interval_score = scores[-1] - scores[-2]

    # dead market: ≥3 ticks with identical line while score advanced ≥ 4
    static_run = 0
    for i in range(len(series) - 1, 0, -1):
        if series[i] is not None and series[i] == series[i - 1]:
            static_run += 1
        else:
            break
    if static_run >= 2 and score_moved >= 4 and series[-1] is not None:
        out["dead_market"] = _signal(True, min(0.9, 0.45 + 0.08 * static_run))

    vel, accel = _velocity(rows)
    mean_vel = None
    if len(scores) >= 3:
        mean_vel = abs(scores[-1] - scores[0]) / max(len(scores) - 1, 1)

    # false momentum: recent burst, line did not follow
    if vel and vel > 2.5 and abs(last_interval_line) < 0.5:
        out["false_momentum"] = _signal(True, min(0.9, 0.4 + 0.1 * vel))

    # bull trap: line raised while scoring stalled
    if last_interval_line > 0.5 and last_interval_score <= 1:
        out["bull_trap"] = _signal(True, min(0.9, 0.45 + 0.25 * last_interval_line))

    # bear trap: line cut while scoring keeps coming
    if last_interval_line < -0.5 and last_interval_score >= 2:
        out["bear_trap"] = _signal(True, min(0.9, 0.45 + 0.2 * abs(last_interval_line)))

    # reverse bull trap: line cut into a scoring surge
    if last_interval_line < -0.5 and vel and vel > 2.0:
        out["reverse_bull_trap"] = _signal(True, min(0.9, 0.4 + 0.2 * vel))

    # late trap: line moved on the freshest tick after being quiet
    if fresh and abs(last_interval_line) >= 0.5 and static_run >= 2:
        out["late_trap"] = _signal(True, min(0.9, 0.45 + 0.15 * abs(last_interval_line)))

    # sharp trap: abrupt line move without score movement
    if abs(last_interval_line) >= 2.0 and abs(last_interval_score) < 2:
        out["sharp_trap"] = _signal(True, min(0.95, 0.5 + 0.1 * abs(last_interval_line)))

    return out


def _timeline_events(rows: list[dict], classification: str) -> list[dict]:
    """Human-readable event timeline derived from actual snapshots."""
    events: list[dict] = []
    rows = [r for r in rows if r.get("home_score") is not None
            and r.get("away_score") is not None]
    if not rows:
        return events
    first = rows[0]
    events.append({
        "t": first["captured_at"], "type": "detected",
        "label": f"Game detected — {first.get('home_team') or '?'} vs "
                 f"{first.get('away_team') or '?'}",
    })
    prev_line, prev_pace, prev_dir = None, None, None
    for i, r in enumerate(rows):
        ts = r["captured_at"]
        if i > 0:
            p = rows[i - 1]
            if (r["home_score"], r["away_score"]) != (p["home_score"], p["away_score"]):
                events.append({
                    "t": ts, "type": "score",
                    "label": f"Score update — {r['home_score']}-{r['away_score']}"
                             f" ({r.get('period_label') or 'Q' + str(r.get('quarter') or '')})",
                })
        line = _f(r["total_line"])
        if line is not None and line != prev_line:
            if prev_line is not None:
                arrow = "▲" if line > prev_line else "▼"
                events.append({
                    "t": ts, "type": "market",
                    "label": f"Market total {prev_line:g} {arrow} {line:g}",
                })
            prev_line = line
    # momentum / pace changes on the aggregated series
    scored = rows
    if len(scored) >= 4:
        for i in range(2, len(scored)):
            win = scored[max(0, i - 2):i + 1]
            vel, _ = _velocity(win)
            if vel is None:
                continue
            direction = "up" if vel > 0.3 else ("down" if vel < -0.3 else "flat")
            if direction != prev_dir and direction != "flat":
                events.append({
                    "t": scored[i]["captured_at"], "type": "momentum",
                    "label": f"Momentum {'building' if direction == 'up' else 'fading'} "
                             f"({vel:+.1f} pts/min)",
                })
            prev_dir = direction
    events.sort(key=lambda e: e["t"])
    # de-dup adjacent identical labels
    out: list[dict] = []
    for e in events:
        if out and out[-1]["label"] == e["label"]:
            continue
        out.append(e)
    return out[-50:]


def _series(rows: list[dict]) -> list[dict]:
    """Per-snapshot derived series for descriptive charts.

    Additive keys on top of the raw snapshot values: combined,
    momentum_score, momentum_direction, pace (observed scoring rate).
    Pure function of stored snapshots — no fabrication.  Prediction
    generation is frozen: no win_prob / confidence / expected_total
    series are produced (charts describe observations only).
    """
    scored_prev: Optional[tuple] = None  # (ts, combined) of previous scored row
    out: list[dict] = []
    n = len(rows)
    for i, r in enumerate(rows):
        h, a = _i(r["home_score"]), _i(r["away_score"])
        combined = (h + a) if (h is not None and a is not None) else None
        line = _f(r["total_line"])
        w1, w2 = _f(r["w1_odds"]), _f(r["w2_odds"])
        ts = _parse_ts(r["captured_at"])
        entry: dict[str, Any] = {
            "t": r["captured_at"],
            "home": h, "away": a, "combined": combined,
            "total_line": line, "spread": _f(r["spread"]),
            "quarter": _i(r["quarter"]), "period": r.get("period_label") or "",
        }
        window = rows[max(0, i - 2):i + 1]
        mom = _momentum(window)
        entry["momentum_score"] = mom["score"]
        entry["momentum_direction"] = mom["direction"]
        # rolling pace (wall-clock vs previous scored snapshot) — scaled to
        # the classification's regulation duration (40 BETUAL / 48 CYBER).
        # A measurement of the observed scoring rate — not a forecast.
        pace: Optional[float] = None
        if combined is not None and scored_prev and ts:
            t0, c0 = scored_prev
            if t0 and ts > t0:
                dt_min = (ts - t0).total_seconds() / 60.0
                if dt_min >= 0.1:
                    full = duration_for(r.get("classification"))[1]
                    p = (combined - c0) / dt_min * full
                    if 20 <= p <= 400:
                        pace = round(p, 1)
        entry["pace"] = pace
        if combined is not None and ts:
            scored_prev = (ts, combined)
        out.append(entry)
    return out


def _game_checkpoint_market(conn: sqlite3.Connection,
                            source_game_id: str) -> list[dict]:
    """Immutable per-checkpoint Market-vs-Fair rows (M009) for the
    game-detail payload.  Sourced from checkpoint_market (frozen at
    first write, never rebased).  Empty list when the table has no rows
    for this game — never fabricated.  NULLs preserved (missing market
    -> signal/outcome/market_vs_fair NULL).

    Checkpoint game-state fields (home/away score, clock, period,
    elapsed_minutes, progress, remaining/current_pace/required_pace) are
    derived deterministically by joining each checkpoint_timestamp to the
    IMMUTABLE snapshots table — the exact source snapshot the checkpoint
    was frozen from (point-in-time state, never the final state; the
    terminal 100 % row's snapshot IS the final snapshot by definition).
    elapsed_minutes is recomputed from the snapshot's clock/period on the
    classification's regulation duration (40 BETUAL / 48 CYBER); stored
    values are the fallback when the snapshot lacks a parseable clock.
    projected_final = blm_fair_value (the existing Fair — one definition).
    """
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoint_market'"
    ).fetchone()
    if not has:
        return []
    from blm_v4.scorecard import (_edge_class, _freshness_bucket,
                                  _market_age_seconds, _market_status,
                                  _period_quarter)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(checkpoint_market)")}
    extra = [c for c in ("market_timestamp", "momentum_state",
                         "momentum_strength", "false_momentum",
                         "false_momentum_confidence", "classification",
                         "progress", "elapsed_minutes") if c in cols]
    ts_sel = ", " + ", ".join(extra) if extra else ""
    rows = conn.execute(
        f"""SELECT checkpoint_pct, checkpoint_timestamp, quarter,
                  opening_line, live_market_line, blm_fair_value,
                  closing_line, actual_final_total, market_vs_fair,
                  signal, blm_vs_olv, blm_vs_clv, olv_to_clv,
                  market_move_toward_blm, outcome{ts_sel}
           FROM checkpoint_market
           WHERE source_game_id = ?
           ORDER BY checkpoint_pct ASC""",
        (source_game_id,),
    ).fetchall()
    # One batched lookup of the checkpoint source snapshots (same
    # captured_at the rows were frozen from — snapshots are immutable).
    snaps: dict[str, dict] = {}
    cpts = [r["checkpoint_timestamp"] for r in rows if r["checkpoint_timestamp"]]
    if cpts:
        marks = ",".join("?" * len(cpts))
        for s in conn.execute(
                f"""SELECT captured_at, quarter, clock, period_label,
                           home_score, away_score
                    FROM snapshots WHERE source_game_id = ?
                      AND captured_at IN ({marks})
                    ORDER BY id ASC""",
                (source_game_id, *cpts)).fetchall():
            snaps[s["captured_at"]] = dict(s)  # last row wins (deterministic)
    out = []
    for r in rows:
        d = dict(r)
        d["market_age_seconds"] = _market_age_seconds(
            d.get("market_timestamp"), d.get("checkpoint_timestamp"))
        d["market_status"] = _market_status(
            d.get("market_timestamp"), d.get("checkpoint_timestamp"))
        d["freshness_bucket"] = _freshness_bucket(d["market_age_seconds"])
        fair, live = d.get("blm_fair_value"), d.get("live_market_line")
        d["blm_market_diff"] = round(fair - live, 2) \
            if fair is not None and live is not None else None
        d["edge_class"] = _edge_class(d["market_status"], d["blm_market_diff"])
        # ── Checkpoint point-in-time state (from the source snapshot) ──
        snap = snaps.get(d.get("checkpoint_timestamp") or "") or {}
        hs, aw = snap.get("home_score"), snap.get("away_score")
        d["home_score_at_checkpoint"] = hs
        d["away_score_at_checkpoint"] = aw
        d["clock_at_checkpoint"] = snap.get("clock")
        d["period_label_at_checkpoint"] = snap.get("period_label")
        if d.get("quarter") is None and snap.get("quarter") is not None:
            d["quarter"] = snap["quarter"]
        q_min, full = duration_for(d.get("classification"))
        elapsed: Optional[float] = None
        label = (snap.get("period_label") or "").lower()
        sq = d.get("quarter")
        if sq is None and snap.get("period_label"):
            sq = _period_quarter(snap.get("period_label"))
        if sq == 2 and label.startswith("half"):
            elapsed = round(full / 2.0, 2)  # half-time boundary in any format
        elif sq is not None and snap.get("clock"):
            elapsed = clock_minutes(sq, str(snap["clock"]), q_min)
        d["elapsed_minutes"] = elapsed if elapsed is not None \
            else d.get("elapsed_minutes")  # stored fallback (recorded basis)
        if elapsed is not None:
            d["progress"] = round(min(1.0, max(0.0, elapsed / full)), 4)
        d["remaining_minutes"] = round(full - d["elapsed_minutes"], 2) \
            if d["elapsed_minutes"] is not None else None
        total = (int(hs) + int(aw)) if (hs is not None and aw is not None) else None
        d["current_total"] = total
        d["current_pace"] = round(total / elapsed, 2) \
            if total is not None and elapsed else None
        d["required_pace"] = round((live - total) / (full - elapsed), 2) \
            if (live is not None and total is not None and elapsed is not None
                and full - elapsed > 0) else None
        out.append(d)
    return out


def _analyze_game(game: dict, rows: list[dict], now: datetime,
                  conn: Optional[sqlite3.Connection] = None,
                  with_checkpoints: bool = False,
                  quality: Optional[dict] = None) -> dict:
    """Derive the live analytics view for ONE game.

    Everything comes from stored, timestamped observations — never
    fabricated.  ``with_checkpoints`` additionally attaches the historical
    checkpoint table (M007-M4) AND the immutable Market-vs-Fair history
    (M009); only the single-game detail route requests it so the /live
    and /games lists stay lean.
    """
    scored = [r for r in rows if r.get("home_score") is not None
              and r.get("away_score") is not None]
    latest = scored[-1] if scored else (rows[-1] if rows else None)
    snap_count = len(rows)
    age = _age_s(latest["captured_at"] if latest else game.get("last_seen_at"), now)

    home_score = _i(latest["home_score"]) if latest else None
    away_score = _i(latest["away_score"]) if latest else None

    # Market state comes from the most recent snapshot that actually
    # carries a market payload.  List-level (panel) snapshots are written
    # every tick without markets; the bookmaker line persists between
    # event-view captures, so the last non-null line is the current state.
    # When the event-view route is down, the eu-swarm WebSocket feed is the
    # independent fallback: its MatchTotal observations carry the same
    # bookmaker O/U line.  The freshest observed line (snapshot OR ws) is
    # what the model sees — never a fabricated value.
    mlatest = _market_snapshot(rows)
    total_line = _f(mlatest["total_line"]) if mlatest else None
    oline = opening_snapshot(rows)
    opening_line = _f(oline["total_line"]) if oline else None
    opening_line_at = oline["captured_at"] if oline else None
    ended = (game.get("status") or "live") == "ended"
    cline = closing_snapshot(rows, ended)
    closing_line = _f(cline["total_line"]) if cline else None
    closing_line_at = cline["captured_at"] if cline else None
    spread = _f(mlatest["spread"]) if mlatest else None
    home_total_line = _f(mlatest["home_total_line"]) if mlatest else None
    away_total_line = _f(mlatest["away_total_line"]) if mlatest else None
    w1 = _f(mlatest["w1_odds"]) if mlatest else None

    mkt_src: Optional[str] = "event-view" if mlatest else None
    ws_obs: Optional[dict] = None
    if conn is not None:
        # clean-data boundary: only post-epoch WS observations may feed
        # the current live market line (legacy lines are audit data only)
        r = conn.execute(
            """SELECT * FROM market_observations
               WHERE source_game_id=? AND market_type='MatchTotal'
                 AND captured_at >= ?
                 AND captured_at = (
                     SELECT MAX(captured_at) FROM market_observations
                     WHERE source_game_id=? AND market_type='MatchTotal'
                       AND captured_at >= ?)
               ORDER BY line_value ASC LIMIT 1""",
            (game["source_game_id"], CLEAN_DATA_EPOCH,
             game["source_game_id"], CLEAN_DATA_EPOCH),
        ).fetchone()
        ws_obs = dict(r) if r else None
    ws_line = _f(ws_obs["line_value"]) if ws_obs else None
    if ws_line is not None and (total_line is None
                                or (ws_obs and mlatest
                                    and ws_obs["captured_at"] > mlatest["captured_at"])):
        total_line = ws_line
        mkt_src = "ws"
    w2 = _f(mlatest["w2_odds"]) if mlatest else None

    # Projection comes from ONE authoritative implementation
    # (blm_v4.projection.project) — never re-implemented in the API layer.
    # When the WS feed supplied the effective line, pin it as the model's
    # observed market input (same pure function, same blend).
    # The effective observed market total for the live-state block (the
    # line the model WOULD have seen — snapshot-carried or eu-swarm WS;
    # never fabricated).  Descriptive only: prediction generation is
    # frozen, so no model expected-total / margin / projection fields are
    # emitted from this live-state object.
    proj = project(rows, total_line if mkt_src == "ws" else None)
    market_total = proj["market_total"]

    # score_line_gap — the LIVE comparison only (directive 2026-09-09):
    # current combined score minus the observed live O/U line at this
    # moment.  Pure arithmetic on two OBSERVED values ("market_total" is
    # the observed line passthrough from project(), never a model output);
    # no BLM fair / trajectory / residual / z / probability / edge goes
    # into it.  A negative gap = score below the live line.  The separate
    # retrospective question (did the FINAL score beat the checkpoint
    # line?) stays in checkpoint_market / prediction_scores.
    score_line_gap: Optional[float] = None
    combined_now = None
    if home_score is not None and away_score is not None:
        combined_now = home_score + away_score
        if market_total is not None:
            score_line_gap = round(combined_now - market_total, 1)

    momentum = _momentum(rows)

    market_efficiency = None
    if market_total and home_score is not None and away_score is not None:
        combined = home_score + away_score
        market_efficiency = round(
            1 - min(abs(combined - market_total) / market_total, 1), 4)

    market_momentum = 0.0
    mrows = [r for r in rows if r.get("total_line") is not None]
    lines = [_f(r["total_line"]) for r in mrows]
    if len(lines) >= 2 and lines[-1] is not None and lines[-2] is not None:
        market_momentum = round(lines[-1] - lines[-2], 2)

    # chart series (score + market + observed scoring-rate over time) —
    # actual stored data, no model series (prediction generation frozen)
    history = _series(rows)
    step = max(1, len(history) // 80)
    if step > 1:
        history = history[::step]

    # the latest authoritative clean projection — feeds BOTH the descriptive
    # projector block and the alert gate, so both read one observation.
    proj_row = _pace_projector_for(game["source_game_id"])

    detail = {
        "game_id": game["source_game_id"],
        "game_db_id": game["id"],
        "source": game["source"],
        "classification": game["classification"],
        # Canonical identity (frozen architecture §8): provider from the
        # frozen family map, competition from the AUTHORITATIVE source
        # metadata (slug + numeric id).  The display name (`competition`)
        # is provenance only — never identity.
        "provider": PROVIDERS.get(game["classification"]),
        "competition_slug": game.get("competition_slug") or None,
        "competition_id": game.get("competition_id") or None,
        "competition": game.get("competition") or "",
        "region": game.get("region") or "",
        "sport": game.get("sport") or "basketball",
        "status": game.get("status") or "live",
        "live": bool(age is not None and age <= LIVE_AGE_S),
        "quality_status": (quality or {}).get("status") or "OK",
        "quality_reason": (quality or {}).get("reason") or None,
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "home_score": home_score,
        "away_score": away_score,
        "period_label": (latest.get("period_label") if latest else None),
        "quarter": (latest.get("quarter") if latest else None),
        "clock": (latest.get("clock") if latest else None),
        "last_update": (latest["captured_at"] if latest else game.get("last_seen_at")),
        "age_s": round(age, 1) if age is not None else None,
        "snapshot_count": snap_count,
        "source_url": game.get("source_url"),
        "market": {
            "opening_line": opening_line,
            "opening_line_at": opening_line_at,
            "closing_line": closing_line,
            "closing_line_at": closing_line_at,
            "total_line": market_total,
            "score_line_gap": score_line_gap,
            "total_line_at": (
                ws_obs["captured_at"] if mkt_src == "ws" and ws_obs
                else (mlatest["captured_at"] if mlatest else None)),
            "total_line_age_s": (
                _age_s(ws_obs["captured_at"], now) if mkt_src == "ws" and ws_obs
                else (_age_s(mlatest["captured_at"], now) if mlatest else None)),
            "market_source": mkt_src,
            "over_odds": (
                _f(ws_obs["over_price"]) if mkt_src == "ws" and ws_obs
                else _f(latest["total_over_odds"]) if latest else None),
            "under_odds": (
                _f(ws_obs["under_price"]) if mkt_src == "ws" and ws_obs
                else _f(latest["total_under_odds"]) if latest else None),
            "spread": spread,
            "spread_indicator": (latest.get("spread_indicator") if latest else None),
            "home_total_line": home_total_line,
            "away_total_line": away_total_line,
            "w1_odds": w1,
            "w2_odds": w2,
        },
        "momentum": momentum,
        "market_efficiency": market_efficiency,
        "market_momentum": market_momentum,
        "foul_correlation": None,
        "history": history,
        "projector": _projector_live_view(proj_row),
        # Historical context is computed ONLY for live games: it is the
        # only case the dashboard renders (a card badge / detail panel for
        # a live observation).  Each lookup costs one indexed range scan of
        # the clean archive, so evaluating it for the whole (mostly ended)
        # list would multiply the /live latency for output nothing draws.
        # The dedicated /game/{id}/historical-context route serves it for
        # any game on demand.
        "historical_context": (
            _historical_context_for(game["source_game_id"])
            if (age is not None and age <= LIVE_AGE_S) else None),
        # authoritative alert eligibility — computed once, consumed verbatim
        # by every dashboard alert path (badge, panel, pulse, audio).  A
        # finished game or a stale observation is ineligible regardless of
        # how well its stored state matches the condition.
        "alert": _alert_gate(game, proj_row, now, age, quality),
    }
    if with_checkpoints and conn is not None:
        detail["checkpoints"] = _game_checkpoints(conn, game["source_game_id"])
        detail["market_vs_fair"] = _game_checkpoint_market(
            conn, game["source_game_id"])
    return detail


# ────────────────────────────────────────────────────────────────────────
# DB reads
# ────────────────────────────────────────────────────────────────────────

def _pace_projector_for(source_game_id: str) -> Optional[dict]:
    """Latest deterministic trajectory row for a game from the clean
    metrics DB (blm_metrics_clean.db) — failure-isolated, None when the
    clean DB or a projection row does not exist yet.
    """
    try:
        from blm_v4.clean_metrics import CleanMetricsStore
        from blm_v4.pace_projector import PaceProjector
        clean_path = _db_path().parent / "blm_metrics_clean.db"
        if not clean_path.exists():
            return None
        return PaceProjector().latest_for_game(
            CleanMetricsStore(clean_path), source_game_id)
    except Exception:
        return None


def _pace_reference(conn: sqlite3.Connection) -> dict:
    """League-specific pace reference for the WHOLE payload, keyed by
    canonical competition slug — failure-isolated ({} on any gap).  One
    grouped scan, cached in-process by the engine below, so the per-poll
    cost does not scale with the number of games or competitions."""
    try:
        from blm_v4.live_analytics.competition_pace import (
            competition_pace_reference,
        )
        return competition_pace_reference(conn)
    except Exception:
        return {}


def _historical_context_for(source_game_id: str) -> dict:
    """League/state-relative historical-context block for a game —
    failure-isolated, explicit no-context state on any gap.  Cheap: a
    benchmark-cache lookup plus two indexed queries (see
    live_analytics/historical_context.py); heavy aggregation is cached
    per canonical key, never per game per poll."""
    try:
        from blm_v4.live_analytics.historical_context import (
            HistoricalContextEngine,
        )
        clean_path = _db_path().parent / "blm_metrics_clean.db"
        if not clean_path.exists():
            return {"status": "no_mature_historical_context",
                    "reason": "no_clean_metrics_database"}
        eng = _historical_engine_for(clean_path)
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True,
                               timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return eng.context_for(conn, source_game_id)
        finally:
            conn.close()
    except Exception as e:
        return {"status": "no_mature_historical_context",
                "reason": "unavailable", "error": str(e)[:200]}


def _historical_engine_for(clean_path):
    """Per-process engine singleton keyed by clean DB path."""
    global _HIST_ENGINE, _HIST_ENGINE_PATH
    try:
        eng = _HIST_ENGINE
        if eng is not None and _HIST_ENGINE_PATH == clean_path:
            return eng
    except NameError:
        pass
    eng = _hist_engine_factory()(clean_path)
    _HIST_ENGINE = eng
    _HIST_ENGINE_PATH = clean_path
    return eng


def _hist_engine_factory():
    from blm_v4.live_analytics.historical_context import HistoricalContextEngine
    return HistoricalContextEngine


_HIST_ENGINE = None
_HIST_ENGINE_PATH = None


# Projector keys that describe OBSERVED/trajectory state.  The frozen
# (stored-for-research) trajectory forecast fields — projected_final_total,
# projection_vs_live_line, fair_total — are NOT emitted in live-state
# payloads: the pace projector may store a deterministic projected
# trajectory for research, but it must not be presented or consumed as a
# betting prediction in the live view.
_PROJECTOR_LIVE_KEYS = (
    "source_game_id", "classification", "captured_at", "period_label",
    "clock", "elapsed_game_minutes", "remaining_game_minutes",
    "progress_pct", "current_total_points", "live_total_line",
    "market_captured_at", "market_age_seconds", "market_status",
    "actual_pts_per_min", "required_pts_per_min", "pace_gap",
    "required_to_actual_ratio",
    "recent_pace_1m", "recent_span_1m", "recent_pace_2m",
    "recent_span_2m", "recent_pace_3m", "recent_span_3m",
    "recent_pace_5m", "recent_span_5m",
    "pace_acceleration", "acceleration_window", "trajectory_state",
    "subsequent_observation_id", "subsequent_actual_pace",
    "subsequent_pace_change", "subsequent_live_line",
    "subsequent_live_line_change", "final_settled_total",
    "status", "computed_at",
)


def _projector_live_view(row: Optional[dict]) -> Optional[dict]:
    """Live-state projector block: descriptive trajectory fields only
    (no projected-fair / forecast fields — prediction generation is
    frozen; trajectory forecasts are stored for research, never served
    to the live view)."""
    if not row:
        return None
    return {k: row.get(k) for k in _PROJECTOR_LIVE_KEYS if k in row}


def _alert_gate(game: dict, proj: Optional[dict], now: datetime,
                age: Optional[float], quality: Optional[dict]) -> dict:
    """Authoritative alert eligibility for one game — the SINGLE source of
    truth every dashboard alert path consumes verbatim (see
    ALERT_MAX_OBS_AGE_S for the rule order and thresholds).

    Returns ``{"eligible": bool, "reason": str | None}``.  ``reason`` names
    the FIRST failed condition so an ineligible game is explainable rather
    than merely hidden.  Terminality is decided from authoritative
    per-frame game time (never a bucket/label/percentage) and a
    game-time field overrides a contradicting game-level status flag.
    """
    if (quality or {}).get("status") == "INVALID":
        return {"eligible": False, "reason": "invalid_quality"}
    if (game.get("status") or "").strip().lower() in ("ended", "finished"):
        return {"eligible": False, "reason": "game_finished"}
    if not proj:
        return {"eligible": False, "reason": "no_live_observation"}
    # the observation that carries the condition must itself be current —
    # an old stored snapshot may never alert on its own stale state
    obs_age = _age_s(proj.get("captured_at"), now)
    if obs_age is None:
        obs_age = age
    if obs_age is None or obs_age > ALERT_MAX_OBS_AGE_S:
        return {"eligible": False, "reason": "stale_observation"}
    pct = proj.get("progress_pct")
    if is_terminal_checkpoint(
            classification=proj.get("classification") or game.get("classification"),
            elapsed_minutes=proj.get("elapsed_game_minutes"),
            # progress_pct is 0..100; the terminal rule reads a 0..1 fraction
            progress=(pct / 100.0 if pct is not None else None),
            quarter=period_quarter(proj.get("period_label")),
            clock=proj.get("clock"),
            period_label=proj.get("period_label"),
            game_status=game.get("status")):
        return {"eligible": False, "reason": "terminal_observation"}
    remaining = proj.get("remaining_game_minutes")
    if remaining is None or remaining < ALERT_MIN_REMAINING_MINUTES:
        return {"eligible": False, "reason": "below_min_remaining"}
    return {"eligible": True, "reason": None}


def _quality_map(conn: sqlite3.Connection,
                 source_game_ids: list[str]) -> dict[str, dict]:
    """Authoritative game_quality state for a set of games (M009-M5
    frontend integrity).

    game_quality is written ONLY by the scorecard quality gate, and only
    ever with status='INVALID' + reason.  A game absent from the map is
    therefore analytically valid.  Frontend eligibility MUST read this
    map — never re-derive validity from snapshot heuristics in the
    browser (the backend gate is the single source of truth).
    """
    if not source_game_ids:
        return {}
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='game_quality'").fetchone()
    if not has:
        # scorecard tables absent (bare pipeline DB) — nothing flagged
        # INVALID, everything eligible.
        return {}
    qmarks = ",".join("?" * len(source_game_ids))
    return {
        r["source_game_id"]: {"status": r["status"], "reason": r["reason"]}
        for r in conn.execute(
            f"SELECT source_game_id, status, reason FROM game_quality "
            f"WHERE source_game_id IN ({qmarks})", source_game_ids)
    }


def _load_games(conn: sqlite3.Connection, classification: Optional[str] = None,
                limit: int = 100) -> list[dict]:
    q = "SELECT * FROM games"
    params: tuple = ()
    if classification:
        q += " WHERE classification=?"
        params = (classification,)
    q += " ORDER BY last_seen_at DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, params + (limit,))]


def _load_snapshots(conn: sqlite3.Connection, source_game_id: str,
                    limit: int = 400, since: Optional[str] = None) -> list[dict]:
    """Snapshots for a game ascending; ``since`` (clean epoch) restricts to
    post-epoch observations only — analytical views must never consume
    pre-epoch (legacy) observations."""
    q = """
        SELECT s.* FROM snapshots s
        JOIN games g ON g.id = s.game_id
        WHERE g.source_game_id = ?"""
    params: list = [source_game_id]
    if since:
        q += " AND s.captured_at >= ?"
        params.append(since)
    q += " ORDER BY s.captured_at ASC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params)]


def _load_collector_state() -> Optional[dict]:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return None


def _db_stats(conn: sqlite3.Connection, now: datetime) -> dict:
    per_class: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT classification, COUNT(*) AS c FROM games GROUP BY classification"
    ):
        per_class[r["classification"]] = {"games": r["c"], "snapshots": 0}
    for r in conn.execute(
        "SELECT classification, COUNT(*) AS c FROM snapshots GROUP BY classification"
    ):
        per_class.setdefault(r["classification"], {"games": 0, "snapshots": 0})
        per_class[r["classification"]]["snapshots"] = r["c"]
    last = conn.execute(
        "SELECT MAX(captured_at) AS m FROM snapshots"
    ).fetchone()["m"]
    live = conn.execute("""
        SELECT COUNT(*) AS c FROM games g
        WHERE g.status = 'live'
          AND EXISTS (SELECT 1 FROM snapshots s
                      WHERE s.game_id = g.id
                        AND s.captured_at >= ?)
    """, (now.replace(microsecond=0).isoformat(),)).fetchone()["c"]
    return {
        "total_games": conn.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"],
        "total_snapshots": conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"],
        "reconciliations": conn.execute(
            "SELECT COUNT(*) AS c FROM reconciliation").fetchone()["c"],
        "reconciled_ok": conn.execute(
            "SELECT COUNT(*) AS c FROM reconciliation WHERE result='matched'"
        ).fetchone()["c"],
        "per_class": per_class,
        "last_snapshot_at": last,
        "last_snapshot_age_s": _age_s(last, now),
        "live_games": live,
    }


# ────────────────────────────────────────────────────────────────────────
# Router
# ────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v4", tags=["blm-v4"])

# Historical-context routes (league/state-relative UNDER context) —
# registered on the SAME router so the live surface stays unified.
try:
    from blm_v4.api_historical_context import register as _register_hist_ctx
    _register_hist_ctx(router)
except Exception:  # pragma: no cover - context layer is optional at boot
    pass


@router.get("/status")
def v4_status() -> dict:
    now = datetime.now(timezone.utc)
    state = _load_collector_state()
    try:
        conn = _connect()
    except Exception:
        return {"status": "offline", "collector": state, "db": None,
                "server_time": now.isoformat()}
    try:
        db = _db_stats(conn, now)
    finally:
        conn.close()
    # collector status: running (heartbeat fresh), stalled, offline
    if state is None:
        col_status = "offline"
    else:
        last_tick_age = _age_s(state.get("last_tick_at"), now)
        if last_tick_age is not None and last_tick_age <= 90:
            col_status = "running" if state.get("status") == "running" else "stalled"
        else:
            col_status = "offline"
    return {
        "status": col_status,
        "collector": state,
        "db": db,
        "server_time": now.isoformat(),
    }


@router.get("/live")
def v4_live(classification: Optional[str] = Query(None)) -> dict:
    """LIVE view — CLEAN post-epoch games ONLY (the frontend's current
    analytical surface).  Legacy/pre-clean games never appear here; their
    data is reachable only through explicitly labeled legacy paths.
    Analysis is computed exclusively from post-epoch observations."""
    now = datetime.now(timezone.utc)
    conn = _connect()
    try:
        games = _load_games(conn, classification)
        # clean-data boundary: exclude games that started before the epoch
        games = [g for g in games
                 if is_clean_ts(g.get("first_seen_at"))]
        qm = _quality_map(conn, [g["source_game_id"] for g in games])
        out = []
        for g in games:
            rows = _load_snapshots(conn, g["source_game_id"],
                                   since=CLEAN_DATA_EPOCH)
            if not rows:
                # games table entry with no post-epoch snapshots yet —
                # still show it (NULL fields, never legacy fallback)
                out.append(_analyze_game(g, [], now, conn,
                                         quality=qm.get(g["source_game_id"])))
            else:
                out.append(_analyze_game(g, rows, now, conn,
                                         quality=qm.get(g["source_game_id"])))
        # League-specific pace reference (mean settled final total /
        # regulation minutes per canonical competition).  Served so the
        # browser compares REQUIRED pace against the game's OWN league
        # without carrying a competition table of its own.  Failure
        # isolated: an unreadable population yields {} and the alert
        # layer then produces nothing rather than borrowing a number.
        pace_reference = _pace_reference(conn)
    finally:
        conn.close()
    out.sort(key=lambda g: (not g["live"], -(g["age_s"] or 0)))
    for g in out:
        g["data_quality"] = CLEAN
        # The actionable UNDER verdict, computed HERE and consumed verbatim
        # by every surface — the browser never reconstructs the condition.
        # Failure isolated per game: an unexpected shape yields an inactive
        # block rather than breaking the payload.
        try:
            proj = g.get("projector") or {}
            entry = pace_reference.get(g.get("competition_slug")) or {}
            # AUTHORITATIVE MARKET GATE (directive LIVE MARKETS ONLY,
            # 2026-09-12): the quantitative condition is necessary but NOT
            # sufficient.  An active alert also requires a genuinely live
            # game AND a live market line, so a market that was live and has
            # since gone stale — or a finished game whose line is still
            # stored — can never keep presenting itself as a current live
            # opportunity.  The genuine-live verdict and its reason are the
            # values this payload ALREADY publishes (g["live"] /
            # g["live_reason"]), so there is one live definition here, not
            # two that could drift.  The reason is EXPOSED in the sibling
            # block: suppression is never silent, and the quantitative
            # numbers are still served.
            eligibility = under_alert_eligibility(
                proj.get("market_status"), g.get("live"), g.get("live_reason"))
            g["under_alert_eligibility"] = eligibility
            g["under_alert"] = under_alert_state(
                proj.get("actual_pts_per_min"), proj.get("required_pts_per_min"),
                entry.get("avg_pace"), proj.get("progress_pct"),
                entry.get("games"), eligible=eligibility["eligible"])
        except Exception:
            g["under_alert_eligibility"] = under_alert_eligibility(
                None, False, None)
            g["under_alert"] = under_alert_state(
                None, None, None, eligible=False)
    return {
        "generated_at": now.isoformat(),
        "data_epoch": CLEAN_DATA_EPOCH,
        "data_quality": CLEAN,
        "source": "clean_post_epoch",
        "collector": _load_collector_state(),
        "pace_reference": pace_reference,
        "games": out,
        "totals": {
            "live": sum(1 for g in out if g["live"]),
            "total": len(out),
        },
    }


@router.get("/games")
def v4_games(classification: Optional[str] = Query(None), limit: int = Query(200, le=1000)) -> dict:
    """Full game list partitioned by data quality: every item carries
    data_quality (CLEAN = first observation at/after the epoch, LEGACY =
    contains pre-epoch observations).  Latest-state rows are computed
    from post-epoch snapshots only."""
    conn = _connect()
    try:
        games = _load_games(conn, classification, limit)
        items = []
        for g in games:
            rows = _load_snapshots(conn, g["source_game_id"], limit=5,
                                   since=CLEAN_DATA_EPOCH)
            latest = rows[-1] if rows else None
            items.append({
                "game_id": g["source_game_id"],
                "classification": g["classification"],
                "competition": g.get("competition") or "",
                "home_team": g["home_team"],
                "away_team": g["away_team"],
                "status": g.get("status") or "live",
                "first_seen_at": g.get("first_seen_at"),
                "last_seen_at": g.get("last_seen_at"),
                "data_quality": game_data_quality(g.get("first_seen_at")),
                "home_score": _i(latest["home_score"]) if latest else None,
                "away_score": _i(latest["away_score"]) if latest else None,
                "quarter": _i(latest["quarter"]) if latest else None,
                "clock": latest.get("clock") if latest else None,
                "snapshot_count": len(rows),
            })
    finally:
        conn.close()
    clean_n = sum(1 for i in items if i["data_quality"] == CLEAN)
    return {"total": len(items), "data_epoch": CLEAN_DATA_EPOCH,
            "games": items,
            "totals": {"clean": clean_n, "legacy": len(items) - clean_n}}


@router.get("/scorecard/events")
def v4_scorecard_events(
    direction: Optional[str] = Query(None, pattern="^(BLM_OVER|BLM_UNDER)$"),
    freshness: Optional[str] = Query(None, pattern="^(LIVE|STALE|MISSING)$"),
    checkpoint: Optional[int] = Query(None, ge=0, le=100),
    min_diff: Optional[float] = Query(None, ge=0),
    max_diff: Optional[float] = Query(None, ge=0),
    game: Optional[str] = None,
    quality: Optional[str] = Query(None, pattern="^(CLEAN|LEGACY)$"),
    predictive: str = Query("eligible", pattern="^(eligible|excluded|all)$"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """M009-M5 — the underlying event dataset behind the disparity bands.

    One row per (game, checkpoint): observed market line, BLM fair,
    signed differential, direction (BLM_OVER/BLM_UNDER), market_status
    (LIVE/STALE/MISSING — never substituted), market age, momentum,
    BLM's side, actual, settlement.  Filters are applied in Python;
    min_diff/max_diff are on MAGNITUDE (direction separates sign).
    Contaminated games are excluded at the source (checkpoint_market).

    Clean-data boundary: default (and analytical) view is CLEAN games
    only (started at/after the clean epoch).  quality=LEGACY is the
    explicit audit path for pre-clean rows and is never a default.

    TERMINAL EXCLUSION (directive): the default ``predictive=eligible``
    view serves NON-TERMINAL rows only — terminal checkpoints are
    settlement/audit data and can never be consumed as research evidence
    through this endpoint.  ``predictive=excluded`` serves the terminal
    population alone (audit); ``predictive=all`` serves everything with
    the explicit eligibility state on every row (audit/reconstruction).
    Terminal rows are never deleted — they remain available for final
    settlement, audit and game reconstruction here and in game detail."""
    from blm_v4.clean_boundary import CLEAN, LEGACY
    from blm_v4.scorecard import (_CM_ELIGIBLE_SQL_CLEAN, _CM_ELIGIBLE_SQL_LEGACY,
                                  _market_age_seconds, _market_status,
                                  _period_quarter)
    quality = quality or CLEAN
    elig_sql = (_CM_ELIGIBLE_SQL_CLEAN if quality == CLEAN
                else _CM_ELIGIBLE_SQL_LEGACY)
    conn = _connect()
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='checkpoint_market'").fetchone()
        if not has:
            return {"total": 0, "rows": []}
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(checkpoint_market)")}
        extra = [c for c in ("market_timestamp", "momentum_state",
                             "momentum_strength", "false_momentum",
                             "false_momentum_confidence", "classification",
                             "progress", "elapsed_minutes", "terminal")
                 if c in cols]
        # qualify with cm. — the games join also carries classification
        sel = ", ".join(f"cm.{c}" for c in extra) + "," if extra else ""
        # Headline dataset: apply the SAME logical-exclusion eligibility as
        # market_vs_fair (single definition — _CM_ELIGIBLE_SQL) so a game
        # re-verified INVALID after its rows were frozen can never reappear
        # through the events dataset.  Frozen rows stay in checkpoint_market
        # for audit (game detail still serves them).
        rows = conn.execute(
            f"""SELECT cm.source_game_id, cm.checkpoint_pct, cm.checkpoint_timestamp,
                      cm.live_market_line, cm.blm_fair_value, cm.actual_final_total,
                      cm.outcome, {sel} g.home_team, g.away_team, g.first_seen_at
               FROM checkpoint_market cm
               JOIN games g ON g.source_game_id = cm.source_game_id
               {elig_sql}
               ORDER BY cm.source_game_id, cm.checkpoint_pct""").fetchall()
        # Point-in-time game state per checkpoint row (same batched
        # snapshot join + calculation as _game_checkpoint_market) so the
        # events table can show the actual clock/elapsed game state next
        # to the checkpoint target % — never the terminal state.
        cpts = [r["checkpoint_timestamp"] for r in rows if r["checkpoint_timestamp"]]
        snaps: dict[str, dict] = {}
        if cpts:
            marks = ",".join("?" * len(cpts))
            for s in conn.execute(
                    f"""SELECT captured_at, quarter, clock, period_label,
                               home_score, away_score
                        FROM snapshots WHERE captured_at IN ({marks})
                        ORDER BY id ASC""",
                    (*cpts,)).fetchall():
                snaps[s["captured_at"]] = dict(s)
    finally:
        conn.close()

    events = []
    for r in rows:
        d = dict(r)
        # Checkpoint point-in-time game state — identical resolution to
        # _game_checkpoint_market (structured quarter → label fallback →
        # half-time boundary → stored recorded basis).
        snap = snaps.get(d.get("checkpoint_timestamp") or "") or {}
        q_min, full = duration_for(d.get("classification"))
        label = (snap.get("period_label") or "").lower()
        sq = d.get("quarter")
        if sq is None and snap.get("quarter") is not None:
            sq = snap["quarter"]
        if sq is None and snap.get("period_label"):
            sq = _period_quarter(snap.get("period_label"))
        elapsed: Optional[float] = None
        if sq == 2 and label.startswith("half"):
            elapsed = round(full / 2.0, 2)
        elif sq is not None and snap.get("clock"):
            elapsed = clock_minutes(sq, str(snap["clock"]), q_min)
        d["elapsed_minutes"] = elapsed if elapsed is not None \
            else d.get("elapsed_minutes")
        d["progress"] = round(min(1.0, max(0.0, elapsed / full)), 4) \
            if elapsed is not None else d.get("progress")
        d["remaining_minutes"] = round(full - d["elapsed_minutes"], 2) \
            if d["elapsed_minutes"] is not None else None
        d["period_label_at_checkpoint"] = snap.get("period_label")
        d["clock_at_checkpoint"] = snap.get("clock")
        # ── terminal-eligibility state (directive) ──────────────────
        # Prefer the stamped terminal column (writer / startup migration);
        # rows predating the stamp are classified by the authoritative
        # predicate from their own game-time evidence — never 100% alone
        # when better fields exist.
        term = d.get("terminal")
        if term is None:
            # BUCKET-INDEPENDENT (directive): the checkpoint bucket is
            # never terminal evidence — the row's own game-time decides.
            term = is_terminal_checkpoint(
                classification=d.get("classification"),
                elapsed_minutes=d.get("elapsed_minutes"),
                progress=d.get("progress"),
                quarter=snap.get("quarter"),
                clock=snap.get("clock"),
                period_label=snap.get("period_label"),
            )
        d["terminal"] = int(bool(term))
        d["predictive_eligible"] = 0 if term else 1
        d["predictive_validation"] = predictive_validation_label(term)
        d["exclusion_reason"] = TERMINAL_EXCLUSION_REASON if term else None
        line, fair, actual = d["live_market_line"], d["blm_fair_value"], d["actual_final_total"]
        diff = round(fair - line, 2) if fair is not None and line is not None else None
        dirn = ("BLM_OVER" if diff and diff > 0 else
                "BLM_UNDER" if diff and diff < 0 else None)
        status = _market_status(d.get("market_timestamp"), d["checkpoint_timestamp"])
        blm_side = ("OVER" if fair is not None and line is not None and fair > line else
                    "UNDER" if fair is not None and line is not None and fair < line else None)
        oc = d["outcome"]
        blm_won = (True if oc in ("OVER_WIN", "UNDER_WIN") else
                   False if oc in ("OVER_LOSS", "UNDER_LOSS") else None)
        events.append({
            "game": d["source_game_id"],
            "data_quality": game_data_quality(d.get("first_seen_at")),
            "classification": d.get("classification"),
            "home_team": d["home_team"], "away_team": d["away_team"],
            "checkpoint_pct": d["checkpoint_pct"],
            "checkpoint_ts": d["checkpoint_timestamp"],
            "market_line": line, "blm_fair": fair, "diff": diff,
            "direction": dirn,
            "market_status": status,
            "market_age_seconds": _market_age_seconds(
                d.get("market_timestamp"), d["checkpoint_timestamp"]),
            "period_label_at_checkpoint": d["period_label_at_checkpoint"],
            "clock_at_checkpoint": d["clock_at_checkpoint"],
            "elapsed_minutes": d["elapsed_minutes"],
            "progress": d["progress"],
            "remaining_minutes": d["remaining_minutes"],
            "momentum_state": d.get("momentum_state"),
            "momentum_strength": d.get("momentum_strength"),
            "false_momentum": d.get("false_momentum"),
            "blm_side": blm_side,
            "actual": actual, "outcome": oc, "blm_won": blm_won,
            "terminal": d["terminal"],
            "predictive_eligible": d["predictive_eligible"],
            "predictive_validation": d["predictive_validation"],
            "exclusion_reason": d["exclusion_reason"],
        })

    if direction:
        events = [e for e in events if e["direction"] == direction]
    if freshness:
        events = [e for e in events if e["market_status"] == freshness]
    if checkpoint is not None:
        events = [e for e in events if e["checkpoint_pct"] == checkpoint]
    if min_diff is not None:
        events = [e for e in events
                  if e["diff"] is not None and abs(e["diff"]) >= min_diff]
    if max_diff is not None:
        events = [e for e in events
                  if e["diff"] is not None and abs(e["diff"]) <= max_diff]
    if game:
        needle = game.lower()
        events = [e for e in events if needle in e["game"].lower()]
    # TERMINAL EXCLUSION boundary (directive): default view = predictive
    # research population only.  Terminal rows stay stored (settlement/
    # audit) and are reachable via predictive=excluded / predictive=all.
    if predictive == "eligible":
        events = [e for e in events if not e.get("terminal")]
    elif predictive == "excluded":
        events = [e for e in events if e.get("terminal")]
    n_terminal = sum(1 for e in events if e.get("terminal"))
    total = len(events)
    return {"total": total, "rows": events[:limit],
            "data_epoch": CLEAN_DATA_EPOCH, "data_quality": quality,
            "predictive_filter": predictive,
            "n_terminal": n_terminal,
            "n_predictive_eligible": total - n_terminal,
            "source": "clean_post_epoch" if quality == CLEAN else "legacy_audit",
            "data_source": "clean_post_epoch" if quality == CLEAN else "legacy_audit"}


@router.get("/scorecard")
def v4_scorecard() -> dict:
    """Projection-accuracy scorecard (persisted, quality-gated) — CLEAN
    games only (aggregations filter to the post-epoch population)."""
    from blm_v4.projection import MODEL_VERSION
    from blm_v4.scorecard import Scorecard
    db = _db_path()
    sc = Scorecard(db)
    return {
        "model_version": MODEL_VERSION,
        "data_epoch": CLEAN_DATA_EPOCH,
        "data_quality": CLEAN,
        "source": "clean_post_epoch",
        "summary": sc.summary(),
        "fixed_checkpoints": sc.fixed_checkpoints(),
        "by_progress": sc.by_progress(),
        "market_compare": sc.market_compare(),
        "market_vs_fair": sc.market_vs_fair(),
        "recent": sc.recent(25),
    }


@router.get("/trends")
def v4_trends() -> dict:
    """Historical market & time-of-day trends over CLEAN games only.

    Observations, never model rules: every percentage carries its sample
    size, and missing lines exclude the game from that metric only."""
    from blm_v4.trends import (analytics_tz, grouped_periods,
                               market_movement, market_performance,
                               model_vs_market, time_of_day)
    conn = _connect()
    try:
        return {
            "analytics_tz": analytics_tz(),
            "grouped_periods": [
                f"{a:02d}-{b:02d}" for a, b in grouped_periods()],
            "market_performance": market_performance(conn),
            "time_of_day": time_of_day(conn),
            "market_movement": market_movement(conn),
            "model_vs_market": model_vs_market(conn),
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        }
    finally:
        conn.close()


@router.get("/history/{game_id}")
def v4_history(game_id: str, limit: int = Query(500, le=2000)) -> dict:
    """Explicit HISTORY / AUDIT path: serves the game's FULL observation
    history (clean + legacy) for audit/reference only — never a current
    analytical view.  data_quality tells the consumer which partition the
    game belongs to."""
    conn = _connect()
    try:
        rows = _load_snapshots(conn, game_id, limit)
        game = conn.execute(
            "SELECT * FROM games WHERE source_game_id=?", (game_id,)
        ).fetchone()
    finally:
        conn.close()
    if not rows and not game:
        raise HTTPException(status_code=404, detail=f"Game {game_id!r} not found")
    return {
        "game_id": game_id,
        "section": "history_audit",
        "data_epoch": CLEAN_DATA_EPOCH,
        "data_quality": game_data_quality(
            game["first_seen_at"] if game else None),
        "classification": game["classification"] if game else None,
        "home_team": game["home_team"] if game else None,
        "away_team": game["away_team"] if game else None,
        "total": len(rows),
        "snapshots": rows[-limit:],
    }


@router.get("/game/{game_id}")
def v4_game_detail(game_id: str) -> dict:
    """Single-game detail — the current analytical view.  Analysis and
    checkpoint tables are computed from post-epoch observations ONLY
    (clean-data boundary); pre-epoch rows for a legacy game are reachable
    through the explicit /api/v4/history/{game_id} audit path."""
    conn = _connect()
    try:
        game = conn.execute(
            "SELECT * FROM games WHERE source_game_id=?", (game_id,)
        ).fetchone()
        if not game:
            raise HTTPException(status_code=404, detail=f"Game {game_id!r} not found")
        rows = _load_snapshots(conn, game_id, 1000, since=CLEAN_DATA_EPOCH)
        qm = _quality_map(conn, [game_id])
        detail = _analyze_game(dict(game), rows, datetime.now(timezone.utc),
                               conn, with_checkpoints=True,
                               quality=qm.get(game_id))
        # checkpoint tables: point-in-time frozen history — keep only rows
        # frozen from post-epoch snapshots (legacy rows are audit data)
        detail["checkpoints"] = [c for c in detail["checkpoints"]
                                  if (c.get("source_snapshot_at") or "")
                                  >= CLEAN_DATA_EPOCH]
        detail["market_vs_fair"] = [c for c in detail["market_vs_fair"]
                                     if (c.get("checkpoint_timestamp") or "")
                                     >= CLEAN_DATA_EPOCH]
        # TERMINAL SEPARATION (directive): PREDICTIVE CHECKPOINTS default
        # to predictive_eligible=1 / terminal=0 — the game's end state
        # (e.g. 40.00/40.00 = 100.0% TERMINAL) is served separately as
        # SETTLEMENT / TERMINAL audit data.  Same storage, same immutable
        # rows, same authoritative game-time predicate (never the bucket
        # label); nothing is deleted.
        all_mvf = detail["market_vs_fair"]
        for c in all_mvf:
            # BUCKET-INDEPENDENT (directive): the checkpoint bucket is
            # never terminal evidence — the row's own game-time decides.
            term = is_terminal_checkpoint(
                classification=c.get("classification")
                or game["classification"],
                elapsed_minutes=c.get("elapsed_minutes"),
                progress=c.get("progress"),
                quarter=c.get("quarter"),
                clock=c.get("clock_at_checkpoint"),
                period_label=c.get("period_label_at_checkpoint"),
            )
            c["terminal"] = int(bool(term))
            c["predictive_eligible"] = 0 if term else 1
            c["predictive_validation"] = predictive_validation_label(term)
            c["exclusion_reason"] = TERMINAL_EXCLUSION_REASON if term else None
        detail["market_vs_fair_settlement"] = [c for c in all_mvf
                                                if c["terminal"]]
        detail["market_vs_fair"] = [c for c in all_mvf if not c["terminal"]]
        detail["checkpoints_settlement"] = _prediction_settlement_rows(
            conn, game["source_game_id"])
        detail["terminal_exclusion"] = {
            "rule": "TERMINAL = SETTLEMENT/AUDIT ONLY; "
                    "NON-TERMINAL = PREDICTIVE RESEARCH ELIGIBLE",
            "n_rows": len(all_mvf),
            "n_terminal": len(detail["market_vs_fair_settlement"]),
            "n_predictive_eligible": len(detail["market_vs_fair"]),
        }
        detail["timeline"] = _timeline_events(rows, game["classification"])
        detail["raw"] = rows[-1] if rows else None
        detail["data_epoch"] = CLEAN_DATA_EPOCH
        detail["data_quality"] = game_data_quality(game["first_seen_at"])
        detail["data_source"] = "clean_post_epoch"
        detail["clean_snapshot_count"] = len(rows)
    finally:
        conn.close()
    return detail


# ────────────────────────────────────────────────────────────────────────
# Deviation / benchmark / z-score — research/calibration reads ONLY.
# The empirical market-vs-trajectory residual benchmark (Phase 2).  These
# endpoints serve the RESEARCH / CALIBRATION surface (never the live
# predictive view): z-scores are descriptive measurements with explicit
# bucket maturity — no O/U, no edge, no probability, no recommendation.
# ────────────────────────────────────────────────────────────────────────

_MATURITY_LIMITS = {
    "exploratory": "N < 100",
    "provisional": "100 <= N < 1000",
    "established": "N >= 1000",
}

_DEVIATION_NOTE = (
    "Empirical market-vs-trajectory deviation instrument (research only). "
    "residual = live total line - pace-projected final total at that "
    "observation. z_score is standardized against the bucket's PRIOR "
    "residuals (no self-contamination, no future leakage). "
    "EXPLORATORY/PROVISIONAL/ESTABLISHED are operational maturity labels "
    "for the bucket size N, NOT claims of statistical significance; N is "
    "the count of eligible observations in this league+period bucket, "
    "never total observations. z is NULL when the bucket std is "
    "unavailable (N<2) or zero. This is not an O/U or betting model."
)


@router.get("/deviation/benchmarks")
def v4_deviation_benchmarks(
    classification: Optional[str] = None,
) -> dict:
    """Current empirical benchmark state per bucket (league + quarter):
    N / mean / std / min / max / status.  Research/calibration only."""
    clean_path = _db_path().parent / "blm_metrics_clean.db"
    if not clean_path.exists():
        return {
            "section": "deviation_benchmarks", "benchmarks": [],
            "maturity": _MATURITY_LIMITS, "note": _DEVIATION_NOTE,
        }
    try:
        from blm_v4.deviation import DeviationStore
        store = DeviationStore(clean_path)
        buckets = store.bucket_summaries(classification=classification)
    except Exception:
        buckets = []
    return {
        "section": "deviation_benchmarks",
        "classification": classification,
        "benchmarks": buckets,
        "maturity": _MATURITY_LIMITS,
        "note": _DEVIATION_NOTE,
    }


@router.get("/deviation/validation")
def v4_deviation_validation() -> dict:
    """Retrospective research: what subsequently happened after each
    measured market-vs-trajectory deviation.  Read-only aggregate over
    the stored deviation dataset + its STORED outcome fields — never a
    predictive model, no O/U / edge / probability / recommendation."""
    clean_path = _db_path().parent / "blm_metrics_clean.db"
    if not clean_path.exists():
        return {"section": "deviation_validation", "n_gated_rows": 0,
                "note": "no clean metrics database yet",
                "sign_buckets": [], "magnitude_buckets": [],
                "correlations": {}, "by_context": [], "by_progress": [],
                "ungated_comparison": {}, "scatter": {}}
    try:
        from blm_v4.deviation_analysis import DeviationAnalysis
        return DeviationAnalysis(clean_path).validation()
    except Exception:
        return {"section": "deviation_validation", "n_gated_rows": 0,
                "note": "validation unavailable (clean DB read failed)",
                "sign_buckets": [], "magnitude_buckets": [],
                "correlations": {}, "by_context": [], "by_progress": [],
                "ungated_comparison": {}, "scatter": {}}


@router.get("/game/{game_id}/pace-z")
def v4_game_pace_z(game_id: str) -> dict:
    """PACE Z-SCORE for one game — the authoritative descriptive Z of the
    new architecture.

    z = (actual_pace − μ) / σ against the strictly-PRIOR historical
    population at the same (provider, competition, period, progress
    bucket) — never a market-vs-trajectory residual, never derived from
    the live line or score_line_gap, never computed in the frontend.
    The game's own observation is excluded; nothing is fabricated when
    the population is ineligible/degenerate/undersized (z = None plus a
    benchmark_status)."""
    try:
        from blm_v4.live_analytics.service import pace_z_payload
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True,
                               timeout=30)
        try:
            return pace_z_payload(conn, _db_path().parent
                                  / "blm_metrics_clean.db", game_id,
                                  history=120)
        finally:
            conn.close()
    except Exception as e:
        return {"game_id": game_id, "z": None, "n": 0,
                "benchmark_status": "unavailable", "error": str(e)[:200],
                "benchmark_key": None, "mean_pace": None, "std_pace": None,
                "actual_pace": None, "period": None, "progress_pct": None,
                "provider": None, "competition": None, "series": []}


@router.get("/game/{game_id}/deviation")
def v4_game_deviation(game_id: str) -> dict:
    """Per-game deviation research payload: the market line vs the
    observational trajectory, the signed residual, and the provisional
    z-score with its stored benchmark snapshot — over game time.  Empty
    series when the clean DB has no residuals for this game yet."""
    clean_path = _db_path().parent / "blm_metrics_clean.db"
    if not clean_path.exists():
        return {
            "game_id": game_id, "section": "deviation_research",
            "classification": None, "total": 0, "series": [],
            "buckets": [], "maturity": _MATURITY_LIMITS,
            "note": _DEVIATION_NOTE,
        }
    try:
        from blm_v4.deviation import DeviationStore
        store = DeviationStore(clean_path)
        rows = store.residuals_for_game(game_id)
        cls = rows[-1].get("classification") if rows else None
        buckets = (store.bucket_summaries(classification=cls)
                   if cls else [])
        # per-observation context from the trajectory rows + snapshots
        # (read-only join; deviation.py itself is untouched): period/clock,
        # home/away score, observed + required pace, pace_gap
        ctx: dict[int, dict] = {}
        if rows:
            conn = sqlite3.connect(f"file:{clean_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                for x in conn.execute(
                        """SELECT p.observation_id, p.period_label, p.clock,
                                  p.actual_pts_per_min, p.required_pts_per_min,
                                  p.pace_gap, o.home_score, o.away_score
                           FROM clean_projections p
                           LEFT JOIN clean_observations o ON o.id = p.observation_id"""):
                    ctx[int(x["observation_id"])] = dict(x)
            finally:
                conn.close()
        keys = (
            "captured_at", "elapsed_game_minutes", "progress_pct",
            "current_total_points", "live_total_line",
            "projected_final_total", "market_trajectory_residual",
            "benchmark_n", "benchmark_mean", "benchmark_std",
            "benchmark_status", "z_score",
        )
        series = []
        for r in rows:
            item = {k: r.get(k) for k in keys}
            c = ctx.get(r.get("observation_id")) or {}
            for k in ("period_label", "clock", "actual_pts_per_min",
                      "required_pts_per_min", "pace_gap",
                      "home_score", "away_score"):
                item[k] = c.get(k)
            series.append(item)
    except Exception:
        cls, buckets, series = None, [], []
    return {
        "game_id": game_id, "section": "deviation_research",
        "classification": cls, "total": len(series), "series": series,
        "buckets": buckets, "maturity": _MATURITY_LIMITS,
        "note": _DEVIATION_NOTE,
    }


@router.get("/game/{game_id}/market-lines")
def v4_game_market_lines(game_id: str) -> dict:
    """Observed live O/U line history for one game — READ-ONLY additive
    exposure for the primary game chart (SCORE vs LIVE LINE — MARKET
    MOVEMENT).  Every stored market_observations row of type MatchTotal
    at its exact capture time; clean-epoch boundary only (pre-epoch rows
    are audit data, reachable via /api/v4/history).  No research
    calculation is touched: this reads the collector's own market
    observations.  Gaps between observations stay gaps — the frontend
    must never interpolate, substitute, or fabricate a line."""
    conn = _connect()
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_observations'").fetchone()
        if not has:
            return {"game_id": game_id, "section": "market_line_history",
                    "data_epoch": CLEAN_DATA_EPOCH, "total": 0,
                    "observations": []}
        rows = conn.execute(
            """SELECT captured_at, line_value, over_price, under_price,
                      home_score, away_score, period_label, clock
               FROM market_observations
               WHERE source_game_id = ? AND market_type = 'MatchTotal'
                 AND captured_at >= ? AND line_value IS NOT NULL
               ORDER BY captured_at ASC, id ASC""",
            (game_id, CLEAN_DATA_EPOCH),
        ).fetchall()
        out = []
        prev = None
        for r in rows:
            t = r["captured_at"]
            adjacent = 0
            if prev is not None:
                try:
                    dt = (_parse_ts(t) - _parse_ts(prev)).total_seconds()
                    adjacent = 1 if dt <= 120 else 0
                except Exception:
                    adjacent = 0
            out.append({
                "captured_at": t,
                "line_value": _f(r["line_value"]),
                "over_price": _f(r["over_price"]),
                "under_price": _f(r["under_price"]),
                "home_score": _i(r["home_score"]),
                "away_score": _i(r["away_score"]),
                "period_label": r["period_label"],
                "clock": r["clock"],
                "adjacent": adjacent,
            })
            prev = t
    finally:
        conn.close()
    return {"game_id": game_id, "section": "market_line_history",
            "data_epoch": CLEAN_DATA_EPOCH, "total": len(out),
            "observations": out}
