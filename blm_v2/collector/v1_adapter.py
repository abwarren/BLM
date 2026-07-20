"""
Adapter that wraps the V1 SnapshotCollector to expose the V2 Collector protocol.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from blm_v1.collector import SnapshotCollector as V1Collector
from blm_v2.collector.base import Collector
from blm_v2.collector.snapshot import RawSnapshot


class V1CollectorAdapter(Collector):
    """Wraps V1 SnapshotCollector to match the V2 Collector protocol.

    The V1 collector runs Playwright in a blocking loop on a daemon thread.
    This adapter starts it on init, and exposes its latest state as a
    ``RawSnapshot`` for the V2 scheduler pipeline.
    """

    def __init__(self, headless: bool = True):
        self._v1 = V1Collector(headless=headless)
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def latest_snapshot(self) -> Optional[RawSnapshot]:
        state = self._v1.latest_state
        if not state:
            return None
        return RawSnapshot.from_v1_dict(state)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._v1.start, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._v1.stop()
