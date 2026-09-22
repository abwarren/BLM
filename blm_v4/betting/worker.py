"""The auto-betting WORKER — a small dedicated decision thread.

Every ``poll_interval_s`` it:

  1. reads the /api/v4/live payload through the server's own API
     function (in-process — no HTTP hop, same authority the dashboard
     consumes);
  2. evaluates every game through the executor's ten-condition gate;
  3. executes passing candidates through the configured provider
     (DRY_RUN by default) and records the honest outcome.

Kill-switch semantics: the switch is read FRESH FROM THE STORE on every
poll — flipping AUTO BETTING OFF takes effect within one interval, and
a restart defaults it OFF unless it was explicitly persisted enabled.

The worker never touches alert logic, never writes to the analytics
DBs, and logs no credential material (it holds none).
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.provider import provider_from_config
from blm_v4.betting.store import BettingStore


class BettingWorker:
    def __init__(self, cfg: BettingConfig, store: BettingStore,
                 live_payload_fn, poll_interval_s: float = 5.0):
        """``live_payload_fn()`` returns the current /api/v4/live games
        list (in-process callable; injected so tests can stub it)."""
        self.cfg = cfg
        self.store = store
        self._live_payload_fn = live_payload_fn
        self.poll_interval_s = poll_interval_s
        self.provider = provider_from_config(cfg)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="blm-betting-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                self.poll_once()
                self.last_error = None
            except Exception as e:  # the worker NEVER dies noisy
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                traceback.print_exc(limit=2)

    def poll_once(self) -> dict:
        """One evaluation pass over the current live payload."""
        # the kill switch is read fresh each pass (fail closed on any
        # store error — store.get_config returns the OFF default then)
        enabled = self.store.is_enabled()
        unit_price = self.store.get_unit_price()
        summary = {"enabled": enabled, "candidates": 0, "executed": 0,
                   "would_bet": 0, "no_bet": 0}
        if not enabled:
            return summary
        games = self._live_payload_fn() or []
        stats = self.store.today_stats()
        for g in games:
            res = evaluate(g, cfg=self.cfg, store=self.store,
                           enabled=True, unit_price=unit_price,
                           stats=stats)
            if res["decision"] == "NO_BET":
                summary["no_bet"] += 1
                continue
            summary["candidates"] += 1
            cand = res["candidate"]
            if res["decision"] == "WOULD_BET":
                # DRY_RUN: record the outcome on the claimed record so
                # the dashboard's recent-executions list shows it
                self.store.update_status(
                    cand["execution_id"], "ACCEPTED",
                    provider_ref=f"dryrun-{cand['execution_id']}",
                    error_message="DRY_RUN — no real bet submitted")
                summary["would_bet"] += 1
                continue
            out = execute(cand, cfg=self.cfg, store=self.store,
                          provider=self.provider)
            if out.get("status") in ("ACCEPTED", "SUBMITTED"):
                summary["executed"] += 1
            # refresh the running stats so later candidates in this same
            # pass respect the updated exposure/count
            stats = self.store.today_stats()
        return summary
