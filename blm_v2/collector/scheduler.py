"""Async snapshot scheduler for BLM V2 — 20-second enrichment pipeline.

Drives the core BLM loop:
  poll V1 collector → enrich via BLM engine → write to time-series → fire events

Uses a pure ``asyncio`` loop with tick timing, missed-tick compensation, and
structured logging.  All external dependencies are injected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from blm_v2.collector.base import Collector
from blm_v2.collector.snapshot import RawSnapshot
from blm_v2.timeseries.base import TimeSeriesDB

logger = logging.getLogger(__name__)

# ── Scheduling defaults ───────────────────────────────────────────

DEFAULT_TICK_S = 20.0
"""Target interval (seconds) between enrichment ticks."""

MAX_CATCHUP_TICKS = 3
"""Maximum consecutive missed ticks to catch up on before skipping."""


# ── Protocol for the BLM enrichment engine ───────────────────────

class BlmEngine(Protocol):
    """Structural protocol for the BLM analysis engine.

    The engine accepts a ``RawSnapshot`` and returns an enriched dict
    containing all original fields plus BLM-derived metrics.
    """

    async def enrich(self, snapshot: RawSnapshot) -> dict[str, Any]:
        """Run the BLM analysis pipeline on a raw snapshot.

        Returns a dict suitable for ``TimeSeriesDB.write_snapshot()``.
        """  # type: ignore[empty-body]
        ...  # pragma: no cover


# ── Protocol for the event bus emitter ───────────────────────────

class EventEmitter(Protocol):
    """Minimal event emitter protocol for the scheduler's use.

    Scheduler only needs ``emit`` — the full ``EventBus`` interface
    (subscribe, unsubscribe, etc.) is for other consumers.
    """

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Fire an event with a type label and payload dict."""


# ── Tick statistics ──────────────────────────────────────────────

@dataclass
class SchedulerStats:
    """Exposed runtime statistics for monitoring and dashboards."""

    total_ticks: int = 0
    successful_ticks: int = 0
    failed_ticks: int = 0
    skipped_ticks: int = 0
    last_tick_duration_s: float = 0.0
    avg_tick_duration_s: float = 0.0
    last_tick_ts: Optional[float] = None
    started_at: Optional[float] = None
    uptime_s: float = 0.0


# ── Scheduler ─────────────────────────────────────────────────────


