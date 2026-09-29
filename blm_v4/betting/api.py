"""The betting API — status + settings, all server-authoritative.

Credentials NEVER appear here: the status payload reports boolean
presence only.  The frontend cannot raise limits, cannot touch the
dry-run mode, and cannot enable auto betting beyond what the server's
own configuration permits — the switch the frontend flips is the
persisted kill switch, and the DRY_RUN authority sits above it.
"""
from __future__ import annotations

import math
import hashlib
from typing import Optional

from fastapi import APIRouter, HTTPException

from blm_v4.betting.config import BettingConfig, credentials_present
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.provider import provider_from_config
from blm_v4.betting.store import BettingStore

router = APIRouter(prefix="/api/v4/betting", tags=["blm-v4-betting"])

_store: Optional[BettingStore] = None
_cfg: Optional[BettingConfig] = None
_live_payload_fn = None


def configure_betting(store: BettingStore, cfg: BettingConfig,
                      live_payload_fn=None) -> None:
    """Wire the process-wide store/config (called from server.py)."""
    global _store, _cfg, _live_payload_fn
    _store = store
    _cfg = cfg
    if live_payload_fn is not None:
        _live_payload_fn = live_payload_fn


def _require() -> tuple[BettingStore, BettingConfig]:
    if _store is None or _cfg is None:
        raise HTTPException(status_code=503,
                            detail="betting layer not configured")
    return _store, _cfg


@router.get("/status")
def betting_status() -> dict:
    """AUTO BETTING switch state, dry-run mode, today's stats and recent
    executions.  Fail-closed: any store error reads as OFF/empty."""
    store, cfg = _require()
    enabled = store.is_enabled()
    unit_price = store.get_unit_price()
    stats = store.today_stats()
    limit = cfg.max_daily_exposure
    remaining = None
    if stats["verifiable"] and limit is not None:
        remaining = max(0.0, round(limit - stats["amount"], 2))

    # In DRY_RUN the stake totals are SIMULATED EXPOSURE — no real money
    # has moved.  The mandate (§8) requires the two to never be mixed.
    # ``amount_real`` is always R0.00 in DRY_RUN; ``amount_simulated``
    # carries the would-be total so the limits engine still operates
    # correctly.  The frontend renders the appropriate label based on
    # ``dry_run``.
    amount_raw = (round(stats["amount"], 2)
                  if stats["amount"] is not None else None)
    today_block = {
        "bets": stats["bets"],
        "units": (round(stats["bets"] * cfg.stake_units, 2)
                  if stats["bets"] is not None else None),
        # amount: the value that matters for limit enforcement (simulated
        # in DRY_RUN, real in LIVE) — the limits engine uses this.
        "amount": amount_raw,
        # explicit split so the frontend can label correctly (§8)
        "amount_real": 0.0 if cfg.dry_run else amount_raw,
        "amount_simulated": amount_raw if cfg.dry_run else 0.0,
        "is_simulated": cfg.dry_run,
        "remaining_exposure": remaining,
    }
    return {
        "enabled": enabled,
        "dry_run": cfg.dry_run,
        "live_money_enabled": cfg.live_money_enabled,
        "mode": "DRY_RUN" if cfg.dry_run else "LIVE",
        "unit_price": unit_price,
        "stake_units": cfg.stake_units,
        "max_stake_units": 1.0,
        "max_stake_per_bet": cfg.max_stake_per_bet,
        "max_bets_per_day": cfg.max_bets_per_day,
        "max_daily_exposure": limit,
        "game_controls": store.game_controls(),
        "today": today_block,
        "credentials": credentials_present(),   # booleans ONLY
        "recent": [_public_execution(r) for r in store.recent(25)],
    }


