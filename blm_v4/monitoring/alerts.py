"""Alert state machine — dedupe, confirmation, and the full incident lifecycle.

The directive asks for three timestamps per issue (when it started, when it was
detected, when it recovered) and for duplicate alerts to be suppressed. So a
condition is *seen* on one cycle, *confirmed* after ``confirm_cycles``
consecutive sightings (that is when the alert fires, exactly once), and *closed*
on the first clean cycle — with the whole lifecycle written to a JSONL journal.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Incident:
    key: str
    label: str
    severity: str
    started_at: str
    detected_at: str
    recovered_at: str | None = None
    state: str = "OPEN"
    detail: str = ""
    occurrences: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlertTracker:
    path: str = ""
    confirm_cycles: int = 2
    pending: dict[str, dict] = field(default_factory=dict)   # key -> {first_seen, count, label, detail}
    open_incidents: dict[str, Incident] = field(default_factory=dict)
    journal_path: str = ""
    _emitted: list[dict] = field(default_factory=list)

    # ── persistence ────────────────────────────────────────────────────
    def load(self) -> "AlertTracker":
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self.pending = raw.get("pending", {})
                self.open_incidents = {
                    k: Incident(**v) for k, v in raw.get("open_incidents", {}).items()}
            except (OSError, ValueError, TypeError):
                self.pending, self.open_incidents = {}, {}
        return self

    def save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        payload = {
            "pending": self.pending,
            "open_incidents": {k: v.as_dict() for k, v in self.open_incidents.items()},
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)          # atomic: a reader never sees half a file

    # ── state machine ──────────────────────────────────────────────────
    def observe(self, report: dict, now: float) -> list[dict]:
        """Feed one health report. Returns the alerts to emit THIS cycle."""
        th = self.confirm_cycles
        seen: set[str] = set()
        emitted: list[dict] = []

        for comp in report.get("components", []):
            color = comp.get("color")
            if color not in ("RED", "YELLOW"):
                continue
            key = f"{comp['key']}:{color.lower()}"
            seen.add(key)
            severity = "critical" if color == "RED" else "warning"

            if key in self.open_incidents:                     # already open: no duplicate
                inc = self.open_incidents[key]
                inc.occurrences += 1
                inc.detail = comp.get("detail") or inc.detail
                continue

            slot = self.pending.setdefault(key, {
                "first_seen": now, "count": 0,
                "label": comp.get("label"), "detail": comp.get("detail"), "severity": severity})
            slot["count"] += 1
            slot["detail"] = comp.get("detail") or slot.get("detail")

            if slot["count"] >= th:                            # confirmed -> fire once
                inc = Incident(
                    key=key, label=comp.get("label") or key, severity=severity,
                    started_at=_iso(slot["first_seen"]), detected_at=_iso(now),
                    detail=slot.get("detail") or "", occurrences=slot["count"])
                self.open_incidents[key] = inc
                self.pending.pop(key, None)
                emitted.append({"event": "ALERT", **inc.as_dict()})

        # recovery: a condition not seen this cycle (and past confirmation) closes
        for key in list(self.open_incidents):
            if key in seen:
                continue
            inc = self.open_incidents.pop(key)
            inc.recovered_at = _iso(now)
            inc.state = "RECOVERED"
            emitted.append({"event": "RECOVERY", **inc.as_dict()})

        # a condition that flaps back before confirmation resets its counter
        for key in list(self.pending):
            if key not in seen:
                self.pending.pop(key, None)

        if emitted and self.journal_path:
            self._append_journal(emitted)
        self._emitted = emitted
        return emitted

    def _append_journal(self, events: list[dict]) -> None:
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, default=str) + "\n")

    def summary(self) -> dict:
        return {
            "open": [v.as_dict() for v in self.open_incidents.values()],
            "pending": len(self.pending),
        }
