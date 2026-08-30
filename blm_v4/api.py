"""
BLM V4 — PokerBet Pipeline API (dashboard data source).

Serves the classification-aware live view the operator dashboard renders:

    GET /api/v4/status   — collector heartbeat + DB freshness (per class)
    GET /api/v4/live     — every live/recent game with latest market state,
                            derived BLM-style analytics, signals and the
                            snapshot history needed for charts
    GET /api/v4/games    — all known games (summary)
    GET /api/v4/history/{game_id} — full snapshot history for one game
    GET /api/v4/game/{game_id}    — single-game detail (live + history +
                            timeline events)

Everything is read from the SAME ``blm_pokerbet.db`` the collector writes
(read-only URI connection, WAL-safe).  No new pipeline, no duplicated
storage — classification, identity and snapshots are the collector's own.

Derived analytics (win probability, confidence, pace, projections,
momentum, traps) are computed HERE from the actual collected snapshots so
every game — not just the single V2-engine game — gets a full model card.
They are labelled as derived and are a pure function of stored data.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from blm_v4.projection import closing_snapshot, opening_snapshot, project

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

DEFAULT_DB = Path(__file__).resolve().parent.parent / "blm_pokerbet.db"
STATE_FILE = Path(__file__).resolve().parent / "state" / "collector_state.json"

# A game is considered LIVE if its latest snapshot is fresher than this.
LIVE_AGE_S = 15 * 60
FULL_GAME_MINUTES = 40.0  # cyber/virtual basketball game length (4 × 10)


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
    """(velocity pts/min, acceleration pts/min²) over the last 3 snapshots."""
    scored = [r for r in rows if r.get("home_score") is not None
              and r.get("away_score") is not None]
    if len(scored) < 2:
        return None, None
    times = [_parse_ts(r["captured_at"]) for r in scored]
    vals = [r["home_score"] + r["away_score"] for r in scored]
    deltas: list[float] = []
    for i in range(1, len(scored)):
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
    """Per-snapshot derived series for model-history charts.

    Additive keys on top of the raw snapshot values: combined, win_prob,
    momentum_score, momentum_direction, confidence, pace, expected_total.
    Pure function of stored snapshots — no fabrication.
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
            "win_prob": _implied_win(w1, w2),
        }
        window = rows[max(0, i - 2):i + 1]
        mom = _momentum(window)
        entry["momentum_score"] = mom["score"]
        entry["momentum_direction"] = mom["direction"]
        entry["confidence"] = _confidence(
            i + 1, line is not None, _f(r["spread"]) is not None,
            w1 is not None and w2 is not None, i == n - 1,
        )
        # rolling pace (wall-clock vs previous scored snapshot)
        pace: Optional[float] = None
        if combined is not None and scored_prev and ts:
            t0, c0 = scored_prev
            if t0 and ts > t0:
                dt_min = (ts - t0).total_seconds() / 60.0
                if dt_min >= 0.1:
                    p = (combined - c0) / dt_min * FULL_GAME_MINUTES
                    if 20 <= p <= 400:
                        pace = round(p, 1)
        entry["pace"] = pace or line
        if pace and line:
            entry["expected_total"] = round(0.7 * pace + 0.3 * line, 1)
        else:
            entry["expected_total"] = pace or line
        if combined is not None and ts:
            scored_prev = (ts, combined)
        out.append(entry)
    return out


