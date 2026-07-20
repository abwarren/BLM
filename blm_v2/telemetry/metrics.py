"""
BLM V2 — In-Memory Performance Metrics Collector

Tracks key BLM performance counters in a simple thread-safe dict store
with min / max / avg / count for each metric name.

Tracked metrics:
  - snapshot_write_time    — time to persist a snapshot to storage
  - api_response_time      — time to generate an API response
  - websocket_delivery_time — time to serialise + push a snapshot to WS
  - engine_calc_time       — time to run all BLM engine stages
  - replay_frame_time      — time to load / assemble a replay frame

Usage:
    metrics = get_metrics_collector()
    with metrics.timer("snapshot_write_time"):
        write_snapshot(...)

    report = metrics.snapshot()
    print(report["snapshot_write_time"]["avg"])
"""

import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional


# ── Data structures ───────────────────────────────────────────────────

@dataclass
class MetricEntry:
    """Accumulated statistics for a single metric name."""
    count: int = 0
    total: float = 0.0
    min_val: float = float("inf")
    max_val: float = float("-inf")

    def record(self, value: float) -> None:
        self.count += 1
        self.total += value
        if value < self.min_val:
            self.min_val = value
        if value > self.max_val:
            self.max_val = value

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count > 0 else 0.0

    def reset(self) -> None:
        self.count = 0
        self.total = 0.0
        self.min_val = float("inf")
        self.max_val = float("-inf")


@dataclass
class MetricsSnapshot:
    """A point-in-time read of all metrics."""
    metrics: Dict[str, "MetricSnapshotItem"] = field(default_factory=dict)
    total_requests: int = 0
    uptime_seconds: float = 0.0


@dataclass
class MetricSnapshotItem:
    count: int
    min: float
    max: float
    avg: float


# ── Collector ─────────────────────────────────────────────────────────

class MetricsCollector:
    """Thread-safe in-memory metrics store.

    Designed for operational observability, not long-term analytics.
    Snapshots are cheap — call them freely for /metrics endpoints.
    """

    VALID_METRICS = frozenset({
        "snapshot_write_time",
        "api_response_time",
        "websocket_delivery_time",
        "engine_calc_time",
        "replay_frame_time",
    })

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[str, MetricEntry] = {
            name: MetricEntry() for name in self.VALID_METRICS
        }
        self._total_requests: int = 0
        self._start_time: float = time.monotonic()

    # ── Recording ─────────────────────────────────────────────────

    def record(self, name: str, value: float) -> None:
        """Record a single timing *value* (in seconds) for metric *name*."""
        if name not in self.VALID_METRICS:
            raise ValueError(
                f"Unknown metric {name!r}. Valid: {sorted(self.VALID_METRICS)}"
            )
        with self._lock:
            self._store[name].record(value)

    def record_request(self) -> None:
        """Increment the total request counter."""
        with self._lock:
            self._total_requests += 1

    # ── Timer context manager ─────────────────────────────────────

    def timer(self, name: str):
        """Context manager that times a block and records the duration.

        Usage::

            with metrics.timer("engine_calc_time"):
                engine.run()
        """
        return _TimerContext(self, name)

    # ── Reading ───────────────────────────────────────────────────

    def snapshot(self) -> MetricsSnapshot:
        """Return a point-in-time copy of all accumulated metrics."""
        with self._lock:
            items = {}
            for name, entry in self._store.items():
                if entry.count > 0:
                    items[name] = MetricSnapshotItem(
                        count=entry.count,
                        min=entry.min_val,
                        max=entry.max_val,
                        avg=round(entry.avg, 4),
                    )
            return MetricsSnapshot(
                metrics=items,
                total_requests=self._total_requests,
                uptime_seconds=round(time.monotonic() - self._start_time, 2),
            )

    def get(self, name: str) -> Optional[MetricSnapshotItem]:
        """Return a snapshot of a single metric, or ``None`` if no data."""
        with self._lock:
            entry = self._store.get(name)
            if entry is None or entry.count == 0:
                return None
            return MetricSnapshotItem(
                count=entry.count,
                min=entry.min_val,
                max=entry.max_val,
                avg=round(entry.avg, 4),
            )

    def reset(self) -> None:
        """Reset all accumulated metrics."""
        with self._lock:
            for entry in self._store.values():
                entry.reset()
            self._total_requests = 0
            self._start_time = time.monotonic()


class _TimerContext:
    """Internal context manager returned by ``MetricsCollector.timer()``."""

    def __init__(self, collector: MetricsCollector, name: str) -> None:
        self._collector = collector
        self._name = name
        self._start: Optional[float] = None

    def __enter__(self) -> "_TimerContext":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        start = self._start if self._start is not None else time.perf_counter()
        elapsed = time.perf_counter() - start
        self._collector.record(self._name, elapsed)


# ── Module-level singleton ────────────────────────────────────────────

_collector: Optional[MetricsCollector] = None
_lock_for_singleton: threading.Lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Return the application-wide ``MetricsCollector`` singleton."""
    global _collector
    if _collector is None:
        with _lock_for_singleton:
            if _collector is None:
                _collector = MetricsCollector()
    return _collector


def reset_metrics_collector() -> None:
    """Replace the singleton with a fresh collector (useful in tests)."""
    global _collector
    with _lock_for_singleton:
        _collector = MetricsCollector()
