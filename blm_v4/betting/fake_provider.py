"""FAKE BOOKMAKER providers — pre-runtime auto-bet validation (TEST ONLY).

Two providers, two layers of the test pyramid (directive
"BLM AUTO-BET — PRE-RUNTIME VALIDATION ARCHITECTURE"):

``FakeBookmakerContractProvider``  — Phase 1/2: behaves like the REAL
    provider's CONTRACT: login → account identity → game search → market
    search → accept bet → bet id → status → settlement.  Every failure
    mode the real bookmaker can produce is SCRIPTABLE:

        success · wrong account · bad credentials · expired session ·
        market disappeared · line changed · duplicate request · timeout ·
        HTTP error · malformed response · rejected wager · settlement

    No network.  No credentials.  Deterministic.  This is the provider
    the whole BettingWorker/provider integration is hammered against.

``FakePokerBetProvider`` — the same contract shaped as the real
    ``PokerBetProvider`` stand-in for the live-mode code path (DRY_RUN
    off) in TEST: submit() answers from the same scenario engine without
    any transport.

Both implement the EXACT ``BetProvider`` contract (one bet, one honest
answer, statuses SUBMITTED/ACCEPTED/REJECTED/FAILED/UNKNOWN) so contract
tests here prove the worker's handling of every path before the real
PokerBetProvider transport is ever written.

HARD BOUNDARY: nothing in this module performs I/O, reads credentials,
or knows about production.  It is inert by construction and safe to run
anywhere; it is still only ever WIRED in the TEST stack.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from blm_v4.betting.provider import (
    BetProvider,
    ProviderAmbiguous,
    ProviderUnavailable,
)

#: the TEST bookmaker's authenticated account id — a synthetic identity
#: for the account-identity guard to verify against.  NOT a credential.
TEST_BOOKMAKER_ACCOUNT_ID = "TEST-ACCT-0001"

#: the TEST bookmaker's fixture game / market identities.
TEST_GAME_ID = "TEST-GAME-9001"
TEST_MARKET_ID = "TEST-MARKET-TOTAL"
TEST_BET_TYPE_ID = "TEST-BETTYPE-UNDER"


class FakeBookmakerContractProvider(BetProvider):
    """Scriptable fake bookmaker: the full contract, every failure mode.

    Scenario state is set DIRECTLY by tests (``set_scenario``), or a
    scripted outcome queue can be pushed with ``queue_result`` for
    sequential-response tests.  With no scenario and no scripted queue
    the default is an ACCEPTED bet — the happy path.

    Thread-safe: each submit snapshots the scenario atomically, so
    concurrent-worker tests can hammer one instance.
    """

    name = "fake_pokerbet_contract"

    #: the fake bookmaker REQUIRES account identity — the guard under
    #: test must compare authenticated vs configured on every submission.
    requires_account_identity = True

    #: scenario → (provider outcome, error_code) mapping used by submit().
    #: the worker maps exceptions/statuses exactly as it would for the
    #: real provider — that mapping IS the contract under test.
    _SCENARIOS: dict[str, tuple[str, Optional[str]]] = {
        "success":           ("ACCEPTED",  None),
        "wrong_account":     ("REJECTED",  "ACCOUNT_MISMATCH"),
        "bad_credentials":   ("FAILED",    "AUTH_FAILED"),
        "expired_session":   ("FAILED",    "SESSION_EXPIRED"),
        "market_disappeared": ("FAILED",   "MARKET_NOT_FOUND"),
        "line_changed":      ("REJECTED",  "LINE_CHANGED"),
        "duplicate_request": ("REJECTED",  "DUPLICATE_REQUEST"),
        "timeout":           ("UNKNOWN",   "PROVIDER_TIMEOUT"),
        "http_error":        ("FAILED",    "PROVIDER_HTTP_500"),
        "malformed":         ("UNKNOWN",   "PROVIDER_RESPONSE_MALFORMED"),
        "rejected":          ("REJECTED",  "WAGER_REJECTED"),
        "insufficient_funds": ("REJECTED", "INSUFFICIENT_FUNDS"),
        "market_closed":     ("REJECTED",  "MARKET_CLOSED"),
    }

    def __init__(self, *, account_id: str = TEST_BOOKMAKER_ACCOUNT_ID,
                 game_id: str = TEST_GAME_ID,
                 market_id: str = TEST_MARKET_ID,
                 bet_type_id: str = TEST_BET_TYPE_ID,
                 latency_s: float = 0.0):
        self.authenticated_account_id = account_id
        self.game_id = game_id
        self.market_id = market_id
        self.bet_type_id = bet_type_id
        self.latency_s = latency_s
        self._scenario = "success"
        self._scripted: list[dict] = []
        self._lock = threading.Lock()
        self._submissions: list[dict] = []
        self._settlements: dict[str, dict] = {}
        self._fail_logins = 0
        self._login_calls = 0
        self._configured_account_id: Optional[str] = account_id

    # ── TEST control surface ──────────────────────────────────────────
    def set_scenario(self, scenario: str) -> None:
        assert scenario in self._SCENARIOS, f"unknown scenario {scenario!r}"
        with self._lock:
            self._scenario = scenario

    def queue_result(self, status: str, provider_ref: Optional[str] = None,
                     error_code: Optional[str] = None,
                     error_message: Optional[str] = None,
                     accepted_amount: Optional[float] = None) -> None:
        """Script the NEXT submit() answer(s), overriding the scenario."""
        with self._lock:
            self._scripted.append({
                "status": status, "provider_ref": provider_ref,
                "error_code": error_code, "error_message": error_message,
                "accepted_amount": accepted_amount})

    def set_configured_account(self, account_id: Optional[str]) -> None:
        """The account the OPERATOR configured (guard compares it against
        the account the bookmaker authenticated)."""
        self._configured_account_id = account_id

    @property
    def configured_account_id(self) -> Optional[str]:
        return self._configured_account_id

    # ── the bookmaker contract (login → account → game → market → bet) ─
    def login(self, username: str = "test-user",
              password: str = "test-pass") -> dict:
        """Authentication.  Scripted failures raise as the real one
        would; success returns the AUTHENTICATED account identity —
        the value the account guard verifies (never a credential)."""
        with self._lock:
            self._login_calls += 1
            fail = self._fail_logins > 0
            if fail:
                self._fail_logins -= 1
        if fail:
            raise ProviderUnavailable("bad credentials (scripted)")
        return {"authenticated": True,
                "account_id": self.authenticated_account_id,
                "session": f"test-session-{uuid.uuid4().hex[:8]}"}

    def fail_next_login(self, times: int = 1) -> None:
        with self._lock:
            self._fail_logins += times

    def login_calls(self) -> int:
        with self._lock:
            return self._login_calls

    def search_game(self, game_id: str) -> dict:
        """Game lookup.  market_disappeared hides the fixture game."""
        scenario = self._scenario_snapshot()
        if scenario == "market_disappeared" or game_id != self.game_id:
            return {"found": False, "game_id": game_id}
        return {"found": True, "game_id": game_id,
                "live": True, "competition": "TEST"}

    def search_market(self, game_id: str,
                      market: str = "TOTAL") -> dict:
        """Market lookup.  market_disappeared hides the market; the
        fixture carries a stable line for the happy path."""
        scenario = self._scenario_snapshot()
        if scenario == "market_disappeared" or game_id != self.game_id:
            return {"found": False, "game_id": game_id, "market": market}
        return {"found": True, "market_id": self.market_id,
                "bet_type_id": self.bet_type_id, "line": 3.5,
                "open": scenario != "market_closed"}

    # ── BetProvider contract ──────────────────────────────────────────
    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        if self.latency_s:
            time.sleep(self.latency_s)
        with self._lock:
            self._submissions.append({
                "execution_id": execution_id, "game_id": game_id,
                "alert_id": alert_id, "selection": selection,
                "price": price, "stake_amount": stake_amount,
                "market": market, "line": line, "at": time.time()})
            scripted = self._scripted.pop(0) if self._scripted else None
        if scripted is not None:
            ref = scripted.get("provider_ref") or f"fake-{execution_id}"
            if scripted["status"] == "ACCEPTED":
                self._settlements[ref] = {
                    "bet_id": ref, "status": "OPEN",
                    "stake_amount": stake_amount, "selection": selection}
            return {"status": scripted["status"],
                    "provider_ref": ref,
                    "error_code": scripted.get("error_code"),
                    "error_message": scripted.get("error_message"),
                    "accepted_amount": scripted.get("accepted_amount")}

        scenario = self._scenario_snapshot()
        if scenario == "timeout":
            # the real transport's timeout surfaces as an UNKNOWN outcome —
            # money MAY be in play; the worker must never guess
            raise ProviderAmbiguous("request timed out (scripted)")
        if scenario == "malformed":
            raise ProviderAmbiguous("malformed provider response (scripted)")
        if scenario == "bad_credentials":
            raise ProviderUnavailable("authentication rejected (scripted)")
        if scenario == "expired_session":
            raise ProviderUnavailable("session expired (scripted)")
        if scenario == "http_error":
            raise ProviderUnavailable("provider HTTP 500 (scripted)")

        status, error_code = self._SCENARIOS[scenario]
        ref = f"fake-{uuid.uuid4().hex[:12]}"
        if status == "ACCEPTED":
            self._settlements[ref] = {
                "bet_id": ref, "status": "OPEN",
                "stake_amount": stake_amount, "selection": selection}
        return {"status": status,
                "provider_ref": ref if status != "FAILED" else None,
                "error_code": error_code,
                "error_message": (error_code.lower() if error_code else None),
                "accepted_amount": (stake_amount if status == "ACCEPTED"
                                    else None)}

    # ── settlement contract (bet id → status → settlement) ───────────
    def settle(self, provider_ref: str, result: str = "WON") -> dict:
        """Advance an ACCEPTED bet to SETTLED (TEST bookmaker side)."""
        with self._lock:
            rec = self._settlements.get(provider_ref)
            if rec is None:
                return {"bet_id": provider_ref, "status": "UNKNOWN"}
            rec["status"] = "SETTLED"
            rec["result"] = result
            return dict(rec)

    def settlement(self, provider_ref: str) -> dict:
        with self._lock:
            rec = self._settlements.get(provider_ref)
            return dict(rec) if rec else {"bet_id": provider_ref,
                                          "status": "UNKNOWN"}

    def submissions(self) -> list[dict]:
        with self._lock:
            return list(self._submissions)

    def submission_count(self) -> int:
        with self._lock:
            return len(self._submissions)

    def _scenario_snapshot(self) -> str:
        with self._lock:
            return self._scenario


class FakePokerBetProvider(FakeBookmakerContractProvider):
    """The real ``PokerBetProvider`` stand-in for live-mode TEST runs.

    Same name/shape as the real provider (``name = "pokerbet"``) so the
    executor's provider-name audit path is exercised identically, with
    the fake bookmaker's scripted behaviour underneath.  Never touches
    the network; never reads credentials.
    """

    name = "pokerbet"


def make_test_config(root, tmp_db_path) -> "BettingConfig":
    """An ARMED TEST configuration: every limit configured (so the
    executor's ten-condition gate can pass), dry-run ON, TEST-only
    endpoints.  Used by the TEST stack and the integration tests."""
    import os
    from blm_v4.betting.config import BettingConfig
    env = {
        "BETTING_DRY_RUN": "true",
        "BETTING_MAX_STAKE_PER_BET": "10.0",
        "BETTING_MAX_BETS_PER_DAY": "50",
        "BETTING_MAX_DAILY_EXPOSURE": "100.0",
        "BETTING_STAKE_UNITS": "1.0",
        "BETTING_PROVIDER_BASE_URL": "http://127.0.0.1:2263",
        "BETTING_DB_PATH": str(tmp_db_path),
    }
    saved = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        cfg = BettingConfig.from_env(root)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return cfg