def _analyze_game(game: dict, rows: list[dict], now: datetime,
                  conn: Optional[sqlite3.Connection] = None) -> dict:
    """Build the full dashboard payload for one game from its snapshots."""
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
        r = conn.execute(
            """SELECT * FROM market_observations
               WHERE source_game_id=? AND market_type='MatchTotal'
                 AND captured_at = (
                     SELECT MAX(captured_at) FROM market_observations
                     WHERE source_game_id=? AND market_type='MatchTotal')
               ORDER BY line_value ASC LIMIT 1""",
            (game["source_game_id"], game["source_game_id"]),
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
    proj = project(rows, total_line if mkt_src == "ws" else None)
    pace = proj["pace"]
    market_total = proj["market_total"]
    expected_total = proj["expected_total"]
    expected_margin = proj["expected_margin"]
    home_projection = proj["home_projection"]
    away_projection = proj["away_projection"]

    momentum = _momentum(rows)
    vel, accel = momentum["velocity"], momentum["acceleration"]

    signals = _detect_signals(rows)
    active = [k for k, v in signals.items() if v["active"]]
    trap_meter = min(100.0, 5.0 * len(active) + sum(
        v["confidence"] * 40 for v in signals.values() if v["active"]))
    trap_level = ("high" if trap_meter >= 60 else
                  "medium" if trap_meter >= 30 else "low")

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

    # chart series (score + market + model over time) — actual stored data
    history = _series(rows)
    step = max(1, len(history) // 80)
    if step > 1:
        history = history[::step]

    return {
        "game_id": game["source_game_id"],
        "game_db_id": game["id"],
        "source": game["source"],
        "classification": game["classification"],
        "competition": game.get("competition") or "",
        "region": game.get("region") or "",
        "sport": game.get("sport") or "basketball",
        "status": game.get("status") or "live",
        "live": bool(age is not None and age <= LIVE_AGE_S),
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
        "model": {
            "win_probability": _implied_win(w1, w2),
            "confidence": _confidence(
                snap_count, market_total is not None, spread is not None,
                w1 is not None and w2 is not None,
                bool(age is not None and age <= 120),
            ),
            "expected_total": expected_total,
            "expected_margin": expected_margin,
            "home_projection": home_projection,
            "away_projection": away_projection,
            "pace": pace,
            "possessions": None,
        },
        "momentum": momentum,
        "signals": {
            **signals,
            "trap_meter": round(trap_meter, 1),
            "trap_meter_level": trap_level,
            "active": active,
        },
        "market_efficiency": market_efficiency,
        "market_momentum": market_momentum,
        "foul_correlation": None,
        "history": history,
    }


# ────────────────────────────────────────────────────────────────────────
# DB reads
# ────────────────────────────────────────────────────────────────────────

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
                    limit: int = 400) -> list[dict]:
    rows = conn.execute("""
        SELECT s.* FROM snapshots s
        JOIN games g ON g.id = s.game_id
        WHERE g.source_game_id = ?
        ORDER BY s.captured_at ASC LIMIT ?
    """, (source_game_id, limit)).fetchall()
    return [dict(r) for r in rows]


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
    now = datetime.now(timezone.utc)
    conn = _connect()
    try:
        games = _load_games(conn, classification)
        out = []
        for g in games:
            rows = _load_snapshots(conn, g["source_game_id"])
            if not rows:
                # games table entry with no snapshots yet — still show it
                out.append(_analyze_game(g, [], now, conn))
            else:
                out.append(_analyze_game(g, rows, now, conn))
    finally:
        conn.close()
    out.sort(key=lambda g: (not g["live"], -(g["age_s"] or 0)))
    return {
        "generated_at": now.isoformat(),
        "collector": _load_collector_state(),
        "games": out,
        "totals": {
            "live": sum(1 for g in out if g["live"]),
            "total": len(out),
        },
    }


@router.get("/games")
def v4_games(classification: Optional[str] = Query(None), limit: int = Query(200, le=1000)) -> dict:
    conn = _connect()
    try:
        games = _load_games(conn, classification, limit)
        items = []
        for g in games:
            rows = _load_snapshots(conn, g["source_game_id"], limit=5)
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
                "home_score": _i(latest["home_score"]) if latest else None,
                "away_score": _i(latest["away_score"]) if latest else None,
                "quarter": _i(latest["quarter"]) if latest else None,
                "clock": latest.get("clock") if latest else None,
                "snapshot_count": len(rows),
            })
    finally:
        conn.close()
    return {"total": len(items), "games": items}


@router.get("/scorecard")
def v4_scorecard() -> dict:
    """Projection-accuracy scorecard (persisted, quality-gated)."""
    from blm_v4.projection import MODEL_VERSION
    from blm_v4.scorecard import Scorecard
    db = _db_path()
    sc = Scorecard(db)
    return {
        "model_version": MODEL_VERSION,
        "summary": sc.summary(),
        "fixed_checkpoints": sc.fixed_checkpoints(),
        "by_progress": sc.by_progress(),
        "market_compare": sc.market_compare(),
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
        "classification": game["classification"] if game else None,
        "home_team": game["home_team"] if game else None,
        "away_team": game["away_team"] if game else None,
        "total": len(rows),
        "snapshots": rows[-limit:],
    }


@router.get("/game/{game_id}")
def v4_game_detail(game_id: str) -> dict:
    conn = _connect()
    try:
        game = conn.execute(
            "SELECT * FROM games WHERE source_game_id=?", (game_id,)
        ).fetchone()
        if not game:
            raise HTTPException(status_code=404, detail=f"Game {game_id!r} not found")
        rows = _load_snapshots(conn, game_id, 1000)
        detail = _analyze_game(dict(game), rows, datetime.now(timezone.utc), conn)
        detail["timeline"] = _timeline_events(rows, game["classification"])
        detail["raw"] = rows[-1] if rows else None
    finally:
        conn.close()
    return detail
