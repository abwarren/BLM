"""THE BET ENGINE (directive §1+§3) — the execution subsystem core.

    BetEngine → BookmakerAdapter → TestAdapter / LiveAdapter

The engine owns ONE bet's life across the explicit state machine
(:mod:`blm_v4.betting.machine`):

    SIGNAL → ELIGIBLE → PRECHECK → SUBMITTING → ACCEPTED → CONFIRMED
                                                             → SETTLED
    PRECHECK     → REJECTED
    SUBMITTING   → SUBMISSION_FAILED | UNKNOWN
    UNKNOWN      → RECONCILING → CONFIRMED | REJECTED

Idempotency (§5) is enforced TWICE, deliberately: the engine's claim
store (UNIQUE idempotency key — survives retry, restart, worker/collector
restart, frontend reconnect, duplicate delivery) and the adapter's own
order book.  Every engine decision is appended to the audit trail with
the exact reason.  NO bet may transition directly from SIGNAL to
CONFIRMED — the machine refuses it structurally.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Optional

from blm_v4.betting.adapters import (
    AdapterAmbiguousError,
    AdapterSubmitError,
    BookmakerAdapter,
    TestBookmakerAdapter,
)
from blm_v4.betting.machine import (
    ACCEPTED,
    CONFIRMED,
    ELIGIBLE,
    PRECHECK,
    REJECTED,
    RECONCILING,
    SIGNAL,
    SUBMISSION_FAILED,
    SUBMITTING,
    SETTLED,
    UNKNOWN,
    InvalidTransition,
    is_terminal,
    validate_transition,
)
from blm_v4.betting.precheck import PrecheckLimits, run_precheck
from blm_v4.betting.session import BookmakerSession


class ClaimStore:
    """SQLite-backed UNIQUE idempotency claims (§5) — a separate,
    self-contained failure domain (the ``blm_betting.db`` pattern).

    The INSERT-OR-IGNORE + SELECT pair inside BEGIN IMMEDIATE is the
    atomic claim: two concurrent engines can never both win, and the
    claim survives process restarts (worker restart, collector restart,
    frontend reconnect are all the same case: the key is already here).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS bet_claims (
        idempotency_key TEXT PRIMARY KEY,
        execution_id    TEXT NOT NULL,
        signal_fingerprint TEXT,
        state           TEXT NOT NULL,
        reason          TEXT,
        provider_ref    TEXT,
        created_at_utc  TEXT NOT NULL,
        updated_at_utc  TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        # a ":memory:" store must keep ONE connection — each new sqlite
        # connection to ":memory:" would otherwise see an empty database
        self._mem_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._mem_conn = self._new_conn()
        with self._conn() as c:
            c.executescript(self.SCHEMA)

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        return self._new_conn()

    def claim(self, key: str, execution_id: str,
              fingerprint: str = "") -> tuple[bool, Optional[dict]]:
        now = _utcnow()
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                """INSERT OR IGNORE INTO bet_claims
                   (idempotency_key, execution_id, signal_fingerprint,
                    state, created_at_utc, updated_at_utc)
                   VALUES (?,?,?,?,?,?)""",
                (key, execution_id, fingerprint, "CLAIMED", now, now))
            if cur.rowcount == 0:
                row = c.execute(
                    "SELECT * FROM bet_claims WHERE idempotency_key=?",
                    (key,)).fetchone()
                c.commit()
                return False, (dict(row) if row else None)
            row = c.execute(
                "SELECT * FROM bet_claims WHERE idempotency_key=?",
                (key,)).fetchone()
            c.commit()
            return True, (dict(row) if row else None)

    def update(self, key: str, *, state: Optional[str] = None,
               reason: Optional[str] = None,
               provider_ref: Optional[str] = None) -> None:
        sets, args = ["updated_at_utc=?"], [_utcnow()]
        if state is not None:
            sets.append("state=?")
            args.append(state)
        if reason is not None:
            sets.append("reason=?")
            args.append(str(reason)[:300])
        if provider_ref is not None:
            sets.append("provider_ref=?")
            args.append(provider_ref)
        args.append(key)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE bet_claims SET {', '.join(sets)} "
                      "WHERE idempotency_key=?", args)
            c.commit()

    def get(self, key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM bet_claims WHERE idempotency_key=?",
                (key,)).fetchone()
        return dict(row) if row else None


def _utcnow() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def idempotency_key_for(signal: dict) -> str:
    """The idempotency key derives from the bet's IMMUTABLE identity
    (§5): canonical game id + market + selection + the signal's
    checkpoint identity — never from timestamps, prices or display
    text, so a re-delivered/retried signal hashes identically."""
    identity = "|".join(str(signal.get(k) or "") for k in (
        "game_id", "market", "selection", "checkpoint", "alert_id"))
    return "ik-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


