"""REAL_MONEY_TEST one-shot manual authorization — safety contract tests.

Scope: the manual-only, single-use R2.00 (TOTAL/UNDER) authorization that lets
ONE manual execution proceed while the global AUTO-BET switch is OFF, WITHOUT
re-arming the autonomous executor.  All providers are stubs (no network, no CDP,
no real bet).

Decisions locked (2026-10-06):
  consume_on_valid_execution_request=True · token_required_when_global_off=True ·
  strict_game_id_binding=True · ttl=300s · market=TOTAL · direction=UNDER ·
  stake=2.00 ZAR · retries=0 · max_attempts=1 · cli_only · audited.
"""
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.betting import api
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.store import BettingStore
from blm_v4.betting.worker import BettingWorker

GID = "evt-canonical-001"
LINE = 193.5


def cfg(tmp_path, **kw):
    base = dict(dry_run=True, max_stake_per_bet=50, max_bets_per_day=10,
                max_daily_exposure=500, stake_units=1.0, min_unit_price=1,
                max_unit_price=1000, db_path=str(tmp_path / "betting.db"))
    base.update(kw)
    return BettingConfig(**base)


def game(gid=GID):
    captured = datetime.now(timezone.utc).isoformat()
    return {
        "game_id": gid, "live": True,
        "market": {"total_line": LINE, "market_status": "LIVE"},
        "projector": {"live_total_line": LINE, "market_status": "LIVE",
                      "market_age_seconds": 1, "captured_at": captured,
                      "required_pts_per_min": 5, "progress_pct": 80},
        "under_alert_eligibility": {"eligible": True},
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": LINE},
        "under_alert_fingerprint": {"fingerprints_fired": []},
    }


class StubProvider:
    """Records every provider call — the proof that a bet was/was not reached."""
    name = "stub"

    def __init__(self):
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "ACCEPTED",
                "provider_ref": "stub-" + str(kwargs.get("execution_id"))[:8]}


def client_for(tmp_path, games=None, **cfg_kw):
    store = BettingStore(str(tmp_path / "betting.db"))
    config = cfg(tmp_path, **cfg_kw)
    current = list(games) if games else [game()]
    api.configure_betting(store, config, live_payload_fn=lambda: current)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), store, config


def issue(store, gid=GID, ttl=300.0, stake=2.00):
    return store.issue_manual_authorization(
        mode="REAL_MONEY_TEST", stake=stake, currency="ZAR", market="TOTAL",
        direction="UNDER", game_id=gid, ttl_s=ttl, authorized_by="operator-test")


def payload(token=None, gid=GID, key="manual-r2-0001", **over):
    p = {"game_id": gid, "market": "TOTAL", "direction": "UNDER",
         "line": LINE, "stake": 2.00, "idempotency_key": key}
    if token is not None:
        p["authorization"] = token
    p.update(over)
    return p


# ── T1 — Auto-Bet OFF blocks autonomous execution ─────────────────────────
def test_autobet_off_blocks_autonomous_execution(tmp_path):
    store = BettingStore(str(tmp_path / "betting.db"))
    config = cfg(tmp_path)
    store.set_unit_price(2.0)
    assert store.is_enabled() is False

    out = evaluate(game(), cfg=config, store=store, enabled=store.is_enabled(),
                   unit_price=2.0, stats=store.today_stats())
    assert out["decision"] == "NO_BET"
    assert out["reason"] == "auto_betting_off"

    stub = StubProvider()
    w = BettingWorker(config, store, lambda: [game()])
    w.provider = stub
    summary = w.poll_once()
    assert summary["enabled"] is False
    assert summary["candidates"] == 0 and summary["executed"] == 0
    assert stub.calls == []


