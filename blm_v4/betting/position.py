"""Auto-Bet POSITION — the canonical bettable opportunity (directive 2026-10-05).

Three populations are deliberately SEPARATED:

    SIGNAL    a BLM UNDER alert (a ``checkpoint_market`` row / the served
              ``under_alert`` block).  A raw signal is NEVER a bet and NEVER
              contributes to ROI.
    POSITION  an opportunity that PASSED the Auto-Bet eligibility gate, at a
              concrete executable line + price.  ONE per opportunity.
    BET       an execution attempt on a position (SUBMITTING → … ).

WHERE A POSITION IS CREATED (the directive's key question)
----------------------------------------------------------
A position is created at exactly ONE place: the moment the shared Auto-Bet
command decision is ALLOW (``blm_v4.betting.command.build_command`` →
``rung.validate_execution`` ALLOW **and** ``stake.resolve_stake`` ok) — or, in
the autonomous ladder, the equivalent ALLOW of ``executor.evaluate`` before its
idempotent ``store.claim``.  Both producers converge on
:func:`build_position`; an alert that FAILED eligibility is never a position.

The executable LINE + PRICE come from the LIVE market read at position time
(the resolver's ``MarketObservation``: ``line`` + ``price`` odds) — never a
later observation and never the settlement line.

CANONICAL IDEMPOTENCY KEY
-------------------------
The OPPORTUNITY identity — ``game_id + market_id + direction + checkpoint``
(+ the alert id) — NEVER a timestamp, price or display string, and — unlike the
command-provenance key — deliberately NOT source-scoped, so the MANUAL and
AUTONOMOUS producers, retries, browser refreshes and worker restarts all map to
the SAME position.  The execution ledger's UNIQUE idempotency key is what makes
a second BET on the same position impossible.

Pure data + lifecycle + accounting.  No DB, no network, no provider.  Fail
CLOSED.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Tuple

# ── markets / directions (MVP scope: the alert is always UNDER on TOTAL) ──
MARKET_TOTAL = "TOTAL"
DIRECTION_UNDER = "UNDER"
DIRECTION_OVER = "OVER"

# ── the position lifecycle (directive item 6) ─────────────────────────────
SIGNAL = "SIGNAL"
ELIGIBLE = "ELIGIBLE"
POSITION_INTENT = "POSITION_INTENT"
EXECUTION_ATTEMPT = "EXECUTION_ATTEMPT"
BET_PLACED = "BET_PLACED"
SETTLED = "SETTLED"
REJECTED = "REJECTED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
RECONCILIATION = "RECONCILIATION"

STATES: Tuple[str, ...] = (
    SIGNAL, ELIGIBLE, POSITION_INTENT, EXECUTION_ATTEMPT, BET_PLACED,
    SETTLED, REJECTED, FAILED, UNKNOWN, RECONCILIATION)

#: happy path + every legal failure path (directive item 6, verbatim).
_TRANSITIONS: Tuple[Tuple[str, str], ...] = (
    (SIGNAL, ELIGIBLE),
    (ELIGIBLE, POSITION_INTENT),
    (ELIGIBLE, REJECTED),
    (POSITION_INTENT, EXECUTION_ATTEMPT),
    (POSITION_INTENT, REJECTED),
    (EXECUTION_ATTEMPT, BET_PLACED),
    (EXECUTION_ATTEMPT, FAILED),
    (EXECUTION_ATTEMPT, UNKNOWN),
    (UNKNOWN, RECONCILIATION),
    (RECONCILIATION, BET_PLACED),      # reconciled to a real placement
    (RECONCILIATION, REJECTED),        # reconciled to "never went through"
    (BET_PLACED, SETTLED),
)
TRANSITIONS: dict[Tuple[str, str], bool] = {t: True for t in _TRANSITIONS}

#: states that end a position's life.  NOTE: UNKNOWN is NOT terminal — it must
#: be reconciled (UNKNOWN is NEVER treated as BET_PLACED).
TERMINAL: frozenset = frozenset({SETTLED, REJECTED, FAILED})

#: results
RESULT_WIN = "WIN"
RESULT_LOSS = "LOSS"
RESULT_PUSH = "PUSH"


class InvalidPositionTransition(Exception):
    """An out-of-matrix position transition was attempted.  Fail closed."""


def validate_transition(current: str, new: str) -> None:
    if current not in STATES or new not in STATES \
            or (current, new) not in TRANSITIONS:
        raise InvalidPositionTransition(
            f"illegal position transition {current!r} -> {new!r}")


def can_transition(current: str, new: str) -> bool:
    try:
        validate_transition(current, new)
        return True
    except InvalidPositionTransition:
        return False


# ── the canonical key + id ────────────────────────────────────────────────
def position_key(*, game_id: Any, market_id: str = MARKET_TOTAL,
                 direction: str = DIRECTION_UNDER, checkpoint: Any,
                 alert_id: Optional[str] = None) -> Optional[str]:
    """The canonical, source-agnostic, immutable idempotency key.

    ``game_id | market_id | direction | checkpoint`` (+ alert id).  Returns
    None (fail closed) when the game id or checkpoint is missing.
    """
    gid = str(game_id or "").strip()
    if not gid or checkpoint is None:
        return None
    parts = [gid, str(market_id or MARKET_TOTAL).upper(),
             str(direction or DIRECTION_UNDER).upper(), str(checkpoint)]
    if alert_id:
        parts.append(str(alert_id))
    return "|".join(parts)


def position_id_for(key: str) -> str:
    return "pos-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


# ── the position ──────────────────────────────────────────────────────────
@dataclass
class Position:
    position_id: str
    game_id: str
    market_id: str
    checkpoint: Any
    direction: str
    trigger_line: Optional[float] = None        # the FROZEN alert line (reference)
    trigger_time: Optional[str] = None
    executable_line: Optional[float] = None     # live market LINE at position time
    executable_odds: Optional[float] = None     # DECIMAL ODDS at position/exec time
    stake: Optional[float] = None
    signal_id: Optional[str] = None
    source: Optional[str] = None
    alert_id: Optional[str] = None
    mode: Optional[str] = None
    key: Optional[str] = None
    state: str = POSITION_INTENT
    execution_id: Optional[str] = None
    provider_ref: Optional[str] = None          # bookmaker execution/reference ID
    executed_at: Optional[str] = None           # execution timestamp
    result: Optional[str] = None                # WIN | LOSS | PUSH
    settled_total: Optional[float] = None
    created_at: Optional[str] = None

    # ── classification (ROI relevance) ────────────────────────────────
    @property
    def is_rejected(self) -> bool:
        return self.state == REJECTED

    @property
    def is_placed(self) -> bool:
        return self.state in (BET_PLACED, SETTLED) or bool(self.provider_ref)

    @property
    def is_settled(self) -> bool:
        return self.state == SETTLED

    @property
    def is_pending(self) -> bool:
        return self.state in (SIGNAL, ELIGIBLE, POSITION_INTENT,
                              EXECUTION_ATTEMPT, BET_PLACED, UNKNOWN,
                              RECONCILIATION)

    @property
    def is_bettable(self) -> bool:
        """A position that PASSED the eligibility gate (not rejected)."""
        return self.state in (POSITION_INTENT, EXECUTION_ATTEMPT, BET_PLACED,
                              SETTLED, UNKNOWN, RECONCILIATION)


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def build_position(*, game_id: Any, checkpoint: Any,
                   market_id: str = MARKET_TOTAL,
                   direction: str = DIRECTION_UNDER,
                   trigger_line: Any = None,
                   executable_line: Any = None,
                   executable_odds: Any = None,
                   stake: Any = None, trigger_time: Optional[str] = None,
                   signal_id: Optional[str] = None,
                   alert_id: Optional[str] = None,
                   source: Optional[str] = None, mode: Optional[str] = None,
                   execution_id: Optional[str] = None,
                   provider_ref: Optional[str] = None,
                   executed_at: Optional[str] = None,
                   created_at: Optional[str] = None,
                   state: str = POSITION_INTENT) -> Position:
    """Build THE canonical position for one opportunity (fail closed).

    Call this ONLY from the eligibility-ALLOW point.  Raises ValueError when
    the opportunity identity (game_id + checkpoint) is missing — an
    unidentifiable opportunity must never become a position.

    ``executable_line`` (the market LINE) and ``executable_odds`` (the DECIMAL
    ODDS) are SEPARATE fields and are NEVER interchangeable — a betting line is
    NOT odds and must never be used as one.
    """
    key = position_key(game_id=game_id, market_id=market_id,
                       direction=direction, checkpoint=checkpoint,
                       alert_id=alert_id)
    if key is None:
        raise ValueError("position identity missing (game_id/checkpoint)")
    if state not in STATES:
        raise ValueError(f"unknown position state {state!r}")
    return Position(
        position_id=position_id_for(key), key=key,
        game_id=str(game_id).strip(), market_id=str(market_id).upper(),
        direction=str(direction).upper(), checkpoint=checkpoint,
        trigger_line=_f(trigger_line), trigger_time=trigger_time,
        executable_line=_f(executable_line),
        executable_odds=_f(executable_odds),
        stake=_f(stake), signal_id=signal_id,
        source=source, alert_id=alert_id, mode=mode,
        execution_id=execution_id, provider_ref=provider_ref,
        executed_at=executed_at, created_at=created_at, state=state)


def position_from_command(command: dict, *, executable_line: Any = None,
                          executable_odds: Any = None,
                          stake: Any = None,
                          executed_at: Optional[str] = None,
                          created_at: Optional[str] = None) -> Optional[Position]:
    """Create the position for a shared Auto-Bet command — ONLY when the
    command decision is ALLOW.  A REJECTED command is NEVER a position.

    ``executable_odds`` MUST be supplied by the caller from the live market
    read at position time (the command carries NO odds); ``executable_line``
    defaults to the validator's ``current_line``.  The line and the odds are
    SEPARATE and are never substituted for one another.
    """
    if not isinstance(command, dict) or command.get("decision") != "ALLOW":
        return None
    rung = command.get("rung") or {}
    stk = command.get("stake") or {}
    # the OPPORTUNITY identity is the ALERT checkpoint (immutable), never the
    # actual trigger percent; fall back to the validator's field only if the
    # command carries no explicit checkpoint.
    cp = command.get("checkpoint")
    if cp is None:
        cp = rung.get("trigger_checkpoint_percent")
    line = (executable_line if executable_line is not None
            else rung.get("current_line"))
    return build_position(
        game_id=command.get("game_id"),
        checkpoint=cp,
        market_id=command.get("market", MARKET_TOTAL),
        direction=command.get("selection", DIRECTION_UNDER),
        trigger_line=rung.get("trigger_line"),
        executable_line=line,
        executable_odds=executable_odds,
        stake=stake if stake is not None else stk.get("stake"),
        trigger_time=rung.get("trigger_timestamp"),
        signal_id=command.get("alert_id"),
        alert_id=command.get("alert_id"),
        source=command.get("source"), mode=command.get("mode"),
        executed_at=executed_at, created_at=created_at)


def position_from_candidate(candidate: dict, *,
                            created_at: Optional[str] = None
                            ) -> Optional[Position]:
    """Create the position for an executor candidate (the autonomous path).

    IMPORTANT: the executor's ``candidate["price"]`` is the market LINE (not
    odds) — it maps to ``executable_line``.  Decimal odds are read ONLY from an
    explicit ``executable_odds``/``odds`` field and stay None (→ ROI
    unavailable) when absent.  The line is NEVER used as odds.
    """
    if not isinstance(candidate, dict):
        return None
    odds = candidate.get("executable_odds", candidate.get("odds"))
    line = candidate.get("executable_line", candidate.get("price"))
    return build_position(
        game_id=candidate.get("game_id"),
        checkpoint=candidate.get("checkpoint"),
        market_id=candidate.get("market", MARKET_TOTAL),
        direction=candidate.get("selection", DIRECTION_UNDER),
        trigger_line=candidate.get("triggered_line"),
        executable_line=line,
        executable_odds=odds,
        stake=candidate.get("stake_amount"),
        signal_id=candidate.get("alert_id"),
        alert_id=candidate.get("alert_id"),
        source="autonomous",
        execution_id=candidate.get("execution_id"),
        provider_ref=candidate.get("provider_ref"),
        executed_at=candidate.get("executed_at"),
        created_at=created_at)


# ── dedupe book (repeated observations → same position) ───────────────────
class PositionBook:
    """In-memory dedupe layer mirroring the ledger's UNIQUE idempotency key.

    ``claim`` returns the EXISTING position for a repeated observation / alert
    / retry / refresh / restart (never a second one) and counts the duplicate
    attempts that were blocked.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Position] = {}
        self.duplicates_blocked = 0

    def claim(self, pos: Position) -> Tuple[bool, Position]:
        existing = self._by_id.get(pos.position_id)
        if existing is not None:
            self.duplicates_blocked += 1
            return (False, existing)
        self._by_id[pos.position_id] = pos
        return (True, pos)

    def get(self, position_id: str) -> Optional[Position]:
        return self._by_id.get(position_id)

    def transition(self, position_id: str, new_state: str) -> Position:
        pos = self._by_id[position_id]
        validate_transition(pos.state, new_state)
        pos.state = new_state
        return pos

    def positions(self) -> list[Position]:
        return list(self._by_id.values())


