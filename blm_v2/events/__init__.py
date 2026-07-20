"""
BLM V2 — Event System Package

Typed async event bus with pub/sub pattern for decoupled communication
between BLM platform components.

Subsystems:
  - bus:      Central event bus with register/unregister, fire-and-forget,
              type safety, and handler filtering
  - handlers: Concrete event handler implementations that respond to
              domain events (snapshots, predictions, traps, logging)
"""

from blm_v2.events.bus import (
    EventBus,
    EventFilter,
    Subscription,
    get_event_bus,
    reset_event_bus,
)
from blm_v2.events.handlers import (
    LoggingHandler,
    MetricsHandler,
    PersistenceHandler,
    WebSocketBroadcaster,
    TrapAlertHandler,
)

__all__ = [
    "EventBus",
    "EventFilter",
    "Subscription",
    "get_event_bus",
    "reset_event_bus",
    "LoggingHandler",
    "MetricsHandler",
    "PersistenceHandler",
    "WebSocketBroadcaster",
    "TrapAlertHandler",
]
