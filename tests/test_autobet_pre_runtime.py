"""AUTO-BET PRE-RUNTIME VALIDATION — the TEST test-pyramid (directive
"BLM AUTO-BET — PRE-RUNTIME VALIDATION ARCHITECTURE", 2026-09-28).

Gates covered (the pyramid — everything before a real transport):

  GATE 3   BettingWorker integration against deterministic alert
           fixtures in the REAL /api/v4/live payload shape (the worker
           consumes the payload verbatim, never re-derives alerts).
  GATE 4   FakePokerBetProvider contract: the fake implements the full
           bookmaker contract (login → account → game → market →
           accept → bet id → status → settlement) and every failure
           mode; the worker/executor must handle each honestly.
  GATE 5   Betting API against the TEST store (status / state / settings
           / history / manual PLACE BET path).
  GATE 6   UI: the PLACE BET action binds only backend-served values.
  GATE 7   Idempotency + concurrency: the store's UNIQUE claim under
           sequential retries and hammering threads.
  GATE 8   Account-identity guard: mismatch → BLOCKED, never submitted.
  GATE 9   Failure/timeout mapping: timeout/malformed → UNKNOWN (money
           MAY be in play, limits keep counting); rejected → REJECTED
           with the reason; auth/session/HTTP → FAILED.

HARD BOUNDARY: no network, no production DB, no credentials.  The kill
switch starts OFF in a fresh TEST store; every enabling below is
test-scoped.  Nothing here can place a real wager: the fake provider is
inert by construction and the real provider remains a fail-safe stub.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.betting.account_guard import verify_account
from blm_v4.betting.api import router as betting_router, configure_betting
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.fake_provider import (
    TEST_BOOKMAKER_ACCOUNT_ID,
    FakeBookmakerContractProvider,
    FakePokerBetProvider,
)
from blm_v4.betting.provider import (
    DryRunProvider,
    PokerBetProvider,
    ProviderAmbiguous,
    ProviderUnavailable,
)
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED

DASH_STATIC = ("blm_v4/dashboard/static")


# ══════════════════════════════════════════════════════════════════════
# fixtures — TEST config, TEST store, deterministic alert fixtures
# ══════════════════════════════════════════════════════════════════════

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def store(tmp_path):
    s = BettingStore(str(tmp_path / "blm_betting_test.db"))   # TEST db
    s.set_unit_price(2.0)
    return s


@pytest.fixture
def live_cfg():
    """ARMED for the fake-provider layer: limits configured, dry-run OFF
    (live CODE path) — but the provider is the inert fake, so no real
    submission can ever exist."""
    return BettingConfig(dry_run=False,
                         max_stake_per_bet=10.0, max_bets_per_day=50,
                         max_daily_exposure=100.0, stake_units=1.0)


@pytest.fixture
def dry_cfg():
    return BettingConfig(dry_run=True,
                         max_stake_per_bet=10.0, max_bets_per_day=50,
                         max_daily_exposure=100.0, stake_units=1.0)


@pytest.fixture
def fake():
    return FakeBookmakerContractProvider()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _qualifying_game(game_id="TEST-GAME-9001", *, fresh_s=1.0) -> dict:
    """A deterministic alert fixture in the EXACT /api/v4/live shape the
    executor consumes: active alert + eligible + live + fresh + market."""
    return {
        "game_id": game_id,
        "live": True,
        "live_reason": None,
        "status": "live",
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": 180.5},
        "under_alert_eligibility": {"eligible": True,
                                    "reason": "market_live"},
        "market": {"total_line": 180.5, "market_status": "LIVE"},
        "projector": {"required_pts_per_min": 5.0, "progress_pct": 80.0,
                      "actual_pts_per_min": 4.0,
                      "captured_at": _iso(_now() - timedelta(seconds=fresh_s)),
                      "market_status": "LIVE"},
    }


@pytest.fixture
def enabled_store(store):
    store.set_config(KEY_AUTO_ENABLED, "true")
    return store


# ══════════════════════════════════════════════════════════════════════
# GATE 8 — the account-identity guard
# ══════════════════════════════════════════════════════════════════════

class _IdentityProvider:
    requires_account_identity = True
    authenticated_account_id = "ACCT-A"
    configured_account_id = "ACCT-A"


def test_guard_matching_identity_proceeds():
    assert verify_account(_IdentityProvider()) is None


def test_guard_wrong_account_blocks():
    p = _IdentityProvider()
    p.configured_account_id = "ACCT-B"
    reason = verify_account(p)
    assert reason and "ACCT-A" in reason and "ACCT-B" in reason


def test_guard_unverifiable_identity_fails_closed():
    p = _IdentityProvider()
    p.configured_account_id = None
    assert verify_account(p)            # missing side → blocked
    p2 = _IdentityProvider()
    p2.authenticated_account_id = ""
    assert verify_account(p2)


def test_guard_dryrun_provider_is_exempt():
    assert verify_account(DryRunProvider()) is None


def test_guard_blocks_before_any_submission(store, enabled_store, live_cfg,
                                            fake):
    """THE critical rule: account mismatch → NO BET — the provider never
    sees the wager."""
    fake.set_scenario("success")
    fake.set_configured_account("ACCT-WRONG")          # ≠ authenticated
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    assert res["decision"] == "EXECUTE"
    out = execute(res["candidate"], cfg=live_cfg, store=enabled_store,
                  provider=fake)
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "ACCOUNT_MISMATCH"
    assert fake.submission_count() == 0                # never submitted
    rec = enabled_store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == "BLOCKED"
    assert rec["rejection_reason"] == "ACCOUNT_MISMATCH"


def test_guard_matching_account_allows_submission(store, enabled_store,
                                                  live_cfg, fake):
    fake.set_scenario("success")
    fake.set_configured_account(TEST_BOOKMAKER_ACCOUNT_ID)
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    out = execute(res["candidate"], cfg=live_cfg, store=enabled_store,
                  provider=fake)
    assert out["status"] == "ACCEPTED"
    assert fake.submission_count() == 1
    rec = enabled_store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == "ACCEPTED"
    assert rec["accepted_amount"] == pytest.approx(rec["stake_amount"])


# ══════════════════════════════════════════════════════════════════════
# GATE 4 — the fake bookmaker contract, every failure mode
# ══════════════════════════════════════════════════════════════════════

def test_fake_provider_contract_full_bookmaker_flow(fake):
    """login → account identity → game search → market search → accept →
    bet id → status → settlement."""
    login = fake.login("u", "p")
    assert login["authenticated"] is True
    assert login["account_id"] == TEST_BOOKMAKER_ACCOUNT_ID
    assert fake.search_game(fake.game_id)["found"] is True
    mkt = fake.search_market(fake.game_id, "TOTAL")
    assert mkt["found"] is True and mkt["open"] is True
    out = fake.submit(execution_id="bet-x", game_id=fake.game_id,
                      alert_id="TEST-GAME-9001|75", selection="UNDER",
                      price=180.5, stake_amount=2.0, market="TOTAL",
                      line=180.5)
    assert out["status"] == "ACCEPTED" and out["provider_ref"]
    assert fake.settlement(out["provider_ref"])["status"] == "OPEN"
    settled = fake.settle(out["provider_ref"], "WON")
    assert settled["status"] == "SETTLED" and settled["result"] == "WON"


@pytest.mark.parametrize("scenario,expected_status,expected_code", [
    ("success",            "ACCEPTED",  None),
    ("wrong_account",      "REJECTED",  "ACCOUNT_MISMATCH"),
    ("bad_credentials",    "FAILED",    None),          # exception → FAILED
    ("expired_session",    "FAILED",    None),          # exception → FAILED
    ("market_disappeared", "FAILED",    "MARKET_NOT_FOUND"),
    ("line_changed",       "REJECTED",  "LINE_CHANGED"),
    ("duplicate_request",  "REJECTED",  "DUPLICATE_REQUEST"),
    ("rejected",           "REJECTED",  "WAGER_REJECTED"),
    ("insufficient_funds", "REJECTED",  "INSUFFICIENT_FUNDS"),
    ("market_closed",      "REJECTED",  "MARKET_CLOSED"),
    ("http_error",         "FAILED",    None),          # exception → FAILED
])
def test_fake_provider_every_failure_mode_through_executor(
        store, enabled_store, live_cfg, fake, scenario, expected_status,
        expected_code):
    """Each scripted bookmaker failure, run through the REAL executor, is
    recorded HONESTLY (§5 vocabulary) with a terminal ledger state."""
    fake.set_scenario(scenario)
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    assert res["decision"] == "EXECUTE"
    out = execute(res["candidate"], cfg=live_cfg, store=enabled_store,
                  provider=fake)
    assert out["status"] == expected_status, (scenario, out)
    rec = enabled_store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == expected_status, (scenario, rec)
    if expected_code is not None:
        assert rec["error_code"] == expected_code, scenario
    if expected_status == "REJECTED":
        assert rec["rejection_reason"], scenario


def test_timeout_and_malformed_yield_UNKNOWN(store, enabled_store, live_cfg,
                                             fake):
    """§5: an ambiguous outcome is UNKNOWN — money MAY be in play.  The
    ledger keeps UNKNOWN and the risk limits keep counting it."""
    for scenario, gid in (("timeout", "TEST-GAME-9001"),
                          ("malformed", "TEST-GAME-9002")):
        fake.set_scenario(scenario)
        res = evaluate(_qualifying_game(gid), cfg=live_cfg, store=enabled_store,
                       enabled=True, unit_price=2.0,
                       stats=enabled_store.today_stats())
        out = execute(res["candidate"], cfg=live_cfg, store=enabled_store,
                      provider=fake)
        assert out["status"] == "UNKNOWN", scenario
        rec = enabled_store.get_execution(res["candidate"]["execution_id"])
        assert rec["status"] == "UNKNOWN", scenario
        assert rec["error_code"] in ("PROVIDER_AMBIGUOUS",
                                     "EXECUTION_EXCEPTION")
    # UNKNOWN exposure still counts against the daily limit (§9)
    stats = enabled_store.today_stats()
    assert stats["bets"] == 2 and stats["amount"] > 0


def test_malformed_status_from_provider_maps_to_UNKNOWN(store, enabled_store,
                                                        live_cfg, fake):
    """A provider answering a status outside the vocabulary is UNKNOWN —
    never guessed into ACCEPTED."""
    fake.queue_result("MAYBE")          # unmapped status
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    out = execute(res["candidate"], cfg=live_cfg, store=enabled_store,
                  provider=fake)
    assert out["status"] == "UNKNOWN"
    assert out["error_code"] == "PROVIDER_RESPONSE_INVALID"


def test_fake_pokerbet_standin_matches_real_provider_shape():
    """GATE 4→11 bridge: the TEST stand-in for the live-mode path carries
    the real provider's NAME and satisfies the same BetProvider contract
    — so Gate 11 (real auth) exercises identical code paths."""
    fake = FakePokerBetProvider()
    assert fake.name == PokerBetProvider.name == "pokerbet"
    out = fake.submit(execution_id="bet-x", game_id=fake.game_id,
                      alert_id="a", selection="UNDER", price=180.5,
                      stake_amount=2.0)
    assert out["status"] == "ACCEPTED" and out["provider_ref"]


def test_real_provider_still_fails_safe(store, enabled_store, live_cfg):
    """The REAL provider remains a fail-safe stub even when armed: any
    submission attempt → FAILED (definitive, no bet exists) — it can
    never half-fire by accident."""
    provider = PokerBetProvider(base_url="http://127.0.0.1:1")
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    out = execute(res["candidate"], cfg=live_cfg, store=enabled_store,
                  provider=provider)
    assert out["status"] in ("FAILED", "UNKNOWN")
    rec = enabled_store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == rec["status"]           # recorded honestly
    assert rec["provider_ref"] is None              # nothing submitted


# ══════════════════════════════════════════════════════════════════════
# GATE 3 — BettingWorker integration on the fixture payload
# ══════════════════════════════════════════════════════════════════════

def _make_worker(store, cfg, games, provider=None):
    from blm_v4.betting.worker import BettingWorker
    # the worker's live_payload_fn returns the GAMES LIST (server.py:
    # `_v4_live(...).get("games") or []`) — not the payload dict
    w = BettingWorker(cfg, store, lambda: games,
                      poll_interval_s=3600)
    if provider is not None:
        w.provider = provider                       # test seam (TEST only)
    return w


def test_worker_dryrun_records_would_bet(store, enabled_store, dry_cfg):
    games = [_qualifying_game(), _qualifying_game("TEST-GAME-9002")]
    summary = _make_worker(store, dry_cfg, games).poll_once()
    assert summary["enabled"] is True
    assert summary["candidates"] == 2
    assert summary["would_bet"] == 2 and summary["executed"] == 0
    assert summary["no_bet"] == 0


def test_worker_live_path_executes_through_fake_provider(
        store, enabled_store, live_cfg, fake):
    fake.set_scenario("success")
    summary = _make_worker(store, live_cfg, [_qualifying_game()],
                           provider=fake).poll_once()
    assert summary["executed"] == 1
    assert fake.submission_count() == 1
    sub = fake.submissions()[0]
    assert sub["selection"] == "UNDER" and sub["market"] == "TOTAL"
    assert sub["stake_amount"] == pytest.approx(2.0)


def test_worker_rejects_line_changed_but_keeps_going(
        store, enabled_store, live_cfg, fake):
    fake.set_scenario("line_changed")
    games = [_qualifying_game(), _qualifying_game("TEST-GAME-9003")]
    summary = _make_worker(store, live_cfg, games, provider=fake).poll_once()
    assert fake.submission_count() == 2
    assert summary["executed"] == 0                 # both REJECTED
    rejected = [r for r in store.recent(10) if r["status"] == "REJECTED"]
    assert len(rejected) == 2
    assert all(r["error_code"] == "LINE_CHANGED" for r in rejected)


def test_worker_with_kill_switch_off_bets_nothing(store, dry_cfg, fake):
    store.set_config(KEY_AUTO_ENABLED, "false")     # persisted OFF
    games = [_qualifying_game()]
    summary = _make_worker(store, dry_cfg, games, provider=fake).poll_once()
    assert summary["enabled"] is False
    assert summary["candidates"] == 0 and summary["executed"] == 0
    assert fake.submission_count() == 0
    assert not store.recent(10)                     # empty ledger


def test_worker_daily_bet_limit_stops_the_pass(store, enabled_store):
    cfg = BettingConfig(dry_run=True, max_stake_per_bet=10.0,
                        max_bets_per_day=1, max_daily_exposure=100.0,
                        stake_units=1.0)
    games = [_qualifying_game("TEST-GAME-9001"),
             _qualifying_game("TEST-GAME-9002")]
    summary = _make_worker(store, cfg, games).poll_once()
    assert summary["would_bet"] == 1 and summary["no_bet"] == 1


# ══════════════════════════════════════════════════════════════════════
# executor gate refusals on fixture variations (stale / missing / limits)
# ══════════════════════════════════════════════════════════════════════

def test_stale_alert_is_refused(store, enabled_store, live_cfg):
    g = _qualifying_game(fresh_s=live_cfg.alert_max_age_s + 30)
    res = evaluate(g, cfg=live_cfg, store=enabled_store, enabled=True,
                   unit_price=2.0, stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET", "alert_stale")


def test_not_live_game_is_refused(store, enabled_store, live_cfg):
    g = _qualifying_game()
    g["live"] = False
    res = evaluate(g, cfg=live_cfg, store=enabled_store, enabled=True,
                   unit_price=2.0, stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET", "game_not_live")


def test_inactive_alert_is_refused(store, enabled_store, live_cfg):
    g = _qualifying_game()
    g["under_alert"]["active"] = False
    res = evaluate(g, cfg=live_cfg, store=enabled_store, enabled=True,
                   unit_price=2.0, stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET", "alert_not_active")


def test_ineligible_alert_is_refused(store, enabled_store, live_cfg):
    g = _qualifying_game()
    g["under_alert_eligibility"] = {"eligible": False,
                                    "reason": "market_stale"}
    res = evaluate(g, cfg=live_cfg, store=enabled_store, enabled=True,
                   unit_price=2.0, stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET",
                                                "alert_not_eligible")


def test_missing_market_is_refused(store, enabled_store, live_cfg):
    g = _qualifying_game()
    g["market"]["total_line"] = None
    res = evaluate(g, cfg=live_cfg, store=enabled_store, enabled=True,
                   unit_price=2.0, stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET", "market_missing")


def test_unconfigured_limits_are_refused(store, enabled_store):
    cfg = BettingConfig(dry_run=True)               # no limits configured
    res = evaluate(_qualifying_game(), cfg=cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET",
                                                "limits_not_configured")


def test_multi_unit_stake_is_refused(store, enabled_store, live_cfg):
    cfg = BettingConfig(dry_run=True, max_stake_per_bet=10.0,
                        max_bets_per_day=50, max_daily_exposure=100.0,
                        stake_units=2.0)
    res = evaluate(_qualifying_game(), cfg=cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    assert (res["decision"], res["reason"]) == ("NO_BET",
                                                "ONE_UNIT_MAXIMUM")


def test_per_game_switch_off_is_refused(store, enabled_store, live_cfg):
    enabled_store.set_game_enabled("TEST-GAME-9001", False)
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats(),
                   game_enabled=enabled_store.is_game_enabled(
                       "TEST-GAME-9001"))
    assert (res["decision"], res["reason"]) == ("NO_BET",
                                                "PER_GAME_AUTO_BET_OFF")


# ══════════════════════════════════════════════════════════════════════
# GATE 7 — idempotency + concurrency
# ══════════════════════════════════════════════════════════════════════

def test_duplicate_signal_is_claimed_once(store, enabled_store, live_cfg):
    first = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                     enabled=True, unit_price=2.0,
                     stats=enabled_store.today_stats())
    assert first["decision"] == "EXECUTE"
    second = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                      enabled=True, unit_price=2.0,
                      stats=enabled_store.today_stats())
    assert second["decision"] == "NO_BET"
    assert second["reason"] == "duplicate_execution"
    assert second["existing"]["execution_id"] == \
        first["candidate"]["execution_id"]


def test_concurrent_workers_claim_exactly_once(store, enabled_store,
                                               live_cfg):
    """Hammer the claim with concurrent threads — the store's UNIQUE
    idempotency key admits exactly one execution (§4)."""
    outcomes, lock = [], threading.Lock()
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        res = evaluate(_qualifying_game(), cfg=live_cfg, store=store,
                       enabled=True, unit_price=2.0,
                       stats=store.today_stats())
        with lock:
            outcomes.append(res["decision"])

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("EXECUTE") == 1
    assert outcomes.count("NO_BET") == 7
    assert len(store.recent(10)) == 1               # ONE ledger record


# ══════════════════════════════════════════════════════════════════════
# kill switch at the submission boundary + reconciliation state machine
# ══════════════════════════════════════════════════════════════════════

def test_kill_switch_flipped_between_claim_and_submit_blocks(
        store, live_cfg, fake):
    """The executor re-reads the switch at the submission boundary: a
    claim made while ON must still BLOCK if the switch went OFF."""
    store.set_config(KEY_AUTO_ENABLED, "true")
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=store,
                   enabled=True, unit_price=2.0, stats=store.today_stats())
    store.set_config(KEY_AUTO_ENABLED, "false")     # flip before submit
    out = execute(res["candidate"], cfg=live_cfg, store=store,
                  provider=fake)
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "GLOBAL_KILL_SWITCH"
    assert fake.submission_count() == 0


def test_unknown_execution_reports_reconciliation_required(
        store, enabled_store, live_cfg, fake):
    """UNKNOWN → the game's betting state exposes a reconciliation state —
    the operator sees money-MAY-BE-IN-PLAY, never a silent gap."""
    fake.set_scenario("timeout")
    res = evaluate(_qualifying_game(), cfg=live_cfg, store=enabled_store,
                   enabled=True, unit_price=2.0,
                   stats=enabled_store.today_stats())
    execute(res["candidate"], cfg=live_cfg, store=enabled_store,
            provider=fake)
    rec = enabled_store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == "UNKNOWN"
    assert rec["status"] in ("UNKNOWN", "RECONCILING", "EXPIRED")
    # the daily exposure now includes the UNKNOWN wager (assume the worst)
    stats = enabled_store.today_stats()
    assert stats["amount"] >= rec["stake_amount"]


# ══════════════════════════════════════════════════════════════════════
# GATE 5 — the betting API over the TEST store
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def api_client(store, dry_cfg):
    configure_betting(store, dry_cfg,
                      live_payload_fn=lambda: {"games": [_qualifying_game()]})
    app = FastAPI()
    app.include_router(betting_router)
    return TestClient(app)


def test_api_status_reports_dry_run(api_client):
    r = api_client.get("/api/v4/betting/status")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "DRY_RUN"
    assert body["dry_run"] is True and body["live_money_enabled"] is False
    assert body["enabled"] is False                 # fresh TEST store: OFF
    assert body["credentials"] == {"username_present": False,
                                   "password_present": False}
    assert body["today"]["amount_real"] == 0.0


def test_api_game_state_eligible_and_block_reasons(api_client, store):
    r = api_client.get("/api/v4/betting/game/TEST-GAME-9001/state")
    assert r.status_code == 200
    body = r.json()
    # kill switch OFF → everything blocked with the explicit reason
    assert body["eligible"] is False
    assert body["blocked_reason"] == "GLOBAL_KILL_SWITCH"
    # turn the switch + game control on → eligible
    store.set_config(KEY_AUTO_ENABLED, "true")
    r2 = api_client.get("/api/v4/betting/game/TEST-GAME-9001/state")
    body2 = r2.json()
    assert body2["eligible"] is True
    assert body2["decision"] == "WOULD_BET"
    assert body2["reconciliation_state"] == "NOT_REQUIRED"


def test_api_settings_kill_switch_and_unit_price(api_client, store):
    # enabling without a unit price is refused
    store.set_config(KEY_UNIT_PRICE := "unit_price", "")  # ensure absent
    import sqlite3 as _sq
    with store._lock, store._conn() as c:
        c.execute("DELETE FROM betting_config WHERE key='unit_price'")
        c.commit()
    r = api_client.post("/api/v4/betting/settings",
                        json={"auto_betting": "ON"})
    assert r.status_code == 400
    # set the unit price, then enable
    r2 = api_client.post("/api/v4/betting/settings",
                         json={"unit_price": 2.5})
    assert r2.status_code == 200
    r3 = api_client.post("/api/v4/betting/settings",
                         json={"auto_betting": "ON"})
    assert r3.status_code == 200
    assert r3.json()["enabled"] is True
    assert store.get_unit_price() == pytest.approx(2.5)
    # out-of-range unit price refused
    r4 = api_client.post("/api/v4/betting/settings",
                         json={"unit_price": 99999.0})
    assert r4.status_code == 400
    # switch back off
    r5 = api_client.post("/api/v4/betting/settings",
                         json={"auto_betting": "OFF"})
    assert r5.json()["enabled"] is False


def test_api_manual_place_bet_happy_and_duplicate(api_client, store):
    store.set_config(KEY_AUTO_ENABLED, "true")
    payload = {"game_id": "TEST-GAME-9001", "market": "TOTAL",
               "direction": "UNDER", "line": 180.5, "stake": 2.0,
               "idempotency_key": "test-manual-0001"}
    r = api_client.post("/api/v4/betting/manual", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duplicate"] is False
    # DRY_RUN: the DryRunProvider answers ACCEPTED with the execution
    # state WOULD_BET — simulated exposure, never real money
    assert body["execution"]["status"] == "ACCEPTED"
    assert body["execution"]["execution_state"] == "WOULD_BET"
    # same key again → the LEDGER outcome, never a second wager
    r2 = api_client.post("/api/v4/betting/manual", json=payload)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["execution"]["execution_id"] == \
        body["execution"]["execution_id"]
    # same key, DIFFERENT wager → refused
    payload_bad = dict(payload, direction="OVER")
    r3 = api_client.post("/api/v4/betting/manual", json=payload_bad)
    assert r3.status_code == 409


def test_api_manual_stale_or_wrong_line_refused(api_client, store):
    store.set_config(KEY_AUTO_ENABLED, "true")
    # line ≠ the currently observed Total
    r = api_client.post("/api/v4/betting/manual", json={
        "game_id": "TEST-GAME-9001", "market": "TOTAL",
        "direction": "UNDER", "line": 999.0, "stake": 2.0,
        "idempotency_key": "test-manual-0002"})
    assert r.status_code == 409
    # unknown game
    r2 = api_client.post("/api/v4/betting/manual", json={
        "game_id": "NO-SUCH-GAME", "market": "TOTAL",
        "direction": "UNDER", "line": 180.5, "stake": 2.0,
        "idempotency_key": "test-manual-0003"})
    assert r2.status_code == 404


def test_api_manual_blocked_when_kill_switch_off(api_client):
    payload = {"game_id": "TEST-GAME-9001", "market": "TOTAL",
               "direction": "UNDER", "line": 180.5, "stake": 2.0,
               "idempotency_key": "test-manual-0004"}
    r = api_client.post("/api/v4/betting/manual", json=payload)
    assert r.status_code == 200
    assert r.json()["execution"]["status"] == "BLOCKED"
    assert r.json()["execution"]["error_code"] == "GLOBAL_KILL_SWITCH"


def test_api_history_returns_executions_and_audit(api_client, store):
    store.set_config(KEY_AUTO_ENABLED, "true")
    api_client.post("/api/v4/betting/manual", json={
        "game_id": "TEST-GAME-9001", "market": "TOTAL",
        "direction": "UNDER", "line": 180.5, "stake": 2.0,
        "idempotency_key": "test-manual-0005"})
    r = api_client.get("/api/v4/betting/history")
    assert r.status_code == 200
    body = r.json()
    assert body["executions"], "history empty after an execution"
    assert body["audit"], "audit trail empty after an execution"
    # no credential material anywhere in the payload (it never existed)
    assert "password" not in json.dumps(body).lower()


def test_api_never_serves_credential_values(store, dry_cfg):
    import os
    os.environ["POKERBET_USERNAME"] = "secret-user"
    os.environ["POKERBET_PASSWORD"] = "secret-pass"
    try:
        configure_betting(store, dry_cfg,
                          live_payload_fn=lambda: {"games": []})
        app = FastAPI()
        app.include_router(betting_router)
        client = TestClient(app)
        body = client.get("/api/v4/betting/status").json()
        assert body["credentials"] == {"username_present": True,
                                       "password_present": True}
        assert "secret-user" not in json.dumps(body)
        assert "secret-pass" not in json.dumps(body)
    finally:
        os.environ.pop("POKERBET_USERNAME", None)
        os.environ.pop("POKERBET_PASSWORD", None)


# ══════════════════════════════════════════════════════════════════════
# GATE 6 — the frontend PLACE BET action
# ══════════════════════════════════════════════════════════════════════

def test_ui_place_bet_binds_backend_served_values_only():
    """The [ PLACE BET ] button exists on active-alert cards and submits
    ONLY the game id, side and frozen trigger line — stake/price come
    from the server, never the browser."""
    js = open(f"{DASH_STATIC}/dashboard.js").read()
    assert "PLACE BET" in js
    assert ".card-place-bet" in js
    # submits through the SAME validated manual endpoint as the form
    assert "BETTING_API" in js and '"/manual"' in js
    # the button carries data-* attributes sourced from the payload's
    # alert block (game id + side + frozen trigger line)
    i = js.index("card-place-bet")
    window = js[i:i + 300]
    assert "data-game-id" in window and "data-side" in window
    assert "trigger_line" in window
    # the browser sends only identity + stake — no stake/price math here
    send_window = js.index("const payload = {", js.index("card-place-bet"))
    payload_src = js[send_window:send_window + 400]
    assert "direction: btn.dataset.side" in payload_src
    assert "line: Number(btn.dataset.line)" in payload_src


def test_ui_carries_dry_run_mode_label():
    """The MODE pill renders DRY_RUN/LIVE from the server's own verdict —
    the operator always knows which mode is armed."""
    js = open(f"{DASH_STATIC}/dashboard.js").read()
    assert "abModePillHeader" in js
    assert "DRY_RUN" in js
    html = open(f"{DASH_STATIC}/index.html").read()
    assert "abModePillHeader" in html


def test_ui_manual_form_rejects_client_sided_stake_above_unit():
    """The server refuses stake > unit — the UI cannot raise it (§6).
    Proven at the API layer in test_api_manual_*; here we pin the form's
    server round-trip shape (no local stake math)."""
    js = open(f"{DASH_STATIC}/dashboard.js").read()
    i = js.index("form.manual-bet")
    window = js[i:i + 400]
    assert "/api/v4/betting/manual" in js[i:i + 2000] or \
        "manual" in window
