"""BLM V2 — Replay Engine

Asynchronous replay engine for stepping through historical game snapshots.
Interpolates between snapshots for smooth playback at configurable speeds.

Usage::

    engine = ReplayEngine(ts_db=my_ts_interface)
    await engine.load_game("game_123")
    await engine.play()
    await asyncio.sleep(5)
    await engine.pause()
    frame = engine.get_current_frame()
"""

from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime
from typing import Any, Callable, Optional

from blm_v2.timeseries.base import TimeSeriesDB


# ── Constants ──────────────────────────────────────────────────────────

TARGET_FPS: float = 60.0
"""Target frame rate for interpolation."""

FRAME_INTERVAL_S: float = 1.0 / TARGET_FPS
"""Time between frames at 1x speed."""

SNAPSHOT_INTERVAL_S: float = 20.0
"""Expected interval between actual snapshots in the DB."""

SUPPORTED_SPEEDS: list[float] = [1.0, 2.0, 4.0, 8.0, 16.0]
"""Supported playback speed multipliers."""


# ── Interpolation Helpers ──────────────────────────────────────────────


def _lerp(a: float, b: float, t: float) -> float:
    """Linearly interpolate between two floats."""
    return a + (b - a) * t


def _interpolate_snapshots(
    prev: dict[str, Any],
    curr: dict[str, Any],
    t: float,
) -> dict[str, Any]:
    """Interpolate between two snapshots at time t [0.0, 1.0].

    Performs deep interpolation on numeric fields in the standard
    BLM sub-dicts (game_state, blm, momentum, trap_detection, etc.).
    Non-numeric fields are copied from the current snapshot.
    """
    result = copy.deepcopy(curr)

    # ── Game state interpolation ──────────────────────────────────
    prev_gs = prev.get("game_state", {})
    curr_gs = curr.get("game_state", {})
    if isinstance(prev_gs, dict) and isinstance(curr_gs, dict):
        for key in ("home_score", "away_score", "margin", "total"):
            if key in prev_gs and key in curr_gs:
                result.setdefault("game_state", {})[key] = round(
                    _lerp(prev_gs[key], curr_gs[key], t)
                )

    # ── BLM score interpolation ───────────────────────────────────
    prev_blm = prev.get("blm", {})
    curr_blm = curr.get("blm", {})
    if isinstance(prev_blm, dict) and isinstance(curr_blm, dict):
        for key in ("confidence", "win_probability", "expected_margin", "expected_total"):
            if key in prev_blm and key in curr_blm and prev_blm[key] is not None and curr_blm[key] is not None:
                result.setdefault("blm", {})[key] = _lerp(
                    prev_blm[key], curr_blm[key], t
                )

    # ── Trap detection interpolation ──────────────────────────────
    prev_trap = prev.get("trap_detection", {})
    curr_trap = curr.get("trap_detection", {})
    if isinstance(prev_trap, dict) and isinstance(curr_trap, dict):
        for key in ("trap_meter", "bull_trap", "bear_trap", "late_trap",
                     "sharp_trap", "dead_market", "false_momentum", "reverse_bull_trap"):
            if key in prev_trap and key in curr_trap:
                result.setdefault("trap_detection", {})[key] = _lerp(
                    prev_trap[key], curr_trap[key], t
                )

    # ── Momentum interpolation ────────────────────────────────────
    prev_mom = prev.get("momentum", {})
    curr_mom = curr.get("momentum", {})
    if isinstance(prev_mom, dict) and isinstance(curr_mom, dict):
        for key in ("momentum_score", "momentum_velocity", "momentum_acceleration"):
            if key in prev_mom and key in curr_mom:
                result.setdefault("momentum", {})[key] = _lerp(
                    prev_mom[key], curr_mom[key], t
                )

    # ── Pace interpolation ────────────────────────────────────────
    prev_pace = prev.get("pace", {})
    curr_pace = curr.get("pace", {})
    if isinstance(prev_pace, dict) and isinstance(curr_pace, dict):
        for key in ("real_pace", "expected_pace"):
            if key in prev_pace and key in curr_pace:
                result.setdefault("pace", {})[key] = _lerp(
                    prev_pace[key], curr_pace[key], t
                )

    return result


