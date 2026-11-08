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
from blm_v4.betting import rung as rung_rule
from blm_v4.betting.store import BettingStore

#: Decimal odds bounds for a real selection.  A rendered number outside
#: this range is not an odds price (it is a line, a stake or noise) and is
#: refused rather than recorded as the bet's price.
MIN_ODDS = 1.01
MAX_ODDS = 1000.0

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


def _candidate_game(candidate: dict) -> dict:
    """Reconstruct the execution-relevant game payload from a claimed
    candidate.  Used only when the caller cannot supply a FRESH payload;
    the live path always passes the freshly re-read one."""
    return {
        "game_id": candidate.get("game_id"),
        "live": True,
        "under_alert": {
            "active": True,
            "checkpoint": candidate.get("checkpoint"),
            "alert_id": candidate.get("alert_id"),
            "trigger_line": candidate.get("triggered_line"),
            "trigger_captured_at": candidate.get("alert_captured_at"),
        },
        "market": {
            "total_line": candidate.get("current_line"),
            "observed_lines": candidate.get("observed_lines"),
            "market_status": candidate.get("market_status"),
            "under_odds": candidate.get("price"),
        },
        "projector": {"captured_at": candidate.get("alert_captured_at")},
    }


def _revalidate_at_submission(candidate: dict, game: dict,
                              cfg: BettingConfig, store: BettingStore
                              ) -> Optional[str]:
    """MANDATORY submission-time revalidation (§Phase Five).

    Returns ``None`` when the bet may proceed, else a stable refusal code.
    Every gate is re-read NOW — a candidate that qualified a moment ago is
    not automatically eligible.
    """
    execution_id = candidate["execution_id"]
    game_id = str(candidate.get("game_id") or "")
    # game identity must still be the claimed game
    if str(game.get("game_id") or "") != game_id or not game_id:
        return "GAME_IDENTITY_CHANGED"
    # A MANUAL command is not an alert-driven Auto-Bet: it has no frozen
    # trigger line and no rung strategy.  It still passes the duplicate,
    # limit, unit-price and stake gates below.  (The API route has already
    # verified the line against the CURRENT observed total.)
    manual = (str(candidate.get("checkpoint") or "").upper() == "MANUAL"
              or str(candidate.get("alert_id") or "").startswith("manual:"))
    if not manual:
        # the canonical rung + identity validator, on the CURRENT payload
        check = rung_rule.autonomous_execution_command(
            game, market=str(candidate.get("market") or "TOTAL"),
            selection=str(candidate.get("selection") or "UNDER"))
        if check.get("decision") != rung_rule.ALLOW:
            return "RUNG_REJECTED:" + str(check.get("reason"))
        if _finite(check.get("trigger_line")) != _finite(
                candidate.get("triggered_line")):
            return "TRIGGER_LINE_CHANGED"
        # freshness of the observation the alert verdict came from
        age = _alert_age_s(game)
        if age is None or age > cfg.alert_max_age_s:
            return "ALERT_STALE_AT_SUBMISSION"
        market = game.get("market") or {}
        if str(market.get("market_status") or "").upper() not in (
                "LIVE", "OPEN"):
            return "MARKET_NOT_ACTIVE"
        # the selection's CURRENT line and odds must match what was claimed
        if _finite(check.get("current_line")) != _finite(
                candidate.get("current_line")):
            return "LINE_CHANGED"
        odds = _finite(market.get("under_odds"))
        if odds is None or not (MIN_ODDS <= odds <= MAX_ODDS):
            return "ODDS_UNAVAILABLE"
        if abs(odds - float(candidate.get("price"))) > 1e-9:
            return "ODDS_CHANGED"
    # duplicate execution status (another attempt for this game?)
    try:
        holder = store.has_blocking_execution(
            game_id, exclude_execution_id=execution_id)
    except Exception:  # noqa: BLE001 — unverifiable duplicate guard
        return "DUPLICATE_CHECK_UNAVAILABLE"
    if holder is not None:
        return "DUPLICATE_GAME_EXECUTION"
    # financial limits, re-read at the boundary.  The claimed row is now
    # PENDING, so its own stake/count must be excluded from "existing".
    stats = store.today_stats()
    if not stats.get("verifiable"):
        return "LIMITS_UNVERIFIABLE"
    own = store.get_execution(execution_id) or {}
    own_counted = 1 if (own.get("day_utc") == stats.get("day")
                        and own.get("status") in (
                            "PENDING", "WOULD_BET", "SUBMITTING",
                            "SUBMITTED", "ACCEPTED", "UNKNOWN",
                            "RECONCILING")) else 0
    own_amount = float(own.get("stake_amount") or 0.0) if own_counted else 0.0
    stake = float(candidate.get("stake_amount") or 0.0)
    if stake <= 0:
        return "STAKE_INVALID"
    # UNIT PRICE CONSISTENCY: when the ledger has a persisted price it is
    # the authority and must equal the price the candidate was built from.
    # (The worker always passes the store's own value.)  A caller that has
    # not persisted one at all is a direct/simulated invocation; the
    # candidate's already-validated price stands.
    unit = store.get_unit_price()
    if unit is None:
        unit = _finite(candidate.get("unit_price"))
    if unit is None or abs(
            float(unit) - float(candidate.get("unit_price"))) > 1e-9:
        return "UNIT_PRICE_CHANGED"
    if abs(stake - round(float(unit) * float(cfg.stake_units), 2)) > 0.005:
        return "STAKE_CALCULATION_MISMATCH"
    if cfg.max_stake_per_bet is not None \
            and stake > cfg.max_stake_per_bet:
        return "MAX_STAKE_EXCEEDED"
    if cfg.max_bets_per_day is not None \
            and stats["bets"] - own_counted >= cfg.max_bets_per_day:
        return "DAILY_BET_LIMIT_REACHED"
    if cfg.max_daily_exposure is not None \
            and (stats["amount"] - own_amount) + stake \
            > cfg.max_daily_exposure:
        return "DAILY_EXPOSURE_REACHED"
    return None


