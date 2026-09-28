"""Bounded, in-process timing counters for collector performance diagnosis.

This module records measurements only. It does not configure SQLite, alter
collection cadence, or persist anything outside the collector's normal state
file. Samples are bounded; lifetime totals are simple counters.
"""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from contextlib import contextmanager
import threading
import time
from typing import Iterator


_current_tick: ContextVar[int | None] = ContextVar("blm_perf_tick", default=None)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values.sort()
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 3)


class RollingPerformance:
    """Thread-safe operation counters with bounded samples and tick counts."""

    def __init__(self, sample_limit: int = 256, tick_limit: int = 128):
        self.sample_limit = sample_limit
        self.tick_limit = tick_limit
        self._lock = threading.RLock()
        self._tick = 0
        self._ticks: deque[int] = deque(maxlen=tick_limit)
        self._metrics: dict[str, dict] = {}

    @contextmanager
    def tick_scope(self) -> Iterator[int]:
        with self._lock:
            self._tick += 1
            tick_id = self._tick
            self._ticks.append(tick_id)
        token = _current_tick.set(tick_id)
        try:
            yield tick_id
        finally:
            _current_tick.reset(token)

    @contextmanager
    def measure(self, name: str) -> Iterator["Measurement"]:
        sample = Measurement(self, name, _current_tick.get())
        started = time.perf_counter()
        try:
            yield sample
        finally:
            self.record(name, (time.perf_counter() - started) * 1000.0,
                        tick_id=sample.tick_id, counters=sample.counters)

    def record(self, name: str, elapsed_ms: float, *, tick_id: int | None = None,
               counters: dict[str, int] | None = None) -> None:
        if tick_id is None:
            tick_id = _current_tick.get()
        with self._lock:
            metric = self._metrics.setdefault(name, {
                "samples": deque(maxlen=self.sample_limit), "calls": 0,
                "total_ms": 0.0, "max_ms": 0.0, "counters": {},
                "tick_calls": {}, "tick_counters": {},
            })
            metric["samples"].append((round(elapsed_ms, 3), tick_id))
            metric["calls"] += 1
            metric["total_ms"] += elapsed_ms
            metric["max_ms"] = max(metric["max_ms"], elapsed_ms)
            if tick_id is not None:
                metric["tick_calls"][tick_id] = metric["tick_calls"].get(tick_id, 0) + 1
                tc = metric["tick_counters"].setdefault(tick_id, {})
            else:
                tc = None
            for key, value in (counters or {}).items():
                metric["counters"][key] = metric["counters"].get(key, 0) + value
                if tc is not None:
                    tc[key] = tc.get(key, 0) + value
            keep = set(self._ticks)
            for old in list(metric["tick_calls"]):
                if old not in keep:
                    metric["tick_calls"].pop(old, None)
                    metric["tick_counters"].pop(old, None)

    def snapshot(self) -> dict:
        with self._lock:
            latest_tick = self._tick
            operations = {}
            for name, metric in self._metrics.items():
                samples = [x[0] for x in metric["samples"]]
                operations[name] = {
                    "calls_total": metric["calls"],
                    "total_ms": round(metric["total_ms"], 3),
                    "p50_ms": _percentile(samples.copy(), .50),
                    "p95_ms": _percentile(samples.copy(), .95),
                    "max_ms": round(metric["max_ms"], 3),
                    "calls_this_tick": metric["tick_calls"].get(latest_tick, 0),
                    "counters_total": dict(metric["counters"]),
                    "counters_this_tick": dict(metric["tick_counters"].get(latest_tick, {})),
                }
            top_time = sorted(operations.items(), key=lambda x: x[1]["total_ms"], reverse=True)[:10]
            top_calls = sorted(
                ((n, v) for n, v in operations.items() if n.startswith("sql.")),
                key=lambda x: x[1]["calls_total"], reverse=True,
            )[:10]
            sql_calls_tick = sum(
                v["counters_this_tick"].get("sql_calls", 0)
                for v in operations.values()
            )
            def latest(name: str) -> float | None:
                vals = self._metrics.get(name, {}).get("samples")
                return vals[-1][0] if vals else None
            totals = {}
            per_tick = {}
            for key, names in {
                "observations_loaded": ("clean.valid_observations",),
                "projections_rebuilt": ("clean.replace_projections",),
                "residuals_processed": ("deviation.refresh_game",),
            }.items():
                totals[key] = sum(
                    self._metrics.get(n, {}).get("counters", {}).get(key, 0)
                    for n in names
                )
                metric = self._metrics.get(names[0], {})
                per_tick[key] = metric.get("tick_counters", {}).get(
                    latest_tick, {}).get(key, 0)
            accepted = self._metrics.get("collector.record_clean", {}).get(
                "counters", {}).get("accepted_snapshots", 0)
            if not accepted:
                accepted = 0
            insert_metric = self._metrics.get("sql.main.insert_snapshot", {})
            snapshots_this_tick = insert_metric.get("tick_counters", {}).get(
                latest_tick, {}).get("rows_inserted", 0)
            snapshots_total = insert_metric.get("counters", {}).get("rows_inserted", 0)
            projections_inserted_metric = self._metrics.get(
                "sql.clean.projection_insert", {})
            projections_deleted_metric = self._metrics.get(
                "sql.clean.projections_delete", {})
            projections_inserted_tick = projections_inserted_metric.get(
                "tick_counters", {}).get(latest_tick, {}).get(
                    "projections_inserted", 0)
            projections_deleted_tick = projections_deleted_metric.get(
                "tick_counters", {}).get(latest_tick, {}).get(
                    "projections_deleted", 0)
            return {
                "tick_id": latest_tick,
                "tick_duration_ms": latest("collector.tick_duration"),
                "page_content_ms": latest("collector.page_content"),
                "snapshot_persist_ms": latest("collector.store_list_snapshot"),
                "clean_record_ms": latest("collector.record_clean"),
                "projection_refresh_ms": latest("pace.refresh_game"),
                "deviation_refresh_ms": latest("deviation.refresh_game"),
                "state_metrics_ms": latest("collector.state_metrics"),
                "observations_loaded": per_tick["observations_loaded"],
                "projections_rebuilt": per_tick["projections_rebuilt"],
                "residuals_processed": per_tick["residuals_processed"],
                "observations_loaded_total": totals["observations_loaded"],
                "projections_rebuilt_total": totals["projections_rebuilt"],
                "projections_inserted_this_tick": projections_inserted_tick,
                "projections_inserted_total": projections_inserted_metric.get(
                    "counters", {}).get("projections_inserted", 0),
                "projections_deleted_this_tick": projections_deleted_tick,
                "projections_deleted_total": projections_deleted_metric.get(
                    "counters", {}).get("projections_deleted", 0),
                "residuals_processed_total": totals["residuals_processed"],
                "accepted_clean_snapshots": accepted,
                "snapshots_per_tick": snapshots_this_tick,
                "snapshots_inserted_total": snapshots_total,
                "per_accepted_snapshot": {
                    "observations_loaded": round(
                        totals["observations_loaded"] / accepted, 3) if accepted else None,
                    "projections_rebuilt": round(
                        totals["projections_rebuilt"] / accepted, 3) if accepted else None,
                    "residuals_processed": round(
                        totals["residuals_processed"] / accepted, 3) if accepted else None,
                },
                "sql_call_count": sql_calls_tick,
                "deviation_sql_time_total_ms": round(sum(
                    v["total_ms"] for n, v in operations.items()
                    if n.startswith("sql.deviation.")
                ), 3),
                "deviation_python_time_total_ms": round(
                    operations.get("deviation.python_time", {}).get("total_ms", 0.0), 3),
                "pace_sql_time_total_ms": round(sum(
                    v["total_ms"] for n, v in operations.items()
                    if n.startswith("sql.clean.") or n == "clean.replace_projections"
                ), 3),
                "pace_python_time_total_ms": round(
                    operations.get("pace.python_computation", {}).get("total_ms", 0.0), 3),
                "top_cumulative_ms": [{"operation": n, **v} for n, v in top_time],
                "top_sql_calls": [{"operation": n, **v} for n, v in top_calls],
                "operations": operations,
            }


class Measurement:
    def __init__(self, owner: RollingPerformance, name: str, tick_id: int | None):
        self.owner = owner
        self.name = name
        self.tick_id = tick_id
        self.counters: dict[str, int] = {}

    def add(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(value)


PERFORMANCE = RollingPerformance()
