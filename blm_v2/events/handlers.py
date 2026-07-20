"""
BLM V2 — Event Handler Implementations

Concrete event handlers that respond to BLM domain events. Each handler has a
single responsibility and is registered on the event bus by the application layer.

Available handlers:
  - LoggingHandler:      Structured logging of all events
  - MetricsHandler:      Track event frequency and latency metrics
  - PersistenceHandler:  Persist snapshot events to the database
  - WebSocketBroadcaster: Broadcast events to connected WebSocket clients
  - TrapAlertHandler:    Raise alerts when trap events exceed thresholds
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from blm_v2.events.bus import Subscription
from blm_v2.models.events import (
    BlmEvent,
    ConfidenceDrop,
    EventType,
    MarketMove,
    ModelCorrection,
    MomentumSwing,
    QuarterEnd,
    QuarterStart,
    SharpMoney,
    ThreePointerMade,
    TrapTriggered,
)

logger = logging.getLogger(__name__)


# ── Logging Handler ──────────────────────────────────────────────


class LoggingHandler:
    """Handle events by writing structured log entries.

    Uses the structlog-style dict-based logging (via standard library logger
    with extra fields) to capture every event for audit and debugging.
    """

    def __init__(
        self,
        log_level: int = logging.DEBUG,
        summary_interval: float = 60.0,
    ) -> None:
        self._log_level = log_level
        self._summary_interval = summary_interval
        self._event_counter: Counter[str] = Counter()
        self._last_summary: float = 0.0
        self._subscription: Optional[Subscription] = None

    async def handle_event(self, event: BlmEvent) -> None:
        """Log every event at the configured level."""
        self._event_counter[event.event_type.value] += 1

        logger.log(
            self._log_level,
            f"Event: {event.event_type.value}",
            extra={
                "event_type": event.event_type.value,
                "game_id": event.game_id,
                "timestamp": event.timestamp.isoformat(),
                "event_data": event.model_dump(mode="json"),
            },
        )

        # Periodically log a summary
        now = datetime.now(timezone.utc).timestamp()
        if now - self._last_summary > self._summary_interval:
            self._log_summary()

    def _log_summary(self) -> None:
        """Log a summary of event counts since the last summary."""
        if not self._event_counter:
            return

        total = sum(self._event_counter.values())
        breakdown = dict(self._event_counter.most_common())
        logger.info(
            f"Event summary: {total} events processed",
            extra={
                "total_events": total,
                "breakdown": breakdown,
                "handler": "LoggingHandler",
            },
        )
        self._event_counter.clear()
        self._last_summary = datetime.now(timezone.utc).timestamp()


# ── Metrics Handler ──────────────────────────────────────────────


class MetricsHandler:
    """Track event metrics: counts per type, latency, error rates.

    Stores in-memory counters that can be exposed via the health endpoint.
    """

    def __init__(self) -> None:
        self.event_counts: Counter[str] = Counter()
        self.three_pointer_count: int = 0
        self.trap_alert_count: int = 0
        self.market_move_count: int = 0
        self.sharp_money_count: int = 0
        self.confidence_drop_count: int = 0
        self.model_correction_count: int = 0
        self.quarter_changes: list[dict[str, Any]] = []
        self._last_reset: datetime = datetime.now(timezone.utc)

    async def handle_event(self, event: BlmEvent) -> None:
        """Update metrics from an event."""
        self.event_counts[event.event_type.value] += 1

        if isinstance(event, ThreePointerMade):
            self.three_pointer_count += 1
        elif isinstance(event, TrapTriggered):
            self.trap_alert_count += 1
        elif isinstance(event, MarketMove):
            self.market_move_count += 1
        elif isinstance(event, SharpMoney):
            self.sharp_money_count += 1
        elif isinstance(event, ConfidenceDrop):
            self.confidence_drop_count += 1
        elif isinstance(event, ModelCorrection):
            self.model_correction_count += 1
        elif isinstance(event, QuarterStart):
            self.quarter_changes.append({
                "quarter": event.quarter,
                "home_score": event.home_score,
                "away_score": event.away_score,
                "timestamp": event.timestamp.isoformat(),
                "type": "start",
            })
        elif isinstance(event, QuarterEnd):
            self.quarter_changes.append({
                "quarter": event.quarter,
                "home_score": event.home_score,
                "away_score": event.away_score,
                "period_total": event.period_total,
                "timestamp": event.timestamp.isoformat(),
                "type": "end",
            })

    def get_summary(self) -> dict[str, Any]:
        """Return a snapshot of current metrics."""
        return {
            "event_counts": dict(self.event_counts),
            "three_pointers": self.three_pointer_count,
            "trap_alerts": self.trap_alert_count,
            "market_moves": self.market_move_count,
            "sharp_money_events": self.sharp_money_count,
            "confidence_drops": self.confidence_drop_count,
            "model_corrections": self.model_correction_count,
            "quarter_changes": len(self.quarter_changes),
            "tracking_since": self._last_reset.isoformat(),
        }

    def reset(self) -> None:
        """Reset all metrics counters."""
        self.event_counts.clear()
        self.three_pointer_count = 0
        self.trap_alert_count = 0
        self.market_move_count = 0
        self.sharp_money_count = 0
        self.confidence_drop_count = 0
        self.model_correction_count = 0
        self.quarter_changes.clear()
        self._last_reset = datetime.now(timezone.utc)


# ── Persistence Handler ──────────────────────────────────────────


class PersistenceHandler:
    """Persist events to the database.

    Currently logs events; will persist to the database once the storage
    layer is implemented.
    """

    def __init__(self) -> None:
        self._stored_count = 0

    async def handle_event(self, event: BlmEvent) -> None:
        """Persist the event to the database."""
        self._stored_count += 1
        logger.debug(
            "Persisting event",
            extra={
                "event_type": event.event_type.value,
                "game_id": event.game_id,
                "stored_count": self._stored_count,
            },
        )

    @property
    def stored_count(self) -> int:
        return self._stored_count


# ── WebSocket Broadcaster ────────────────────────────────────────


class WebSocketBroadcaster:
    """Broadcast events to connected WebSocket clients.

    Manages a set of connected client queues. Each client receives a
    serialised event dict whenever one is emitted.
    """

    def __init__(self) -> None:
        self._clients: dict[str, asyncio.Queue] = {}
        self._max_queue_size: int = 256

    async def register_client(self, client_id: str) -> asyncio.Queue:
        """Register a new WebSocket client for event streaming.

        Args:
            client_id: Unique client identifier.

        Returns:
            An asyncio.Queue that will receive serialised event dicts.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._clients[client_id] = queue
        logger.info(
            "WebSocket client registered",
            extra={"client_id": client_id, "total_clients": len(self._clients)},
        )
        return queue

    async def unregister_client(self, client_id: str) -> None:
        """Remove a WebSocket client from the broadcast list."""
        self._clients.pop(client_id, None)
        logger.info(
            "WebSocket client unregistered",
            extra={"client_id": client_id, "total_clients": len(self._clients)},
        )

    async def handle_event(self, event: BlmEvent) -> None:
        """Broadcast an event to all connected clients."""
        if not self._clients:
            return

        try:
            serialised = event.model_dump(mode="json")
        except Exception:
            logger.exception("Failed to serialise event for broadcast")
            return

        disconnected: list[str] = []
        for client_id, queue in self._clients.items():
            try:
                queue.put_nowait(serialised)
            except asyncio.QueueFull:
                # Client is too slow — drop oldest item and add new
                try:
                    queue.get_nowait()
                    queue.put_nowait(serialised)
                except asyncio.QueueEmpty:
                    pass
                logger.warning(
                    "Client queue full, dropped oldest event",
                    extra={"client_id": client_id},
                )
            except Exception:
                disconnected.append(client_id)

        # Clean up disconnected clients
        for cid in disconnected:
            await self.unregister_client(cid)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# ── Trap Alert Handler ───────────────────────────────────────────