def evaluate(game: dict, *, cfg: BettingConfig, store: BettingStore,
             enabled: bool, unit_price: Optional[float],
             stats: dict, claim: bool = True,
             game_enabled: bool = True) -> dict:
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
    # the bookmaker market must be genuinely open — a suspended/closed
    # market carries no selectable outcome.
    if str(market.get("market_status") or "").upper() not in (
            "LIVE", "OPEN"):
        return _no("market_not_active", ik)
    # ODDS ARE NOT LINES (§Phase Two).  The bet's price is the rendered
    # UNDER decimal price; the line is a point value.  Missing/out-of-range
    # odds block — the line is never substituted for the price.
    odds = _finite(market.get("under_odds"))
    if odds is None:
        return _no("odds_missing", ik)
    if not (MIN_ODDS <= odds <= MAX_ODDS):
        return _no("odds_invalid", ik)
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
    # ── RUNG VALIDATION (§Phase Three / PW-04) — the canonical, shared
    #    pre-execution validator, called on the AUTONOMOUS path.  The
    #    original trigger line is the frozen alert line; the current line
    #    must be within one rung DOWN of it.  Unprovable inputs reject.
    rung_check = rung_rule.autonomous_execution_command(
        game, market="TOTAL", selection="UNDER")
    if rung_check.get("decision") != rung_rule.ALLOW:
        store.audit("rung_reject", idempotency_key=ik,
                    reason=str(rung_check.get("reason"))[:200],
                    details=rung_check)
        return _no("rung_rejected:" + str(rung_check.get("reason")), ik)
    # the trigger line is IMMUTABLE — never the current bookmaker total
    if _finite(rung_check.get("trigger_line")) != trig_line:
        return _no("trigger_line_mutated", ik)
    # ── fingerprint context (RECORDED — never a bet trigger) ──────────
    fp = game.get("under_alert_fingerprint") or {}
    fingerprints_fired = fp.get("fingerprints_fired") or []
    fingerprint_count = fp.get("fingerprint_count")
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
        "price": odds,
        "unit_price": unit_price,
        "stake_units": units,
        "stake_amount": stake_amount,
        "requested_amount": stake_amount,
        "simulated_amount": stake_amount if cfg.dry_run else None,
        "unit_limit": unit_price,
        "status": STATUS_PENDING,
        # snapshot for submission-time revalidation + the audit record
        "current_line": line,
        "observed_lines": list(market.get("observed_lines") or []),
        "rung_size": rung_check.get("rung_size"),
        "rungs_moved": rung_check.get("rungs_moved"),
        "market_status": market.get("market_status"),
        "alert_captured_at": (game.get("projector") or {}).get("captured_at"),
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
            "line_and_odds": {
                "trigger_line": trig_line, "current_line": line,
                "under_odds": odds,
                "rung_size": rung_check.get("rung_size"),
                "rungs_moved": rung_check.get("rungs_moved")},
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
            "current_line": line,
            "price": odds,
            "unit_price": unit_price,
            "stake_units": units,
            "stake_amount": stake_amount,
            "observed_lines": list(market.get("observed_lines") or []),
            "rung_size": rung_check.get("rung_size"),
            "rungs_moved": rung_check.get("rungs_moved"),
            "market_status": market.get("market_status"),
            "alert_captured_at": (game.get("projector") or {}).get("captured_at"),
            "fingerprints_present": fingerprints_fired,
            "fingerprint_count": fingerprint_count,
        },
    }


