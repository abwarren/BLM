"""BLM EXECUTION — TOTAL EXECUTOR (the persistent state machine).

Treats each parlay as an EXECUTION JOB, not a collection of clicks:

    RESOLVE → SELECT → VERIFY → (recoverable failure: RE-RESOLVE)
        → repeat until the leg is confirmed in the betslip
        → next leg → verify the full slip → place → verify the order
        → ORDER_PLACED

Persistence rules (directive §6/§7):
  * recoverable conditions (line/price changed, selection vanished,
    DOM rerendered, click not registered, slip slow to update) return
    the engine to RESOLVING_LEG — a FRESH resolution, never a repeated
    click on stale DOM;
  * every retry re-inspects the betslip FIRST (idempotency): a leg
    already present is never added again;
  * the loop is bounded: 1 + MAX_SELECTION_RETRIES attempts per leg,
    watchdog wall-clock budgets per leg and per job — no infinite loop;
  * abort is checked between every step;
  * in any non-LIVE mode the engine STOPS at BETSLIP_READY — it never
    clicks a selection, never places an order (DRY_RUN by construction,
    not by hope).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from blm_v4.execution.adapter import AdapterUnavailable, SelectionResolver
from blm_v4.execution.betslip_verifier import (
    BETSLIP_DUPLICATE,
    BETSLIP_NOT_UPDATED,
    BETSLIP_UNREADABLE,
    VERIFIED,
    verify_full_betslip,
    verify_leg_in_betslip,
)
from blm_v4.execution.config import ExecutionConfig
from blm_v4.execution.selection_model import (
    AbortEvent,
    AbortRequested,
    MODE_LIVE,
    ParlayJob,
    STATE_BETSLIP_READY,
    STATE_COMPLETE,
    STATE_CREATED,
    STATE_CURRENT_SELECTION_FOUND,
    STATE_DRY_RUN_COMPLETE,
    STATE_EXECUTION_STARTED,
    STATE_LEG_CONFIRMED,
    STATE_NON_RECOVERABLE,
    STATE_ORDER_PLACED,
    STATE_PLACING_PARLAY,
    STATE_RESOLVING_LEG,
    STATE_SELECTING_LEG,
    STATE_USER_ABORT,
    STATE_VERIFYING_LEG,
    STATE_VERIFYING_ORDER,
    Selection,
)
from blm_v4.execution.selection_resolver import resolve_selection
from blm_v4.execution.audit import ExecutionAudit

_RECOVERABLE_RESOLUTION = ("POSITION_NOT_FOUND", "DOM_CHANGED")


class ExecutionFailure(Exception):
    """Internal: a terminal job failure with its directive reason."""


def _utcnow() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class TotalExecutor:
    """Executes ParlayJobs against a bookmaker adapter."""

    def __init__(self, adapter: SelectionResolver, cfg: ExecutionConfig,
                 *, store=None, audit: Optional[ExecutionAudit] = None,
                 abort: Optional[AbortEvent] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        self.adapter = adapter
        self.cfg = cfg
        self.store = store
        # No auditor injected → wire the STORE's audit sink when a store
        # is present (direct construction must still audit); without a
        # store the auditor is a no-op (pure-engine unit tests).
        self.audit = audit or ExecutionAudit(
            store.audit if store is not None else None)
        self.abort = abort or AbortEvent()
        self._clock = clock
        self._sleep = sleeper

    # ── public entry points ────────────────────────────────────────────
    def run_matrix(self, execution_id: str, jobs: list[ParlayJob],
                   mode: str) -> list[ParlayJob]:
        """Run jobs sequentially; abort between jobs terminates the rest
        (already-COMPLETE/terminal jobs are left untouched)."""
        for job in jobs:
            if job.is_terminal:
                continue
            self.run_job(execution_id, job, mode)
            if self.abort.is_requested():
                break
        return jobs

    def run_job(self, execution_id: str, job: ParlayJob,
                mode: str) -> ParlayJob:
        """Execute one parlay job to a terminal state."""
        if job.is_terminal:
            return job
        job.status = STATE_EXECUTION_STARTED
        job.started_at = _utcnow()
        job.last_error = None
        self._persist_job(execution_id, job, mode)
        self.audit.state(STATE_EXECUTION_STARTED, job.parlay_id)
        job_started = self._clock()
        try:
            lines, prices = self._run_legs(execution_id, job, mode,
                                           job_started)
            self._set_state(execution_id, job, mode, STATE_BETSLIP_READY)
            self._final_verify(execution_id, job, mode, lines, prices,
                               job_started)
            if mode == MODE_LIVE:
                self._place(execution_id, job, mode, job_started)
                self._set_state(execution_id, job, mode, STATE_ORDER_PLACED)
                self._set_state(execution_id, job, mode, STATE_COMPLETE)
                # the order reference (the bookmaker's own receipt) is
                # surfaced on the job for the UI and the audit log
                conf = self.adapter.read_order_confirmation()
                if conf:
                    job.order_reference = conf.get("reference")
            else:
                # DRY_RUN / RESOLVE_ONLY / TEST_VERIFY: everything except
                # the real submission.  The stop is BY CONSTRUCTION.
                self.audit.log(
                    "PLACEMENT_SKIPPED", parlay_id=job.parlay_id,
                    reason=f"mode={mode} (non-LIVE never places)")
                self._set_state(execution_id, job, mode,
                                STATE_DRY_RUN_COMPLETE)
        except AbortRequested:
            job.status = STATE_USER_ABORT
            job.completed_at = _utcnow()
            self._persist_job(execution_id, job, mode)
            self.audit.state(STATE_USER_ABORT, job.parlay_id)
        except ExecutionFailure as e:
            job.status = STATE_NON_RECOVERABLE
            job.last_error = str(e)
            job.completed_at = _utcnow()
            self._persist_job(execution_id, job, mode)
            self.audit.state(STATE_NON_RECOVERABLE, job.parlay_id,
                             reason=str(e))
        except AdapterUnavailable as e:
            job.status = STATE_NON_RECOVERABLE
            job.last_error = f"BROWSER_DISCONNECTED: {e}"
            job.completed_at = _utcnow()
            self._persist_job(execution_id, job, mode)
            self.audit.state(STATE_NON_RECOVERABLE, job.parlay_id,
                             reason=job.last_error)
        return job

    # ── legs ───────────────────────────────────────────────────────────
    def _run_legs(self, execution_id: str, job: ParlayJob, mode: str,
                  job_started: float) -> tuple[list, list]:
        lines_at_click: list = []
        prices_at_click: list = []
        for idx, sel in enumerate(job.legs, start=1):
            self.abort.check(f"leg {idx} start")
            self._check_job_deadline(job, job_started)
            job.current_leg = idx
            line, price = self._ensure_leg(execution_id, job, idx, sel,
                                           mode, job_started)
            # the CURRENT values the leg was confirmed with — surfaced on
            # the job for the UI/audit (the attempts log keeps every leg)
            job.resolved_line, job.resolved_price = line, price
            lines_at_click.append(line)
            prices_at_click.append(price)
        return lines_at_click, prices_at_click

    def _ensure_leg(self, execution_id: str, job: ParlayJob, leg_index: int,
                    sel: Selection, mode: str,
                    job_started: float) -> tuple[Optional[float],
                                                 Optional[float]]:
        """Resolve → select → verify ONE leg until it is confirmed in the
        betslip (bounded by 1 + max_selection_retries attempts).

        Returns the (line, price) captured for the confirmed leg."""
        max_attempts = 1 + self.cfg.max_selection_retries
        attempt = 0
        while True:
            self.abort.check(f"leg {leg_index} attempt {attempt + 1}")
            self._check_job_deadline(job, job_started)
            attempt += 1
            if attempt > max_attempts:
                raise ExecutionFailure(
                    f"MAX_RETRIES_EXCEEDED leg {leg_index} "
                    f"({sel.event} {sel.position}) after {max_attempts} "
                    "attempts")

            # ── IDEMPOTENCY: inspect the CURRENT slip BEFORE clicking.
            # A leg already present (from an earlier attempt whose
            # response was lost, or a reconnect) is NEVER added again.
            pre = verify_leg_in_betslip(self.adapter, sel, None, None)
            if pre.outcome in (VERIFIED, BETSLIP_DUPLICATE) \
                    and pre.matched is not None:
                self._record_attempt(execution_id, job, leg_index, attempt,
                                     sel, None, None, None, None,
                                     "ALREADY_IN_SLIP",
                                     verified_line=pre.matched.get("line"),
                                     verified_price=pre.matched.get("price"))
                self.audit.log(
                    "ALREADY_IN_SLIP", parlay_id=job.parlay_id,
                    leg_index=leg_index,
                    reason="idempotency: leg present — not added again")
                return (self._num(pre.matched.get("line")),
                        self._num(pre.matched.get("price")))

            # ── RESOLVING_LEG: a completely FRESH resolution ───────────
            self._set_state(execution_id, job, mode, STATE_RESOLVING_LEG,
                            leg_index=leg_index)
            try:
                res = resolve_selection(self.adapter, sel,
                                        settle_ms=self.cfg.settle_ms)
            except AdapterUnavailable:
                raise
            if not res.ok:
                self._record_attempt(execution_id, job, leg_index, attempt,
                                     sel, None, None, None, None,
                                     res.reason)
                if res.reason in _RECOVERABLE_RESOLUTION:
                    self._sleep_retry()
                    continue  # back to RESOLVING_LEG
                raise ExecutionFailure(
                    f"{res.reason}: {sel.event} {sel.market} "
                    f"{sel.position}")

            obs = res.observation
            self._set_state(execution_id, job, mode,
                            STATE_CURRENT_SELECTION_FOUND,
                            leg_index=leg_index)
            self.audit.resolved(job.parlay_id, leg_index, sel.event,
                                sel.position, obs.line, obs.price)

            # ── SELECTING_LEG: click the CURRENT selection ─────────────
            self._set_state(execution_id, job, mode, STATE_SELECTING_LEG,
                            leg_index=leg_index)
            self.abort.check("click")
            try:
                clicked = self.adapter.click_selection(obs)
            except AdapterUnavailable:
                raise
            except Exception:
                clicked = False
            self._record_attempt(execution_id, job, leg_index, attempt,
                                 sel, obs.line, obs.price, obs.line,
                                 obs.price,
                                 "CLICKED" if clicked
                                 else "CLICK_NOT_REGISTERED")
            if not clicked:
                self._sleep_retry()
                continue  # back to RESOLVING_LEG (fresh resolution)

            # ── VERIFYING_LEG: the slip must actually show it ──────────
            self._set_state(execution_id, job, mode, STATE_VERIFYING_LEG,
                            leg_index=leg_index)
            check: Optional[object] = None
            verified = False
            for v in range(self.cfg.verify_attempts):
                self.abort.check("betslip verify")
                self._check_job_deadline(job, job_started)
                self._sleep(self.cfg.slip_wait_ms / 1000.0)
                try:
                    check = verify_leg_in_betslip(
                        self.adapter, sel, obs.line, obs.price)
                except AdapterUnavailable:
                    raise
                except Exception:
                    check = None
                if check is not None and getattr(
                        check, "outcome", None) in (VERIFIED,
                                                    BETSLIP_DUPLICATE):
                    verified = True
                    break
            if verified and check.outcome == BETSLIP_DUPLICATE:
                # Present more than once — cannot be fixed by clicking;
                # safe direction is to stop and report.
                self._record_attempt(execution_id, job, leg_index, attempt,
                                     sel, obs.line, obs.price, obs.line,
                                     obs.price, BETSLIP_DUPLICATE,
                                     error_code="DUPLICATE_IN_SLIP")
                raise ExecutionFailure(
                    f"BETSLIP_VERIFICATION_FAILED leg {leg_index}: "
                    "duplicate entries in slip")
            if not verified:
                outcome = (check.outcome if check is not None
                           else BETSLIP_UNREADABLE)
                self._record_attempt(execution_id, job, leg_index, attempt,
                                     sel, obs.line, obs.price, obs.line,
                                     obs.price, BETSLIP_NOT_UPDATED,
                                     error_code=outcome)
                self._sleep_retry()
                continue  # back to RESOLVING_LEG — never blind re-click

            # ── LEG_CONFIRMED ──────────────────────────────────────────
            m = check.matched or {}
            v_line, v_price = self._num(m.get("line")), self._num(
                m.get("price"))
            self._record_attempt(execution_id, job, leg_index, attempt,
                                 sel, obs.line, obs.price, obs.line,
                                 obs.price, "LEG_CONFIRMED",
                                 verified_line=v_line,
                                 verified_price=v_price)
            self._set_state(execution_id, job, mode, STATE_LEG_CONFIRMED,
                            leg_index=leg_index)
            self.audit.leg_confirmed(job.parlay_id, leg_index, v_line,
                                     v_price, attempt)
            return obs.line, obs.price

    # ── final slip verification + placement ────────────────────────────
    def _final_verify(self, execution_id: str, job: ParlayJob, mode: str,
                      lines: list, prices: list, job_started: float) -> None:
        """Pre-placement gate: EVERY requested leg present, exactly once,
        in the CURRENT slip; identity verified; and — immediately before
        placement — every leg's market re-confirmed STILL ACTIVE on the
        bookmaker (a resolved-once suspended market must never be placed
        into).  Missing/problem legs are re-added (recoverably) before
        the gate can pass."""
        for round_no in range(1 + self.cfg.max_selection_retries):
            self.abort.check("final betslip verification")
            self._check_job_deadline(job, job_started)
            full = verify_full_betslip(self.adapter, job.legs, lines, prices)
            if full["ok"]:
                # MARKET STILL ACTIVE (directive 2026-09-23, step 5):
                # a FRESH suspended check of every leg at the placement
                # boundary.  An observation that cannot be re-resolved
                # right now is treated as not active (the earlier
                # resolution is stale; fail closed, recoverably).
                inactive: list[str] = []
                for sel in job.legs:
                    try:
                        cur = self.adapter.find_position(
                            sel.event, sel.market, sel.position)
                    except AdapterUnavailable:
                        raise
                    except Exception:
                        cur = None
                    if cur is None or cur.suspended:
                        inactive.append(
                            f"{sel.event} {sel.position} (resolved={bool(cur)},"
                            f" suspended={bool(cur and cur.suspended)})")
                if inactive:
                    self.audit.log(
                        "MARKET_NOT_ACTIVE", parlay_id=job.parlay_id,
                        reason="; ".join(inactive))
                    raise ExecutionFailure(
                        "MARKET_NOT_ACTIVE at placement: "
                        + "; ".join(inactive))
                self.audit.log("BETSLIP_READY_VERIFIED",
                               parlay_id=job.parlay_id,
                               details={"legs": len(job.legs)})
                return
            # Re-run _ensure_leg for legs the slip lost — it is
            # idempotent (won't double-add) and bounded.
            readded = False
            for i, sel in enumerate(job.legs):
                if any(m.get("event") == sel.event
                       and m.get("position") == sel.position
                       for m in full["missing"]):
                    self._ensure_leg(execution_id, job, i + 1, sel, mode,
                                     job_started)
                    readded = True
            if not readded and full["problems"]:
                raise ExecutionFailure(
                    "BETSLIP_VERIFICATION_FAILED: "
                    + "; ".join(p["reason"] for p in full["problems"]))
            if not readded:
                self._sleep_retry()
        raise ExecutionFailure("BETSLIP_VERIFICATION_FAILED: full-slip "
                               "verification did not pass")

    def _place(self, execution_id: str, job: ParlayJob, mode: str,
               job_started: float) -> None:
        """LIVE ONLY.  Submit, then verify the bookmaker actually
        accepted — a place-button click is never proof."""
        self._set_state(execution_id, job, mode, STATE_PLACING_PARLAY)
        self.abort.check("place parlay")
        # Ledger guard: a recorded submission for this parlay means the
        # order may already be in play — NEVER insert a second row.
        prior = self.store.get_order(job.parlay_id) if self.store else None
        if prior is not None:
            self.audit.log("ORDER_SUBMISSION_EXISTS",
                           parlay_id=job.parlay_id,
                           reason="ledger already has a submission — "
                                  "not resubmitting")
            result = {"status": prior.get("status") or "UNKNOWN",
                      "provider_ref": prior.get("provider_ref")}
        else:
            result = self.adapter.place_parlay(job.stake_amount)
            status = result.get("status", "UNKNOWN")
            recorded = (self.store.record_order(
                execution_id, job.parlay_id, 1, job.stake_amount, status,
                provider_ref=result.get("provider_ref"),
                error_code=result.get("error_code"),
                game_ids=job.game_ids)
                if self.store else True)
            if not recorded:
                self.audit.log("ORDER_LEDGER_REFUSED",
                               parlay_id=job.parlay_id,
                               reason="submission row already exists")
        status = result.get("status", "UNKNOWN")
        if status == "ACCEPTED":
            return
        if status in ("REJECTED", "FAILED"):
            raise ExecutionFailure(f"ORDER_{status}")
        # SUBMITTED / UNKNOWN: poll for the bookmaker's OWN confirmation.
        self._set_state(execution_id, job, mode, STATE_VERIFYING_ORDER)
        for _ in range(self.cfg.verify_attempts):
            self.abort.check("order confirmation")
            self._check_job_deadline(job, job_started)
            self._sleep(self.cfg.slip_wait_ms / 1000.0)
            conf = self.adapter.read_order_confirmation()
            if conf:
                self.audit.log("ORDER_CONFIRMED",
                               parlay_id=job.parlay_id,
                               details={"ref": conf.get("reference")})
                return
        raise ExecutionFailure(
            "ORDER_UNCONFIRMED: submission recorded but the bookmaker "
            "did not confirm acceptance — no resubmission (duplicate "
            "protection)")

    # ── helpers ────────────────────────────────────────────────────────
    def _set_state(self, execution_id: str, job: ParlayJob, mode: str,
                   state: str, leg_index: Optional[int] = None) -> None:
        job.status = state
        self._persist_job(execution_id, job, mode)
        self.audit.state(state, job.parlay_id, leg_index=leg_index)

    def _persist_job(self, execution_id: str, job: ParlayJob,
                     mode: str) -> None:
        if self.store is None:
            return
        try:
            self.store.upsert_job(execution_id, job.to_dict(), mode)
        except Exception:
            pass  # persistence must never break the decision path

    def _record_attempt(self, execution_id: str, job: ParlayJob,
                        leg_index: int, attempt_no: int, sel: Selection,
                        disc_line, disc_price, click_line, click_price,
                        outcome: str, verified_line=None,
                        verified_price=None,
                        error_code: Optional[str] = None) -> None:
        job.retry_count = max(0, attempt_no - 1)
        while len(job.attempts_per_leg) < leg_index:
            job.attempts_per_leg.append([])
        job.attempts_per_leg[leg_index - 1].append({
            "attempt": attempt_no, "outcome": outcome,
            "discovered_line": disc_line, "discovered_price": disc_price,
        })
        if self.store is None:
            return
        try:
            self.store.record_attempt(
                execution_id, job.parlay_id, leg_index, attempt_no,
                sel.to_dict(), disc_line, disc_price, click_line,
                click_price, outcome, verified_line, verified_price,
                error_code)
        except Exception:
            pass

    def _check_job_deadline(self, job: ParlayJob, started: float) -> None:
        if (self._clock() - started) > self.cfg.job_timeout_s:
            raise ExecutionFailure(
                f"WATCHDOG_TIMEOUT after {self.cfg.job_timeout_s}s")

    def _sleep_retry(self) -> None:
        self._sleep(self.cfg.retry_delay_ms / 1000.0)

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
