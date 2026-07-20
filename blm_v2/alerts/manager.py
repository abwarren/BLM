"""BLM V2 — Alert Rules Engine

Configurable alert triggers that evaluate game snapshots and generate
actionable alerts.  Supports deduplication, lifecycle management, and
persistence via the storage interface.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional, Protocol, runtime_checkable

from blm_v2.alerts import Alert, AlertSeverity, AlertType


# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_DEDUP_WINDOW_S: float = 60.0
"""Alerts of the same type for the same game within this window are suppressed."""

DEFAULT_TRAP_THRESHOLD: float = 0.80
"""Trap meter values above this threshold trigger a trap alert."""

DEFAULT_CONFIDENCE_THRESHOLD: float = 0.55
"""Confidence values below this threshold trigger a confidence drop alert."""

DEFAULT_VEGAS_MOVEMENT_THRESHOLD: float = 2.0
"""Line movements >= this value trigger a Vegas movement alert."""

DEFAULT_BLM_MOVEMENT_THRESHOLD: float = 10.0
"""BLM score changes >= this value trigger a BLM movement alert."""

DEFAULT_STEAM_THRESHOLD: float = 1.5
"""Steam movement values above this threshold trigger a sharp money alert."""

DEFAULT_LATE_TRAP_MINUTES: float = 2.0
"""Traps detected within this many minutes of the end of a quarter are 'late'."""


# ── Storage Protocol ───────────────────────────────────────────────────


@runtime_checkable
class AlertStorageProtocol(Protocol):
    """Minimal storage interface required by the alert manager.

    Implementations can wrap the existing ``StorageDB`` or provide
    an independent backend.
    """

    async def save_alert(self, alert: dict) -> None:
        """Persist an alert record."""
        ...

    async def get_alerts(
        self,
        game_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return alerts, optionally filtered by game_id, newest first."""
        ...

    async def acknowledge_alert(self, alert_id: str) -> None:
        """Mark an alert as acknowledged."""
        ...

    async def dismiss_alert(self, alert_id: str) -> None:
        """Mark an alert as dismissed."""
        ...


# ── In-Memory Alert Store ──────────────────────────────────────────────


