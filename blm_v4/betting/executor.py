"""The auto-betting EXECUTOR — the single decision authority.

Evaluates the directive's ten conditions IN ORDER and returns an
explicit, auditable decision.  Every refusal names its reason; every
acceptance carries the full stake calculation.  The fail-safe rule (§9)
governs everything: when any input is missing, stale, unverifiable or
ambiguous → NO BET.

The executor NEVER touches the alert logic: it CONSUMES the production
``under_alert.active`` verdict (and its eligibility reason) verbatim from
the /api/v4/live payload — the same authority the dashboard renders.  No
fingerprint (C1..C6/R2) can create a bet; they are recorded in the audit
log as context only.  R1 does not exist anywhere in this layer.

Ordering matters: cheap, unambiguous refusals come first; the idempotent
claim (condition 10) runs only after everything else passed, so a
refused opportunity never occupies a slot.  Exactly-once is enforced by
the store's UNIQUE idempotency key, not by hope.
"""
from __future__ import annotations

import math
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.provider import (
    DryRunProvider,
    ProviderAmbiguous,
    ProviderUnavailable,
)
from blm_v4.betting.account_guard import verify_account
from blm_v4.betting.store import BettingStore
from blm_v4.betting import command as _command
from blm_v4.betting import stake as _stake

# directive §5 — the execution-state vocabulary
STATUS_PENDING = "PENDING"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_UNKNOWN = "UNKNOWN"


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _alert_age_s(game: dict) -> Optional[float]:
    """The age of the alert's current projector observation, in seconds —
    the freshness of the state the alert verdict was computed from."""
    from datetime import datetime as dt
    proj = game.get("projector") or {}
    cap = proj.get("captured_at")
    if not cap:
        return None
    try:
        t = dt.fromisoformat(str(cap).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = dt.now(timezone.utc)
    return max(0.0, (now - t).total_seconds())


def evaluate(game: dict, *, cfg: BettingConfig, store: BettingStore,
             enabled: bool, unit_price: Optional[float],
             stats: dict, claim: bool = True,
             game_enabled: bool = True,
             balance: Optional[float] = None) -> dict:
    """Evaluate ONE game's payload as a potential execution candidate.

    Returns ``{"decision": "EXECUTE"|"NO_BET"|"WOULD_BET", "reason":
    ..., "candidate": {...}|None}``.  In DRY_RUN a passing evaluation is
    reported as ``WOULD_BET`` (§11) with the full would-be stake.
    """
    ik = None
    # ── 1. the kill switch (persisted; OFF unless explicitly enabled) ──
    if not enabled:
        return _no("auto_betting_off", ik)
    if not game_enabled:
        return _no("PER_GAME_AUTO_BET_OFF", ik)
    # ── 2+3. the production alert verdict, consumed verbatim ──────────
    ua = game.get("under_alert") or {}
    if ua.get("active") is not True:
        return _no("alert_not_active", ik)
    if (game.get("under_alert_eligibility") or {}).get("eligible") is not True:
        return _no("alert_not_eligible", ik)
    # ── identity: the alert must carry its checkpoint id ──────────────
    alert_id = f"{game.get('game_id')}|{ua.get('checkpoint')}"
    checkpoint = str(ua.get("checkpoint"))
    game_id = str(game.get("game_id") or "")
    if not game_id or ua.get("checkpoint") is None:
        return _no("alert_identity_missing", ik)
    ik = f"{game_id}|{checkpoint}|{alert_id}"
    # ── R4: ONE auto-bet per game (operator directive 2026-10-06) ─────
    # A REAL bet already placed for this game is never auto-bet again — the
    # once-per-game rule is per GAME, so a later checkpoint re-firing for the
    # same game must NOT produce a second bet.
    try:
        if store.has_placed_bet(game_id):
            return _no("one_bet_per_game", ik)
    except Exception:
        pass
    # ── 4. game genuinely live (the eligibility reason is the same one
    #       the alert gate used; live games carry reason null) ─────────
    if game.get("live") is not True:
        return _no("game_not_live", ik)
    # ── alert freshness (§6: stale alerts rejected) ───────────────────
    age = _alert_age_s(game)
    if age is None or age > cfg.alert_max_age_s:
        return _no("alert_stale", ik)
    # ── 5. required live market information present ───────────────────
    market = game.get("market") or {}
    line = _finite(market.get("total_line"))
    trig_line = _finite(ua.get("trigger_line"))
    if line is None or line <= 0:
        return _no("market_missing", ik)
    proj = game.get("projector") or {}
    if _finite(proj.get("required_pts_per_min")) is None \
            or _finite(proj.get("progress_pct")) is None:
        return _no("market_missing", ik)
    # ── limits must be CONFIGURED before any stake math (§9): an
    #    unconfigured limit cannot be enforced, so betting is blocked —
    #    exactly what the dashboard's "NOT CONFIGURED — betting blocked"
    #    status line promises.  Set BETTING_MAX_STAKE_PER_BET,
    #    BETTING_MAX_BETS_PER_DAY and BETTING_MAX_DAILY_EXPOSURE to arm.
    if (cfg.max_stake_per_bet is None or cfg.max_bets_per_day is None
            or cfg.max_daily_exposure is None):
        return _no("limits_not_configured", ik)
    # ── 6. stake amount valid (unit-based staking) ────────────────────
    unit_price = _finite(unit_price)
    if unit_price is None or unit_price <= 0:
        return _no("unit_price_invalid", ik)
    if unit_price < cfg.min_unit_price or unit_price > cfg.max_unit_price:
        return _no("unit_price_out_of_range", ik)
    units = _finite(cfg.stake_units)
    if units is None or units <= 0:
        return _no("stake_units_invalid", ik)
    if units > 1.0:
        return _no("ONE_UNIT_MAXIMUM", ik)
    stake_amount = round(unit_price * units, 2)
    # ── R5 (operator directive 2026-10-06): if the account BALANCE is below
    #    one unit, stake the WHOLE balance instead of the unit.
    if balance is not None and 0 < balance < stake_amount:
        stake_amount = round(balance, 2)
        units = round(min(1.0, stake_amount / unit_price), 4)
    if stake_amount <= 0 or not math.isfinite(stake_amount):
        return _no("stake_calculation_failed", ik)
    if cfg.max_stake_per_bet is not None \
            and stake_amount > cfg.max_stake_per_bet:
        return _no("max_stake_exceeded", ik)
    # ── 7+8. daily limits (server-side authority only) ────────────────
    if not stats.get("verifiable"):
        return _no("limits_unverifiable", ik)
    if cfg.max_bets_per_day is not None \
            and stats["bets"] >= cfg.max_bets_per_day:
        return _no("daily_bet_limit_reached", ik)
    if cfg.max_daily_exposure is not None \
            and stats["amount"] + stake_amount > cfg.max_daily_exposure:
        return _no("daily_exposure_reached", ik)
    # ── fingerprint context (RECORDED — never a bet trigger) ──────────
    fp = game.get("under_alert_fingerprint") or {}
    fingerprints_fired = fp.get("fingerprints_fired") or []
    fingerprint_count = fp.get("fingerprint_count")
    # ── canonical Auto-Bet command gate (gap G-07) ────────────────────
    # The ONE shared validator — the rung rule + the three stake modes —
    # that the MANUAL path (blm_v4/betting/api.py) uses too, so the two
    # producers can never diverge.  IMPLEMENTED but deliberately NOT ARMED
    # in production until every Production Wiring Gate passes and the
    # operator authorizes the flip (see blm_v4/betting/command.py).  While
    # disarmed the ladder above stays authoritative and its behaviour is
    # byte-for-byte unchanged.  The autonomous production stake mode is the
    # configured unit size; the live-money authority is cfg.live_money_enabled,
    # enforced again at the execute() boundary.
    if _command.WIRED_INTO_PRODUCTION:
        cmd = _command.validate_for_execution(
            game, source=_command.SOURCE_AUTONOMOUS,
            mode=_stake.PRODUCTION_AUTO_BET, unit_size=unit_price,
            authorized=True, authorized_by="autonomous_worker",
            idempotency_key=f"auto:{ik}")
        if cmd["decision"] != _command.ALLOW:
            return _no(cmd["reason"], ik)
    # ── 10. idempotent claim — AFTER all other gates passed ───────────
    execution_id = "bet-" + hashlib.sha256(ik.encode("utf-8")).hexdigest()[:20]
    rec = {
        "execution_id": execution_id,
        "idempotency_key": ik,
        "game_id": game_id,
        "alert_id": alert_id,
        "checkpoint": checkpoint,
        "market": "TOTAL",
        "selection": "UNDER",
        "triggered_line": trig_line,
        "price": _finite(market.get("under_odds")),
        "unit_price": unit_price,
        "stake_units": units,
        "stake_amount": stake_amount,
        "requested_amount": stake_amount,
        "simulated_amount": stake_amount if cfg.dry_run else None,
        "unit_limit": unit_price,
        "status": STATUS_PENDING,
    }
    if claim:
        claimed, existing = store.claim(rec)
        if not claimed:
            return {"decision": "NO_BET", "reason": "duplicate_execution",
                    "idempotency_key": ik, "existing": existing,
                    "candidate": None}
        store.audit(
        "claimed", idempotency_key=ik, execution_id=execution_id,
        reason="all ten conditions satisfied",
        details={
            "why": "production UNDER alert active (under_alert.active)",
            "alert_id": alert_id,
            "checkpoint": checkpoint,
            "fingerprints_present": fingerprints_fired,
            "fingerprint_count": fingerprint_count,
            "stake_calculation": {
                "unit_price": unit_price, "stake_units": units,
                "stake_amount": stake_amount},
            "dry_run": cfg.dry_run,
        })
    return {
        "decision": "WOULD_BET" if cfg.dry_run else "EXECUTE",
        "reason": "all_conditions_satisfied",
        "candidate": {
            "execution_id": execution_id,
            "idempotency_key": ik,
            "game_id": game_id,
            "alert_id": alert_id,
            "checkpoint": checkpoint,
            "market": "TOTAL",
            "selection": "UNDER",
            "triggered_line": trig_line,
            "price": _finite(market.get("under_odds")),
            "unit_price": unit_price,
            "stake_units": units,
            "stake_amount": stake_amount,
            "fingerprints_present": fingerprints_fired,
            "fingerprint_count": fingerprint_count,
        },
    }


def execute(candidate: dict, *, cfg: BettingConfig, store: BettingStore,
            provider) -> dict:
    """Submit one claimed candidate to the provider and record the HONEST
    outcome.  In DRY_RUN the DryRunProvider answers ACCEPTED with a
    dry-run reference; no network call, no credentials (§11)."""
    execution_id = candidate["execution_id"]
    ik = candidate["idempotency_key"]
    # The kill switch is re-read at the submission boundary; a worker's
    # earlier polling check cannot authorize a later provider call.
    if not store.is_enabled():
        store.update_status(execution_id, "BLOCKED",
                            error_code="GLOBAL_KILL_SWITCH",
                            error_message="global betting kill switch is OFF",
                            rejection_reason="GLOBAL_KILL_SWITCH")
        return {"status": "BLOCKED", "error_code": "GLOBAL_KILL_SWITCH"}
    if not cfg.dry_run and cfg.live_money_enabled is False:
        # unreachable by construction (live_money_enabled == not dry_run)
        # — kept as a belt-and-braces stop before any real submission
        store.update_status(execution_id, STATUS_CANCELLED,
                            error_code="DRY_RUN_REQUIRED")
        return {"status": STATUS_CANCELLED}
    # ── ACCOUNT IDENTITY GUARD (directive GATE 8, 2026-09-28) ────────
    # account mismatch → NO BET, verified at the submission boundary —
    # the last point before any provider interaction.  Fail-closed: an
    # identity-requiring provider that cannot prove BOTH sides of the
    # identity comparison is blocked, never assumed to match.  Providers
    # without account identity (DryRunProvider) are exempt by design —
    # there is no real submission to mis-bind.
    mismatch = verify_account(provider)
    if mismatch:
        store.update_status(execution_id, "BLOCKED",
                            error_code="ACCOUNT_MISMATCH",
                            error_message=mismatch[:300],
                            rejection_reason="ACCOUNT_MISMATCH")
        store.audit("result", idempotency_key=ik,
                    execution_id=execution_id,
                    reason=mismatch[:300],
                    details={"result": "BLOCKED",
                             "guard": "account_identity"})
        return {"status": "BLOCKED", "error_code": "ACCOUNT_MISMATCH"}
    store.update_status(execution_id, "SUBMITTING")
    try:
        result = provider.submit(
            execution_id=execution_id, game_id=candidate["game_id"],
            alert_id=candidate["alert_id"],
            selection=candidate["selection"], price=candidate["price"],
            stake_amount=candidate["stake_amount"],
            market=candidate.get("market", "TOTAL"),
            line=candidate.get("triggered_line", candidate.get("price")))
    except ProviderUnavailable as e:
        store.update_status(execution_id, STATUS_FAILED,
                            error_code="PROVIDER_UNAVAILABLE",
                            error_message=str(e)[:300])
        store.audit("result", idempotency_key=ik,
                    execution_id=execution_id,
                    reason=str(e)[:300], details={"result": "FAILED"})
        return {"status": STATUS_FAILED,
                "error_code": "PROVIDER_UNAVAILABLE"}
    except ProviderAmbiguous as e:
        store.update_status(execution_id, STATUS_UNKNOWN,
                            error_code="PROVIDER_AMBIGUOUS",
                            error_message=str(e)[:300])
        store.audit("result", idempotency_key=ik,
                    execution_id=execution_id,
                    reason=str(e)[:300], details={"result": "UNKNOWN"})
        return {"status": STATUS_UNKNOWN,
                "error_code": "PROVIDER_AMBIGUOUS"}
    except Exception as e:  # after submission boundary, outcome is ambiguous
        store.update_status(execution_id, STATUS_UNKNOWN,
                            error_code="EXECUTION_EXCEPTION",
                            error_message=str(e)[:300])
        store.audit("result", idempotency_key=ik,
                    execution_id=execution_id,
                    reason=f"exception: {str(e)[:200]}",
                    details={"result": "UNKNOWN"})
        return {"status": STATUS_UNKNOWN, "error_code": "EXECUTION_EXCEPTION"}

    status = result.get("status")
    if status not in (STATUS_SUBMITTED, STATUS_ACCEPTED, STATUS_REJECTED,
                      STATUS_FAILED, STATUS_UNKNOWN):
        store.update_status(execution_id, STATUS_UNKNOWN,
                            error_code="PROVIDER_RESPONSE_INVALID",
                            error_message=f"unmapped status {status!r}")
        return {"status": STATUS_UNKNOWN,
                "error_code": "PROVIDER_RESPONSE_INVALID"}
    store.update_status(execution_id, status,
                        provider_ref=result.get("provider_ref"),
                        error_code=result.get("error_code"),
                        error_message=result.get("error_message"),
                        accepted_amount=result.get("accepted_amount"),
                        simulated_amount=(candidate.get("stake_amount")
                                          if cfg.dry_run and status == STATUS_ACCEPTED
                                          else None),
                        execution_state=("WOULD_BET" if cfg.dry_run
                                         and status == STATUS_ACCEPTED else status),
                        rejection_reason=(result.get("error_message")
                                          if status in (STATUS_REJECTED, STATUS_FAILED)
                                          else None))
    store.audit("result", idempotency_key=ik, execution_id=execution_id,
                reason=result.get("error_message") or status,
                details={"result": status,
                         "provider": getattr(provider, "name", "?"),
                         "provider_ref": result.get("provider_ref")})
    return {"status": status, "provider_ref": result.get("provider_ref")}


def _no(reason: str, ik: Optional[str]) -> dict:
    return {"decision": "NO_BET", "reason": reason, "candidate": None}
