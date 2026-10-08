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
from collections import Counter
from pathlib import Path
from typing import Optional

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.provider import provider_from_config
from blm_v4.betting.store import BettingStore
from blm_v2.telemetry.logging import get_logger

logger = get_logger("betting_worker")


class BettingWorker:
    def __init__(self, cfg: BettingConfig, store: BettingStore,
                 live_payload_fn, poll_interval_s: float = 5.0,
                 trigger_feed=None):
        """``live_payload_fn()`` returns the current /api/v4/live games
        list (in-process callable; injected so tests can stub it).

        ``trigger_feed`` (optional) switches the worker to EVENT-DRIVEN mode:
        instead of a full-scan poll cycle it is woken by each newly emitted
        BLM UNDER trigger transition and evaluates ONLY that game.  The poll
        path stays the default when no feed is supplied.
        """
        self.cfg = cfg
        self.store = store
        self._live_payload_fn = live_payload_fn
        self.poll_interval_s = poll_interval_s
        self.trigger_feed = trigger_feed
        self.provider = provider_from_config(cfg)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        self._last_pass_log = 0.0
        self._no_bet_reasons: Counter = Counter()

    def start(self) -> None:
        target = (self._run_trigger_driven if self.trigger_feed is not None
                  else self._run)
        self._thread = threading.Thread(
            target=target, name="blm-betting-worker", daemon=True)
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
            self._log_pass(summary, games=0, balance=None)
            return summary
        games = self._live_payload_fn() or []
        stats = self.store.today_stats()
        balance = self._balance()
        self._no_bet_reasons = Counter()      # per-pass, so the log is a census
        for g in games:
            stats = self._dispatch_game(g, summary, stats, unit_price, balance)
        self._log_pass(summary, games=len(games), balance=balance)
        return summary

    def _log_pass(self, summary: dict, games: int = 0, balance=None) -> None:
        """Throttled visibility into the decision pass.

        The worker is otherwise silent by design, which hid a stale/empty
        payload, a switched-off engine and a whole pass of rejected games for
        hours.  A pass that produced a candidate logs immediately; every other
        pass logs at most once a minute.  ``games``/``balance``/``no_bet_top``
        ride on the LOG rather than the returned summary, whose exact shape is
        an existing contract.
        """
        now = time.monotonic()
        interesting = bool(summary.get("candidates") or summary.get("would_bet")
                           or summary.get("executed"))
        if not interesting and (now - self._last_pass_log) < 60.0:
            return
        self._last_pass_log = now
        # WHICH gate rejected them: the executor already returns a reason for
        # every NO_BET; without this the count said nothing about why.
        reasons = getattr(self, "_no_bet_reasons", None)
        top = ", ".join(f"{r}={n}" for r, n in reasons.most_common(5)) if reasons else None
        logger.info("betting_pass",
                    enabled=summary.get("enabled"),
                    games=games,
                    candidates=summary.get("candidates", 0),
                    would_bet=summary.get("would_bet", 0),
                    executed=summary.get("executed", 0),
                    no_bet=summary.get("no_bet", 0),
                    balance=balance,
                    no_bet_top=top or None)

    def _balance(self):
        """R5: the signed-in account balance, or None when unprovable."""
        fn = getattr(self.provider, "account_balance", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:
            return None

    # ── the SHARED decision block ─────────────────────────────────────
    def _dispatch_game(self, g: dict, summary: dict, stats: dict,
                       unit_price=None, balance=None) -> dict:
        """Evaluate ONE game and act on the decision.

        Used by BOTH the poll cycle and the event-driven trigger path so the
        gate has exactly one implementation and the two paths cannot drift.
        Returns the refreshed running stats.
        """
        if unit_price is None:
            unit_price = self.store.get_unit_price()
        res = evaluate(g, cfg=self.cfg, store=self.store,
                       enabled=True, unit_price=unit_price,
                       stats=stats, balance=balance,
                       game_enabled=self.store.is_game_enabled(
                           g.get("game_id")))
        if res["decision"] == "NO_BET":
            summary["no_bet"] += 1
            # keep WHY: the executor names the failing gate, and the count
            # alone cannot tell "nothing in band" from "all below the floor"
            reason = str(res.get("reason") or "unspecified")
            if reason == "alert_not_eligible":
                # the eligibility dict carries its own, more specific reason
                # (stale_state, market_missing, not-live, ...).  Read it from
                # the game rather than widening the executor's return shape.
                sub = (g.get("under_alert_eligibility") or {}).get("reason")
                if sub:
                    reason = f"{reason}:{sub}"
            self._no_bet_reasons[reason] += 1
            return stats
        summary["candidates"] += 1
        cand = res["candidate"]
        if res["decision"] == "WOULD_BET":
            # DRY_RUN: record the outcome on the claimed record so
            # the dashboard's recent-executions list shows it
            self.store.update_status(
                cand["execution_id"], "ACCEPTED",
                provider_ref=f"dryrun-{cand['execution_id']}",
                error_message="DRY_RUN — no real bet submitted",
                simulated_amount=cand["stake_amount"],
                execution_state="WOULD_BET")
            summary["would_bet"] += 1
            # refresh running stats so later candidates in this same
            # pass respect the updated exposure/count (§4: limits
            # enforced within a single poll pass, not just across
            # passes — WOULD_BET records count toward the limits
            # immediately after they are persisted)
            return self.store.today_stats()
        out = execute(cand, cfg=self.cfg, store=self.store,
                      provider=self.provider)
        if out.get("status") in ("ACCEPTED", "SUBMITTED"):
            summary["executed"] += 1
        # refresh the running stats so later candidates in this same
        # pass respect the updated exposure/count
        return self.store.today_stats()

    # ── EVENT-DRIVEN path: dispatch NEW trigger transitions ───────────
    def dispatch_triggers(self, events) -> dict:
        """Evaluate ONLY the games that a newly emitted trigger names.

        No scan of the full live payload, no waiting for a poll cycle.  A
        trigger whose game is not in the current live payload is counted and
        SKIPPED (fail closed — never a manufactured candidate).  The decision
        still runs through the same executor gate as the poll path.
        """
        summary = {"enabled": False, "triggered": len(events),
                   "candidates": 0, "executed": 0, "would_bet": 0, "no_bet": 0,
                   "no_live_payload": 0}
        if not self.store.is_enabled():
            return summary                      # kill switch, read fresh
        summary["enabled"] = True
        by_id = {str((g or {}).get("game_id")): g
                 for g in (self._live_payload_fn() or [])}
        stats = self.store.today_stats()
        unit_price = self.store.get_unit_price()
        balance = self._balance()
        for ev in events:
            game = by_id.get(str(getattr(ev, "game_id", "")))
            if game is None:
                summary["no_live_payload"] += 1
                continue
            stats = self._dispatch_game(game, summary, stats, unit_price,
                                        balance)
        return summary

    def _run_trigger_driven(self) -> None:
        """Consume the durable trigger stream and dispatch immediately."""
        while not self._stop.is_set():
            try:
                self.trigger_feed.consume(self._on_trigger, stop=self._stop,
                                          timeout_s=1.0)
            except Exception:                                  # noqa: BLE001
                self.last_error = traceback.format_exc()[-2000:]
                self._stop.wait(1.0)

    def _on_trigger(self, event) -> dict:
        return self.dispatch_triggers([event])
