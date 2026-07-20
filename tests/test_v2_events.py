"""Tests for BLM V2 Event Bus.

Tests the async pub/sub mechanism, handler registration, event type filtering,
and fire-and-forget dispatch.  All tests use the real EventBus without external
dependencies.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from blm_v2.events.bus import EventBus
from blm_v2.models.events import (
    BlmEvent,
    EventType,
    ThreePointerMade,
    QuarterEnd,
    TrapTriggered,
    MomentumSwing,
)


@pytest.fixture
def bus():
    """Return a clean EventBus for each test."""
    b = EventBus()
    yield b
    b.clear()


@pytest.fixture
async def async_bus():
    """Return a clean EventBus (async fixture)."""
    b = EventBus()
    yield b
    b.clear()


# ═════════════════════════════════════════════════════════════════════
# Publish / Subscribe
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_publish_subscribe(bus):
    """A registered handler is called when an event is emitted."""
    received: List[BlmEvent] = []

    async def handler(event: BlmEvent):
        received.append(event)

    bus.register(ThreePointerMade, handler)

    event = ThreePointerMade(
        game_id="g-1",
        team="home",
        score_before=50,
        score_after=53,
    )
    await bus.emit(event)

    assert len(received) == 1
    assert received[0].game_id == "g-1"
    assert isinstance(received[0], ThreePointerMade)


@pytest.mark.asyncio
async def test_publish_subscribe_multiple_handlers(bus):
    """Multiple handlers for the same event type all fire."""
    received = []

    async def h1(event):
        received.append("h1")

    async def h2(event):
        received.append("h2")

    bus.register(ThreePointerMade, h1)
    bus.register(ThreePointerMade, h2)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    assert received == ["h1", "h2"]


@pytest.mark.asyncio
async def test_global_handler(bus):
    """A glob al handler (event_type=None) receives all events."""
    received = []

    async def global_h(event):
        received.append(type(event).__name__)

    bus.register(None, global_h)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    await bus.emit(QuarterEnd(game_id="g-1", quarter=1, home_score=20, away_score=18))

    assert "ThreePointerMade" in received
    assert "QuarterEnd" in received


# ═════════════════════════════════════════════════════════════════════
# Unsubscribe
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unsubscribe_by_subscription(bus):
    """A subscription.cancel() prevents the handler from being called."""
    received = []

    async def handler(event):
        received.append(event)

    sub = bus.register(ThreePointerMade, handler)
    sub.cancel()

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    assert len(received) == 0


@pytest.mark.asyncio
async def test_unsubscribe_by_function(bus):
    """unregister(handler) removes the handler."""
    received = []

    async def handler(event):
        received.append(event)

    bus.register(ThreePointerMade, handler)
    bus.unregister(handler)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    assert len(received) == 0


@pytest.mark.asyncio
async def test_unsubscribe_typed(bus):
    """unregister(handler, event_type) only removes from that type."""
    received = []

    async def handler(event):
        received.append(event)

    bus.register(ThreePointerMade, handler)
    bus.register(QuarterEnd, handler)
    bus.unregister(handler, ThreePointerMade)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    await bus.emit(QuarterEnd(game_id="g-1", quarter=1, home_score=20, away_score=18))

    assert len(received) == 1
    assert isinstance(received[0], QuarterEnd)


# ═════════════════════════════════════════════════════════════════════
# Event type filtering
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_event_type_filtering(bus):
    """Handler only receives events of the registered type."""
    three_pt_received = []
    quarter_received = []

    async def three_h(event):
        three_pt_received.append(event)

    async def quarter_h(event):
        quarter_received.append(event)

    bus.register(ThreePointerMade, three_h)
    bus.register(QuarterEnd, quarter_h)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    await bus.emit(QuarterEnd(game_id="g-1", quarter=1, home_score=20, away_score=18))

    assert len(three_pt_received) == 1
    assert len(quarter_received) == 1


@pytest.mark.asyncio
async def test_predicate_filter(bus):
    """A filter_fn can suppress handler invocations."""
    received = []

    async def handler(event):
        received.append(event)

    async def only_home(event: BlmEvent) -> bool:
        return isinstance(event, ThreePointerMade) and event.team == "home"

    bus.register(ThreePointerMade, handler, filter_fn=only_home)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    await bus.emit(ThreePointerMade(game_id="g-1", team="away", score_before=10, score_after=13))

    assert len(received) == 1
    assert received[0].team == "home"


# ═════════════════════════════════════════════════════════════════════
# Fire and forget
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fire_and_forget(bus):
    """fire_and_forget=True dispatches without blocking."""
    received = []

    async def handler(event):
        await asyncio.sleep(0.05)
        received.append(event)

    bus.register(TrapTriggered, handler)

    await bus.emit(
        TrapTriggered(
            game_id="g-1",
            trap_type="bull_trap",
            trap_score=0.8,
        ),
        fire_and_forget=True,
    )

    # Handler runs in background — give it time.
    await asyncio.sleep(0.1)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_fire_and_forget_no_block(bus):
    """emit with fire_and_forget returns immediately."""
    received = []

    async def slow_handler(event):
        await asyncio.sleep(10.0)  # very slow
        received.append(event)

    bus.register(MomentumSwing, slow_handler)

    # Should return immediately, not wait 10 seconds.
    start = asyncio.get_event_loop().time()
    await bus.emit(
        MomentumSwing(game_id="g-1", direction="up", magnitude=5.0, new_momentum_score=65.0),
        fire_and_forget=True,
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 1.0  # returned quickly


# ═════════════════════════════════════════════════════════════════════
# Edge cases
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_handlers_no_error(bus):
    """emit without handlers doesn't raise."""
    event = ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3)
    await bus.emit(event)  # Should not raise


@pytest.mark.asyncio
async def test_invalid_event_type(bus):
    """emit with a non-BlmEvent raises ValueError."""
    with pytest.raises(ValueError):
        await bus.emit("not an event")  # type: ignore


@pytest.mark.asyncio
async def test_clear_removes_all_handlers(bus):
    """clear() removes all subscriptions."""
    received = []

    async def handler(event):
        received.append(event)

    bus.register(ThreePointerMade, handler)
    bus.clear()

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    assert len(received) == 0


@pytest.mark.asyncio
async def test_handler_exception_swallowed(bus):
    """Handler exceptions are logged but don't crash the bus."""
    received = []

    async def good_handler(event):
        received.append("good")

    async def bad_handler(event):
        raise RuntimeError("boom!")

    bus.register(ThreePointerMade, good_handler)
    bus.register(ThreePointerMade, bad_handler)

    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    assert received == ["good"]


@pytest.mark.asyncio
async def test_handler_metrics(bus):
    """Bus tracks event and handler counts."""
    async def handler(event):
        pass

    bus.register(ThreePointerMade, handler)

    assert bus.total_events_emitted == 0
    await bus.emit(ThreePointerMade(game_id="g-1", team="home", score_before=0, score_after=3))
    assert bus.total_events_emitted == 1
    assert bus.total_handlers_invoked == 1
