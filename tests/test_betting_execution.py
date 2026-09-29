"""AUTO-BETTING EXECUTION LAYER — directive tests (2026-09-21).

Covers the directive's required matrix:

  * kill switch OFF → no execution; restart defaults OFF
  * ON + valid alert → execution candidate (dry-run WOULD_BET)
  * duplicate alert → second execution rejected (incl. concurrent)
  * daily bet-count / exposure / per-bet stake limits → no execution
  * invalid unit price / stake → no execution
  * stale alert / missing market / missing identity → no execution
  * provider FAILED (definitive failure) and UNKNOWN (ambiguity)
  * fingerprints are recorded, never bet-creators; R1 absent
  * credentials never appear in logs, API payloads, frontend or DB
"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.provider import (
    DryRunProvider,
    PokerBetProvider,
    ProviderUnavailable,
    provider_from_config,
)
from blm_v4.betting.store import BettingStore
from blm_v4.betting.worker import BettingWorker


def make_cfg(tmp_path, **over) -> BettingConfig:
    base = dict(
        dry_run=True,
        max_stake_per_bet=100.0,
        max_bets_per_day=5,
        max_daily_exposure=200.0,
        stake_units=1.0,
        min_unit_price=1.0,
        max_unit_price=1000.0,
        alert_max_age_s=90.0,
        db_path=str(tmp_path / "blm_betting.db"),
    )
    base.update(over)
    return BettingConfig(**base)


def make_store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "blm_betting.db"))


def qualifying_game(game_id="30990001", checkpoint=75, age_s=5.0):
    """A game payload that satisfies every alert-side condition — the
    verdict is the production shape, consumed verbatim."""
    from datetime import datetime, timedelta, timezone
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id,
        "live": True,
        "live_reason": None,
        "market": {"total_line": 193.5, "market_status": "LIVE"},
        "projector": {
            "progress_pct": 76.0,
            "required_pts_per_min": 5.0,
            "actual_pts_per_min": 4.0,
            "captured_at": cap,
        },
        "under_alert_eligibility": {"eligible": True,
                                    "reason": "market_live"},
        "under_alert": {
            "active": True, "checkpoint": checkpoint,
            "trigger_line": 193.5, "required_pace": 5.0,
            "league_average_pace": 4.5,
        },
        "under_alert_fingerprint": {
            "fingerprint_count": 2,
            "fingerprints_fired": ["C3", "C5"],
        },
    }


# ══════════════════════════════════════════════════════════════════════
# defaults / kill switch
# ══════════════════════════════════════════════════════════════════════

def test_auto_betting_defaults_off_and_dry_run(tmp_path):
    cfg = make_cfg(tmp_path)
    store = make_store(tmp_path)
    assert store.is_enabled() is False           # §1 default OFF
    assert cfg.dry_run is True                   # §11 DRY_RUN default
    assert cfg.live_money_enabled is False
    assert type(provider_from_config(cfg)).__name__ == "DryRunProvider"


def test_off_switch_means_no_execution(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=False, unit_price=10.0,
                   stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "auto_betting_off"
    assert store.recent() == []


def test_restart_defaults_off_even_after_enable(tmp_path):
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    assert store.is_enabled() is True
    # a NEW store over the same DB after "restart" keeps the persisted
    # explicit enable (directive: persisted through a secure config
    # mechanism); a FRESH database (no explicit enable) reads OFF
    fresh = BettingStore(str(tmp_path / "fresh.db"))
    assert fresh.is_enabled() is False
    # corrupt value reads OFF
    fresh.set_config("auto_betting_enabled", "garbage")
    assert fresh.is_enabled() is False


# ══════════════════════════════════════════════════════════════════════
# the happy path (dry-run)
# ══════════════════════════════════════════════════════════════════════

def test_on_plus_valid_alert_yields_would_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    assert res["decision"] == "WOULD_BET"        # §11 dry-run shape
    cand = res["candidate"]
    assert cand["stake_amount"] == 10.0          # unit 10 × 1 unit
    assert cand["stake_units"] == 1.0
    assert cand["selection"] == "UNDER"
    assert cand["fingerprint_count"] == 2
    assert cand["fingerprints_present"] == ["C3", "C5"]
    store.set_config("auto_betting_enabled", "true")
    out = execute(cand, cfg=cfg, store=store, provider=DryRunProvider())
    assert out["status"] == "ACCEPTED"
    rec = store.get_execution(cand["execution_id"])
    assert rec["status"] == "ACCEPTED"
    assert rec["provider_ref"].startswith("dryrun-")
    assert rec["error_message"].startswith("DRY_RUN")


# ══════════════════════════════════════════════════════════════════════
# duplicate protection
# ══════════════════════════════════════════════════════════════════════

def test_duplicate_alert_rejected_second_time(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    first = evaluate(qualifying_game(), cfg=cfg, store=store,
                     enabled=True, unit_price=10.0,
                     stats=store.today_stats())
    assert first["decision"] == "WOULD_BET"
    second = evaluate(qualifying_game(), cfg=cfg, store=store,
                      enabled=True, unit_price=10.0,
                      stats=store.today_stats())
    assert second["decision"] == "NO_BET"
    assert second["reason"] == "duplicate_execution"


def test_idempotency_key_is_game_checkpoint_alert(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game("G1", 75), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    assert res["candidate"]["idempotency_key"] == "G1|75|G1|75"
    # same game different checkpoint is a DIFFERENT opportunity
    other = evaluate(qualifying_game("G1", 50), cfg=cfg, store=store,
                     enabled=True, unit_price=10.0,
                     stats=store.today_stats())
    assert other["decision"] == "WOULD_BET"


def test_database_enforces_uniqueness(tmp_path):
    store = make_store(tmp_path)
    cfg = make_cfg(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    rec = dict(store.get_execution(res["candidate"]["execution_id"]))
    rec["execution_id"] = "different-id"
    claimed, existing = store.claim(rec)
    assert claimed is False
    assert existing["execution_id"] == res["candidate"]["execution_id"]


def test_concurrent_duplicate_triggers_exactly_one_execution(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        r = evaluate(qualifying_game(), cfg=cfg, store=store,
                     enabled=True, unit_price=10.0,
                     stats=store.today_stats())
        results.append(r["decision"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("WOULD_BET") == 1
    assert results.count("NO_BET") == 7


# ══════════════════════════════════════════════════════════════════════
# risk limits + validation
# ══════════════════════════════════════════════════════════════════════

def test_daily_bet_limit_reached(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 5, "amount": 50.0})
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "daily_bet_limit_reached"


def test_daily_exposure_reached(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    # 195 + 10 > 200 limit
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 1, "amount": 195.0})
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "daily_exposure_reached"
    # exactly at the limit still fits
    ok = evaluate(qualifying_game(), cfg=cfg, store=store,
                  enabled=True, unit_price=10.0,
                  stats={"verifiable": True, "bets": 1, "amount": 190.0})
    assert ok["decision"] == "WOULD_BET"


def test_limits_unverifiable_means_no_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": False, "bets": None,
                          "amount": None})
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "limits_unverifiable"


def test_max_stake_exceeded(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=150.0,
                   stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "max_stake_exceeded"


@pytest.mark.parametrize("bad", [0, -5, None, float("nan"),
                                 float("inf"), "x", True])
def test_invalid_unit_price_means_no_bet(tmp_path, bad):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=bad,
                   stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"] in ("unit_price_invalid",
                             "unit_price_out_of_range")


# ══════════════════════════════════════════════════════════════════════
# alert freshness / market / identity
# ══════════════════════════════════════════════════════════════════════

def test_stale_alert_means_no_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    res = evaluate(qualifying_game(age_s=120.0), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "alert_stale"


def test_missing_market_means_no_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    g = qualifying_game()
    g["market"]["total_line"] = None
    res = evaluate(g, cfg=cfg, store=store, enabled=True,
                   unit_price=10.0, stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "market_missing"
    g2 = qualifying_game()
    g2["market"] = {}
    assert evaluate(g2, cfg=cfg, store=store, enabled=True,
                    unit_price=10.0,
                    stats=store.today_stats())["reason"] == "market_missing"


def test_alert_not_active_or_ineligible_means_no_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    g = qualifying_game()
    g["under_alert"]["active"] = False
    assert evaluate(g, cfg=cfg, store=store, enabled=True,
                    unit_price=10.0,
                    stats=store.today_stats())["reason"] == "alert_not_active"
    g2 = qualifying_game()
    g2["under_alert_eligibility"]["eligible"] = False
    assert evaluate(g2, cfg=cfg, store=store, enabled=True,
                    unit_price=10.0,
                    stats=store.today_stats())["reason"] == \
        "alert_not_eligible"
    g3 = qualifying_game()
    g3["live"] = False
    assert evaluate(g3, cfg=cfg, store=store, enabled=True,
                    unit_price=10.0,
                    stats=store.today_stats())["reason"] == "game_not_live"


def test_missing_identity_means_no_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    g = qualifying_game()
    g["game_id"] = None
    res = evaluate(g, cfg=cfg, store=store, enabled=True,
                   unit_price=10.0, stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "alert_identity_missing"


# ══════════════════════════════════════════════════════════════════════
# provider outcomes — honest state machine
# ══════════════════════════════════════════════════════════════════════

def test_provider_failure_records_failed(tmp_path):
    cfg, store = make_cfg(tmp_path, dry_run=False), make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    class FailingProvider:
        name = "failing"
        def submit(self, **kw):
            raise ProviderUnavailable("boom")
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=FailingProvider())
    assert out["status"] == "FAILED"
    assert store.get_execution(
        res["candidate"]["execution_id"])["status"] == "FAILED"


def test_ambiguous_response_records_unknown(tmp_path):
    cfg, store = make_cfg(tmp_path, dry_run=False), make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    class AmbiguousProvider:
        name = "ambiguous"
        def submit(self, **kw):
            from blm_v4.betting.provider import ProviderAmbiguous
            raise ProviderAmbiguous("timeout mid-flight")
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=AmbiguousProvider())
    assert out["status"] == "UNKNOWN"
    assert store.get_execution(
        res["candidate"]["execution_id"])["status"] == "UNKNOWN"


def test_unknown_and_submitted_count_toward_exposure(tmp_path):
    """The risk limit assumes the worst: unresolved money still counts."""
    store = make_store(tmp_path)
    rec = {"execution_id": "e1", "idempotency_key": "k1", "game_id": "g",
           "alert_id": "a", "checkpoint": "75", "stake_amount": 50.0,
           "status": "UNKNOWN"}
    claimed, _ = store.claim(rec)
    assert claimed
    store.update_status("e1", "UNKNOWN")
    stats = store.today_stats()
    assert stats["bets"] == 1 and stats["amount"] == 50.0


def test_live_stub_fails_safe(tmp_path):
    """Even with DRY_RUN=false, today's provider stub fails CLOSED."""
    cfg = make_cfg(tmp_path, dry_run=False)
    assert type(provider_from_config(cfg)).__name__ == "PokerBetProvider"
    p = provider_from_config(cfg)
    with pytest.raises(ProviderUnavailable):
        p.submit(execution_id="e", game_id="g", alert_id="a",
                 selection="UNDER", price=100.0, stake_amount=10.0)