# ── T2 — a valid token permits exactly one manual execution ───────────────
def test_valid_token_permits_exactly_one_manual_execution(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    assert store.is_enabled() is False
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)

    token = issue(store)
    r = c.post("/api/v4/betting/manual", json=payload(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duplicate"] is False
    assert body["execution"]["status"] == "ACCEPTED"
    assert len(stub.calls) == 1
    assert store.manual_authorization_is_consumed(
        token, body["execution"]["execution_id"]) is True


# ── T3 — wrong binding is rejected (provider never reached, token kept) ───
@pytest.mark.parametrize("over", [
    {"game_id": "crossed"},
    {"direction": "OVER"},
    {"stake": 1.00},
])
def test_wrong_binding_rejected(tmp_path, monkeypatch, over):
    games = [game(GID), game("evt-other")]
    c, store, _ = client_for(tmp_path, games=games, dry_run=False)
    store.set_unit_price(2.0)
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)

    # token is bound to a DIFFERENT game than the request (and to UNDER/R2)
    token = issue(store, gid="evt-other")
    if over.get("game_id") == "crossed":
        req = payload(token, gid=GID, key="cross-0001")
    else:
        req = payload(token, gid="evt-other", key="cross-0001", **over)

    body = c.post("/api/v4/betting/manual", json=req).json()["execution"]
    assert body["status"] == "BLOCKED"
    assert body["error_code"] == "MANUAL_AUTHORIZATION_INVALID"
    assert stub.calls == []
    # a binding mismatch does NOT consume the token
    assert store.manual_authorization_is_consumed(token, "anything") is False


def test_wrong_market_rejected_by_endpoint(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)
    token = issue(store)
    r = c.post("/api/v4/betting/manual",
               json=payload(token, market="MONEYLINE"))
    assert r.status_code == 400
    assert stub.calls == []


# ── T4 — token replay is rejected ─────────────────────────────────────────
def test_token_replay_rejected(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)

    token = issue(store)
    first = c.post("/api/v4/betting/manual",
                   json=payload(token, key="replay-1")).json()
    assert first["execution"]["status"] == "ACCEPTED"
    second = c.post("/api/v4/betting/manual",
                    json=payload(token, key="replay-2")).json()
    assert second["execution"]["status"] == "BLOCKED"
    assert second["execution"]["error_code"] == "MANUAL_AUTHORIZATION_INVALID"
    assert len(stub.calls) == 1


# ── T5 — provider is never reached after a failed authorization ───────────
def test_provider_never_reached_after_failed_authorization(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)

    # no token → unchanged OFF behaviour
    b0 = c.post("/api/v4/betting/manual",
                json=payload(None, key="fail-0000")).json()["execution"]
    assert b0["status"] == "BLOCKED"
    assert b0["error_code"] == "GLOBAL_KILL_SWITCH"

    # invalid token
    b1 = c.post("/api/v4/betting/manual",
                json=payload("not-a-real-token", key="fail-0001")).json()["execution"]
    assert b1["status"] == "BLOCKED"
    assert b1["error_code"] == "MANUAL_AUTHORIZATION_INVALID"

    # expired token
    expired = issue(store, ttl=0.01)
    time.sleep(0.05)
    b2 = c.post("/api/v4/betting/manual",
                json=payload(expired, key="fail-0002")).json()["execution"]
    assert b2["status"] == "BLOCKED"
    assert b2["error_code"] == "MANUAL_AUTHORIZATION_INVALID"

    assert stub.calls == []


# ── T6 — ONE token → ONE attempt → consumed → SAME token again BLOCKED ────
def test_one_token_one_attempt_then_consumed_same_token_blocked(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)

    t1 = issue(store)
    first = c.post("/api/v4/betting/manual",
                   json=payload(t1, key="one-0001")).json()
    assert first["execution"]["status"] == "ACCEPTED"
    assert len(stub.calls) == 1

    # same token, fresh idempotency key → BLOCKED (already consumed)
    again = c.post("/api/v4/betting/manual",
                   json=payload(t1, key="one-0002")).json()
    assert again["execution"]["status"] == "BLOCKED"
    assert again["execution"]["error_code"] == "MANUAL_AUTHORIZATION_INVALID"
    assert len(stub.calls) == 1

    # A SEPARATELY issued token legitimately authorizes another test — the
    # application is NOT globally one-shot; the contract is per-token.
    t2 = issue(store)
    third = c.post("/api/v4/betting/manual",
                   json=payload(t2, key="one-0003")).json()
    assert third["execution"]["status"] == "ACCEPTED"
    assert len(stub.calls) == 2


# ── T7 — the autonomous path cannot use a token ───────────────────────────
def test_autonomous_path_cannot_use_token(tmp_path):
    store = BettingStore(str(tmp_path / "betting.db"))
    config = cfg(tmp_path)
    store.set_unit_price(2.0)
    token = issue(store)                      # a VALID, ISSUED token exists
    stub = StubProvider()
    w = BettingWorker(config, store, lambda: [game()])
    w.provider = stub
    summary = w.poll_once()                   # global OFF
    assert summary["enabled"] is False and stub.calls == []
    # the autonomous path never consumes the token
    assert store.manual_authorization_is_consumed(token, "worker") is False

    # Direct boundary: worker-style execute() presents no authorization → BLOCKED
    config2 = cfg(tmp_path, dry_run=False)
    store.set_config("auto_betting_enabled", "true")
    cand = evaluate(game(), cfg=config2, store=store, enabled=True,
                    unit_price=2.0, stats=store.today_stats())["candidate"]
    store.set_config("auto_betting_enabled", "false")
    res = execute(cand, cfg=config2, store=store, provider=StubProvider())
    assert res["status"] == "BLOCKED"
    assert res["error_code"] == "GLOBAL_KILL_SWITCH"


# ── T8 — an expired token is rejected (standalone) ────────────────────────
def test_expired_token_rejected(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)
    tok = issue(store, ttl=0.01)
    time.sleep(0.05)
    body = c.post("/api/v4/betting/manual", json=payload(tok)).json()["execution"]
    assert body["status"] == "BLOCKED"
    assert body["error_code"] == "MANUAL_AUTHORIZATION_INVALID"
    assert stub.calls == []


# ── issuance invariants are fail-closed ───────────────────────────────────
def test_issue_refuses_bad_invariants(tmp_path):
    store = BettingStore(str(tmp_path / "betting.db"))
    common = dict(mode="REAL_MONEY_TEST", stake=2.00, currency="ZAR",
                  market="TOTAL", direction="UNDER", game_id=GID, ttl_s=300,
                  authorized_by="operator-test")
    assert issue(store)                       # the exact invariants succeed
    for bad in ({**common, "stake": 3.00}, {**common, "currency": "USD"},
                {**common, "mode": "PRODUCTION_AUTO_BET"},
                {**common, "game_id": ""}, {**common, "authorized_by": ""},
                {**common, "ttl_s": 0}, {**common, "direction": "OVER"},
                {**common, "market": "MONEYLINE"}):
        with pytest.raises(ValueError):
            store.issue_manual_authorization(**bad)


# ── regression: global ON behaviour is unchanged (no token needed) ────────
def test_global_on_manual_unchanged_no_token_needed(tmp_path, monkeypatch):
    c, store, _ = client_for(tmp_path, dry_run=False)
    store.set_unit_price(2.0)
    store.set_config("auto_betting_enabled", "true")
    stub = StubProvider()
    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: stub)
    body = c.post("/api/v4/betting/manual", json=payload(None)).json()
    assert body["execution"]["status"] == "ACCEPTED"
    assert len(stub.calls) == 1
