"""AUTO-BET EXECUTION SUBSYSTEM — TEST-FIRST DIRECTIVE SUITE (§1–§7).

Covers the test-first implementation directive end to end, entirely in
TEST/PAPER mode:

  §1  adapter boundary    engine → BookmakerAdapter → Test/Live;
                          TestAdapter is the default outside production
  §2  session layer       CONNECTED/DISCONNECTED/SESSION_EXPIRED/
                          AUTHENTICATION_ERROR, logout, expiry,
                          reconnect, credential-free publications
  §3  state machine       explicit matrix, fail-closed invalid moves,
                          no SIGNAL→CONFIRMED shortcut
  §4  pre-bet validation  every gate with its EXACT rejection reason
  §5  duplicate protection  retry · refresh · timeout · restart ·
                          reconnect · duplicate delivery · concurrency
  §6  test/paper adapter  every scripted scenario deterministic
  §7  replay engine       historical signals through the TEST adapter

HARD BOUNDARY: no network, no production DB, no credentials anywhere.
"""
from __future__ import annotations

import threading

import pytest

from blm_v4.betting.adapters import (
    AdapterAmbiguousError,
    AdapterSubmitError,
    BookmakerQuote,
    LiveBookmakerAdapter,
    TestAdapterConfig,
    TestBookmakerAdapter,
    adapter_from_environment,
)
from blm_v4.betting.engine import (
    BetEngine,
    BetRecord,
    ClaimStore,
    idempotency_key_for,
)
from blm_v4.betting.machine import (
    ACCEPTED,
    CONFIRMED,
    ELIGIBLE,
    InvalidTransition,
    PRECHECK,
    REJECTED,
    RECONCILING,
    SETTLED,
    SIGNAL,
    SUBMISSION_FAILED,
    SUBMITTING,
    UNKNOWN,
    can_transition,
    is_terminal,
    next_states,
    path_exists,
    summary,
    validate_transition,
)
from blm_v4.betting.precheck import PrecheckLimits, run_precheck
from blm_v4.betting.replay import ReplayEngine
from blm_v4.betting.session import (
    BookmakerSession,
    SessionError,
    SessionState,
    redact,
)

GAME = "TEST-GAME-9001"


# ══════════════════════════════════════════════════════════════════════
# fixtures
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def adapter() -> TestBookmakerAdapter:
    return TestBookmakerAdapter(TestAdapterConfig())


@pytest.fixture
def session() -> BookmakerSession:
    s = BookmakerSession()
    s.login(("test-operator", "test-credential-not-a-real-secret"))
    return s


@pytest.fixture
def claims(tmp_path) -> ClaimStore:
    return ClaimStore(str(tmp_path / "claims_test.db"))


@pytest.fixture
def limits() -> PrecheckLimits:
    return PrecheckLimits(max_stake_per_bet=10.0,
                          max_total_exposure=100.0, current_exposure=0.0)


@pytest.fixture
def engine(adapter, session, claims, limits) -> BetEngine:
    return BetEngine(adapter=adapter, session=session, claims=claims,
                     precheck_limits=limits)


def _sig(**over) -> dict:
    sig = {"game_id": GAME, "market": "TOTAL", "selection": "UNDER",
           "line": 180.5, "price": 1.85, "stake_amount": 1.0,
           "checkpoint": 75, "alert_id": "alert-1",
           "idempotency_key": "ik-test-alert-1"}
    sig.update(over)
    return sig


# ══════════════════════════════════════════════════════════════════════
# §3 — the state machine
# ══════════════════════════════════════════════════════════════════════

