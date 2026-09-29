"""BOOKMAKER ADAPTER BOUNDARY (directive §1 + §6).

    BetEngine → BookmakerAdapter → TestAdapter / LiveAdapter

The engine NEVER contains bookmaker-specific execution logic: it sees
only this adapter protocol.  ``TestBookmakerAdapter`` is a deterministic,
pure-python bookmaker simulator (no network, no browser, no DB) whose
responses are SCRIPTED per scenario, so tests can reproduce every
failure mode; it is the DEFAULT adapter wherever production is not
explicitly configured.  ``LiveBookmakerAdapter`` is the real-money
adapter STUB — construction is possible only with an explicit
production opt-in, and submission always raises ``AdapterSubmitError``
with code ``LIVE_NOT_IMPLEMENTED`` until the real transport exists, so
live execution requires code, not configuration.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional


class AdapterSubmitError(Exception):
    """Definitive, structured submit failure (no money moved)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class AdapterAmbiguousError(Exception):
    """The submit outcome could not be determined — money MAY be in
    play; the engine must keep the bet UNKNOWN and reconcile."""


# ── the protocol ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Book:
    """One selection identity offered by the bookmaker."""

    event_id: str
    market_id: str          # e.g. "TOTAL" / "MONEYLINE_TOTAL_UNDER"
    position: str           # e.g. "UNDER"
    line: Optional[float]
    price: Optional[float]
    market_status: str = "OPEN"      # OPEN | SUSPENDED | CLOSED
    event_status: str = "LIVE"       # LIVE | ENDED | ABANDONED


@dataclass(frozen=True)
class BookmakerQuote:
    event_id: str
    market_id: str
    position: str
    line: Optional[float]
    price: Optional[float]
    market_status: str
    event_status: str


