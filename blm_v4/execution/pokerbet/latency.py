"""Trigger-to-submit latency instrumentation — MEASUREMENT ONLY.

Records one monotonic timestamp per stage of the execution path so the
trigger → submit time can be broken down and improved.  Pure bookkeeping: it
never gates, delays, retries or changes any decision, and passing no trace
changes nothing.

The stage order is the path the operator asked to instrument::

    trigger_detected → trigger_line_frozen → market_observed →
    selection_added → stake_entered → stake_verified → submit_enabled →
    submit_clicked

``trigger_detected`` / ``trigger_line_frozen`` are marked by the CALLER (the
bridge or the probe), because the adapter only becomes involved once the signal
already exists.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

STAGES = ("trigger_detected", "trigger_line_frozen", "market_observed",
          "selection_added", "stake_entered", "stake_verified",
          "submit_enabled", "submit_clicked")


def now_iso() -> str:
    """Wall-clock stamp, matched to the collector's ``captured_at`` format."""
    return datetime.now(timezone.utc).isoformat()


class LatencyTrace:
    """Per-execution stage timeline.  First mark of a stage wins."""

    def __init__(self, alert_id: Optional[str] = None) -> None:
        self.alert_id = alert_id
        self.t0 = time.monotonic()
        self.stages: dict = {}
        self.wall: dict = {}

    def mark(self, stage: str) -> float:
        if stage not in self.stages:
            self.stages[stage] = time.monotonic()
            self.wall[stage] = now_iso()
        return self.stages[stage]

    def marked(self, stage: str) -> bool:
        return stage in self.stages

    def offset_s(self, stage: str) -> Optional[float]:
        t = self.stages.get(stage)
        return None if t is None else round(t - self.t0, 3)

    def report(self) -> dict:
        """Per-stage offset from t0, plus the delta between consecutive stages."""
        out: dict = {"alert_id": self.alert_id, "stages": {}, "wall": dict(self.wall)}
        prev = None
        for s in STAGES:
            if s not in self.stages:
                continue
            off = round(self.stages[s] - self.t0, 3)
            out["stages"][s] = {
                "offset_s": off,
                "delta_s": None if prev is None else round(off - prev, 3)}
            prev = off
        if prev is not None:
            out["total_s"] = prev
        return out
