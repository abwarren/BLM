"""
BLM V2 — Typed Async Event Bus

A publish/subscribe event bus with:
  - Type-safe event dispatch via generic handler registration
  - Async handler execution with optional fire-and-forget
  - Event type filtering and handler predicates
  - Handler timeout warnings
  - Thread-safe singleton access

Usage:
    from blm_v2.events.bus import get_event_bus
    from blm_v2.models.events import ThreePointerMade

    bus = get_event_bus()

    async def log_three(event: ThreePointerMade) -> None:
        print(f"Three! {event.shooter}")

    bus.register(ThreePointerMade, log_three)
    await bus.emit(ThreePointerMade(game_id="g-1", team="home", ...))
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import defaultdict
from typing import (
    Any,
    Awaitable,
    Callable,
    Optional,
    Protocol,
    TypeVar,
)

from blm_v2.models.events import BlmEvent, EventType, event_from_dict

logger = logging.getLogger(__name__)

# ── Type aliases ─────────────────────────────────────────────────


E = TypeVar("E", bound=BlmEvent)

# A handler is an async callable that accepts a specific event type
EventHandlerFn = Callable[[E], Awaitable[None]]


class EventFilter(Protocol):
    """Protocol for event filter predicates.

    A filter is an async callable that receives an event and returns ``True``
    if the handler should be invoked.
    """

    async def __call__(self, event: BlmEvent) -> bool: ...


# ── Subscription ─────────────────────────────────────────────────


class Subscription:
    """Represents a registered handler subscription on the event bus.

    Provides a ``cancel()`` method for unregistration and metadata for
    observability.
    """

    def __init__(
        self,
        bus: EventBus,
        event_type: type[BlmEvent] | None,
        handler: EventHandlerFn,
        handler_id: str,
        description: str = "",
    ) -> None:
        self._bus = bus
        self.event_type = event_type
        self.handler = handler
        self.handler_id = handler_id
        self.description = description
        self.created_at = time.time()
        self.invocation_count = 0
        self.last_invoked_at: float | None = None
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        """True if this subscription has been cancelled."""
        return self._cancelled

    def cancel(self) -> None:
        """Remove this subscription from the event bus."""
        self._bus._unregister(self)
        self._cancelled = True

    def __repr__(self) -> str:
        return (
            f"Subscription(id={self.handler_id!r}, "
            f"type={self.event_type.__name__ if self.event_type else 'ALL'}, "
            f"cancelled={self._cancelled})"
        )


# ── Event Bus ────────────────────────────────────────────────────


class EventBus:
    """Typed asynchronous publish/subscribe event bus.

    Features:
      - Register handlers for specific event types or all events
      - Async emit with optional fire-and-forget (no await)
      - Event filtering via predicate functions
      - Handler timeout warnings for long-running handlers
      - Thread-safe singleton management
      - Metric tracking (total events, handler counts)
    """

    def __init__(self, max_handlers: int = 100, handler_timeout: float = 10.0) -> None:
        self._max_handlers = max_handlers
        self._handler_timeout = handler_timeout

        # Registry: event_type -> list of (handler_fn, filter_fn, subscription)
        self._handlers: dict[type[BlmEvent], list[tuple[EventHandlerFn, EventFilter | None, Subscription]]] = defaultdict(list)

        # Global handlers that receive ALL events (wildcard catch-all)
        self._global_handlers: list[tuple[EventHandlerFn, EventFilter | None, Subscription]] = []

        # Metrics
        self._total_events_emitted = 0
        self._total_handlers_invoked = 0
        self._handler_id_counter = 0

        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    # ── Properties ──────────────────────────────────────────────

    @property
    def total_events_emitted(self) -> int:
        """Total count of events emitted since bus creation."""
        return self._total_events_emitted

    @property
    def total_handlers_invoked(self) -> int:
        """Total count of handler invocations since bus creation."""
        return self._total_handlers_invoked

    @property
    def registered_handler_count(self) -> int:
        """Count of currently registered handlers (typed + global)."""
        typed_count = sum(len(v) for v in self._handlers.values())
        return typed_count + len(self._global_handlers)

    @property
    def registered_event_types(self) -> list[type[BlmEvent]]:
        """Event types that have at least one registered handler."""
        return [et for et, handlers in self._handlers.items() if handlers]

    # ── Registration ────────────────────────────────────────────

    def register(
        self,
        event_type: type[E] | None,
        handler: EventHandlerFn[E],
        *,
        filter_fn: EventFilter | None = None,
        description: str = "",
    ) -> Subscription:
        """Register a handler for a specific event type (or all events if None).

        Args:
            event_type: The event type to listen for, or ``None`` to receive all events.
            handler: Async callable accepting the event instance.
            filter_fn: Optional async predicate for additional filtering.
            description: Human-readable description for observability.

        Returns:
            A ``Subscription`` that can be used to cancel this handler.

        Raises:
            ValueError: If the handler limit would be exceeded.
        """
        self._handler_id_counter += 1
        handler_id = f"h{self._handler_id_counter}"

        subscription = Subscription(
            bus=self,
            event_type=event_type,
            handler=handler,
            handler_id=handler_id,
            description=description or handler.__name__,
        )

        entry = (handler, filter_fn, subscription)

        if event_type is None:
            if len(self._global_handlers) >= self._max_handlers:
                raise ValueError(
                    f"Global handler limit ({self._max_handlers}) exceeded"
                )
            self._global_handlers.append(entry)
            logger.debug(
                "Registered global handler",
                extra={"handler_id": handler_id, "name": subscription.description},
            )
        else:
            if len(self._handlers[event_type]) >= self._max_handlers:
                raise ValueError(
                    f"Handler limit ({self._max_handlers}) exceeded for "
                    f"{event_type.__name__}"
                )
            self._handlers[event_type].append(entry)
            logger.debug(
                "Registered typed handler",
                extra={
                    "handler_id": handler_id,
                    "event_type": event_type.__name__,
                    "name": subscription.description,
                },
            )

        return subscription

    def unregister(self, handler: EventHandlerFn, event_type: type[BlmEvent] | None = None) -> bool:
        """Remove a handler by function reference.

        Args:
            handler: The handler function to remove.
            event_type: Restrict removal to a specific event type.
                        If None, searches all handlers.

        Returns:
            True if the handler was found and removed.
        """
        found = False

        if event_type is not None:
            # Remove from typed handlers
            self._handlers[event_type] = [
                (h, f, s) for h, f, s in self._handlers[event_type]
                if h is not handler and not s._cancelled
            ]
            found = True  # Optimistic; we cleaned the list regardless
        else:
            # Search all typed handler lists
            for et in list(self._handlers.keys()):
                original_count = len(self._handlers[et])
                self._handlers[et] = [
                    (h, f, s) for h, f, s in self._handlers[et]
                    if h is not handler and not s._cancelled
                ]
                if len(self._handlers[et]) < original_count:
                    found = True
                # Clean up empty type buckets
                if not self._handlers[et]:
                    del self._handlers[et]

            # Remove from global handlers
            original_count = len(self._global_handlers)
            self._global_handlers = [
                (h, f, s) for h, f, s in self._global_handlers
                if h is not handler and not s._cancelled
            ]
            if len(self._global_handlers) < original_count:
                found = True

        return found

    def _unregister(self, subscription: Subscription) -> None:
        """Internal: remove a subscription by its Subscription object."""
        et = subscription.event_type
        if et is None:
            self._global_handlers = [
                (h, f, s) for h, f, s in self._global_handlers
                if s is not subscription
            ]
        else:
            self._handlers[et] = [
                (h, f, s) for h, f, s in self._handlers[et]
                if s is not subscription
            ]
            if not self._handlers[et]:
                del self._handlers[et]

    # ── Emission ────────────────────────────────────────────────

    async def emit(
        self,
        event: BlmEvent,
        fire_and_forget: bool = False,
        raise_handler_errors: bool = False,
    ) -> None:
        """Emit an event to all matching handlers.

        Args:
            event: The event instance to dispatch.
            fire_and_forget: If True, handlers run in the background without
                             blocking the caller.
            raise_handler_errors: If True, handler exceptions bubble up.
                                  If False (default), they are logged and swallowed.

        Raises:
            ValueError: If the event is not a valid BlmEvent instance.
        """
        if not isinstance(event, BlmEvent):
            raise ValueError(
                f"Expected BlmEvent instance, got {type(event).__name__}"
            )

        event_type = type(event)
        self._total_events_emitted += 1

        # Collect handlers from both typed and global registries
        handlers_to_call: list[tuple[EventHandlerFn, EventFilter | None, Subscription]] = []

        # Typed handlers
        for entry in self._handlers.get(event_type, []):
            handlers_to_call.append(entry)

        # Typed handlers registered for parent classes
        for et, entries in self._handlers.items():
            if et is not event_type and issubclass(event_type, et):
                handlers_to_call.extend(entries)

        # Global (wildcard) handlers
        handlers_to_call.extend(self._global_handlers)

        if not handlers_to_call:
            logger.debug(
                "No handlers for event",
                extra={
                    "event_type": event_type.__name__,
                    "event_id": event.game_id,
                },
            )
            return

        # Wrap in a task if fire-and-forget
        if fire_and_forget:
            asyncio.ensure_future(
                self._dispatch(event, handlers_to_call, raise_handler_errors)
            )
        else:
            await self._dispatch(event, handlers_to_call, raise_handler_errors)

    async def emit_dict(
        self,
        data: dict,
        fire_and_forget: bool = False,
        raise_handler_errors: bool = False,
    ) -> BlmEvent:
        """Deserialise a dict and emit the resulting event.

        Uses ``event_from_dict`` to determine the correct concrete event type.

        Args:
            data: Dict with at least an ``event_type`` key.
            fire_and_forget: If True, handlers run in the background.
            raise_handler_errors: If True, handler exceptions bubble up.

        Returns:
            The deserialised event instance.

        Raises:
            ValueError: If the event dict cannot be deserialised.
        """
        event = event_from_dict(data)
        await self.emit(event, fire_and_forget=fire_and_forget, raise_handler_errors=raise_handler_errors)
        return event

    # ── Internal dispatch ───────────────────────────────────────

    async def _dispatch(
        self,
        event: BlmEvent,
        handlers: list[tuple[EventHandlerFn, EventFilter | None, Subscription]],
        raise_errors: bool,
    ) -> None:
        """Execute all handlers for an event, respecting filters."""
        for handler_fn, filter_fn, subscription in handlers:
            # Check filter predicate if present
            if filter_fn is not None:
                try:
                    should_handle = await filter_fn(event)
                    if not should_handle:
                        continue
                except Exception:
                    logger.exception(
                        "Event filter raised exception",
                        extra={
                            "event_type": type(event).__name__,
                            "handler_id": subscription.handler_id,
                        },
                    )
                    if raise_errors:
                        raise
                    continue

            # Invoke handler
            start = time.monotonic()
            try:
                if asyncio.iscoroutinefunction(handler_fn):
                    await handler_fn(event)
                else:
                    # Allow synchronous handlers (wrapped in await)
                    result = handler_fn(event)
                    if asyncio.iscoroutine(result):
                        await result
            except asyncio.TimeoutError:
                logger.warning(
                    "Handler timed out",
                    extra={
                        "handler_id": subscription.handler_id,
                        "handler": subscription.description,
                        "event_type": type(event).__name__,
                    },
                )
            except Exception:
                logger.exception(
                    "Handler raised exception",
                    extra={
                        "handler_id": subscription.handler_id,
                        "handler": subscription.description,
                        "event_type": type(event).__name__,
                    },
                )
                if raise_errors:
                    raise
            finally:
                elapsed = time.monotonic() - start
                subscription.invocation_count += 1
                subscription.last_invoked_at = time.time()
                self._total_handlers_invoked += 1

                if elapsed > self._handler_timeout:
                    logger.warning(
                        "Slow handler detected",
                        extra={
                            "handler_id": subscription.handler_id,
                            "handler": subscription.description,
                            "elapsed_seconds": round(elapsed, 3),
                            "event_type": type(event).__name__,
                        },
                    )

    # ── Utility ─────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove ALL handlers from the bus. Useful for testing."""
        self._handlers.clear()
        self._global_handlers.clear()
        self._total_events_emitted = 0
        self._total_handlers_invoked = 0

    def get_handler_stats(self) -> dict[str, Any]:
        """Return a snapshot of bus metrics and subscription info."""
        return {
            "total_events_emitted": self._total_events_emitted,
            "total_handlers_invoked": self._total_handlers_invoked,
            "registered_handler_count": self.registered_handler_count,
            "registered_event_types": [
                t.__name__ for t in self.registered_event_types
            ],
            "typed_subscriptions": {
                t.__name__: [
                    {
                        "handler_id": s.handler_id,
                        "description": s.description,
                        "invocation_count": s.invocation_count,
                        "created_at": s.created_at,
                        "cancelled": s.is_cancelled,
                    }
                    for _, _, s in entries
                ]
                for t, entries in self._handlers.items()
            },
            "global_subscriptions": [
                {
                    "handler_id": s.handler_id,
                    "description": s.description,
                    "invocation_count": s.invocation_count,
                    "created_at": s.created_at,
                    "cancelled": s.is_cancelled,
                }
                for _, _, s in self._global_handlers
            ],
        }


# ── Singleton ───────────────────────────────────────────────────


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the application-wide EventBus singleton.

    The bus is initialised with settings from the global config on first call.
    """
    global _bus
    if _bus is None:
        from blm_v2.config import get_settings

        settings = get_settings()
        _bus = EventBus(
            max_handlers=settings.event_bus_max_handlers,
            handler_timeout=settings.event_bus_handler_timeout,
        )
        logger.info("Event bus initialised")
    return _bus


def reset_event_bus() -> EventBus:
    """Reset and recreate the event bus singleton. Useful in tests."""
    global _bus
    _bus = None
    return get_event_bus()