def _public_execution(r: dict) -> dict:
    """The frontend-safe view of one execution record (§7).  No internal
    error detail beyond the short provider message; no credentials ever
    existed on the record."""
    status = r.get("status")
    return {
        "execution_id": r.get("execution_id"),
        "game_id": r.get("game_id"),
        "alert_id": r.get("alert_id"),
        "checkpoint": r.get("checkpoint"),
        "market": r.get("market"),
        "line": r.get("triggered_line"),
        "selection": r.get("selection"),
        "price": r.get("price"),
        "unit_price": r.get("unit_price"),
        "stake_units": r.get("stake_units"),
        "stake_amount": r.get("stake_amount"),
        "requested_amount": r.get("requested_amount"),
        "accepted_amount": r.get("accepted_amount"),
        "simulated_amount": r.get("simulated_amount"),
        "unit_limit": r.get("unit_limit"),
        "rejection_reason": r.get("rejection_reason"),
        "status": r.get("status"),
        "execution_state": (r.get("execution_state") or status),
        "provider_ref": r.get("provider_ref"),
        "error_code": r.get("error_code"),
        "error_message": r.get("error_message"),
        "requested_at_utc": r.get("requested_at_utc"),
        "submitted_at_utc": r.get("submitted_at_utc"),
        "resolved_at_utc": r.get("resolved_at_utc"),
        "elapsed_ms": r.get("elapsed_ms"),
        "day_utc": r.get("day_utc"),
    }


def _current_games() -> list[dict]:
    if _live_payload_fn is None:
        raise HTTPException(status_code=503,
                            detail="live game authority is unavailable")
    try:
        result = _live_payload_fn() or []
        return result.get("games", []) if isinstance(result, dict) else result
    except Exception:
        raise HTTPException(status_code=503,
                            detail="live game authority is unavailable")


@router.get("/game/{game_id}/state")
def game_betting_state(game_id: str) -> dict:
    """Server-authoritative game eligibility and latest ledger state."""
    store, cfg = _require()
    game = next((g for g in _current_games()
                 if str(g.get("game_id")) == str(game_id)), None)
    if game is None:
        raise HTTPException(status_code=404, detail="canonical game not found")
    enabled = store.is_enabled()
    check = evaluate(game, cfg=cfg, store=store, enabled=enabled,
                     unit_price=store.get_unit_price(),
                     stats=store.today_stats(), claim=False,
                     game_enabled=store.is_game_enabled(game_id))
    latest = store.latest_for_game(game_id)
    reason = check.get("reason")
    if reason == "auto_betting_off":
        reason = "GLOBAL_KILL_SWITCH"
    eligible = check["decision"] != "NO_BET"
    return {
        "game_id": game_id,
        "auto_bet_enabled": enabled,
        "game_auto_bet_enabled": store.is_game_enabled(game_id),
        "eligible": eligible,
        "blocked_reason": None if eligible else reason,
        "decision": check["decision"],
        "state": (latest or {}).get("execution_state") or
                 (latest or {}).get("status") or
                 ("ARMED" if enabled and eligible else "BLOCKED" if not eligible else "IDLE"),
        "latest_execution": _public_execution(latest) if latest else None,
        "reconciliation_state": ((latest or {}).get("status")
                                 if latest and latest.get("status") in
                                 ("UNKNOWN", "RECONCILING", "EXPIRED")
                                 else "NOT_REQUIRED"),
    }


@router.post("/game/{game_id}/auto-bet")
def set_game_auto_bet(game_id: str, payload: dict) -> dict:
    """Set a per-game preference; global kill switch remains authoritative."""
    store, _cfg = _require()
    if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
        raise HTTPException(status_code=400, detail="enabled must be boolean")
    if not any(str(g.get("game_id")) == str(game_id)
               for g in _current_games()):
        raise HTTPException(status_code=404, detail="canonical game not found")
    store.set_game_enabled(game_id, payload["enabled"])
    return {"game_id": game_id,
            "game_auto_bet_enabled": store.is_game_enabled(game_id),
            "global_auto_bet_enabled": store.is_enabled()}


