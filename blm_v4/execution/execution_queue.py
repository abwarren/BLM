"""BLM EXECUTION — EXECUTION QUEUE (run orchestration).

Turns a validated matrix into a RUN: an execution_id, one ParlayJob per
combination, executed sequentially by the TotalExecutor.  Owns:

  * the request gate — stake/exposure validation, the conflict/duplicate
    gate, and the HARD MODE LADDER (LIVE is refused unless the env
    explicitly allows it; DRY_RUN/RESOLVE_ONLY/TEST_VERIFY never place);
  * the in-memory abort registry (the API flips the event; the executor
    checks it between every step);
  * per-run bookkeeping for the UI (a tiny snapshot the API reads).
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from blm_v4.execution.audit import ExecutionAudit
from blm_v4.execution.config import ExecutionConfig
from blm_v4.execution.parlay_matrix import build_matrix
from blm_v4.execution.selection_model import (
    MODE_LIVE,
    MODES,
    AbortEvent,
    ParlayJob,
    Selection,
    validate_selection,
)
from blm_v4.execution.total_executor import TotalExecutor

MODES_CLICKING = (MODE_LIVE,)   # modes permitted to click a selection


class RunRequestError(Exception):
    """The run request is invalid — reported to the user, never run."""


@dataclass
class RunState:
    """A snapshot of one run for the UI/API."""

    execution_id: str
    mode: str
    status: str = "PENDING"          # PENDING | RUNNING | ABORTING | DONE
    parlays: list = field(default_factory=list)
    total_exposure: float = 0.0
    stake_per_parlay: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id, "mode": self.mode,
            "status": self.status,
            "total_exposure": round(self.total_exposure, 2),
            "stake_per_parlay": round(self.stake_per_parlay, 2),
            "error": self.error,
            "parlays": [p.to_dict() if hasattr(p, "to_dict") else p
                        for p in self.parlays],
        }


class ExecutionQueue:
    """Validates requests, runs them, and hands out abort handles."""

    def __init__(self, cfg: ExecutionConfig, *, store=None,
                 adapter_factory=None,
                 audit: Optional[ExecutionAudit] = None):
        self.cfg = cfg
        self.store = store
        self.audit = audit or ExecutionAudit(
            store.audit if store is not None else None)
        # adapter_factory(mode) -> SelectionResolver; injected so phase ③
        # can supply the CDP bridge and tests supply fakes.  Default:
        # unavailable (phase ① runs purely on injected adapters).
        self._adapter_factory = adapter_factory
        self._abort_lock = threading.Lock()
        self._aborts: dict[str, AbortEvent] = {}
        self._runs: dict[str, RunState] = {}

    # ── request validation (the gate before anything runs) ─────────────
    def validate_request(self, legs: list[Selection], fold_sizes: list[int],
                         stake_per_parlay: float, mode: str) -> dict:
        """Full request validation.  Returns the matrix report; raises
        RunRequestError for anything the user must fix first."""
        if mode not in MODES:
            raise RunRequestError(f"unknown mode {mode!r}")
        if mode == MODE_LIVE and not self.cfg.live_permitted:
            raise RunRequestError(
                "LIVE mode refused: EXECUTION_DRY_RUN is true (the env "
                "must explicitly set EXECUTION_DRY_RUN=false; the UI "
                "must show 🔴 LIVE EXECUTION before the final action)")
        if not legs:
            raise RunRequestError("no selections")
        if not fold_sizes:
            raise RunRequestError("no fold sizes requested")
        try:
            stake = float(stake_per_parlay)
        except (TypeError, ValueError):
            raise RunRequestError("stake must be a number")
        if stake <= 0:
            raise RunRequestError("stake must be positive")
        if (self.cfg.max_stake_per_parlay is not None
                and stake > self.cfg.max_stake_per_parlay):
            raise RunRequestError(
                f"stake {stake} exceeds max stake per parlay "
                f"{self.cfg.max_stake_per_parlay}")
        report = build_matrix(legs, fold_sizes)
        if not report.ok:
            if report.duplicates:
                raise RunRequestError(
                    "duplicate selections: "
                    + ", ".join(f"{d['event']} {d['market']}→{d['position']}"
                                for d in report.duplicates))
            raise RunRequestError(
                "conflicting selections on the same event: "
                + "; ".join(f"{c['event']} {c['market']} has "
                            + "/".join(c["positions"])
                            for c in report.conflicts))
        if not report.combos:
            raise RunRequestError("fold sizes exceed the selection count")
        if len(report.combos) > self.cfg.max_parlays_per_run:
            raise RunRequestError(
                f"{len(report.combos)} combinations exceeds the run cap "
                f"({self.cfg.max_parlays_per_run})")
        exposure = round(stake * len(report.combos), 2)
        if (self.cfg.max_total_exposure is not None
                and exposure > self.cfg.max_total_exposure):
            raise RunRequestError(
                f"total exposure {exposure} exceeds "
                f"EXECUTION_MAX_TOTAL_EXPOSURE "
                f"{self.cfg.max_total_exposure}")
        return {"report": report, "stake": stake, "exposure": exposure}

    # ── run lifecycle ──────────────────────────────────────────────────
    def start_run(self, legs: list[Selection], fold_sizes: list[int],
                  stake_per_parlay: float, mode: str,
                  *, executor: Optional[TotalExecutor] = None,
                  execution_id: Optional[str] = None) -> RunState:
        """Validate, build jobs, execute the run (blocking — the API
        wrapper runs this on its own thread)."""
        validated = self.validate_request(legs, fold_sizes,
                                          stake_per_parlay, mode)
        report = validated["report"]
        stake = validated["stake"]
        execution_id = execution_id or f"exec-{uuid.uuid4().hex[:20]}"
        jobs = [ParlayJob(legs=list(combo), fold_size=len(combo),
                          stake_amount=stake)
                for combo in report.combos]
        run = RunState(execution_id=execution_id, mode=mode,
                       parlays=jobs, total_exposure=validated["exposure"],
                       stake_per_parlay=stake)
        with self._abort_lock:
            self._aborts[execution_id] = AbortEvent()
            self._runs[execution_id] = run
        run.status = "RUNNING"
        self.audit.log(
            "RUN_STARTED", execution_id=execution_id,
            details={"mode": mode, "parlays": len(jobs),
                     "stake_per_parlay": stake,
                     "total_exposure": run.total_exposure})
        try:
            exec_ = executor or self._make_executor(execution_id)
            exec_.run_matrix(execution_id, jobs, mode)
            aborted = self.get_abort(execution_id).is_requested()
            run.status = "DONE"
            if aborted:
                run.error = "USER_ABORT (remaining parlays skipped)"
                self.audit.log("RUN_ABORTED", execution_id=execution_id)
            else:
                self.audit.log("RUN_COMPLETE", execution_id=execution_id,
                               details={
                                   "order_placed": sum(
                                       1 for j in jobs
                                       if j.status == "COMPLETE"),
                                   "dry_run_complete": sum(
                                       1 for j in jobs
                                       if j.status == "DRY_RUN_COMPLETE"),
                                   "aborted": sum(
                                       1 for j in jobs
                                       if j.status == "USER_ABORT"),
                                   "failed": sum(
                                       1 for j in jobs
                                       if j.status ==
                                       "NON_RECOVERABLE_ERROR")})
        finally:
            with self._abort_lock:
                # keep the abort handle briefly for late UI reads; the
                # runs map is the UI's source of truth
                pass
        return run

    def _make_executor(self, execution_id: str) -> TotalExecutor:
        if self._adapter_factory is None:
            raise RunRequestError(
                "no adapter configured (phase ③ wires the CDP bridge; "
                "tests inject a fake adapter directly)")
        abort = self.get_abort(execution_id)
        return TotalExecutor(
            self._adapter_factory(), self.cfg, store=self.store,
            audit=self.audit, abort=abort)

    # ── abort + status ─────────────────────────────────────────────────
    def get_abort(self, execution_id: str) -> AbortEvent:
        with self._abort_lock:
            ev = self._aborts.get(execution_id)
            if ev is None:
                ev = AbortEvent()
                self._aborts[execution_id] = ev
            return ev

    def request_abort(self, execution_id: str) -> bool:
        """ABORT EXECUTION: stops retries, selections and placement at
        the next step boundary; leaves the bookmaker in its current
        state; the run is marked ABORTED."""
        with self._abort_lock:
            ev = self._aborts.get(execution_id)
        if ev is None:
            return False
        ev.request()
        run = self._runs.get(execution_id)
        if run is not None and run.status == "RUNNING":
            run.status = "ABORTING"
        self.audit.log("ABORT_REQUESTED", execution_id=execution_id)
        return True

    def get_run(self, execution_id: str) -> Optional[RunState]:
        return self._runs.get(execution_id)