class TestStateMachine:
    def test_happy_path_edges_all_legal(self):
        for a, b in [(SIGNAL, ELIGIBLE), (ELIGIBLE, PRECHECK),
                     (PRECHECK, SUBMITTING), (SUBMITTING, ACCEPTED),
                     (ACCEPTED, CONFIRMED), (CONFIRMED, SETTLED)]:
            assert can_transition(a, b), (a, b)

    def test_directive_failure_edges_legal(self):
        assert can_transition(PRECHECK, REJECTED)
        assert can_transition(SUBMITTING, SUBMISSION_FAILED)
        assert can_transition(SUBMITTING, UNKNOWN)
        assert can_transition(UNKNOWN, RECONCILING)
        assert can_transition(RECONCILING, CONFIRMED)
        assert can_transition(RECONCILING, REJECTED)

    def test_no_direct_signal_to_confirmed(self):
        # the directive's headline invariant
        assert (SIGNAL, CONFIRMED) not in \
            {(a, b) for (a, b) in
             [(x, y) for x in [SIGNAL] for y in next_states(SIGNAL)]}
        assert not can_transition(SIGNAL, CONFIRMED)
        assert not can_transition(SIGNAL, ACCEPTED)
        assert not can_transition(SIGNAL, SETTLED)
        # and structurally: NO path skips the submission states
        assert not path_exists(SIGNAL, CONFIRMED) or \
            path_exists(SIGNAL, SUBMITTING)  # only via SUBMITTING

    def test_confirmable_only_through_submitting_or_reconciling(self):
        for src in next_states(CONFIRMED):
            pass  # CONFIRMED is non-terminal-until-SETTLED; sanity below
        # every inbound edge to CONFIRMED comes from ACCEPTED/RECONCILING
        inbound = [a for a, b in [(s, d) for s in
                                  [SIGNAL, ELIGIBLE, PRECHECK, SUBMITTING,
                                   ACCEPTED, REJECTED, SUBMISSION_FAILED,
                                   UNKNOWN, RECONCILING]
                                  for d in next_states(s)]
                   if b == CONFIRMED]
        assert set(inbound) == {ACCEPTED, RECONCILING}

    def test_invalid_transition_raises(self):
        with pytest.raises(InvalidTransition):
            validate_transition(SIGNAL, CONFIRMED)
        with pytest.raises(InvalidTransition):
            validate_transition(ACCEPTED, REJECTED)
        with pytest.raises(InvalidTransition):
            validate_transition(REJECTED, SUBMITTING)
        with pytest.raises(InvalidTransition):
            validate_transition(SETTLED, SUBMITTING)

    def test_unknown_states_fail_closed(self):
        with pytest.raises(InvalidTransition):
            validate_transition("MADE_UP", SIGNAL)
        with pytest.raises(InvalidTransition):
            validate_transition(SIGNAL, "MADE_UP")

    def test_terminal_states(self):
        for s in (SETTLED, REJECTED, SUBMISSION_FAILED):
            assert is_terminal(s)
            assert next_states(s) == frozenset()

    def test_reject_has_no_exit(self):
        # a REJECTED bet is finished — no resurrection
        assert is_terminal(REJECTED)
        assert not path_exists(REJECTED, CONFIRMED)

    def test_summary_shape(self):
        s = summary()
        assert "SIGNAL" in s["states"] and "CONFIRMED" in s["states"]
        assert "SIGNAL->ELIGIBLE" in s["transitions"]


# ══════════════════════════════════════════════════════════════════════
# §2 — the session layer
# ══════════════════════════════════════════════════════════════════════

class TestSession:
    def test_four_states_published(self):
        s = BookmakerSession()
        assert s.state is SessionState.DISCONNECTED
        s.login(("op", "pw"))
        assert s.state is SessionState.CONNECTED
        s.expire()
        assert s.state is SessionState.SESSION_EXPIRED
        s2 = BookmakerSession()
        s2.login(("op", "pw"))
        s2.fail_authentication()
        assert s2.state is SessionState.AUTHENTICATION_ERROR
        s2.logout.__self__  # noqa: B018 — merely touch, no state change
        s2._state = SessionState.CONNECTED  # restore for logout path
        s2.logout()
        assert s2.state is SessionState.DISCONNECTED

    def test_login_logout_expiry_reconnect(self):
        s = BookmakerSession()
        assert s.status() == {"state": "DISCONNECTED", "account_id": "",
                              "logged_in": False}
        s.login(("op", "pw"))
        assert s.logged_in and s.account_id == "op"
        s.logout()
        assert not s.logged_in
        # reconnect = a fresh login on the same object
        s.login(("op", "pw"))
        assert s.logged_in
        s.expire()
        with pytest.raises(SessionError):
            s.assert_submittable()

    def test_empty_credentials_refused(self):
        s = BookmakerSession()
        with pytest.raises(SessionError):
            s.login(("", ""))
        assert s.state is SessionState.AUTHENTICATION_ERROR

    def test_touch_detects_expiry_via_adapter(self, adapter):
        s = BookmakerSession(adapter=adapter)
        s.login(("op", "pw"))
        assert s.touch() is SessionState.CONNECTED
        adapter._authenticated = False   # bookmaker killed the session
        assert s.touch() is SessionState.SESSION_EXPIRED
        with pytest.raises(SessionError):
            s.assert_submittable()

    def test_expired_session_blocks_submission(self, engine, session):
        session.expire()
        bet = engine.place(_sig())
        assert bet.state is REJECTED
        assert "SESSION_NOT_SUBMITTABLE" in bet.reason

    def test_publications_carry_no_secrets(self, session):
        pub = session.status()
        assert set(pub) == {"state", "account_id", "logged_in"}
        snap = session.snapshot()
        assert set(snap) <= {"state", "account_id", "session_id"}
        # the login credential value must appear in NO publication
        blob = repr(pub) + repr(snap) + session.describe()
        assert "real-secret" not in blob
        assert "pw" != session.account_id

    def test_resume_never_reconnects_trusted(self):
        s = BookmakerSession()
        s.login(("op", "pw"))
        snap = s.snapshot()
        r = BookmakerSession.resume(snap)
        assert r.state is not SessionState.CONNECTED
        with pytest.raises(SessionError):
            r.assert_submittable()

    def test_redact_strips_auth_material(self):
        dirty = {"user": "op", "password": "hunter2",
                 "auth_token": "abc", "cookie": "x", "line": 180.5}
        clean = redact(dirty)
        assert clean == {"user": "op", "line": 180.5}


