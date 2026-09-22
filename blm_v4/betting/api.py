"""The betting API — status + settings, all server-authoritative.

Credentials NEVER appear here: the status payload reports boolean
presence only.  The frontend cannot raise limits, cannot touch the
dry-run mode, and cannot enable auto betting beyond what the server's
own configuration permits — the switch the frontend flips is the
persisted kill switch, and the DRY_RUN authority sits above it.
"""
from __future__ import annotations

import math
from typing import Optional

from fastapi import APIRouter, HTTPException

from blm_v4.betting.config import BettingConfig, credentials_present
from blm_v4.betting.store import BettingStore

router = APIRouter(prefix="/api/v4/betting", tags=["blm-v4-betting"])

_store: Optional[BettingStore] = None
_cfg: Optional[BettingConfig] = None


def configure_betting(store: BettingStore, cfg: BettingConfig) -> None:
    """Wire the process-wide store/config (called from server.py)."""
    global _store, _cfg
    _store = store
    _cfg = cfg


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
    return {
        "enabled": enabled,
        "dry_run": cfg.dry_run,
        "live_money_enabled": cfg.live_money_enabled,
        "mode": "DRY_RUN" if cfg.dry_run else "LIVE",
        "unit_price": unit_price,
        "stake_units": cfg.stake_units,
        "max_stake_per_bet": cfg.max_stake_per_bet,
        "max_bets_per_day": cfg.max_bets_per_day,
        "max_daily_exposure": limit,
        "today": {
            "bets": stats["bets"],
            "units": (round(stats["bets"] * cfg.stake_units, 2)
                      if stats["bets"] is not None else None),
            "amount": (round(stats["amount"], 2)
                       if stats["amount"] is not None else None),
            "remaining_exposure": remaining,
        },
        "credentials": credentials_present(),   # booleans ONLY
        "recent": [_public_execution(r) for r in store.recent(25)],
    }


def _public_execution(r: dict) -> dict:
    """The frontend-safe view of one execution record (§7).  No internal
    error detail beyond the short provider message; no credentials ever
    existed on the record."""
    return {
        "execution_id": r.get("execution_id"),
        "game_id": r.get("game_id"),
        "alert_id": r.get("alert_id"),
        "checkpoint": r.get("checkpoint"),
        "selection": r.get("selection"),
        "price": r.get("price"),
        "unit_price": r.get("unit_price"),
        "stake_units": r.get("stake_units"),
        "stake_amount": r.get("stake_amount"),
        "status": r.get("status"),
        "error_code": r.get("error_code"),
        "error_message": r.get("error_message"),
        "requested_at_utc": r.get("requested_at_utc"),
        "day_utc": r.get("day_utc"),
    }


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