# ── Replay Engine ──────────────────────────────────────────────────────


class ReplayEngine:
    """Asynchronous replay engine for BLM V2 historical snapshots.

    Loads all snapshots for a completed game and provides frame-by-frame
    playback with interpolation, variable speed, quarter jumping, and
    event-driven progress.

    Frame events fire a callback with the current frame index, total
    frame count, and the current interpolated snapshot.
    """

    def __init__(
        self,
        ts_db: Optional[TimeSeriesDB] = None,
        on_frame: Optional[Callable[[int, int, dict[str, Any]], Any]] = None,
    ) -> None:
        self._ts_db = ts_db
        self._on_frame = on_frame

        # Snapshot data
        self._snapshots: list[dict[str, Any]] = []
        self._game_id: str = ""
        self._total_frames: int = 0

        # Playback state
        self._playing: bool = False
        self._current_frame_index: int = 0
        self._speed: float = 1.0
        self._task: Optional[asyncio.Task] = None

        # Metadata
        self._quarter_boundaries: list[int] = []
        self._interpolated_frames_per_segment: int = 0

    # ── Properties ─────────────────────────────────────────────────

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_paused(self) -> bool:
        return not self._playing

    @property
    def current_frame_index(self) -> int:
        return self._current_frame_index

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def game_id(self) -> str:
        return self._game_id

    @property
    def progress(self) -> float:
        if self._total_frames == 0:
            return 0.0
        return self._current_frame_index / self._total_frames

    @property
    def current_quarter(self) -> int:
        frame = self.get_current_frame()
        if frame:
            meta = frame.get("metadata", {})
            if isinstance(meta, dict):
                return meta.get("quarter", 0)
        return 0

    # ── Lifecycle ──────────────────────────────────────────────────

    async def load_game(self, game_id: str) -> int:
        """Load all snapshots for a completed game.

        Args:
            game_id: The game identifier.

        Returns:
            Total number of frames prepared.
        """
        if self._ts_db is None:
            raise RuntimeError("No TimeSeriesDB configured — cannot load snapshots.")

        raw_snapshots = await self._ts_db.query_snapshots(
            game_id=game_id,
            limit=5000,
        )

        if not raw_snapshots:
            raise ValueError(f"No snapshots found for game '{game_id}'")

        self._snapshots = raw_snapshots
        self._game_id = game_id
        self._current_frame_index = 0
        self._playing = False

        # Build quarter boundary index
        self._quarter_boundaries = []
        prev_q = -1
        for i, snap in enumerate(self._snapshots):
            meta = snap.get("metadata", {}) if isinstance(snap, dict) else {}
            q = meta.get("quarter", 0) if isinstance(meta, dict) else 0
            if q != prev_q:
                self._quarter_boundaries.append(i)
                prev_q = q

        # Calculate total interpolated frames
        segment_frames = max(
            1, int(SNAPSHOT_INTERVAL_S / FRAME_INTERVAL_S / self._speed)
        )
        self._interpolated_frames_per_segment = segment_frames
        self._total_frames = max(0, len(self._snapshots) - 1) * segment_frames

        return self._total_frames

    async def play(self) -> None:
        """Start or resume playback from the current position."""
        if not self._snapshots:
            raise RuntimeError("No game loaded. Call load_game() first.")
        if self._playing:
            return
        self._playing = True
        self._task = asyncio.create_task(self._playback_loop())

    def pause(self) -> None:
        """Pause playback at the current frame."""
        self._playing = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def resume(self) -> None:
        """Resume playback (alias for play())."""
        await self.play()

    def seek(self, position: float) -> None:
        """Seek to a fractional position [0.0, 1.0] in the timeline."""
        pos = max(0.0, min(1.0, position))
        self._current_frame_index = int(pos * self._total_frames)
        self._current_frame_index = max(0, min(self._current_frame_index, max(0, self._total_frames - 1)))

    def set_speed(self, multiplier: float) -> bool:
        """Set the playback speed multiplier (1, 2, 4, 8, 16 supported)."""
        if multiplier in SUPPORTED_SPEEDS:
            was_playing = self._playing
            if was_playing:
                self.pause()
            self._speed = multiplier
            self._recalculate_frames()
            if was_playing:
                asyncio.create_task(self._delayed_resume())
            return True
        return False

    async def _delayed_resume(self) -> None:
        await asyncio.sleep(0.05)
        await self.play()

    # ── Frame Access ───────────────────────────────────────────────

    def get_current_frame(self) -> Optional[dict[str, Any]]:
        """Return the current interpolated frame, or None if empty."""
        if not self._snapshots or self._total_frames == 0:
            return None
        return self._compute_frame(self._current_frame_index)

    def next_frame(self) -> Optional[dict[str, Any]]:
        """Advance one frame forward and return it. None at end."""
        if self._current_frame_index >= self._total_frames - 1:
            return None
        self._current_frame_index += 1
        return self.get_current_frame()

    def previous_frame(self) -> Optional[dict[str, Any]]:
        """Step one frame backward and return it. None at start."""
        if self._current_frame_index <= 0:
            return None
        self._current_frame_index -= 1
        return self.get_current_frame()

    def jump_to_quarter(self, q: int) -> bool:
        """Jump to the start of the specified quarter (1-4, 5+ for OT)."""
        for i, snap in enumerate(self._snapshots):
            meta = snap.get("metadata", {}) if isinstance(snap, dict) else {}
            if isinstance(meta, dict) and meta.get("quarter", 0) == q:
                frame_idx = i * self._interpolated_frames_per_segment
                if frame_idx < self._total_frames:
                    self._current_frame_index = frame_idx
                    return True
        return False

    # ── Internal ───────────────────────────────────────────────────

    def _recalculate_frames(self) -> None:
        """Recalculate total frame count based on current speed."""
        if len(self._snapshots) < 2:
            self._total_frames = 0
            return
        segment_frames = max(
            1, int(SNAPSHOT_INTERVAL_S / FRAME_INTERVAL_S / self._speed)
        )
        self._interpolated_frames_per_segment = segment_frames
        new_total = (len(self._snapshots) - 1) * segment_frames
        if self._total_frames > 0:
            ratio = self._current_frame_index / self._total_frames
            self._current_frame_index = min(int(ratio * new_total), new_total - 1)
        self._total_frames = new_total

    def _compute_frame(self, frame_index: int) -> dict[str, Any]:
        """Compute the frame by interpolating between the two nearest snapshots."""
        if not self._snapshots:
            return {}
        n = len(self._snapshots)
        if n == 1:
            return copy.deepcopy(self._snapshots[0])

        segment_idx = min(
            frame_index // self._interpolated_frames_per_segment,
            n - 2,
        )
        in_segment_t = (
            (frame_index % self._interpolated_frames_per_segment)
            / max(1, self._interpolated_frames_per_segment)
        )

        prev = self._snapshots[segment_idx]
        curr = self._snapshots[segment_idx + 1]
        return _interpolate_snapshots(prev, curr, in_segment_t)

    async def _playback_loop(self) -> None:
        """Main playback loop advancing frames at the configured rate."""
        try:
            while self._playing and self._current_frame_index < self._total_frames:
                loop_start = time.monotonic()

                frame = self._compute_frame(self._current_frame_index)
                if self._on_frame and frame:
                    cb = self._on_frame
                    if asyncio.iscoroutinefunction(cb):
                        await cb(self._current_frame_index, self._total_frames, frame)
                    else:
                        cb(self._current_frame_index, self._total_frames, frame)

                self._current_frame_index += 1

                elapsed = time.monotonic() - loop_start
                sleep_time = max(0.0, (FRAME_INTERVAL_S / self._speed) - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            pass
        finally:
            if self._current_frame_index >= self._total_frames:
                self._playing = False