@dataclass
class BetRecord:
    """Everything known about one bet attempt.  ``state`` moves ONLY
    through :meth:`BetEngine.transition` (matrix-validated)."""

    execution_id: str
    idempotency_key: str
    state: str = SIGNAL
    reason: str = ""
    signal: dict = field(default_factory=dict)
    provider_ref: Optional[str] = None
    error_code: Optional[str] = None
    timeline: list = field(default_factory=list)
    owns_claim: bool = False  # True only while THIS record holds the claim
    placed: bool = True       # False for duplicate echoes (never submitted)

    def as_dict(self) -> dict:
        return {"execution_id": self.execution_id,
                "idempotency_key": self.idempotency_key,
                "state": self.state, "reason": self.reason,
                "provider_ref": self.provider_ref,
                "error_code": self.error_code,
                "placed": self.placed,
                "timeline": list(self.timeline)}


class BetEngine:
    """Drives one bet from SIGNAL to a terminal state.  Thread-safe per
    instance for distinct bets; the claim store serialises duplicates."""

    def __init__(self, *, adapter: BookmakerAdapter,
                 session: BookmakerSession, claims: ClaimStore,
                 precheck_limits: Optional[PrecheckLimits] = None,
                 audit=None):
        self.adapter = adapter
        self.session = session
        self.claims = claims
        self.pre = precheck_limits or PrecheckLimits()
        self._audit = audit  # optional callable(event, **fields)

    # ── plumbing ───────────────────────────────────────────────────────
    def transition(self, bet: BetRecord, new_state: str,
                   reason: str = "") -> None:
        """Matrix-validated state move.  Invalid → raises (fail closed)."""
        validate_transition(bet.state, new_state)
        bet.timeline.append({
            "from": bet.state, "to": new_state, "reason": reason,
            "at": _utcnow()})
        bet.state = new_state
        if reason:
            bet.reason = reason
        if bet.owns_claim:  # a losing duplicate must never write claim state
            self.claims.update(bet.idempotency_key, state=new_state,
                               reason=reason or None,
                               provider_ref=bet.provider_ref)
        if self._audit:
            self._audit("transition", execution_id=bet.execution_id,
                        idempotency_key=bet.idempotency_key,
                        reason=f"{bet.state}->{new_state}: {reason}".strip(),
                        details={"to": new_state})

    def _audit_log(self, event: str, **kw) -> None:
        if self._audit:
            self._audit(event, **kw)

    # ── the pipeline ───────────────────────────────────────────────────
    def place(self, signal: dict, *, auto_betting_enabled: bool = True,
              game_enabled: bool = True) -> BetRecord:
        """Run one bet SIGNAL → terminal state.  Never raises for a
        REFUSED bet (that is a REJECTED outcome); raises only on
        invariant violations (invalid transition = defect)."""
        key = idempotency_key_for(signal)
        execution_id = "be-" + hashlib.sha256(
            (key + _utcnow()).encode()).hexdigest()[:20]
        bet = BetRecord(execution_id=execution_id, idempotency_key=key,
                        signal=dict(signal))

        # ── SIGNAL → ELIGIBLE ──────────────────────────────────────────
        # ── §5 idempotent claim — FIRST, before any state move (a
        # losing duplicate must never touch the original's claim row) ───────────────
        claimed, existing = self.claims.claim(
            key, execution_id, fingerprint=str(signal.get("alert_id") or ""))
        if not claimed:
            # §5: the SAME signal must never create two bets — whatever
            # raced us (retry, refresh, timeout, restart, reconnect,
            # duplicate delivery) learns the ORIGINAL's disposition.
            # The duplicate attempt itself never enters the machine (a
            # SIGNAL→REJECTED edge does not exist — by design).
            return self._original_outcome(bet, existing)
        bet.owns_claim = True

        # ── SIGNAL → ELIGIBLE
        self.transition(bet, ELIGIBLE, "signal accepted for evaluation")

        # ── ELIGIBLE → PRECHECK ────────────────────────────────────────
        self.transition(bet, PRECHECK, "claim won; running pre-bet gate")
        result = run_precheck(
            {**signal, "idempotency_key": key},
            adapter=self.adapter, session=self.session, pre=self.pre,
            game_enabled=game_enabled,
            auto_betting_enabled=auto_betting_enabled,
            already_claimed=False)
        self._audit_log("precheck", execution_id=execution_id,
                        idempotency_key=key, reason=result.reason,
                        details=result.as_dict())
        if not result.ok:
            self.transition(bet, REJECTED, f"{result.reason}: {result.detail}")
            return bet

        # ── PRECHECK → SUBMITTING → adapter ────────────────────────────
        self.transition(bet, SUBMITTING, "precheck passed")
        try:
            resp = self.adapter.submit(
                key,
                event_id=str(signal.get("game_id") or ""),
                market_id=str(signal.get("market") or "TOTAL"),
                position=str(signal.get("selection") or "UNDER"),
                line=signal.get("line"), price=signal.get("price"),
                stake_amount=float(signal.get("stake_amount") or 0.0))
        except AdapterAmbiguousError as e:
            # money MAY be in play → UNKNOWN → RECONCILING (never retry)
            self.transition(bet, UNKNOWN, f"ambiguous submit: {e}")
            return self._reconcile(bet)
        except AdapterSubmitError as e:
            self.transition(bet, SUBMISSION_FAILED,
                            f"{e.code}: {e}")
            return bet

        return self._on_submit_response(bet, resp)

    # ── submission outcomes ────────────────────────────────────────────
    def _on_submit_response(self, bet: BetRecord, resp: dict) -> BetRecord:
        status = str(resp.get("status", "")).upper()
        ref = resp.get("provider_ref")
        if status == "ACCEPTED":
            bet.provider_ref = ref
            self.transition(bet, ACCEPTED, f"accepted (ref {ref})")
            return self._confirm(bet)
        if status == "DUPLICATE":
            # the ADAPTER says this key already produced an order —
            # reconcile onto the ORIGINAL (same bet identity, not a
            # second one): SUBMITTING → UNKNOWN → RECONCILING → CONFIRMED
            self.transition(bet, UNKNOWN,
                            "adapter reports duplicate of an existing order")
            return self._reconcile(bet)
        # definitive refusal from the bookmaker
        code = str(resp.get("error_code") or "BET_REJECTED")
        self.transition(bet, SUBMISSION_FAILED, code)
        return bet

    def _confirm(self, bet: BetRecord) -> BetRecord:
        """ACCEPTED → CONFIRMED requires the bookmaker's own
        confirmation (an honest adapter's ACCEPT carries the receipt)."""
        conf = None
        if bet.provider_ref:
            conf = self.adapter.confirmation(bet.provider_ref)
        if conf is not None:
            self.transition(bet, CONFIRMED,
                            f"confirmed by bookmaker (ref {bet.provider_ref})")
        else:
            # no receipt available → treat as UNKNOWN and reconcile
            self.transition(bet, UNKNOWN,
                            "accepted without confirmation receipt")
            return self._reconcile(bet)
        return bet

    def _reconcile(self, bet: BetRecord) -> BetRecord:
        """UNKNOWN → RECONCILING → CONFIRMED | REJECTED.  Asks the
        adapter what the idempotency key actually produced — never
        blindly retries."""
        self.transition(bet, RECONCILING, "querying adapter for truth")
        order = None
        try:
            order = self.adapter.find_order_by_idempotency(bet.idempotency_key)
        except Exception as e:  # noqa: BLE001 — reconciliation failure
            self.transition(bet, REJECTED,
                            f"reconciliation unavailable: {e}")
            return bet
        if order is None:
            self.transition(bet, REJECTED,
                            "reconciled: no order exists for this key")
            return bet
        bet.provider_ref = order.get("provider_ref") or bet.provider_ref
        self.transition(bet, CONFIRMED,
                        f"reconciled to order {bet.provider_ref}")
        return bet

    def _original_outcome(self, bet: BetRecord,
                          existing: Optional[dict]) -> BetRecord:
        """A duplicate claim hit: return the ORIGINAL's disposition.
        The duplicate record carries the original's execution id, state
        and provider_ref with reason ``duplicate_of_original`` — no
        second order exists or can exist (§5).  The record is marked
        ``placed=False``: it never entered the machine itself."""
        bet.placed = False
        if existing:
            orig_state = existing.get("state") or SIGNAL
            bet.execution_id = existing.get("execution_id",
                                            bet.execution_id)
            bet.provider_ref = existing.get("provider_ref")
            bet.state = orig_state
            bet.reason = f"duplicate_of_original ({orig_state})"
            bet.timeline.append({
                "from": "DUPLICATE", "to": orig_state,
                "reason": bet.reason, "at": _utcnow()})
            if self._audit:
                self._audit("duplicate", execution_id=bet.execution_id,
                            idempotency_key=bet.idempotency_key,
                            reason=bet.reason,
                            details={"original_state": orig_state})
            return bet
        # no claim row (raced out of memory store?) — fail closed:
        # report the attempt as unplaced, never submit blind.
        bet.state = SIGNAL
        bet.reason = "duplicate_detected_without_claim_row"
        return bet
