"""
BLM V2 — WebSocket Handler

Pushes enriched snapshots to connected clients at a configurable interval
(default: 20 seconds).  Clients can subscribe to specific game IDs and
send ping/pong keepalives.

Message protocol (JSON):
  Client → Server:
    {"subscribe": "game_id"}     — Subscribe to a specific game
    {"unsubscribe": "game_id"}   — Unsubscribe from a game
    {"action": "ping"}           — Keepalive ping

  Server → Client:
    {"type": "snapshot", "game_id": "...", "data": {...}}
    {"type": "subscribed", "game_id": "..."}
    {"type": "unsubscribed", "game_id": "..."}
    {"type": "pong", "timestamp": "..."}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect, status

from blm_v2.api.dependencies import TSInterface, StorageInterface, BLMEngineInterface
from blm_v2.telemetry.logging import get_logger
from blm_v2.telemetry.metrics import MetricsCollector

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

DEFAULT_BROADCAST_INTERVAL_S: float = 20.0
PING_TIMEOUT_S: float = 60.0


# ── Connection manager ────────────────────────────────────────────────

class ConnectionManager:
    """Manages all active WebSocket connections and their subscriptions.

    Each connection can be subscribed to zero or more game IDs.
    A connection subscribed to ``"*"`` receives *all* snapshots.
    """

    def __init__(self) -> None:
        self._connections: Dict[WebSocket, Set[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections[websocket] = set()

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected client."""
        async with self._lock:
            self._connections.pop(websocket, None)

    async def subscribe(self, websocket: WebSocket, game_id: str) -> None:
        """Add a game_id subscription for a connected client."""
        async with self._lock:
            subs = self._connections.get(websocket)
            if subs is not None:
                subs.add(game_id)

    async def unsubscribe(self, websocket: WebSocket, game_id: str) -> None:
        """Remove a game_id subscription for a connected client."""
        async with self._lock:
            subs = self._connections.get(websocket)
            if subs is not None:
                subs.discard(game_id)

    async def get_subscribers_for_game(self, game_id: str) -> list[WebSocket]:
        """Return all WebSocket connections interested in *game_id*."""
        targets: list[WebSocket] = []
        async with self._lock:
            for ws, subs in self._connections.items():
                if not subs or "*" in subs or game_id in subs:
                    targets.append(ws)
        return targets

    @property
    def active_connections(self) -> int:
        return len(self._connections)


# ── Singleton ─────────────────────────────────────────────────────────

_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Return the application-wide ``ConnectionManager`` singleton."""
    return _manager


# ── Handler ───────────────────────────────────────────────────────────

async def handle_websocket(
    websocket: WebSocket,
    ts_interface: TSInterface,
    storage_interface: StorageInterface,
    blm_engine: BLMEngineInterface,
    metrics: MetricsCollector,
) -> None:
    """Main WebSocket handler coroutine.

    Accepts the connection, manages subscriptions, pushes snapshots
    every *interval* seconds, and responds to ping/pong keepalives.
    """
    manager = get_connection_manager()
    await manager.connect(websocket)

    logger.info(
        "ws_client_connected",
        client_id=id(websocket),
        active_connections=manager.active_connections,
    )

    broadcast_task: Optional[asyncio.Task] = None

    try:
        # Start a background broadcast loop for this client.
        broadcast_task = asyncio.create_task(
            _broadcast_loop(
                websocket,
                ts_interface,
                blm_engine,
                metrics,
                interval=DEFAULT_BROADCAST_INTERVAL_S,
            )
        )

        # ── Message receive loop ──────────────────────────────
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=PING_TIMEOUT_S
            )

            try:
                msg: dict = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await _send_json(websocket, {
                    "type": "error",
                    "message": "Invalid JSON message",
                })
                continue

            action = msg.get("action") or (
                "subscribe" if "subscribe" in msg else
                "unsubscribe" if "unsubscribe" in msg else
                None
            )

            if action == "ping":
                await _send_json(websocket, {
                    "type": "pong",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })

            elif action == "subscribe" or "subscribe" in msg:
                game_id = msg.get("subscribe", "")
                if not game_id:
                    await _send_json(websocket, {
                        "type": "error",
                        "message": "Missing 'subscribe' value",
                    })
                    continue
                await manager.subscribe(websocket, game_id)
                await _send_json(websocket, {
                    "type": "subscribed",
                    "game_id": game_id,
                })
                logger.debug("ws_subscribed", client_id=id(websocket), game_id=game_id)

            elif action == "unsubscribe" or "unsubscribe" in msg:
                game_id = msg.get("unsubscribe", "")
                if not game_id:
                    await _send_json(websocket, {
                        "type": "error",
                        "message": "Missing 'unsubscribe' value",
                    })
                    continue
                await manager.unsubscribe(websocket, game_id)
                await _send_json(websocket, {
                    "type": "unsubscribed",
                    "game_id": game_id,
                })
                logger.debug("ws_unsubscribed", client_id=id(websocket), game_id=game_id)

            else:
                await _send_json(websocket, {
                    "type": "error",
                    "message": f"Unknown action: {action}",
                })

    except asyncio.TimeoutError:
        logger.info("ws_ping_timeout", client_id=id(websocket))
    except WebSocketDisconnect:
        logger.info("ws_client_disconnected", client_id=id(websocket))
    except Exception:
        logger.exception("ws_handler_error", client_id=id(websocket))
    finally:
        if broadcast_task is not None:
            broadcast_task.cancel()
        await manager.disconnect(websocket)
        logger.debug(
            "ws_cleaned_up",
            client_id=id(websocket),
            active_connections=manager.active_connections,
        )


# ── Internal helpers ──────────────────────────────────────────────────

async def _broadcast_loop(
    websocket: WebSocket,
    ts_interface: TSInterface,
    blm_engine: BLMEngineInterface,
    metrics: MetricsCollector,
    interval: float,
) -> None:
    """Periodically push enriched snapshots to the connected client."""
    while True:
        try:
            await asyncio.sleep(interval)

            with metrics.timer("websocket_delivery_time"):
                # Try to get a live game snapshot and send it.
                live = await ts_interface.get_live_game()
                if live:
                    enriched = await blm_engine.enrich_snapshot(live)
                    await _send_json(websocket, {
                        "type": "snapshot",
                        "game_id": enriched.get("game_id", "unknown"),
                        "data": enriched,
                        "timestamp": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    })
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("ws_broadcast_error")
            # Don't crash the loop — just log and continue.
            await asyncio.sleep(interval)


async def _send_json(websocket: WebSocket, data: dict) -> None:
    """Send a JSON message, silently handling disconnection."""
    try:
        await websocket.send_json(data)
    except WebSocketDisconnect:
        pass  # Cleanup handled by the caller.
    except Exception:
        logger.exception("ws_send_error")