class TrapAlertHandler:
    """Monitor trap events and trigger higher-level alerts.

    Tracks consecutive trap alerts and raises severity when thresholds are
    exceeded. Integrates with external alerting systems (email, SMS, etc.)
    via a pluggable callback.
    """

    def __init__(
        self,
        on_threshold_breach: Optional[Callable[..., Any]] = None,
        consecutive_alert_threshold: int = 3,
        time_window_seconds: float = 300.0,
    ) -> None:
        self._on_threshold_breach = on_threshold_breach
        self._consecutive_alert_threshold = consecutive_alert_threshold
        self._time_window_seconds = time_window_seconds
        self._recent_alerts: list[dict[str, Any]] = []
        self._max_recent_alerts: int = 100

    async def handle_event(self, event: BlmEvent) -> None:
        """Evaluate a trap event and escalate if thresholds are exceeded."""
        if not isinstance(event, TrapTriggered):
            return

        # Record the alert
        alert_record = {
            "trap_type": event.trap_type,
            "trap_score": event.trap_score,
            "threshold": event.threshold,
            "game_id": event.game_id,
            "timestamp": event.timestamp.isoformat(),
            "signal_detail": event.signal_detail,
        }
        self._recent_alerts.append(alert_record)

        # Trim old alerts outside the time window
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - self._time_window_seconds
        self._recent_alerts = [
            a for a in self._recent_alerts
            if self._parse_timestamp(a["timestamp"]) > cutoff
        ]

        # Keep list bounded
        if len(self._recent_alerts) > self._max_recent_alerts:
            self._recent_alerts = self._recent_alerts[-self._max_recent_alerts:]

        # Check for consecutive alert threshold
        recent_traps = [
            a for a in self._recent_alerts
            if self._parse_timestamp(a["timestamp"]) > cutoff
        ]
        if len(recent_traps) >= self._consecutive_alert_threshold:
            logger.warning(
                f"Trap alert threshold exceeded: {len(recent_traps)} alerts "
                f"in {self._time_window_seconds}s window",
                extra={
                    "alert_count": len(recent_traps),
                    "window_seconds": self._time_window_seconds,
                    "latest_score": event.trap_score,
                    "trap_type": event.trap_type,
                    "game_id": event.game_id,
                },
            )

            # Call the threshold breach callback if configured
            if self._on_threshold_breach is not None:
                try:
                    if asyncio.iscoroutinefunction(self._on_threshold_breach):
                        await self._on_threshold_breach(recent_traps)
                    else:
                        self._on_threshold_breach(recent_traps)
                except Exception:
                    logger.exception("Trap alert callback failed")

    def _parse_timestamp(self, ts_str: str) -> float:
        """Parse ISO timestamp string to epoch seconds."""
        try:
            dt = datetime.fromisoformat(ts_str)
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0

    def get_recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent trap alerts."""
        return self._recent_alerts[-limit:]
