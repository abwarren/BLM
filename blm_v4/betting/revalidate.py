"""Gate re-validation at the SUBMISSION boundary.

The gate is evaluated once, when the candidate is CLAIMED.  The provider call
happens later — measured on prod 2026-10-29: claim 22:52:14.548, submit
22:53:25.428, i.e. **71 seconds**.  Anything the game does inside that window was
invisible to the engine, so a candidate could be filled on a state that would no
longer qualify.

Measured case (31398377, KBL): claimed at 75.0% with 135 points, pace 1.116,
shortfall -0.15, price 1.85 — every leg passing.  It reached the book at 77.5%
with the shortfall at -0.38 and the pace at 1.069, i.e. a state the cohort would
have REFUSED.  The bet stood.

This module re-runs the same legs on the NEWEST capture immediately before the
provider call, exactly as ``executor.execute`` re-reads the kill switch there.

FAIL CLOSED: an unprovable state (no capture, or a capture older than
``BET_REVALIDATE_MAX_AGE_S``, default 120 s) is a REFUSAL, not a pass.  With the
feed ticking every ~2 s, a two-minute-old capture for a live game means the
game is gone — and betting on a state we cannot read is the hole this closes.

Env:
    BET_REVALIDATE=1                 enable (DEFAULT: OFF — the code contract
                                     and the suites that pin it are unchanged;
                                     the deployment turns it on in its drop-in)
    BET_REVALIDATE_MAX_AGE_S=120     maximum age of the capture that proves it
    BET_REVALIDATE_MAX_DRIFT=0.10    refuse if the live price fell more than this
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

OK: Optional[tuple[str, str]] = None


def enabled() -> bool:
    return os.environ.get("BET_REVALIDATE", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _max_age() -> float:
    try:
        return float(os.environ.get("BET_REVALIDATE_MAX_AGE_S", "120"))
    except ValueError:
        return 120.0


def _max_drift() -> float:
    try:
        return float(os.environ.get("BET_REVALIDATE_MAX_DRIFT", "0.10"))
    except ValueError:
        return 0.10


def _data_root() -> Path:
    return Path(os.environ.get("BLM_DATA_ROOT", "/home/ubuntu/BLM"))


def fetch_state(game_id: str) -> Optional[dict]:
    """The newest capture for the game, plus the competition's average pace."""
    root = _data_root()
    clean = root / "blm_metrics_clean.db"
    prod = root / "blm_pokerbet.db"
    try:
        cc = sqlite3.connect(f"file:{clean}?mode=ro", uri=True, timeout=20)
        cc.row_factory = sqlite3.Row
        row = cc.execute(
            "SELECT captured_at, total_points, progress_pct, required_pts_per_min,"
            " actual_pts_per_min, under_price, live_total_line, status"
            " FROM clean_observations WHERE source_game_id = ?"
            " ORDER BY captured_at DESC LIMIT 1", (str(game_id),)).fetchone()
        cc.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    out = dict(row)
    out["competition_slug"], out["avg_pace"] = None, None
    try:
        from blm_v4.live_analytics.competition_pace import competition_pace_reference
        p = sqlite3.connect(f"file:{prod}?mode=ro", uri=True, timeout=20)
        p.row_factory = sqlite3.Row
        g = p.execute("SELECT competition_slug FROM games WHERE source_game_id = ?",
                      (str(game_id),)).fetchone()
        if g is not None:
            out["competition_slug"] = g["competition_slug"]
            ref = competition_pace_reference(p)
            out["avg_pace"] = (ref.get(g["competition_slug"]) or {}).get("avg_pace")
        p.close()
    except Exception:                     # never let IO invent a pass
        pass
    return out


def verdict(state: dict, candidate: dict, *, now: Optional[datetime] = None,
            max_age_s: Optional[float] = None,
            max_drift: Optional[float] = None) -> Optional[tuple[str, str]]:
    """Pure decision: ``None`` when the state still passes, else (code, why)."""
    from blm_v4.trade_window import (exec_alert_reason, exec_shortfall_reason,
                                     execution_window_reason, score_reason,
                                     trade_competition_reason, trade_price_reason)

    if not state:
        return ("REVALIDATION_NO_CAPTURE", "no capture for this game at submission")
    if str(state.get("status") or "").upper() != "VALID":
        return ("REVALIDATION_NOT_VALID",
                f"newest capture is {state.get('status')}, not VALID")

    # freshness first: an unreadable age is a refusal (fail closed)
    try:
        at = datetime.fromisoformat(str(state["captured_at"]).replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - at).total_seconds()
    except Exception:
        return ("REVALIDATION_UNREADABLE_CAPTURE", "capture timestamp unreadable")
    limit = max_age_s if max_age_s is not None else _max_age()
    if age > limit:
        return ("REVALIDATION_STALE",
                f"newest capture is {age:.0f}s old (limit {limit:.0f}s)")

    why = (trade_competition_reason(state.get("competition_slug"))
           or execution_window_reason(state.get("progress_pct"))
           or score_reason(state.get("total_points"))
           or exec_alert_reason(state.get("required_pts_per_min"),
                                state.get("avg_pace"),
                                state.get("progress_pct"))
           or exec_shortfall_reason(state.get("required_pts_per_min"),
                                    state.get("actual_pts_per_min"))
           or trade_price_reason(state.get("under_price")))
    if why:
        return (f"REVALIDATION_{str(why).upper()}"[:60],
                f"no longer qualifies at submission: {why}")

    # price drift — the edge was measured at the CLAIMED price
    claimed = candidate.get("price")
    live = state.get("under_price")
    if claimed and live:
        drift = float(claimed) - float(live)
        cap = max_drift if max_drift is not None else _max_drift()
        if drift > cap:
            return ("REVALIDATION_PRICE_DRIFT",
                    f"price fell {drift:.2f} from {float(claimed):.2f} to "
                    f"{float(live):.2f} (limit {cap:.2f})")
    return OK


def revalidate(candidate: dict, *,
               fetch: Optional[Callable[[str], Optional[dict]]] = None,
               now: Optional[datetime] = None,
               max_age_s: Optional[float] = None,
               max_drift: Optional[float] = None
               ) -> Optional[tuple[str, str]]:
    """Returns ``None`` to place, or ``(error_code, message)`` to refuse."""
    if not enabled():
        return OK
    game_id = candidate.get("game_id")
    if game_id is None:
        return ("REVALIDATION_NO_GAME", "candidate carries no game_id")
    state = (fetch or fetch_state)(str(game_id))
    return verdict(state, candidate, now=now, max_age_s=max_age_s,
                   max_drift=max_drift)
