"""Historical-context API surface.

Extends the existing v4 API (no parallel architecture): one read-only
endpoint serving the league/state-relative historical UNDER context for
a game's current live observation, plus a helper used by the live list
to attach the same block per game.

Rules implemented here (see live_analytics/historical_context.py):
  * the benchmark is the canonical PROVIDER|COMPETITION|PERIOD|PROGRESS
    population — never global, never cross-competition
  * remaining_game_minutes >= 2.5 (2.50 INCLUDED) is the analytical
    eligibility rule — not a terminal rule, terminal semantics untouched
  * no mature benchmark → explicit ``no_mature_historical_context``
    (no fallback, no fabrication, game never hidden)
  * descriptive frequencies only — never prediction/probability/EV
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

# shared per-process engine (cache lives in memory + sidecar cache table)
_ENGINE = None
_ENGINE_PATH: Optional[Path] = None


def _engine_for(clean_path: Path):
    global _ENGINE, _ENGINE_PATH
    if _ENGINE is None or _ENGINE_PATH != clean_path:
        from blm_v4.live_analytics.historical_context import HistoricalContextEngine
        _ENGINE = HistoricalContextEngine(clean_path)
        _ENGINE_PATH = clean_path
    return _ENGINE


def register(router: APIRouter) -> None:
    """Attach the historical-context routes to the existing v4 router."""
    from blm_v4.api import _db_path

    @router.get("/game/{game_id}/historical-context")
    def v4_game_historical_context(game_id: str) -> dict:
        """HISTORICAL CONTEXT for one game — descriptive, league/state-
        relative, read-only.

        status='matched' carries the matched canonical benchmark key, the
        own-state average pace, the observation's actual/required pace
        relative to it, the matched population's UNDER statistics and the
        primary UNDER state flag.  Any non-matching case returns the
        explicit ``no_mature_historical_context`` state with a reason —
        never a fallback average, never a fabricated value."""
        clean_path = _db_path().parent / "blm_metrics_clean.db"
        if not clean_path.exists():
            return {"game_id": game_id,
                    "status": "no_mature_historical_context",
                    "reason": "no_clean_metrics_database"}
        try:
            eng = _engine_for(clean_path)
            conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True,
                                   timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                ctx = eng.context_for(conn, game_id)
            finally:
                conn.close()
            ctx["game_id"] = game_id
            return ctx
        except Exception as e:
            return {"game_id": game_id,
                    "status": "no_mature_historical_context",
                    "reason": "unavailable", "error": str(e)[:200]}