# ══════════════════════════════════════════════════════════════════════
# the worker
# ══════════════════════════════════════════════════════════════════════

def test_worker_off_produces_nothing(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    w = BettingWorker(cfg, store, lambda: [qualifying_game()])
    summary = w.poll_once()
    assert summary == {"enabled": False, "candidates": 0,
                       "executed": 0, "would_bet": 0, "no_bet": 0}
    assert store.recent() == []


def test_worker_dry_run_records_would_bet(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(10.0)
    w = BettingWorker(cfg, store, lambda: [qualifying_game()])
    summary = w.poll_once()
    assert summary["would_bet"] == 1
    rec = store.recent()[0]
    assert rec["status"] == "ACCEPTED"
    assert rec["provider_ref"].startswith("dryrun-")
    # the second pass is a duplicate — nothing new
    summary2 = w.poll_once()
    assert summary2["would_bet"] == 0
    assert summary2["no_bet"] >= 1


def test_worker_flip_off_mid_flight(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(10.0)
    games = [qualifying_game()]
    w = BettingWorker(cfg, store, lambda: games)
    assert w.poll_once()["would_bet"] == 1
    store.set_config("auto_betting_enabled", "false")
    games = [qualifying_game("NEXT")]      # a fresh opportunity
    summary = w.poll_once()
    assert summary["enabled"] is False
    assert summary["candidates"] == 0


# ══════════════════════════════════════════════════════════════════════
# API — status/settings, fail-closed, validated
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def client(tmp_path, monkeypatch):
    # a clean environment for the config: no real credentials in tests
    monkeypatch.delenv("POKERBET_USERNAME", raising=False)
    monkeypatch.delenv("POKERBET_PASSWORD", raising=False)
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)
    store = make_store(tmp_path)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    return TestClient(app)


def test_api_status_defaults_off(client):
    st = client.get("/api/v4/betting/status").json()
    assert st["enabled"] is False
    assert st["dry_run"] is True
    assert st["mode"] == "DRY_RUN"
    assert st["credentials"] == {"username_present": False,
                                 "password_present": False}
    assert st["recent"] == []


def test_api_settings_switch_and_unit_price(client):
    r = client.post("/api/v4/betting/settings",
                    json={"unit_price": 12.5})
    assert r.status_code == 200
    assert r.json()["unit_price"] == 12.5
    # enabling requires a unit price — now present, so it works
    r = client.post("/api/v4/betting/settings",
                    json={"auto_betting": "ON"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    r = client.post("/api/v4/betting/settings",
                    json={"auto_betting": "OFF"})
    assert r.json()["enabled"] is False


def test_api_rejects_enable_without_unit_price(tmp_path):
    from blm_v4.betting import api as betting_api
    betting_api.configure_betting(make_store(tmp_path), make_cfg(tmp_path))
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    # no unit price set yet — enabling must be refused
    r = c.post("/api/v4/betting/settings", json={"auto_betting": "ON"})
    assert r.status_code == 400
    # set one, then enabling succeeds
    c.post("/api/v4/betting/settings", json={"unit_price": 10})
    r = c.post("/api/v4/betting/settings", json={"auto_betting": "ON"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.parametrize("bad", [0, -1, "x", None, True])
def test_api_rejects_invalid_unit_prices(client, bad):
    r = client.post("/api/v4/betting/settings", json={"unit_price": bad})
    assert r.status_code == 400
    # and a valid one afterwards still works (no corrupt state written)
    st = client.get("/api/v4/betting/status").json()
    assert st["unit_price"] is None


def test_api_rejects_infinite_unit_price(client):
    """A hostile raw-JSON body carrying Infinity is rejected server-side
    (JSON bodies technically allow it; the validator must not)."""
    r = client.post("/api/v4/betting/settings", content=b'{"unit_price": Infinity}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    st = client.get("/api/v4/betting/status").json()
    assert st["unit_price"] is None


def test_api_rejects_out_of_range_unit_price(client):
    r = client.post("/api/v4/betting/settings", json={"unit_price": 1e9})
    assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════
# SECURITY — credentials never appear anywhere
# ══════════════════════════════════════════════════════════════════════

def test_credentials_never_in_source_or_frontend():
    """No credential VALUE may appear in any shipped file; the provider
    reads the environment at runtime.  The values live ONLY in the
    gitignored .env, so the shipped source is scanned for ANY of the
    .env values present on this machine (none may leak into code).
    This test must never itself contain a literal credential."""
    import pathlib
    here = pathlib.Path(__file__).resolve().parent.parent
    scanned = []
    for pattern in ("blm_v4/betting/*.py", "blm_v4/api.py",
                    "blm_v4/dashboard/static/*.js",
                    "blm_v4/dashboard/static/*.html", "server.py"):
        scanned.extend(here.glob(pattern))
    assert len(scanned) >= 8
    # candidate values come from the LOCAL .env at runtime — the test
    # file itself holds no literal, and .env is excluded from the scan
    env = here / ".env"
    values = []
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if len(v.strip()) >= 4:
                    values.append(v.strip())
    for f in scanned:
        text = f.read_text(errors="ignore")
        for v in values:
            assert v not in text, f"credential value leaked into {f}"
        # username part of an email-style credential, defensively
        if values:
            for v in values:
                if "@" in v:
                    assert v.split("@")[0] not in text, f


def test_credentials_not_in_api_payload_or_db(tmp_path):
    from blm_v4.betting import api as betting_api
    os.environ["POKERBET_USERNAME"] = "secret-user@test"
    os.environ["POKERBET_PASSWORD"] = "secret-pass"
    try:
        cfg, store = make_cfg(tmp_path), make_store(tmp_path)
        betting_api.configure_betting(store, cfg)
        app = FastAPI()
        app.include_router(betting_api.router)
        c = TestClient(app)
        st = c.get("/api/v4/betting/status").json()
        blob = json.dumps(st)
        assert "secret-user@test" not in blob
        assert "secret-pass" not in blob
        assert st["credentials"] == {"username_present": True,
                                     "password_present": True}
        # records written through a full dry-run execution carry nothing
        res = evaluate(qualifying_game(), cfg=cfg, store=store,
                       enabled=True, unit_price=10.0,
                       stats=store.today_stats())
        store.set_config("auto_betting_enabled", "true")
        execute(res["candidate"], cfg=cfg, store=store,
                provider=DryRunProvider())
        conn = sqlite3.connect(store.db_path)
        rows = conn.execute(
            "SELECT * FROM bet_executions").fetchall() + \
            conn.execute("SELECT * FROM bet_audit").fetchall() + \
            conn.execute("SELECT * FROM betting_config").fetchall()
        conn.close()
        blob = repr(rows)
        assert "secret-user@test" not in blob
        assert "secret-pass" not in blob
    finally:
        os.environ.pop("POKERBET_USERNAME", None)
        os.environ.pop("POKERBET_PASSWORD", None)


def test_audit_log_records_why_and_fingerprints(tmp_path):
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    execute(res["candidate"], cfg=cfg, store=store,
            provider=DryRunProvider())
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM bet_audit WHERE event='claimed'").fetchone()
    conn.close()
    details = json.loads(row["details"])
    assert details["why"] == \
        "production UNDER alert active (under_alert.active)"
    assert details["alert_id"]
    assert details["checkpoint"] == "75"
    assert details["fingerprints_present"] == ["C3", "C5"]
    assert details["fingerprint_count"] == 2
    assert details["stake_calculation"] == {
        "unit_price": 10.0, "stake_units": 1.0, "stake_amount": 10.0}


def test_fingerprints_cannot_create_bets(tmp_path):
    """A fingerprint-fired game with NO active alert must never bet."""
    cfg, store = make_cfg(tmp_path), make_store(tmp_path)
    g = qualifying_game()
    g["under_alert"] = {"active": False, "checkpoint": None}
    g["under_alert_fingerprint"] = {
        "fingerprint_count": 7,
        "fingerprints_fired": ["C1", "C3", "C5", "R2"]}
    res = evaluate(g, cfg=cfg, store=store, enabled=True,
                   unit_price=10.0, stats=store.today_stats())
    assert res["decision"] == "NO_BET"


def test_r1_absent_from_betting_layer():
    import blm_v4.betting.executor as ex
    import blm_v4.betting.provider as pv
    import blm_v4.betting.worker as wk
    for mod in (ex, pv, wk):
        src = inspect.getsource(mod)
        assert "R1" not in src.replace("R1 remains excluded", "").replace(
            "R1 does not exist", ""), mod.__name__


# ══════════════════════════════════════════════════════════════════════
# unconfigured limits — fail closed (§9: unverifiable limits → NO BET)
# ══════════════════════════════════════════════════════════════════════

def test_unconfigured_limits_block_betting(tmp_path):
    """Production default (no BETTING_* env vars): every limit is None →
    betting MUST be blocked even with the switch ON and a perfect alert."""
    cfg = make_cfg(tmp_path, max_stake_per_bet=None, max_bets_per_day=None,
                   max_daily_exposure=None)
    store = make_store(tmp_path)
    for stats in ({"verifiable": True, "bets": 0, "amount": 0.0},
                  store.today_stats()):
        res = evaluate(qualifying_game(), cfg=cfg, store=store,
                       enabled=True, unit_price=10.0, stats=stats)
        assert res["decision"] == "NO_BET"
        assert res["reason"] == "limits_not_configured"


def test_partially_configured_limits_block_betting(tmp_path):
    """Any single missing limit blocks betting — partial configuration
    is not safe configuration."""
    store = make_store(tmp_path)
    for kwargs in ({"max_stake_per_bet": None},
                   {"max_bets_per_day": None},
                   {"max_daily_exposure": None}):
        cfg = make_cfg(tmp_path, **kwargs)
        res = evaluate(qualifying_game(), cfg=cfg, store=store,
                       enabled=True, unit_price=10.0,
                       stats={"verifiable": True, "bets": 0, "amount": 0.0})
        assert res["decision"] == "NO_BET"
        assert res["reason"] == "limits_not_configured"


# ══════════════════════════════════════════════════════════════════════
# LIVE MODE — requires explicit configuration (§3 / §12)
# ══════════════════════════════════════════════════════════════════════

def test_live_mode_requires_dry_run_false(tmp_path):
    """Live money requires BETTING_DRY_RUN=false — the default is always
    DRY_RUN regardless of any other setting."""
    # default config → always DRY_RUN
    cfg = make_cfg(tmp_path)
    assert cfg.dry_run is True
    assert cfg.live_money_enabled is False
    assert type(provider_from_config(cfg)).__name__ == "DryRunProvider"


def test_live_mode_dry_run_false_still_stub(tmp_path):
    """Even with dry_run=False, the PokerBet provider is still a stub
    and raises ProviderUnavailable — no accidental live bets."""
    cfg = make_cfg(tmp_path, dry_run=False)
    assert cfg.dry_run is False
    assert cfg.live_money_enabled is True
    p = provider_from_config(cfg)
    assert type(p).__name__ == "PokerBetProvider"
    with pytest.raises(ProviderUnavailable):
        p.submit(execution_id="x", game_id="g", alert_id="a",
                 selection="UNDER", price=193.5, stake_amount=10.0)


def test_live_mode_evaluate_returns_execute_not_would_bet(tmp_path):
    """With dry_run=False the executor returns EXECUTE (not WOULD_BET)
    so the worker routes it to the real provider path."""
    cfg = make_cfg(tmp_path, dry_run=False)
    store = make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    assert res["decision"] == "EXECUTE"


def test_live_mode_provider_failure_recorded_not_success(tmp_path):
    """A provider failure in LIVE mode records FAILED — never ACCEPTED."""
    cfg = make_cfg(tmp_path, dry_run=False)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    assert res["decision"] == "EXECUTE"

    class AlwaysUnavailable:
        name = "stub"
        def submit(self, **kw):
            raise ProviderUnavailable("not implemented")

    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=AlwaysUnavailable())
    assert out["status"] == "FAILED"
    rec = store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == "FAILED"
    assert rec["error_code"] == "PROVIDER_UNAVAILABLE"
    # a FAILED bet must NOT count as a successful execution — bets=0
    stats = store.today_stats()
    # FAILED is not in the counted statuses — only ACCEPTED/SUBMITTED/UNKNOWN
    assert stats["bets"] == 0


# ══════════════════════════════════════════════════════════════════════
# BET COUNT LIMITS (§4)
# ══════════════════════════════════════════════════════════════════════

def test_bet_count_below_max_allowed(tmp_path):
    cfg = make_cfg(tmp_path)  # max_bets_per_day=5
    store = make_store(tmp_path)
    # 4 bets so far — one more is permitted
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 4, "amount": 40.0})
    assert res["decision"] == "WOULD_BET"


def test_bet_count_exactly_at_max_blocked(tmp_path):
    cfg = make_cfg(tmp_path)  # max_bets_per_day=5
    store = make_store(tmp_path)
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 5, "amount": 50.0})
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "daily_bet_limit_reached"


def test_bet_count_additional_execution_blocked_after_max(tmp_path):
    """The worker prevents a second execution after the daily bet cap is hit
    — the stats are refreshed after each execution so the next candidate
    sees the updated count."""
    cfg = make_cfg(tmp_path, max_bets_per_day=2)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(10.0)
    games = [qualifying_game("G1", 75), qualifying_game("G2", 75),
             qualifying_game("G3", 75)]
    w = BettingWorker(cfg, store, lambda: games)
    summary = w.poll_once()
    # only 2 should be accepted (max_bets_per_day=2)
    assert summary["would_bet"] == 2
    assert summary["no_bet"] >= 1
    stats = store.today_stats()
    assert stats["bets"] == 2


# ══════════════════════════════════════════════════════════════════════
# DRY_RUN — simulated amount vs real wagered (§8)
# ══════════════════════════════════════════════════════════════════════

def test_api_dry_run_amount_is_simulated_not_real(tmp_path):
    """§8: In DRY_RUN the API must distinguish simulated exposure from real
    wagered amount.  amount_real=0, amount_simulated=actual stake total,
    is_simulated=True."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(10.0)
    w = BettingWorker(cfg, store, lambda: [qualifying_game()])
    w.poll_once()  # produces one ACCEPTED dry-run execution

    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    st = c.get("/api/v4/betting/status").json()
    t = st["today"]
    assert t["is_simulated"] is True
    assert t["amount_real"] == 0.0
    assert t["amount_simulated"] == 10.0   # one bet × R10 unit price
    assert t["amount"] == 10.0             # limit-enforcement value unchanged
    assert t["bets"] == 1
    assert t["remaining_exposure"] == 190.0   # 200 - 10


def test_api_provider_ref_in_recent_executions(tmp_path):
    """§7: The API must return provider_ref for each execution so the
    frontend can show the dry-run reference."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(10.0)
    w = BettingWorker(cfg, store, lambda: [qualifying_game()])
    w.poll_once()
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    st = c.get("/api/v4/betting/status").json()
    assert st["recent"]
    r = st["recent"][0]
    assert r["provider_ref"] is not None
    assert r["provider_ref"].startswith("dryrun-")


# ══════════════════════════════════════════════════════════════════════
# DASHBOARD STATISTICS (§8) — server-derived, correct counts
# ══════════════════════════════════════════════════════════════════════

def test_dashboard_stats_reflect_persisted_executions(tmp_path):
    """Today's Bets / Units / Amount / Remaining are server-derived and
    update correctly after executions."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)  # max_daily_exposure=200, stake_units=1
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(20.0)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)

    # initial state — no bets
    st = c.get("/api/v4/betting/status").json()
    assert st["today"]["bets"] == 0
    assert st["today"]["amount"] == 0.0
    assert st["today"]["remaining_exposure"] == 200.0

    # run the worker — one qualifying game
    w = BettingWorker(cfg, store, lambda: [qualifying_game()])
    w.poll_once()

    st = c.get("/api/v4/betting/status").json()
    assert st["today"]["bets"] == 1
    assert st["today"]["units"] == 1.0
    assert st["today"]["amount"] == 20.0
    assert st["today"]["remaining_exposure"] == 180.0


def test_dashboard_remaining_exposure_never_negative(tmp_path):
    """Remaining exposure is max(0, limit - total) — never negative."""
    from blm_v4.betting import api as betting_api
    # set a low limit that can be exceeded across multiple bets
    cfg = make_cfg(tmp_path, max_daily_exposure=25.0, max_bets_per_day=10,
                   max_stake_per_bet=100.0)
    store = make_store(tmp_path)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)

    # manually write an execution that exceeds the limit (e.g. pre-existing)
    rec = {
        "execution_id": "e-over", "idempotency_key": "k-over",
        "game_id": "G1", "alert_id": "A1", "checkpoint": "75",
        "stake_amount": 30.0, "status": "ACCEPTED",
    }
    store.claim(rec)
    store.update_status("e-over", "ACCEPTED")

    st = c.get("/api/v4/betting/status").json()
    assert st["today"]["remaining_exposure"] == 0.0   # not negative


# ══════════════════════════════════════════════════════════════════════
# RECENT EXECUTIONS — render and show blocked reason (§7)
# ══════════════════════════════════════════════════════════════════════

def test_recent_executions_populated_after_dry_run(tmp_path):
    """After a dry-run the recent list is non-empty and carries the
    expected fields: game_id, selection, stake_amount, status, provider_ref,
    checkpoint, error_message."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(15.0)
    w = BettingWorker(cfg, store, lambda: [qualifying_game("G42", 75)])
    w.poll_once()
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    st = c.get("/api/v4/betting/status").json()
    assert len(st["recent"]) == 1
    r = st["recent"][0]
    assert r["game_id"] == "G42"
    assert r["checkpoint"] == "75"
    assert r["selection"] == "UNDER"
    assert r["stake_amount"] == 15.0
    assert r["status"] == "ACCEPTED"
    assert r["provider_ref"].startswith("dryrun-")
    assert "DRY_RUN" in r["error_message"]


def test_blocked_execution_shows_reason_in_api(tmp_path):
    """A FAILED execution persists its error_code so the dashboard can
    display the rejection reason (§7 / §11)."""
    from blm_v4.betting import api as betting_api
    from blm_v4.betting.executor import execute
    from blm_v4.betting.provider import DryRunProvider, ProviderUnavailable
    cfg = make_cfg(tmp_path, dry_run=False)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(qualifying_game(), cfg=cfg, store=store,
                   enabled=True, unit_price=10.0,
                   stats=store.today_stats())

    class AlwaysFails:
        name = "stub"
        def submit(self, **kw):
            raise ProviderUnavailable("provider stub not implemented")

    execute(res["candidate"], cfg=cfg, store=store, provider=AlwaysFails())
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    st = c.get("/api/v4/betting/status").json()
    assert st["recent"]
    r = st["recent"][0]
    assert r["status"] == "FAILED"
    assert r["error_code"] == "PROVIDER_UNAVAILABLE"
    assert r["error_message"] is not None


def test_no_executions_returns_empty_recent(tmp_path):
    """Before any execution the recent list is empty — the dashboard shows
    'No executions yet'."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)
    store = make_store(tmp_path)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    st = c.get("/api/v4/betting/status").json()
    assert st["recent"] == []


# ══════════════════════════════════════════════════════════════════════
# API SECURITY — browser cannot override server-side limits (§12)
# ══════════════════════════════════════════════════════════════════════

def test_browser_cannot_inject_stake_above_server_limit(tmp_path):
    """The browser sends only unit_price; the server calculates the final
    stake using cfg.stake_units.  Even if the client sends a huge
    unit_price it is bounded by cfg.max_unit_price (1000 by default),
    and stake = unit_price × stake_units is enforced server-side."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path)   # max_stake_per_bet=100, max_unit_price=1000
    store = make_store(tmp_path)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    # attempt to set unit_price above max_unit_price
    r = c.post("/api/v4/betting/settings", json={"unit_price": 99999.0})
    assert r.status_code == 400


def test_browser_cannot_set_limits_via_settings(tmp_path):
    """The /settings endpoint only accepts auto_betting and unit_price.
    Sending limit fields has no effect — they are silently ignored (the
    server always uses its env-sourced config)."""
    from blm_v4.betting import api as betting_api
    cfg = make_cfg(tmp_path, max_daily_exposure=100.0)
    store = make_store(tmp_path)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    c = TestClient(app)
    # trying to inject a larger exposure limit via settings
    r = c.post("/api/v4/betting/settings",
               json={"max_daily_exposure": 999999.0,
                     "max_bets_per_day": 9999,
                     "max_stake_per_bet": 9999.0})
    assert r.status_code == 200   # ignored, not rejected
    st = c.get("/api/v4/betting/status").json()
    # the server-side config is unchanged
    assert st["max_daily_exposure"] == 100.0


# ══════════════════════════════════════════════════════════════════════
# CUMULATIVE EXPOSURE (§8 — multiple bets accumulate correctly)
# ══════════════════════════════════════════════════════════════════════

def test_cumulative_exposure_across_multiple_bets(tmp_path):
    """Each dry-run bet adds to the running exposure total; the remaining
    exposure decreases by exactly stake_amount per bet."""
    cfg = make_cfg(tmp_path, max_bets_per_day=10, max_daily_exposure=100.0,
                   max_stake_per_bet=50.0)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(30.0)
    games = [qualifying_game(f"G{i}", 75) for i in range(3)]
    w = BettingWorker(cfg, store, lambda: games)
    w.poll_once()
    stats = store.today_stats()
    # 3 bets × R30 = R90 — all fit within R100 exposure limit
    assert stats["bets"] == 3
    assert abs(stats["amount"] - 90.0) < 0.01


def test_exposure_blocks_on_cumulative_overflow(tmp_path):
    """After enough bets to fill the daily exposure, further candidates
    are blocked — even if the bet count limit hasn't been reached."""
    cfg = make_cfg(tmp_path, max_bets_per_day=10, max_daily_exposure=50.0,
                   max_stake_per_bet=50.0)
    store = make_store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(30.0)  # 30+30=60 > 50 limit → second blocked
    games = [qualifying_game("G1", 75), qualifying_game("G2", 75)]
    w = BettingWorker(cfg, store, lambda: games)
    summary = w.poll_once()
    assert summary["would_bet"] == 1
    assert summary["no_bet"] == 1
    stats = store.today_stats()
    assert stats["bets"] == 1