class SnapshotScheduler:
    """Async scheduler that drives the BLM enrichment pipeline on a 20 s tick.

    Dependency injection through constructor — the scheduler never instantiates
    any subsystem itself.  This makes it testable (swap in mock collector /
    mock TS / mock engine) and deployable to different environments.

    Usage::

        collector = V1CollectorAdapter(...)
        ts_db = SQLiteTimeSeries()
        engine = BlmEngine(...)
        bus = get_event_bus()

        scheduler = SnapshotScheduler(
            collector=collector,
            ts_db=ts_db,
            engine=engine,
            event_bus=bus,
            tick_s=20.0,
        )

        # Start in a background task
        asyncio.create_task(scheduler.run())

        # Later
        await scheduler.stop()
    """

    def __init__(
        self,
        collector: Collector,
        ts_db: TimeSeriesDB,
        engine: BlmEngine,
        event_bus: EventEmitter,
        *,
        tick_s: float = DEFAULT_TICK_S,
    ) -> None:
        self._collector = collector
        self._ts_db = ts_db
        self._engine = engine
        self._event_bus = event_bus
        self._tick_s = tick_s

        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._stats = SchedulerStats()

    # ── Properties ───────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> SchedulerStats:
        """Read-only snapshot of runtime statistics."""
        s = self._stats
        if s.started_at is not None:
            s.uptime_s = time.monotonic() - s.started_at
        return s

    # ── Lifecycle ────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the scheduler loop until ``stop()`` is called.

        This is an infinite coroutine.  Call it with
        ``asyncio.create_task(scheduler.run())``.
        """
        if self._running:
            logger.warning("Scheduler already running")
            return

        self._running = True
        self._stats.started_at = time.monotonic()
        logger.info(
            "SnapshotScheduler started (tick=%.1fs, max_catchup=%d)",
            self._tick_s, MAX_CATCHUP_TICKS,
        )

        # Ensure the collector is running
        if not self._collector.is_running:
            logger.info("Starting collector from scheduler")
            await asyncio.get_event_loop().run_in_executor(None, self._collector.start)

        tick_count = 0
        next_tick = time.monotonic()

        try:
            while self._running:
                tick_start = time.monotonic()

                # ── Missed-tick detection ─────────────────────────
                now = time.monotonic()
                behind_by = now - next_tick

                if behind_by > self._tick_s * 1.5:
                    missed_ticks = int(behind_by / self._tick_s)
                    if missed_ticks > MAX_CATCHUP_TICKS:
                        logger.warning(
                            "Skipping %d missed ticks (behind by %.1fs)",
                            missed_ticks - MAX_CATCHUP_TICKS, behind_by,
                        )
                        self._stats.skipped_ticks += missed_ticks - MAX_CATCHUP_TICKS
                        # Jump forward — skip the excessive backlog
                        next_tick = now
                    else:
                        # Catch up: run the tick now, don't skip
                        logger.info(
                            "Catching up %d missed ticks (behind by %.1fs)",
                            missed_ticks, behind_by,
                        )

                # Advance the tick schedule
                next_tick += self._tick_s

                # ── Execute pipeline ─────────────────────────────
                tick_count += 1
                await self._execute_tick(tick_count)

                # ── Tick timing ───────────────────────────────────
                elapsed = time.monotonic() - tick_start
                self._stats.last_tick_duration_s = elapsed
                self._stats.avg_tick_duration_s = (
                    self._stats.avg_tick_duration_s * (self._stats.total_ticks - 1) + elapsed
                ) / max(self._stats.total_ticks, 1)
                self._stats.last_tick_ts = tick_start

                if elapsed > self._tick_s * 0.8:
                    logger.warning(
                        "Tick %d took %.2fs (%.0f%% of interval)",
                        tick_count, elapsed, (elapsed / self._tick_s) * 100,
                    )

                # ── Sleep until next tick ────────────────────────
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                else:
                    # We've already overshot — run again immediately
                    # (but yield the event loop so other tasks can breathe)
                    await asyncio.sleep(0)

        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled")
        except Exception:
            logger.exception("Scheduler loop crashed")
        finally:
            self._running = False
            logger.info(
                "Scheduler stopped: %d ticks, %d failed, %d skipped",
                self._stats.total_ticks,
                self._stats.failed_ticks,
                self._stats.skipped_ticks,
            )

    async def stop(self) -> None:
        """Signal the scheduler loop to stop and await task completion."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Scheduler stopped")

    # ── Pipeline ─────────────────────────────────────────────────

    async def _execute_tick(self, tick_number: int) -> None:
        """Execute a single BLM enrichment tick.

        Stages:
          1. Poll the collector for the latest raw snapshot
          2. Run it through the BLM engine for enrichment
          3. Write the enriched snapshot to the time-series database
          4. Fire a ``snapshot.enriched`` event on the bus
        """
        self._stats.total_ticks += 1

        # ── 1. Poll collector ────────────────────────────────────
        raw: Optional[RawSnapshot] = None
        try:
            raw = self._collector.latest_snapshot
        except Exception:
            logger.exception("Tick %d: collector poll failed", tick_number)
            self._stats.failed_ticks += 1
            return

        if raw is None:
            logger.debug("Tick %d: no snapshot from collector (waiting for game)", tick_number)
            return

        # ── 2. Enrich via BLM engine ─────────────────────────────
        try:
            enriched: dict[str, Any] = await self._engine.enrich(raw)
        except Exception:
            logger.exception("Tick %d: BLM engine enrichment failed", tick_number)
            self._stats.failed_ticks += 1

            # Write raw snapshot even on enrichment failure
            # (better to have raw data than nothing)
            logger.warning("Tick %d: writing raw snapshot as fallback", tick_number)
            enriched = raw.to_ts_dict()
            enriched["enrichment_failed"] = True

        # Ensure timestamp and game_id are present
        enriched.setdefault("game_id", raw.game_id)
        enriched.setdefault("timestamp", raw.timestamp)

        # ── 3. Write to time-series DB ───────────────────────────
        try:
            await self._ts_db.write_snapshot(enriched)
        except Exception:
            logger.exception("Tick %d: time-series write failed", tick_number)
            self._stats.failed_ticks += 1
            return

        # ── 4. Fire event ────────────────────────────────────────
        try:
            await self._event_bus.emit("snapshot.enriched", {
                "game_id": raw.game_id,
                "timestamp": raw.timestamp,
                "tick": tick_number,
                "duration_s": self._stats.last_tick_duration_s,
                "enrichment_failed": enriched.get("enrichment_failed", False),
            })
        except Exception:
            logger.warning("Tick %d: event bus emit failed (non-fatal)", tick_number)

        self._stats.successful_ticks += 1

        if tick_number % 30 == 0:  # ~every 10 minutes at 20 s ticks
            logger.info(
                "Tick %d: game=%s ts=%s score=%d-%d duration=%.2fs",
                tick_number, raw.game_id, raw.timestamp,
                raw.home_score, raw.away_score,
                self._stats.last_tick_duration_s,
            )