class BookmakerAdapter:
    """The ONLY bookmaker surface the engine knows.  Subclass/replace to
    integrate a real bookmaker; the engine never imports one."""

    name = "abstract"

    # -- identity / session -------------------------------------------
    def authenticate(self, username: str, password: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def is_authenticated(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    # -- market reads ---------------------------------------------------
    def get_event(self, event_id: str) -> Optional[dict]:  # pragma: no cover
        raise NotImplementedError

    def get_market(self, event_id: str, market_id: str) -> Optional[dict]:  # pragma: no cover
        raise NotImplementedError

    def get_selection(self, event_id: str, market_id: str,
                      position: str) -> Optional[BookmakerQuote]:  # pragma: no cover
        raise NotImplementedError

    # -- execution ------------------------------------------------------
    def submit(self, idempotency_key: str, *, event_id: str,
               market_id: str, position: str, line: Optional[float],
               price: Optional[float], stake_amount: float) -> dict:  # pragma: no cover
        raise NotImplementedError

    def balance(self) -> float:  # pragma: no cover
        raise NotImplementedError

    def confirmation(self, provider_ref: str) -> Optional[dict]:  # pragma: no cover
        raise NotImplementedError

    def find_order_by_idempotency(self, idempotency_key: str) -> Optional[dict]:  # pragma: no cover
        raise NotImplementedError


# ── the TEST adapter (deterministic simulator) ─────────────────────────

@dataclass
class TestAdapterConfig:
    event_id: str = "TEST-GAME-9001"
    market_id: str = "TOTAL"
    position: str = "UNDER"
    line: float = 180.5
    price: float = 1.85
    balance: float = 1000.0
    max_stake: float = 10.0
    market_status: str = "OPEN"
    event_status: str = "LIVE"


class TestBookmakerAdapter(BookmakerAdapter):
    """Deterministic TEST bookmaker (§6).  Every failure mode the real
    book can produce is SCRIPTABLE via :meth:`set_scenario` /
    :meth:`queue_response`; with no script the default is a clean ACCEPT.

    Scenarios (§6 list, verbatim): accepted · rejected · odds_changed ·
    line_changed · market_suspended · event_closed · insufficient_balance ·
    auth_failure · timeout · connection_failure · ambiguous_submission ·
    duplicate_submission · settlement.

    HARD BOUNDARY: no I/O, no credentials, no production knowledge —
    inert by construction; still only ever WIRED outside production.
    """

    name = "test_adapter"
    __test__ = False   # pytest: not a test class despite the name

    SCENARIOS = (
        "accepted", "rejected", "odds_changed", "line_changed",
        "market_suspended", "event_closed", "insufficient_balance",
        "auth_failure", "timeout", "connection_failure",
        "ambiguous_submission", "duplicate_submission", "settlement",
    )

    def __init__(self, cfg: Optional[TestAdapterConfig] = None):
        self.cfg = cfg or TestAdapterConfig()
        self._lock = threading.Lock()
        self._scenario = "accepted"
        self._queued: list[dict] = []
        self._authenticated = False
        self._account_id = ""
        self._submitted: dict[str, dict] = {}   # idem_key → order record
        self._submissions: list[dict] = []      # every submit() attempt
        self._fail_auth = 0

    # ── TEST control surface ───────────────────────────────────────────
    def set_scenario(self, scenario: str) -> None:
        assert scenario in self.SCENARIOS, f"unknown scenario {scenario!r}"
        with self._lock:
            self._scenario = scenario

    def queue_response(self, **resp) -> None:
        """Script the NEXT submit() answer(s), overriding the scenario.
        Recognised fields: status, provider_ref, error_code,
        error_message, raise (AdapterSubmitError code),
        raise_ambiguous (message)."""
        with self._lock:
            self._queued.append(resp)

    def fail_next_authentication(self, times: int = 1) -> None:
        with self._lock:
            self._fail_auth += times

    # ── identity / session ---------------------------------------------
    def authenticate(self, username: str, password: str) -> None:
        with self._lock:
            if self._fail_auth > 0:
                self._fail_auth -= 1
                self._authenticated = False
                return
            self._authenticated = True
            self._account_id = username

    def is_authenticated(self) -> bool:
        with self._lock:
            return self._authenticated

    # ── market reads ───────────────────────────────────────────────────
    def get_event(self, event_id: str) -> Optional[dict]:
        cfg = self.cfg
        if event_id != cfg.event_id:
            return None
        status = "ENDED" if self._scenario_snapshot() == "event_closed" \
            else cfg.event_status
        if status == "ENDED":
            return None
        return {"event_id": event_id, "status": status,
                "live": status == "LIVE"}

    def get_market(self, event_id: str, market_id: str) -> Optional[dict]:
        if self.get_event(event_id) is None:
            return None
        if market_id != self.cfg.market_id:
            return None
        status = self._market_status()
        if status == "CLOSED":
            return None
        return {"market_id": market_id, "status": status}

    def get_selection(self, event_id: str, market_id: str,
                      position: str) -> Optional[BookmakerQuote]:
        if self.get_market(event_id, market_id) is None:
            return None
        if position != self.cfg.position:
            return None
        line, price = self._line_price()
        return BookmakerQuote(
            event_id=event_id, market_id=market_id, position=position,
            line=line, price=price, market_status=self._market_status(),
            event_status="LIVE")

    def balance(self) -> float:
        return float(self.cfg.balance)

    def confirmation(self, provider_ref: str) -> Optional[dict]:
        with self._lock:
            rec = self._submitted.get(provider_ref)
            return dict(rec) if rec else None

    def find_order_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        """Reconciliation lookup: the order an idempotency key produced,
        if any (§5: retries must find the ORIGINAL, never re-submit)."""
        with self._lock:
            for rec in self._submitted.values():
                if rec["idempotency_key"] == idempotency_key:
                    return dict(rec)
            return None

    # ── execution ──────────────────────────────────────────────────────
    def submit(self, idempotency_key: str, *, event_id: str,
               market_id: str, position: str, line: Optional[float],
               price: Optional[float], stake_amount: float) -> dict:
        with self._lock:
            self._submissions.append({
                "idempotency_key": idempotency_key, "event_id": event_id,
                "market_id": market_id, "position": position,
                "line": line, "price": price,
                "stake_amount": stake_amount})
            queued = self._queued.pop(0) if self._queued else None
            scenario = self._scenario

        if queued is not None:
            if queued.get("raise"):
                raise AdapterSubmitError(queued["raise"])
            if queued.get("raise_ambiguous"):
                raise AdapterAmbiguousError(queued["raise_ambiguous"])
            resp = {"status": queued.get("status", "ACCEPTED"),
                    "provider_ref": queued.get("provider_ref"),
                    "error_code": queued.get("error_code"),
                    "error_message": queued.get("error_message")}
            if resp["provider_ref"] is None and resp["status"] == "ACCEPTED":
                resp["provider_ref"] = f"test-{uuid.uuid4().hex[:10]}"
            if resp["status"] == "ACCEPTED":
                with self._lock:
                    self._submitted[resp["provider_ref"]] = {
                        "provider_ref": resp["provider_ref"],
                        "idempotency_key": idempotency_key,
                        "status": "CONFIRMED", "stake_amount": stake_amount}
            return resp

        scenario = self._scenario_snapshot()
        # §6 scenario → response map (deterministic)
        if scenario == "accepted":
            with self._lock:
                # adapter-level duplicate protection: same idem key never
                # creates a second accepted order (§5)
                for rec in self._submitted.values():
                    if rec["idempotency_key"] == idempotency_key:
                        return {"status": "DUPLICATE",
                                "provider_ref": rec["provider_ref"],
                                "error_code": "DUPLICATE_SUBMISSION"}
                ref = f"test-{uuid_uuid4_hex()}"
                self._submitted[ref] = {
                    "provider_ref": ref, "idempotency_key": idempotency_key,
                    "status": "CONFIRMED", "stake_amount": stake_amount}
            return {"status": "ACCEPTED", "provider_ref": ref}
        if scenario == "rejected":
            raise AdapterSubmitError("WAGER_REJECTED")
        if scenario == "odds_changed":
            raise AdapterSubmitError("ODDS_CHANGED")
        if scenario == "line_changed":
            raise AdapterSubmitError("LINE_CHANGED")
        if scenario == "market_suspended":
            raise AdapterSubmitError("MARKET_SUSPENDED")
        if scenario == "event_closed":
            raise AdapterSubmitError("EVENT_CLOSED")
        if scenario == "insufficient_balance":
            raise AdapterSubmitError("INSUFFICIENT_FUNDS")
        if scenario == "auth_failure":
            raise AdapterSubmitError("AUTH_REQUIRED")
        if scenario == "timeout":
            raise AdapterAmbiguousError("provider timeout (scripted)")
        if scenario == "connection_failure":
            raise AdapterAmbiguousError("connection lost mid-flight (scripted)")
        if scenario == "ambiguous_submission":
            raise AdapterAmbiguousError(
                "ambiguous provider response (scripted)")
        if scenario == "duplicate_submission":
            with self._lock:
                rec = next((r for r in self._submitted.values()
                            if r["idempotency_key"] == idempotency_key), None)
            if rec is not None:
                return {"status": "DUPLICATE",
                        "provider_ref": rec["provider_ref"],
                        "error_code": "DUPLICATE_SUBMISSION"}
            raise AdapterSubmitError("DUPLICATE_SUBMISSION")
        if scenario == "settlement":
            raise AdapterSubmitError("SETTLEMENT_NOT_A_SUBMIT_OUTCOME")
        raise AdapterSubmitError("SCENARIO_UNMAPPED")  # pragma: no cover

    # ── internals ──────────────────────────────────────────────────────
    def _scenario_snapshot(self) -> str:
        with self._lock:
            return self._scenario

    def _market_status(self) -> str:
        scenario = self._scenario_snapshot()
        if scenario == "market_suspended":
            return "SUSPENDED"
        if scenario == "event_closed":
            return "CLOSED"
        return self.cfg.market_status

    def _line_price(self) -> tuple[Optional[float], Optional[float]]:
        scenario = self._scenario_snapshot()
        line, price = self.cfg.line, self.cfg.price
        if scenario == "line_changed":
            line = round(line + 0.5, 2)
        if scenario == "odds_changed":
            price = round(price + 0.1, 2)
        return line, price

    def submissions(self) -> list[dict]:
        with self._lock:
            return list(self._submissions)

    def submission_count(self) -> int:
        with self._lock:
            return len(self._submissions)

    def orders(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._submitted)


def uuid_uuid4_hex() -> str:
    return uuid.uuid4().hex[:12]


# ── the LIVE adapter (fail-safe stub) ──────────────────────────────────

class LiveBookmakerAdapter(BookmakerAdapter):
    """The real-money adapter — INTENTIONALLY INERT.

    Construction requires the explicit production opt-in flag; submit()
    always fails safe.  Wiring a real transport later requires CODE, so
    live execution cannot be enabled by accident via configuration."""

    name = "live_adapter"

    def __init__(self, *, allow_live: bool = False):
        if not allow_live:
            raise AdapterSubmitError(
                "LIVE_NOT_PERMITTED",
                "LiveBookmakerAdapter requires explicit production opt-in")

    def authenticate(self, username: str, password: str) -> None:
        raise AdapterSubmitError(
            "LIVE_NOT_IMPLEMENTED",
            "live transport intentionally not implemented")

    def is_authenticated(self) -> bool:
        return False

    def get_event(self, event_id: str) -> Optional[dict]:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")

    def get_market(self, event_id: str, market_id: str) -> Optional[dict]:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")

    def get_selection(self, event_id: str, market_id: str,
                      position: str) -> Optional[BookmakerQuote]:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")

    def submit(self, idempotency_key: str, *, event_id: str,
               market_id: str, position: str, line: Optional[float],
               price: Optional[float], stake_amount: float) -> dict:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")

    def balance(self) -> float:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")

    def confirmation(self, provider_ref: str) -> Optional[dict]:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")

    def find_order_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        raise AdapterSubmitError("LIVE_NOT_IMPLEMENTED",
                                 "live transport intentionally not implemented")


def adapter_from_environment(env: Optional[dict] = None) -> BookmakerAdapter:
    """The DEFAULT adapter outside explicit production configuration is
    the TEST adapter (§1: no test/dev/staging/replay/CI environment may
    ever construct the live adapter).  Only
    ``BLM_ADAPTER_MODE=production`` AND ``BLM_ALLOW_LIVE_ADAPTER=1``
    together select the live adapter — and even then it fails safe on
    every call until the real transport is coded."""
    import os
    env = dict(os.environ if env is None else env)
    if env.get("BLM_ADAPTER_MODE", "").strip().lower() == "production" \
            and env.get("BLM_ALLOW_LIVE_ADAPTER", "").strip() == "1":
        return LiveBookmakerAdapter(allow_live=True)
    return TestBookmakerAdapter()