# ── ROI from POSITIONS ONLY ───────────────────────────────────────────────
def roi(positions: Iterable[Position]) -> dict:
    """Return the directive's ROI block for a set of positions.

    ROI is computed EXCLUSIVELY from positions that (a) passed the eligibility
    gate and (b) were actually placed + settled, and ONLY when both the stake
    and the executable odds are known for every settled position.  Otherwise
    ``roi_pct`` is ``None`` (reported unavailable, never estimated).
    """
    ps = list(positions)
    attempted = len(ps)
    rejected = sum(1 for p in ps if p.is_rejected)
    failed = sum(1 for p in ps if p.state == FAILED)
    unknown = sum(1 for p in ps if p.state in (UNKNOWN, RECONCILIATION))
    placed = [p for p in ps if p.is_placed]
    settled = [p for p in ps if p.is_settled]
    pending = [p for p in ps if p.is_pending and p.state != SETTLED]

    total_stake = math.fsum(p.stake for p in placed if p.stake is not None)

    # ROI REFUSES to calculate when the executable ODDS (or stake) are missing
    # on ANY settled position — the betting line is NEVER used as odds.
    priced_settled = [p for p in settled
                      if p.stake is not None and p.executable_odds is not None
                      and p.result in (RESULT_WIN, RESULT_LOSS, RESULT_PUSH)]
    missing = [p.position_id for p in settled
               if p.stake is None or p.executable_odds is None
               or p.result not in (RESULT_WIN, RESULT_LOSS, RESULT_PUSH)]
    complete = (len(priced_settled) == len(settled))

    gross = 0.0
    pnl = 0.0
    if complete:
        for p in priced_settled:
            if p.result == RESULT_WIN:
                gross += p.stake * p.executable_odds
                pnl += p.stake * (p.executable_odds - 1.0)
            elif p.result == RESULT_LOSS:
                pnl -= p.stake
            # PUSH: stake returns, P/L 0
    wins = sum(1 for p in settled if p.result == RESULT_WIN)
    losses = sum(1 for p in settled if p.result == RESULT_LOSS)
    pushes = sum(1 for p in settled if p.result == RESULT_PUSH)
    decided = wins + losses

    roi_pct = (100.0 * pnl / total_stake
               if (complete and total_stake > 0) else None)
    return {
        "positions_attempted": attempted,
        "positions_placed": len(placed),
        "settled_positions": len(settled),
        "pending_positions": len(pending),
        "rejected_positions": rejected,
        "failed_or_unknown_executions": failed + unknown,
        "total_stake": round(total_stake, 2) if placed else 0.0,
        "gross_returns": round(gross, 2) if complete else None,
        "net_pl": round(pnl, 2) if complete else None,
        "roi_pct": round(roi_pct, 2) if roi_pct is not None else None,
        "win_rate": (100.0 * wins / decided if decided else None),
        "wins": wins, "losses": losses, "pushes": pushes,
        "roi_available": bool(roi_pct is not None),
        "missing_odds_positions": missing,
        "note": ("ROI from placed+settled POSITIONS only; REFUSED (None) "
                 "because stake/executable odds are missing on a settled "
                 "position" if not complete else
                 "ROI = settled net P/L / total placed stake"),
    }