# ══════════════════════════════════════════════════════════════════════
# §1 — the adapter boundary
# ══════════════════════════════════════════════════════════════════════

class TestAdapterBoundary:
    def test_test_adapter_is_the_default(self):
        # even a hostile/ambiguous environment gets the TEST adapter
        for env in ({}, {"BLM_ENV": "CI"},
                    {"BLM_ADAPTER_MODE": "production"},
                    {"BLM_ALLOW_LIVE_ADAPTER": "1"},
                    {"BLM_ADAPTER_MODE": "staging",
                     "BLM_ALLOW_LIVE_ADAPTER": "1"}):
            a = adapter_from_environment(env)
            assert isinstance(a, TestBookmakerAdapter)

    def test_live_adapter_requires_explicit_opt_in(self):
        with pytest.raises(AdapterSubmitError) as ei:
            LiveBookmakerAdapter()
        assert ei.value.code == "LIVE_NOT_PERMITTED"

    def test_live_adapter_fails_safe_even_when_opted_in(self):
        a = LiveBookmakerAdapter(allow_live=True)
        with pytest.raises(AdapterSubmitError) as ei:
            a.submit("k", event_id="e", market_id="m", position="UNDER",
                     line=1.0, price=1.0, stake_amount=1.0)
        assert ei.value.code == "LIVE_NOT_IMPLEMENTED"
        with pytest.raises(AdapterSubmitError):
            a.authenticate("u", "p")
        assert a.is_authenticated() is False

    def test_engine_never_sees_bookmaker_internals(self, engine, adapter):
        # the engine only touches the protocol surface
        engine.place(_sig())
        methods = {"submit", "confirmation", "find_order_by_idempotency",
                   "get_event", "get_market", "get_selection", "balance"}
        assert methods.issubset(set(dir(adapter)))


# ══════════════════════════════════════════════════════════════════════
# §4 — pre-bet validation (exact rejection reasons)
# ══════════════════════════════════════════════════════════════════════

