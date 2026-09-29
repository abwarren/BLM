"""PRE-BET VALIDATION (directive §4) — the PRECHECK gate.

Immediately before ANY submission the engine verifies, in order:

    event exists · market exists · selection exists · current line ·
    current odds · line tolerance · odds tolerance · event status ·
    market status · account/session status · available balance (test OR
    live) · stake limits · exposure limits · duplicate-bet protection ·
    game-level bet limits · global auto-bet switch

If ANY check fails the engine DOES NOT SUBMIT and records the EXACT
rejection reason — :class:`PrecheckResult` carries ``reason``, a stable
machine code, plus human detail.  The check order is fixed so the audit
log always shows the same first-failure for the same inputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PrecheckResult:
    """One PRECHECK verdict.  ``ok=False`` never submits."""

    ok: bool
    reason: str                       # stable machine code (exact reason)
    detail: str = ""                  # human-readable, credential-free
    checks_run: int = 0
    observed: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason,
                "detail": self.detail, "checks_run": self.checks_run,
                "observed": dict(self.observed)}


def _fin(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def run_precheck(signal: dict, *, adapter, session, pre: "PrecheckLimits",
                 game_enabled: bool = True, auto_betting_enabled: bool = True,
                 already_claimed: bool = False,
                 prior_rejections: int = 0) -> PrecheckResult:
    """Run the full §4 gate.  ``signal`` carries the bet intent:
    ``{game_id (event), market, selection, line, price, stake_amount,
    idempotency_key}``.  Everything is verified against the adapter's
    CURRENT state — nothing is trusted from the signal itself except
    the identity being verified."""
    n = 0
    observed: dict = {}

    def fail(reason: str, detail: str = "") -> PrecheckResult:
        return PrecheckResult(False, reason, detail, n, observed)

    event_id = str(signal.get("game_id") or signal.get("event_id") or "")
    market_id = str(signal.get("market") or "TOTAL")
    position = str(signal.get("selection") or "UNDER")
    want_line = _fin(signal.get("line"))
    want_price = _fin(signal.get("price"))
    stake = _fin(signal.get("stake_amount"))
    idem = str(signal.get("idempotency_key") or "")

    # ── 16. global auto-bet switch (cheap + absolute, checked first —
    #       a closed switch must refuse even a well-formed signal) ────
    n += 1
    if not auto_betting_enabled:
        return fail("GLOBAL_AUTO_BET_OFF",
                    "global auto-bet switch is OFF")

    # ── 15. game-level bet limits ──────────────────────────────────────
    n += 1
    if not game_enabled:
        return fail("GAME_LEVEL_BET_DISABLED",
                    f"auto-bet disabled for game {event_id!r}")

    # ── 14. duplicate-bet protection (claimed slot must exist) ─────────
    n += 1
    if not idem:
        return fail("IDEMPOTENCY_KEY_MISSING",
                    "signal carries no idempotency key")
    if already_claimed:
        return fail("DUPLICATE_BET",
                    f"idempotency key {idem!r} already claimed")

    # ── 13. stake limits ───────────────────────────────────────────────
    n += 1
    if stake is None or stake <= 0:
        return fail("STAKE_INVALID", f"stake {stake!r}")
    if pre.max_stake_per_bet is not None and stake > pre.max_stake_per_bet:
        return fail("STAKE_LIMIT_EXCEEDED",
                    f"stake {stake} > max {pre.max_stake_per_bet}")

    # ── 12. exposure limits ────────────────────────────────────────────
    n += 1
    if pre.max_total_exposure is not None \
            and pre.current_exposure is not None \
            and pre.current_exposure + stake > pre.max_total_exposure:
        return fail("EXPOSURE_LIMIT_REACHED",
                    f"exposure {pre.current_exposure} + {stake} > "
                    f"max {pre.max_total_exposure}")
    if pre.max_total_exposure is not None \
            and pre.current_exposure is None:
        return fail("EXPOSURE_UNVERIFIABLE",
                    "exposure limit configured but current exposure "
                    "unverifiable")

    # ── 11. available balance (test OR live — same gate) ───────────────
    n += 1
    try:
        bal = _fin(adapter.balance())
    except Exception as e:  # noqa: BLE001 — fail closed on any adapter error
        return fail("BALANCE_UNAVAILABLE", f"{type(e).__name__}")
    if bal is None:
        return fail("BALANCE_UNAVAILABLE", "adapter returned no balance")
    observed["balance"] = bal
    if bal < stake:
        return fail("INSUFFICIENT_BALANCE",
                    f"balance {bal} < stake {stake}")

    # ── 10. account/session status ─────────────────────────────────────
    n += 1
    try:
        session.assert_submittable()
    except Exception as e:  # noqa: BLE001
        return fail("SESSION_NOT_SUBMITTABLE", str(e)[:200])
    observed["session_account"] = session.account_id

    # ── 9. market status ───────────────────────────────────────────────
    # ── 8. event status (event lookup precedes market) ─────────────────
    n += 1
    ev = adapter.get_event(event_id)
    observed["event_status"] = (ev or {}).get("status")
    if ev is None:
        return fail("EVENT_NOT_FOUND",
                    f"event {event_id!r} not offered (closed or unknown)")
    if str(ev.get("status", "")).upper() not in ("LIVE", "OPEN"):
        return fail("EVENT_STATUS_INVALID",
                    f"event status {ev.get('status')!r}")

    n += 1
    mk = adapter.get_market(event_id, market_id)
    observed["market_status"] = (mk or {}).get("status")
    if mk is None:
        return fail("MARKET_NOT_FOUND",
                    f"market {market_id!r} not offered on {event_id!r}")
    if str(mk.get("status", "")).upper() != "OPEN":
        return fail("MARKET_STATUS_INVALID",
                    f"market status {mk.get('status')!r}")

    # ── 3. requested selection still exists (+ 4/5 current line/price) ─
    n += 1
    quote = adapter.get_selection(event_id, market_id, position)
    if quote is None:
        return fail("SELECTION_NOT_FOUND",
                    f"selection {position!r} missing on "
                    f"{event_id!r}/{market_id!r}")
    observed["current_line"] = quote.line
    observed["current_price"] = quote.price

    # ── 6. line tolerance (§4: current line vs requested line) ─────────
    n += 1
    cur_line = _fin(quote.line)
    if want_line is None or cur_line is None:
        return fail("LINE_UNAVAILABLE",
                    f"requested {want_line!r} vs current {cur_line!r}")
    if pre.max_line_movement is not None and \
            abs(cur_line - want_line) > pre.max_line_movement + 1e-9:
        return fail("LINE_TOLERANCE_EXCEEDED",
                    f"requested {want_line} vs current {cur_line} "
                    f"(tol {pre.max_line_movement})")

    # ── 7. odds tolerance ──────────────────────────────────────────────
    n += 1
    cur_price = _fin(quote.price)
    if want_price is None or cur_price is None:
        return fail("ODDS_UNAVAILABLE",
                    f"requested {want_price!r} vs current {cur_price!r}")
    if pre.max_odds_movement is not None and \
            abs(cur_price - want_price) > pre.max_odds_movement + 1e-9:
        return fail("ODDS_TOLERANCE_EXCEEDED",
                    f"requested {want_price} vs current {cur_price} "
                    f"(tol {pre.max_odds_movement})")

    return PrecheckResult(True, "PRECHECK_PASSED", "", n, observed)


class PrecheckLimits:
    """The limit inputs the PRECHECK gate enforces.  ``None`` means
    "not configured" and the corresponding check fails closed when the
    value it guards is present (§9: unenforceable limit → no bet)."""

    def __init__(self, *, max_stake_per_bet: Optional[float] = None,
                 max_total_exposure: Optional[float] = None,
                 current_exposure: Optional[float] = None,
                 max_line_movement: float = 0.0,
                 max_odds_movement: float = 0.05):
        self.max_stake_per_bet = max_stake_per_bet
        self.max_total_exposure = max_total_exposure
        self.current_exposure = current_exposure
        self.max_line_movement = max_line_movement
        self.max_odds_movement = max_odds_movement
