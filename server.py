#!/usr/bin/env python3
"""
BLM V2 — Server Entry Point

Standalone entry point for the BLM V2 platform.  Run with::

    python server.py

Uses uvicorn on port 8000.  Configures structured logging, injects
dependency stubs (or real implementations), and starts a background
scheduler task.

Environment variables:
  BLM_ENV          — "production" or "development" (default: development)
  LOG_LEVEL        — Log level override (default: INFO)
  HOST             — Bind address (default: 0.0.0.0)
  PORT             — Listen port (default: 8000)
  RELOAD           — "true" enables hot-reload (default: false)
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

# Ensure the project root is on sys.path so that ``blm_v2`` imports work
# regardless of how the script is invoked.
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── Environment defaults ──────────────────────────────────────────────

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
RELOAD = os.environ.get("RELOAD", "false").lower() in ("true", "1", "yes")
ENVIRONMENT = os.environ.get("BLM_ENV", "development")


# ═══════════════════════════════════════════════════════════════════════
# Dependency stubs (stand-in until real implementations are wired)
# ═══════════════════════════════════════════════════════════════════════

class _StubTSInterface:
    """Placeholder timeseries interface returning empty data."""

    async def get_latest_snapshot(self, game_id: str) -> Optional[Dict[str, Any]]:
        return None

    async def get_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return []

    async def get_replay_snapshots(self, game_id: str) -> List[Dict[str, Any]]:
        return []

    async def get_chart_data(self, game_id: str) -> List[Dict[str, Any]]:
        return []

    async def get_live_game(self) -> Optional[Dict[str, Any]]:
        return None

    async def list_games(self) -> List[Dict[str, Any]]:
        return []

    async def get_game_detail(self, game_id: str) -> Optional[Dict[str, Any]]:
        return None


class _StubStorageInterface:
    """Placeholder storage interface returning empty data."""

    async def get_game(self, game_id: str) -> Optional[Dict[str, Any]]:
        return None

    async def get_events(self, game_id: str) -> List[Dict[str, Any]]:
        return []

    async def get_alerts(self, game_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    async def get_traps(self, game_id: str) -> Dict[str, Any]:
        return {"game_id": game_id, "active_traps": [], "trap_history": []}

    async def get_model_state(self) -> Dict[str, Any]:
        return {
            "version": "2.0.0",
            "status": "running",
            "total_snapshots_processed": 0,
            "active_games": 0,
        }

    async def list_games(self) -> List[Dict[str, Any]]:
        return []


class _StubEventBus:
    """Placeholder event bus that swallows all events."""

    async def publish(self, event_type: str, data: Any) -> None:
        pass

    async def subscribe(self, event_type: str, handler) -> Any:
        return None

    async def unsubscribe(self, subscription: Any) -> None:
        pass


class _StubBLMEngine:
    """Placeholder BLM engine returning passthrough data."""

    async def enrich_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return {**snapshot, "blm_score": None, "confidence": None, "pace": None, "traps": []}

    async def get_confidence(self, game_id: str) -> Optional[float]:
        return None

    async def get_blm_score(self, game_id: str) -> Optional[float]:
        return None

    async def get_pace(self, game_id: str) -> Optional[float]:
        return None

    async def detect_traps(self, game_id: str) -> List[Dict[str, Any]]:
        return []

    async def get_predictions(self, game_id: str) -> Dict[str, Any]:
        return {}

    async def get_config(self) -> Dict[str, Any]:
        return {"mode": ENVIRONMENT, "version": "2.0.0"}


# ═══════════════════════════════════════════════════════════════════════
# Background scheduler
# ═══════════════════════════════════════════════════════════════════════

async def scheduler_task(interval_s: float = 30.0) -> None:
    """Placeholder background task that runs periodically.

    In production this would drive the snapshot collection pipeline,
    engine computations, and persistence.
    """
    from blm_v2.telemetry.logging import get_logger
    logger = get_logger("scheduler")
    logger.info("scheduler_started", interval_s=interval_s)

    while True:
        try:
            await asyncio.sleep(interval_s)
            logger.debug("scheduler_tick")
        except asyncio.CancelledError:
            logger.info("scheduler_stopped")
            break


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    """Start the BLM V2 server.

    This function:
      1. Configures structured logging.
      2. Wires dependency stubs (swap with real implementations later).
      3. Creates the FastAPI V2 application.
      4. Starts the background scheduler task.
      5. Launches uvicorn.
    """
    # ── 1. Logging ─────────────────────────────────────────────
    from blm_v2.telemetry.logging import setup_logging, get_logger

    setup_logging(environment=ENVIRONMENT)
    logger = get_logger("server")

    logger.info("blm_v2_initializing", environment=ENVIRONMENT, host=HOST, port=PORT)

    # ── 2. Wire dependencies ───────────────────────────────────
    from blm_v2.api.dependencies import wire_dependencies

    ts = _StubTSInterface()
    storage = _StubStorageInterface()
    event_bus = _StubEventBus()
    engine = _StubBLMEngine()

    wire_dependencies(
        ts_interface=ts,
        storage_interface=storage,
        event_bus=event_bus,
        blm_engine=engine,
    )
    logger.info("dependencies_wired", stub=True)

    # ── 3. Create FastAPI app ──────────────────────────────────
    from blm_v2.api.v2_fastapi import create_v2_app

    app = create_v2_app()

    # ── 4. Background scheduler ────────────────────────────────
    @app.on_event("startup")
    async def start_scheduler():
        scheduler = asyncio.create_task(scheduler_task(interval_s=30.0))
        app.state._scheduler_task = scheduler
        logger.info("background_scheduler_started")

    @app.on_event("shutdown")
    async def stop_scheduler():
        scheduler = getattr(app.state, "_scheduler_task", None)
        if scheduler and not scheduler.done():
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass
        logger.info("background_scheduler_stopped")

    # ── 5. Start uvicorn ───────────────────────────────────────
    logger.info(
        "blm_v2_starting",
        version="2.0.0",
        bind=f"{HOST}:{PORT}",
        reload=RELOAD,
    )

    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        reload=RELOAD,
        log_level="info" if ENVIRONMENT == "development" else "warning",
        access_log=False,  # structlog handles our logging
    )


if __name__ == "__main__":
    main()