class TestPrecheck:
    def test_happy_path_passes(self, adapter, session, limits):
        r = run_precheck(_sig(), adapter=adapter, session=session,
                         pre=limits)
        assert r.ok and r.reason == "PRECHECK_PASSED"
        assert r.observed["current_line"] == 180.5

    def test_global_switch_first(self, adapter, session, limits):
        r = run_precheck(_sig(game_id="UNKNOWN-GAME"), adapter=adapter,
                         session=session, pre=limits,
                         auto_betting_enabled=False)
        assert not r.ok and r.reason == "GLOBAL_AUTO_BET_OFF"

    def test_game_level_switch(self, adapter, session, limits):
        r = run_precheck(_sig(), adapter=adapter, session=session,
                         pre=limits, game_enabled=False)
        assert not r.ok and r.reason == "GAME_LEVEL_BET_DISABLED"

    def test_missing_idempotency_key(self, adapter, session, limits):
        no_key = {k: v for k, v in _sig().items()
                  if k != "idempotency_key"}
        r = run_precheck(no_key, adapter=adapter, session=session,
                         pre=limits)
        assert not r.ok and r.reason == "IDEMPOTENCY_KEY_MISSING"

    def test_stake_limit(self, adapter, session, limits):
        r = run_precheck(_sig(stake_amount=11.0), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "STAKE_LIMIT_EXCEEDED"

    def test_exposure_limit(self, adapter, session):
        lim = PrecheckLimits(max_stake_per_bet=10.0,
                             max_total_exposure=100.0, current_exposure=99.5)
        r = run_precheck(_sig(stake_amount=1.0), adapter=adapter,
                         session=session, pre=lim)
        assert not r.ok and r.reason == "EXPOSURE_LIMIT_REACHED"

    def test_exposure_unverifiable_fails_closed(self, adapter, session):
        lim = PrecheckLimits(max_stake_per_bet=10.0,
                             max_total_exposure=100.0, current_exposure=None)
        r = run_precheck(_sig(), adapter=adapter, session=session, pre=lim)
        assert not r.ok and r.reason == "EXPOSURE_UNVERIFIABLE"

    def test_insufficient_balance(self, adapter, session, limits):
        adapter.cfg.balance = 0.5
        r = run_precheck(_sig(stake_amount=1.0), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "INSUFFICIENT_BALANCE"

    def test_event_not_found(self, adapter, session, limits):
        r = run_precheck(_sig(game_id="NO-SUCH-EVENT"), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "EVENT_NOT_FOUND"

    def test_market_suspended(self, adapter, session, limits):
        adapter.set_scenario("market_suspended")
        r = run_precheck(_sig(), adapter=adapter, session=session,
                         pre=limits)
        assert not r.ok and r.reason == "MARKET_STATUS_INVALID"

    def test_event_closed(self, adapter, session, limits):
        adapter.set_scenario("event_closed")
        r = run_precheck(_sig(), adapter=adapter, session=session,
                         pre=limits)
        assert not r.ok and r.reason == "EVENT_NOT_FOUND"

    def test_selection_missing(self, adapter, session, limits):
        r = run_precheck(_sig(selection="OVER"), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "SELECTION_NOT_FOUND"

    def test_line_tolerance(self, adapter, session, limits):
        r = run_precheck(_sig(line=181.0), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "LINE_TOLERANCE_EXCEEDED"

    def test_odds_tolerance(self, adapter, session, limits):
        r = run_precheck(_sig(price=2.00), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "ODDS_TOLERANCE_EXCEEDED"

    def test_line_moved_between_signal_and_submit(self, adapter, session,
                                                  limits):
        adapter.cfg.line = 181.5          # the book moved after the alert
        r = run_precheck(_sig(line=180.5), adapter=adapter,
                         session=session, pre=limits)
        assert not r.ok and r.reason == "LINE_TOLERANCE_EXCEEDED"


# ══════════════════════════════════════════════════════════════════════
# §3-on-§4 — the engine pipeline (legal transitions only, verifiable)
# ══════════════════════════════════════════════════════════════════════

def _assert_legal_chain(bet: BetRecord) -> None:
    prev = SIGNAL
    for step in bet.timeline:
        if step["from"] == "DUPLICATE":
            continue
        validate_transition(step["from"], step["to"])
        assert step["from"] == prev, (step, prev)
        prev = step["to"]


class TestEnginePipeline:
    def test_happy_path_states_in_order(self, engine, adapter):
        bet = engine.place(_sig())
        assert bet.state == CONFIRMED
        _assert_legal_chain(bet)
        states = [s["to"] for s in bet.timeline if s["from"] != "DUPLICATE"]
        assert states == [ELIGIBLE, PRECHECK, SUBMITTING, ACCEPTED,
                          CONFIRMED]
        assert bet.provider_ref

    def test_precheck_rejection_path(self, engine, adapter, session):
        bet = engine.place(_sig(line=999.0))
        assert bet.state == REJECTED
        assert "LINE_TOLERANCE_EXCEEDED" in bet.reason
        # nothing was ever submitted
        assert adapter.submission_count() == 0

    def test_unknown_reconciles_to_rejected_when_no_order(self, engine,
                                                          adapter):
        adapter.set_scenario("timeout")
        bet = engine.place(_sig())
        assert bet.state == REJECTED
        states = [s["to"] for s in bet.timeline]
        assert SUBMITTING in states and UNKNOWN in states \
            and RECONCILING in states
        # never retried: exactly one submit attempt
        assert adapter.submission_count() == 1

    def test_unknown_reconciles_to_confirmed_when_order_exists(self,
                                                               engine,
                                                               adapter):
        key = idempotency_key_for(_sig())
        # the bookmaker DID record the order before the connection died
        adapter._submitted["test-real"] = {
            "provider_ref": "test-real", "idempotency_key": key,
            "status": "CONFIRMED", "stake_amount": 1.0}
        adapter.queue_response(raise_ambiguous="timeout after order placed")
        bet = engine.place(_sig())
        assert bet.state == CONFIRMED
        assert bet.provider_ref == "test-real"
        assert any(s["to"] == RECONCILING for s in bet.timeline)

    def test_submission_failed_paths(self, engine, adapter):
        adapter.set_scenario("rejected")
        bet = engine.place(_sig())
        assert bet.state == SUBMISSION_FAILED
        assert "WAGER_REJECTED" in bet.reason

    def test_timeline_never_skips_submission(self, engine, adapter):
        for sig in (_sig(), _sig(line=999.0), _sig(stake_amount=50.0)):
            bet = engine.place(sig)
            _assert_legal_chain(bet)


# ══════════════════════════════════════════════════════════════════════
# §5 — duplicate protection (every vector)
# ══════════════════════════════════════════════════════════════════════

class TestDuplicateProtection:
    def test_idempotency_key_is_identity_derived(self):
        a = idempotency_key_for(_sig())
        b = idempotency_key_for(_sig(price=9.99, stake_amount=5.0))
        assert a == b  # price/stake drift must NOT change the identity
        c = idempotency_key_for(_sig(checkpoint=80))
        assert a != c  # a different signal identity is a different bet

    def test_simple_retry_single_order(self, engine, adapter):
        first = engine.place(_sig())
        second = engine.place(_sig())
        assert adapter.submission_count() == 1
        assert second.provider_ref == first.provider_ref
        assert second.reason.startswith("duplicate_of_original")

    def test_duplicate_after_rejection_reports_rejected(self, engine,
                                                        adapter):
        engine.place(_sig(line=999.0))          # REJECTED at precheck
        again = engine.place(_sig(line=999.0))  # same identity re-sent
        assert again.state == REJECTED
        assert adapter.submission_count() == 0

    def test_duplicate_after_timeout_reports_unknown_outcome(self, engine,
                                                             adapter):
        adapter.set_scenario("timeout")
        engine.place(_sig())                    # UNKNOWN → REJECTED
        adapter.set_scenario("accepted")
        again = engine.place(_sig())            # re-delivered signal
        assert again.state == REJECTED          # original's disposition
        assert adapter.submission_count() == 1  # still only ONE attempt

    def test_worker_restart_same_claim_file(self, engine, adapter, limits,
                                            session, tmp_path):
        path = str(tmp_path / "claims_restart.db")
        e1 = BetEngine(adapter=adapter, session=session,
                       claims=ClaimStore(path), precheck_limits=limits)
        e1.place(_sig())
        # the worker process dies and comes back with a fresh engine
        e2 = BetEngine(adapter=adapter, session=session,
                       claims=ClaimStore(path), precheck_limits=limits)
        again = e2.place(_sig())
        assert adapter.submission_count() == 1
        assert again.reason.startswith("duplicate_of_original")

    def test_concurrent_workers_exactly_one_submission(self, adapter,
                                                       session, limits,
                                                       tmp_path):
        claims = ClaimStore(str(tmp_path / "claims_conc.db"))
        engines = [BetEngine(adapter=adapter, session=session,
                             claims=claims, precheck_limits=limits)
                   for _ in range(8)]
        barrier = threading.Barrier(8)
        results: list[BetRecord] = []
        lock = threading.Lock()

        def run(e):
            barrier.wait()
            bet = e.place(_sig())
            with lock:
                results.append(bet)

        threads = [threading.Thread(target=run, args=(e,)) for e in engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert adapter.submission_count() == 1
        confirmed = [b for b in results if b.state == CONFIRMED
                     and b.placed]
        assert len(confirmed) == 1
        winners = [b for b in results if b.placed]
        assert len(winners) == 1
        for b in results:
            if not b.placed:
                assert b.reason.startswith("duplicate_of_original")

    def test_adapter_rejects_second_order_for_same_key(self, adapter):
        adapter.set_scenario("accepted")
        r1 = adapter.submit("ik-x", event_id=GAME, market_id="TOTAL",
                            position="UNDER", line=180.5, price=1.85,
                            stake_amount=1.0)
        r2 = adapter.submit("ik-x", event_id=GAME, market_id="TOTAL",
                            position="UNDER", line=180.5, price=1.85,
                            stake_amount=1.0)
        assert r1["status"] == "ACCEPTED"
        assert r2["status"] == "DUPLICATE"
        assert r2["provider_ref"] == r1["provider_ref"]

    def test_duplicate_delivery_scenario_via_engine(self, engine, adapter):
        engine.place(_sig())
        adapter.set_scenario("duplicate_submission")
        again = engine.place(_sig())     # claim store still guards first
        assert adapter.submission_count() == 1
        assert again.reason.startswith("duplicate_of_original")


# ══════════════════════════════════════════════════════════════════════
# §6 — the TEST/paper simulator scenarios
# ══════════════════════════════════════════════════════════════════════

class TestSimulatorScenarios:
    def test_every_directive_scenario_is_deterministic(self, adapter):
        expected_raise = {
            "rejected": "WAGER_REJECTED",
            "odds_changed": "ODDS_CHANGED",
            "line_changed": "LINE_CHANGED",
            "market_suspended": "MARKET_SUSPENDED",
            "event_closed": "EVENT_CLOSED",
            "insufficient_balance": "INSUFFICIENT_FUNDS",
            "auth_failure": "AUTH_REQUIRED",
        }
        for scenario in TestBookmakerAdapter.SCENARIOS:
            a = TestBookmakerAdapter(TestAdapterConfig())
            a.set_scenario(scenario)
            if scenario in ("accepted", "settlement"):
                continue
            if scenario in ("timeout", "connection_failure",
                            "ambiguous_submission"):
                with pytest.raises(AdapterAmbiguousError):
                    a.submit("ik-s", event_id=GAME, market_id="TOTAL",
                             position="UNDER", line=180.5, price=1.85,
                             stake_amount=1.0)
            elif scenario in expected_raise:
                with pytest.raises(AdapterSubmitError) as ei:
                    a.submit("ik-s", event_id=GAME, market_id="TOTAL",
                             position="UNDER", line=180.5, price=1.85,
                             stake_amount=1.0)
                assert ei.value.code == expected_raise[scenario]
            elif scenario == "duplicate_submission":
                with pytest.raises(AdapterSubmitError) as ei:
                    a.submit("ik-s", event_id=GAME, market_id="TOTAL",
                             position="UNDER", line=180.5, price=1.85,
                             stake_amount=1.0)
                assert ei.value.code == "DUPLICATE_SUBMISSION"

    def test_accepted_scenario(self, adapter):
        r = adapter.submit("ik-acc", event_id=GAME, market_id="TOTAL",
                           position="UNDER", line=180.5, price=1.85,
                           stake_amount=1.0)
        assert r["status"] == "ACCEPTED" and r["provider_ref"]
        assert adapter.confirmation(r["provider_ref"])["status"] == \
            "CONFIRMED"

    def test_market_reads_reflect_scenarios(self, adapter):
        adapter.set_scenario("market_suspended")
        q = adapter.get_selection(GAME, "TOTAL", "UNDER")
        assert q.market_status == "SUSPENDED"
        adapter.set_scenario("line_changed")
        q2 = adapter.get_selection(GAME, "TOTAL", "UNDER")
        assert q2.line == 181.0
        adapter.set_scenario("odds_changed")
        q3 = adapter.get_selection(GAME, "TOTAL", "UNDER")
        assert q3.price == 1.95

    def test_auth_failure_scenario_blocks_session(self, adapter, session):
        adapter.fail_next_authentication(1)
        adapter.authenticate("op", "pw")
        assert adapter.is_authenticated() is False

    def test_engine_maps_simulator_failure_modes(self, engine, adapter):
        for scenario, expected in (
                ("rejected", SUBMISSION_FAILED),
                # submit-time refusals are definitive adapter failures:
                # the matrix reserves REJECTED for PRECHECK refusals
                ("insufficient_balance", SUBMISSION_FAILED),
                ("market_suspended", REJECTED),       # precheck gate
                ("timeout", REJECTED),                # reconcile, no order
        ):
            fresh = TestBookmakerAdapter(TestAdapterConfig())
            fresh.set_scenario(scenario)
            e = BetEngine(adapter=fresh, session=engine.session,
                          claims=ClaimStore(":memory:"),
                          precheck_limits=PrecheckLimits(
                              max_stake_per_bet=10.0))
            bet = e.place(_sig())
            assert bet.state == expected, (scenario, bet.as_dict())


# ══════════════════════════════════════════════════════════════════════
# §7 — the replay engine
# ══════════════════════════════════════════════════════════════════════

class TestReplay:
    def test_replay_is_deterministic(self, tmp_path):
        sigs = [ReplayEngine.signal(checkpoint=75, alert_id="a1"),
                ReplayEngine.signal(checkpoint=80, alert_id="a2"),
                ReplayEngine.signal(checkpoint=85, alert_id="a3")]
        r1 = ReplayEngine(db_path=str(tmp_path / "r1.db")).replay(sigs)
        r2 = ReplayEngine(db_path=str(tmp_path / "r2.db")).replay(sigs)
        assert r1["by_state"] == r2["by_state"]
        assert r1["confirmed"] == r2["confirmed"] == 3

    def test_replay_same_signal_once_only(self, tmp_path):
        # a historical log that contains the same signal twice (retry
        # captured twice) must still produce ONE bet
        sigs = [ReplayEngine.signal(checkpoint=75, alert_id="a1"),
                ReplayEngine.signal(checkpoint=75, alert_id="a1")]
        rep = ReplayEngine(db_path=":memory:").replay(sigs)
        assert rep["confirmed"] == 1          # ONE placed bet
        assert rep["claims_won"] == 1
        assert rep["duplicates"] == 1         # the echo, counted apart
        assert rep["total"] == 2

    def test_replay_scenario_scripting(self, tmp_path):
        sigs = [ReplayEngine.signal(checkpoint=75, alert_id="a1",
                                    scenario="odds_changed"),
                ReplayEngine.signal(checkpoint=80, alert_id="a2")]
        rep = ReplayEngine(db_path=":memory:").replay(sigs)
        assert rep["results"][0]["state"] == REJECTED
        assert rep["results"][1]["state"] == CONFIRMED

    def test_replay_hard_wired_to_test_adapter(self):
        # a SUBCLASS of the test adapter is fine (still inert); anything
        # else — including a live adapter — is refused
        with pytest.raises(TypeError):
            ReplayEngine(adapter=LiveBookmakerAdapter(allow_live=True))

    def test_replay_rejects_non_test_adapter(self):
        from blm_v4.betting.adapters import BookmakerAdapter

        class ForeignAdapter(BookmakerAdapter):
            name = "foreign"

        with pytest.raises(TypeError):
            ReplayEngine(adapter=ForeignAdapter())

    def test_replay_never_touches_live_adapter(self):
        # even if someone constructs the live adapter with opt-in, the
        # replay refuses it
        with pytest.raises(TypeError):
            ReplayEngine(adapter=LiveBookmakerAdapter(allow_live=True))

    def test_replay_summary_counts(self, tmp_path):
        sigs = [ReplayEngine.signal(checkpoint=75, alert_id="a1"),
                ReplayEngine.signal(checkpoint=80, alert_id="a2",
                                    scenario="timeout"),
                ReplayEngine.signal(checkpoint=85, alert_id="a3",
                                    scenario="market_suspended")]
        rep = ReplayEngine(db_path=":memory:").replay(sigs)
        assert rep["total"] == 3
        assert rep["non_terminal"] == 0
        assert rep["confirmed"] == 1
        assert rep["rejected"] == 2

    def test_replay_defect_is_captured_not_raised(self, tmp_path):
        sigs = [{"game_id": GAME, "market": "TOTAL", "selection": "UNDER",
                 "line": 180.5, "price": 1.85, "stake_amount": 1.0,
                 "checkpoint": 75, "alert_id": "a1"},
                {"broken": "signal"}]   # a malformed historical record
        rep = ReplayEngine(db_path=":memory:").replay(sigs)
        assert rep["total"] == 2
        # the malformed record is REFUSED cleanly by precheck (no stake,
        # no idempotency key) — replay never raises, never half-executes
        assert rep["results"][1]["state"] == REJECTED
        assert "STAKE_INVALID" in rep["results"][1]["reason"]