@router.get("/history")
def betting_history(q: str = "", status: str = "", game_id: str = "",
                   limit: int = 100) -> dict:
    """Searchable execution ledger and audit trail; credential-free only."""
    store, _cfg = _require()
    executions = store.history(query=q, status=status, game_id=game_id,
                               limit=limit)
    execution_ids = [r["execution_id"] for r in executions]
    audit = []
    for execution_id in execution_ids:
        audit.extend(store.audit_recent(execution_id=execution_id, limit=20))
    audit.sort(key=lambda r: (r.get("at_utc") or "", r.get("id", 0)),
               reverse=True)
    return {
        "executions": [_public_execution(r) for r in executions],
        "audit": audit[:max(1, min(int(limit), 500))],
        "limit": max(1, min(int(limit), 500)),
    }


@router.post("/manual")
def manual_bet(payload: dict) -> dict:
    """Place one validated manual Total bet through the same ledger/provider.

    The caller supplies a canonical event ID and idempotency key. Stake is
    a currency amount and is capped at the server-configured unit price.
    """
    store, cfg = _require()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid request")
    game_id = payload.get("game_id")
    market = payload.get("market")
    direction = payload.get("direction")
    key = payload.get("idempotency_key")
    if not isinstance(game_id, str) or not game_id.strip():
        raise HTTPException(status_code=400, detail="canonical game_id required")
    if market != "TOTAL":
        raise HTTPException(status_code=400, detail="market must be TOTAL")
    if direction not in ("OVER", "UNDER"):
        raise HTTPException(status_code=400, detail="direction must be OVER or UNDER")
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise HTTPException(status_code=400, detail="valid idempotency_key required")
    if isinstance(payload.get("line"), bool) or isinstance(payload.get("stake"), bool):
        raise HTTPException(status_code=400, detail="line and stake must be numeric")
    try:
        line = float(payload.get("line"))
        stake = float(payload.get("stake"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="line and stake must be numeric")
    if not math.isfinite(line) or line <= 0:
        raise HTTPException(status_code=400, detail="invalid total line")
    if not math.isfinite(stake) or stake <= 0:
        raise HTTPException(status_code=400, detail="invalid stake")
    unit = store.get_unit_price()
    if unit is None or unit <= 0:
        raise HTTPException(status_code=409, detail="configured unit is unavailable")
    if unit < cfg.min_unit_price or unit > cfg.max_unit_price:
        raise HTTPException(status_code=409, detail="configured unit is outside the server limit")
    if stake > unit:
        raise HTTPException(status_code=400, detail="stake exceeds one configured unit")
    units = stake / unit
    if units > 1.0:
        raise HTTPException(status_code=400, detail="ONE_UNIT_MAXIMUM")

    # Resolve a prior logical request before checking current market
    # freshness: a lost HTTP response must return its ledger outcome, never
    # create a second wager or turn an UNKNOWN into a resubmission.
    idem = "manual:" + key
    existing = store.get_by_idempotency(idem)
    if existing:
        same = (existing.get("game_id") == game_id
                and existing.get("selection") == direction
                and existing.get("market") == market
                and math.isclose(float(existing.get("price") or 0), line,
                                 abs_tol=0.0001)
                and math.isclose(float(existing.get("stake_amount") or 0), stake,
                                 abs_tol=0.0001))
        if not same:
            raise HTTPException(status_code=409,
                                detail="idempotency key reused for a different wager")
        return {"execution": _public_execution(existing), "duplicate": True}

    game = next((g for g in _current_games()
                 if str(g.get("game_id")) == game_id), None)
    if game is None:
        raise HTTPException(status_code=404, detail="canonical game not found")
    proj, observed = game.get("projector") or {}, game.get("market") or {}
    live_line = proj.get("live_total_line")
    if live_line is None:
        live_line = observed.get("total_line")
    freshness = proj.get("market_status") or observed.get("market_status")
    if game.get("live") is not True or freshness != "LIVE" or live_line is None:
        raise HTTPException(status_code=409, detail="live Total market unavailable or expired")
    if not math.isclose(float(live_line), line, rel_tol=0.0, abs_tol=0.0001):
        raise HTTPException(status_code=409, detail="requested line is not the current observed Total")

    execution_id = "bet-" + hashlib.sha256(idem.encode("utf-8")).hexdigest()[:20]
    alert_id = f"manual:{key}"
    rec = {
        "execution_id": execution_id, "idempotency_key": idem,
        "game_id": game_id, "alert_id": alert_id, "checkpoint": "MANUAL",
        "market": market, "selection": direction, "triggered_line": line,
        "price": line, "unit_price": unit, "stake_units": units,
        "stake_amount": stake, "requested_amount": stake,
        "unit_limit": unit, "simulated_amount": stake if cfg.dry_run else None,
        "status": "PENDING",
    }
    claimed, _ = store.claim(rec)
    if not claimed:
        existing = store.get_by_idempotency(idem)
        if not (existing and existing.get("game_id") == game_id
                and existing.get("selection") == direction
                and existing.get("market") == market
                and math.isclose(float(existing.get("price") or 0), line,
                                 abs_tol=0.0001)
                and math.isclose(float(existing.get("stake_amount") or 0), stake,
                                 abs_tol=0.0001)):
            raise HTTPException(status_code=409,
                                detail="idempotency key reused for a different wager")
        return {"execution": _public_execution(existing), "duplicate": True}
    if not store.is_enabled():
        store.update_status(execution_id, "BLOCKED",
                            error_code="GLOBAL_KILL_SWITCH",
                            error_message="global betting kill switch is OFF",
                            rejection_reason="GLOBAL_KILL_SWITCH")
        blocked = store.get_execution(execution_id)
        return {"execution": _public_execution(blocked), "duplicate": False}
    candidate = {**rec, "idempotency_key": idem}
    outcome = execute(candidate, cfg=cfg, store=store,
                      provider=provider_from_config(cfg))
    return {"execution": _public_execution(store.get_execution(execution_id)),
            "duplicate": False, "result": outcome}


@router.post("/settings")
def betting_settings(payload: dict) -> dict:
    """Update frontend-settable settings: the kill switch and unit price.

    Server-side validation only (§6) — the frontend can never set a
    value outside the configured bounds, and can never touch limits or
    dry-run mode.
    """
    store, cfg = _require()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    if "auto_betting" in payload:
        wanted = payload["auto_betting"]
        if wanted not in ("ON", "OFF", True, False):
            raise HTTPException(status_code=400,
                                detail="auto_betting must be ON or OFF")
        enable = wanted in ("ON", True)
        if enable:
            # enabling requires a valid unit price to already be set —
            # a switch with no stake semantics must not arm (§9)
            if store.get_unit_price() is None:
                raise HTTPException(
                    status_code=400,
                    detail="set a valid unit price before enabling")
        store.set_config("auto_betting_enabled", "true" if enable
                         else "false")
        store.audit("kill_switch", reason="frontend",
                    details={"enabled": enable, "dry_run": cfg.dry_run})

    if "unit_price" in payload:
        raw = payload["unit_price"]
        # reject bools, strings-with-garbage, NaN/inf, zero, negatives,
        # and out-of-range values — everything malformed (§6)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HTTPException(status_code=400,
                                detail="unit_price must be a number")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="unit_price must be a number")
        if not math.isfinite(v) or v <= 0:
            raise HTTPException(status_code=400,
                                detail="unit_price must be positive")
        if v < cfg.min_unit_price or v > cfg.max_unit_price:
            raise HTTPException(
                status_code=400,
                detail=f"unit_price outside permitted range "
                       f"[{cfg.min_unit_price}, {cfg.max_unit_price}]")
        store.set_unit_price(round(v, 2))
        store.audit("unit_price", details={"unit_price": round(v, 2)})

    return betting_status()