class InMemoryAlertStore:
    """Simple in-memory alert store for testing or standalone use.

    Wraps a dict for O(1) lookups.  Not persisted across restarts.
    """

    def __init__(self) -> None:
        self._alerts: dict[str, Alert] = {}

    async def save_alert(self, alert: dict) -> None:
        a = Alert(**alert) if isinstance(alert, dict) else alert
        self._alerts[a.id] = a

    async def get_alerts(
        self,
        game_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        results = []
        for a in self._alerts.values():
            if game_id and a.game_id != game_id:
                continue
            if a.dismissed:
                continue
            results.append(a.to_dict())
        # Newest first
        results.sort(key=lambda x: x["timestamp"], reverse=True)
        return results[:limit]

    async def acknowledge_alert(self, alert_id: str) -> None:
        if alert_id in self._alerts:
            self._alerts[alert_id].acknowledged = True

    async def dismiss_alert(self, alert_id: str) -> None:
        if alert_id in self._alerts:
            self._alerts[alert_id].dismissed = True

    def get_alert(self, alert_id: str) -> Optional[Alert]:
        return self._alerts.get(alert_id)


# ── Alert Manager ──────────────────────────────────────────────────────


class AlertManager:
    """Configurable alert rules engine.

    Evaluates game snapshots against a set of trigger rules and
    generates alerts.  Supports alert deduplication, lifecycle
    management, and pluggable storage backends.

    Usage::

        manager = AlertManager(storage=my_storage)
        alerts = await manager.evaluate_snapshot(snapshot)
        for alert in alerts:
            print(f\"{alert.severity.value.upper()}: {alert.title}\")
    """

    def __init__(
        self,
        storage: Optional[AlertStorageProtocol] = None,
        trap_threshold: float = DEFAULT_TRAP_THRESHOLD,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        vegas_movement_threshold: float = DEFAULT_VEGAS_MOVEMENT_THRESHOLD,
        blm_movement_threshold: float = DEFAULT_BLM_MOVEMENT_THRESHOLD,
        steam_threshold: float = DEFAULT_STEAM_THRESHOLD,
        late_trap_minutes: float = DEFAULT_LATE_TRAP_MINUTES,
        dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S,
    ) -> None:
        self._storage = storage or InMemoryAlertStore()
        self._dedup_window_s = dedup_window_s

        # Configurable thresholds
        self.trap_threshold = trap_threshold
        self.confidence_threshold = confidence_threshold
        self.vegas_movement_threshold = vegas_movement_threshold
        self.blm_movement_threshold = blm_movement_threshold
        self.steam_threshold = steam_threshold
        self.late_trap_minutes = late_trap_minutes

        # Track previous snapshot state for differential alerts
        self._prev_snapshots: dict[str, dict] = {}
        # Track recent (type, game_id) timestamps for dedup
        self._recent_alerts: dict[tuple[str, str], datetime] = {}

    # ── Public API ──────────────────────────────────────────────────

    async def evaluate_snapshot(
        self,
        snapshot: dict,
        game_id: Optional[str] = None,
    ) -> list[Alert]:
        """Evaluate a single snapshot against all active rules.

        Args:
            snapshot: The snapshot dict (as produced by the BLM engine).
            game_id: Optional override; defaults to snapshot["game_id"].

        Returns:
            List of newly generated Alert objects.
        """
        gid = game_id or snapshot.get("game_id", "")
        now = datetime.now()
        generated: list[Alert] = []

        # Run all rule checks
        checks = [
            self._check_trap_meter,
            self._check_momentum_reversal,
            self._check_confidence_drop,
            self._check_large_vegas_movement,
            self._check_large_blm_movement,
            self._check_sharp_money,
            self._check_late_trap,
            self._check_injury_detected,
        ]

        for check in checks:
            alert = check(snapshot, gid, now)
            if alert is not None and not self._is_duplicate(alert, now):
                self._recent_alerts[(alert.type.value, gid)] = now
                await self._storage.save_alert(alert.to_dict())
                generated.append(alert)

        # Store snapshot for differential checks on next evaluation
        self._prev_snapshots[gid] = snapshot

        return generated

    async def acknowledge(self, alert_id: str) -> None:
        """Mark an alert as acknowledged."""
        await self._storage.acknowledge_alert(alert_id)

    async def dismiss(self, alert_id: str) -> None:
        """Dismiss an alert (remove from active feed)."""
        await self._storage.dismiss_alert(alert_id)

    async def get_active_alerts(
        self,
        game_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return active (non-dismissed) alerts.

        Args:
            game_id: Optional filter by game ID.
            limit: Maximum number of alerts to return.

        Returns:
            List of alert dicts, newest first.
        """
        return await self._storage.get_alerts(game_id=game_id, limit=limit)

    # ── Rule Checks ─────────────────────────────────────────────────

    def _check_trap_meter(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when trap meter exceeds threshold."""
        trap = snapshot.get("trap_detection", {})
        trap_val = trap.get("trap_meter", 0.0) if isinstance(trap, dict) else 0.0

        if trap_val >= self.trap_threshold:
            pct = round(trap_val * 100, 1)
            return Alert(
                id=str(uuid.uuid4()),
                game_id=game_id,
                type=AlertType.TRAP_METER_HIGH,
                severity=AlertSeverity.CRITICAL,
                title=f"Trap Meter Critical: {pct}%",
                message=(
                    f"The Trap Meter has reached {pct}%, exceeding the "
                    f"alert threshold of {self.trap_threshold * 100:.0f}%. "
                    "Market manipulation likely — exercise extreme caution."
                ),
                timestamp=now,
                snapshot_data={"trap_meter": trap_val},
            )
        return None

    def _check_momentum_reversal(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when momentum direction changes (up ↔ down)."""
        prev = self._prev_snapshots.get(game_id)
        if prev is None:
            return None

        momentum = snapshot.get("momentum", {})
        prev_momentum = prev.get("momentum", {})

        if not isinstance(momentum, dict) or not isinstance(prev_momentum, dict):
            return None

        direction = momentum.get("momentum_direction", "").lower()
        prev_direction = prev_momentum.get("momentum_direction", "").lower()

        reversal_pairs = {
            ("up", "down"),
            ("down", "up"),
            ("up", "sideways"),
            ("sideways", "down"),
            ("down", "sideways"),
            ("sideways", "up"),
        }

        if (prev_direction, direction) in reversal_pairs:
            return Alert(
                id=str(uuid.uuid4()),
                game_id=game_id,
                type=AlertType.MOMENTUM_REVERSAL,
                severity=AlertSeverity.WARNING,
                title=f"Momentum Reversal: {prev_direction} → {direction}",
                message=(
                    f"Momentum has reversed direction from '{prev_direction}' "
                    f"to '{direction}'. This may signal a shift in game dynamics "
                    "or market sentiment."
                ),
                timestamp=now,
                snapshot_data={
                    "prev_direction": prev_direction,
                    "new_direction": direction,
                },
            )
        return None

    def _check_confidence_drop(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when BLM confidence drops below threshold."""
        blm = snapshot.get("blm", {})
        if isinstance(blm, dict):
            confidence = blm.get("confidence", 1.0) or 1.0
        else:
            confidence = 1.0

        if confidence < self.confidence_threshold:
            pct = round(confidence * 100, 1)
            return Alert(
                id=str(uuid.uuid4()),
                game_id=game_id,
                type=AlertType.CONFIDENCE_DROP,
                severity=AlertSeverity.WARNING,
                title=f"Confidence Drop: {pct}%",
                message=(
                    f"BLM confidence has fallen to {pct}%, below the "
                    f"threshold of {self.confidence_threshold * 100:.0f}%. "
                    "Market conditions are becoming less predictable."
                ),
                timestamp=now,
                snapshot_data={"confidence": confidence},
            )
        return None

    def _check_large_vegas_movement(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when the Vegas line moves significantly."""
        prev = self._prev_snapshots.get(game_id)
        if prev is None:
            return None

        market = snapshot.get("betting_market", {})
        prev_market = prev.get("betting_market", {})

        if not isinstance(market, dict) or not isinstance(prev_market, dict):
            return None

        # Check spread movement
        current_spread = market.get("live_spread") or market.get("spread")
        prev_spread = prev_market.get("live_spread") or prev_market.get("spread")

        if current_spread is not None and prev_spread is not None:
            movement = abs(current_spread - prev_spread)
            if movement >= self.vegas_movement_threshold:
                return Alert(
                    id=str(uuid.uuid4()),
                    game_id=game_id,
                    type=AlertType.LARGE_VEGAS_MOVEMENT,
                    severity=AlertSeverity.CRITICAL,
                    title=f"Large Vegas Movement: {movement:.1f} pts",
                    message=(
                        f"The spread has moved {movement:.1f} points "
                        f"(from {prev_spread:.1f} to {current_spread:.1f}), "
                        f"exceeding the {self.vegas_movement_threshold:.0f} point threshold."
                    ),
                    timestamp=now,
                    snapshot_data={
                        "prev_spread": prev_spread,
                        "current_spread": current_spread,
                        "movement": movement,
                    },
                )

        # Check total line movement
        current_total = market.get("live_total") or market.get("total")
        prev_total = prev_market.get("live_total") or prev_market.get("total")

        if current_total is not None and prev_total is not None:
            movement = abs(current_total - prev_total)
            if movement >= self.vegas_movement_threshold:
                return Alert(
                    id=str(uuid.uuid4()),
                    game_id=game_id,
                    type=AlertType.LARGE_VEGAS_MOVEMENT,
                    severity=AlertSeverity.CRITICAL,
                    title=f"Large Total Movement: {movement:.1f} pts",
                    message=(
                        f"The total line has moved {movement:.1f} points "
                        f"(from {prev_total:.1f} to {current_total:.1f})."
                    ),
                    timestamp=now,
                    snapshot_data={
                        "prev_total": prev_total,
                        "current_total": current_total,
                        "movement": movement,
                    },
                )

        return None

    def _check_large_blm_movement(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when the BLM model's assessment changes significantly."""
        prev = self._prev_snapshots.get(game_id)
        if prev is None:
            return None

        blm = snapshot.get("blm", {})
        prev_blm = prev.get("blm", {})

        if not isinstance(blm, dict) or not isinstance(prev_blm, dict):
            return None

        # Check BLM score movement
        current_score = blm.get("blm_score") or blm.get("expected_margin")
        prev_score = prev_blm.get("blm_score") or prev_blm.get("expected_margin")

        if current_score is not None and prev_score is not None:
            movement = abs(current_score - prev_score)
            if movement >= self.blm_movement_threshold:
                return Alert(
                    id=str(uuid.uuid4()),
                    game_id=game_id,
                    type=AlertType.LARGE_BLM_MOVEMENT,
                    severity=AlertSeverity.CRITICAL,
                    title=f"Large BLM Shift: {movement:.1f} pts",
                    message=(
                        f"The BLM model assessment has shifted {movement:.1f} points "
                        f"(from {prev_score:.1f} to {current_score:.1f}), "
                        f"exceeding the {self.blm_movement_threshold:.0f} point threshold. "
                        "The model's outlook has changed significantly."
                    ),
                    timestamp=now,
                    snapshot_data={
                        "prev_score": prev_score,
                        "current_score": current_score,
                        "movement": movement,
                    },
                )

        # Check win probability movement
        current_wp = blm.get("win_probability")
        prev_wp = prev_blm.get("win_probability")

        if current_wp is not None and prev_wp is not None:
            wp_movement = abs(current_wp - prev_wp) * 100  # to percentage points
            if wp_movement >= self.blm_movement_threshold:
                return Alert(
                    id=str(uuid.uuid4()),
                    game_id=game_id,
                    type=AlertType.LARGE_BLM_MOVEMENT,
                    severity=AlertSeverity.WARNING,
                    title=f"Win Probability Shift: {wp_movement:.1f}%",
                    message=(
                        f"Win probability has shifted {wp_movement:.1f} percentage points "
                        f"(from {prev_wp * 100:.1f}% to {current_wp * 100:.1f}%)."
                    ),
                    timestamp=now,
                    snapshot_data={
                        "prev_wp": prev_wp,
                        "current_wp": current_wp,
                        "wp_movement": wp_movement,
                    },
                )

        return None

    def _check_sharp_money(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when steam movement indicates sharp money entry."""
        market = snapshot.get("betting_market", {})
        if not isinstance(market, dict):
            return None

        steam = market.get("steam_movement") or 0.0
        reverse = market.get("reverse_line_movement", False)

        if steam >= self.steam_threshold:
            return Alert(
                id=str(uuid.uuid4()),
                game_id=game_id,
                type=AlertType.SHARP_MONEY,
                severity=AlertSeverity.CRITICAL,
                title=f"Sharp Money Detected: {steam:.2f}",
                message=(
                    f"Steam movement of {steam:.2f} detected, exceeding the "
                    f"threshold of {self.steam_threshold:.2f}. "
                    f"{'Reverse line movement also detected — strong sharp signal.' if reverse else ''}"
                ).strip(),
                timestamp=now,
                snapshot_data={
                    "steam_movement": steam,
                    "reverse_line_movement": reverse,
                },
            )
        return None

    def _check_late_trap(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when a trap is detected late in a quarter."""
        trap = snapshot.get("trap_detection", {})
        if not isinstance(trap, dict):
            return None

        late_trap_val = trap.get("late_trap", 0.0) or 0.0
        clock = snapshot.get("metadata", {}).get("clock", "")
        quarter = snapshot.get("metadata", {}).get("quarter", 0)

        # Only check within the late window (last N minutes of a quarter)
        if late_trap_val > 0.5 and clock:
            try:
                minutes, seconds = clock.split(":")
                total_seconds = int(minutes) * 60 + int(seconds)
                late_window_s = int(self.late_trap_minutes * 60)
                if total_seconds <= late_window_s:
                    return Alert(
                        id=str(uuid.uuid4()),
                        game_id=game_id,
                        type=AlertType.LATE_TRAP,
                        severity=AlertSeverity.WARNING,
                        title=f"Late Trap: Q{quarter} @ {clock}",
                        message=(
                            f"A late trap indicator ({late_trap_val:.2f}) was detected "
                            f"at {clock} in Q{quarter}, within the "
                            f"{self.late_trap_minutes:.0f}-minute late window."
                        ),
                        timestamp=now,
                        snapshot_data={
                            "late_trap": late_trap_val,
                            "clock": clock,
                            "quarter": quarter,
                        },
                    )
            except (ValueError, IndexError):
                pass

        return None

    def _check_injury_detected(
        self,
        snapshot: dict,
        game_id: str,
        now: datetime,
    ) -> Optional[Alert]:
        """Alert when a player injury flag is set in the snapshot."""
        player_state = snapshot.get("player_state", {})
        if not isinstance(player_state, dict):
            return None

        injuries = player_state.get("injuries", [])
        if not injuries:
            return None

        for injury in injuries:
            if not isinstance(injury, dict):
                continue
            status = injury.get("status", "").lower()
            if status in ("out", "doubtful", "questionable"):
                return Alert(
                    id=str(uuid.uuid4()),
                    game_id=game_id,
                    type=AlertType.INJURY_DETECTED,
                    severity=(
                        AlertSeverity.CRITICAL
                        if status == "out"
                        else AlertSeverity.WARNING
                    ),
                    title=f"Injury: {injury.get('player_name', 'Unknown')} ({status})",
                    message=(
                        f"{injury.get('player_name', 'Unknown player')} of the "
                        f"{injury.get('team', 'unknown team')} is listed as '{status}'"
                        f"{' due to ' + injury.get('injury_type', '') if injury.get('injury_type') else ''}."
                    ),
                    timestamp=now,
                    snapshot_data=injury,
                )

        return None

    # ── Internal Helpers ──────────────────────────────────────────

    def _is_duplicate(self, alert: Alert, now: datetime) -> bool:
        """Check if a similar alert was generated recently (dedup window)."""
        key = (alert.type.value, alert.game_id)
        last_ts = self._recent_alerts.get(key)
        if last_ts is not None:
            elapsed = (now - last_ts).total_seconds()
            if elapsed < self._dedup_window_s:
                return True
        return False