def execute(candidate: dict, *, cfg: BettingConfig, store: BettingStore,
            provider, game: Optional[dict] = None) -> dict:
    """Submit one claimed candidate to the provider and record the HONEST
    outcome.  In DRY_RUN the DryRunProvider answers ACCEPTED with a
    dry-run reference; no network call, no credentials (§11).

    ``game`` is the FRESH live payload re-read immediately before
    submission.  When omitted (direct/simulated callers) the claimed
    candidate's own snapshot is revalidated instead; the freshness gate
    still applies, so an old candidate is refused.
    """
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
    # ── MANDATORY submission-time revalidation (§Phase Five) ──────────
    fresh = game if game is not None else _candidate_game(candidate)
    refusal = _revalidate_at_submission(candidate, fresh, cfg, store)
    if refusal:
        store.update_status(execution_id, "BLOCKED",
                            error_code=refusal.split(":", 1)[0],
                            error_message=refusal[:300],
                            rejection_reason=refusal.split(":", 1)[0])
        store.audit("revalidation", idempotency_key=ik,
                    execution_id=execution_id, reason=refusal[:300],
                    details={"result": "BLOCKED",
                             "source": ("fresh_payload" if game is not None
                                        else "candidate_snapshot")})
        return {"status": "BLOCKED",
                "error_code": refusal.split(":", 1)[0]}
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
    # An ACCEPTED claim without the bookmaker's own reference is not a
    # confirmed bet: keep it UNKNOWN so reconciliation (never a retry)
    # establishes the truth.
    if status == STATUS_ACCEPTED and not result.get("provider_ref"):
        store.update_status(execution_id, STATUS_UNKNOWN,
                            error_code="CONFIRMATION_REFERENCE_MISSING",
                            error_message="provider reported ACCEPTED "
                                          "without a confirmation reference")
        store.audit("result", idempotency_key=ik,
                    execution_id=execution_id,
                    reason="ACCEPTED without a bookmaker reference",
                    details={"result": "UNKNOWN"})
        return {"status": STATUS_UNKNOWN,
                "error_code": "CONFIRMATION_REFERENCE_MISSING"}
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
    return {"status": status, "provider_ref": result.get("provider_ref"),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message")}


def _no(reason: str, ik: Optional[str]) -> dict:
    return {"decision": "NO_BET", "reason": reason, "candidate": None}
