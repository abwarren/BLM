"""Manual and per-game betting API contract tests; all providers are dry-run."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from blm_v4.betting import api
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.provider import DryRunProvider
from blm_v4.betting.store import BettingStore


def cfg(tmp_path, **kwargs):
    base = dict(dry_run=True, max_stake_per_bet=50, max_bets_per_day=10,
                max_daily_exposure=500, stake_units=1.0, min_unit_price=1,
                max_unit_price=1000, db_path=str(tmp_path / "betting.db"))
    base.update(kwargs)
    return BettingConfig(**base)


def game():
    captured = datetime.now(timezone.utc).isoformat()
    return {
        "game_id": "evt-canonical-001", "live": True,
        "market": {"total_line": 193.5, "market_status": "LIVE"},
        "projector": {"live_total_line": 193.5, "market_status": "LIVE",
                      "market_age_seconds": 1, "captured_at": captured,
                      "required_pts_per_min": 5, "progress_pct": 76},
        "under_alert_eligibility": {"eligible": True},
        "under_alert": {"active": True, "checkpoint": 75,
                         "trigger_line": 193.5},
        "under_alert_fingerprint": {"fingerprints_fired": []},
    }


def client_for(tmp_path, g=None):
    store = BettingStore(str(tmp_path / "betting.db"))
    config = cfg(tmp_path)
    current = [g or game()]
    api.configure_betting(store, config, live_payload_fn=lambda: current)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), store, current


def test_one_unit_max_is_enforced_by_auto_executor(tmp_path):
    g = game()
    config = cfg(tmp_path, stake_units=1.01)
    store = BettingStore(config.db_path)
    out = evaluate(g, cfg=config, store=store, enabled=True,
                   unit_price=10, stats=store.today_stats())
    assert out["decision"] == "NO_BET"
    assert out["reason"] == "ONE_UNIT_MAXIMUM"


def test_game_state_is_authoritative_and_read_only(tmp_path):
    c, store, current = client_for(tmp_path)
    store.set_unit_price(10)
    store.set_config("auto_betting_enabled", "true")
    response = c.get("/api/v4/betting/game/evt-canonical-001/state")
    assert response.status_code == 200
    body = response.json()
    assert body["game_id"] == "evt-canonical-001"
    assert body["auto_bet_enabled"] is True
    assert body["eligible"] is True
    assert body["decision"] == "WOULD_BET"
    assert store.recent() == []
    assert c.get("/api/v4/betting/game/display-name/state").status_code == 404


def test_manual_total_is_one_unit_idempotent_and_traceable(tmp_path):
    c, store, _ = client_for(tmp_path)
    store.set_unit_price(20)
    store.set_config("auto_betting_enabled", "true")
    payload = {"game_id": "evt-canonical-001", "market": "TOTAL",
               "direction": "OVER", "line": 193.5, "stake": 20,
               "idempotency_key": "manual-run-0001"}
    first = c.post("/api/v4/betting/manual", json=payload)
    assert first.status_code == 200, first.text
    result = first.json()["execution"]
    assert result["game_id"] == payload["game_id"]
    assert result["alert_id"] == "manual:manual-run-0001"
    assert result["market"] == "TOTAL"
    assert result["selection"] == "OVER"
    assert result["stake_units"] == 1.0
    assert result["requested_amount"] == 20
    assert result["simulated_amount"] == 20
    assert result["execution_state"] == "WOULD_BET"
    again = c.post("/api/v4/betting/manual", json=payload)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["execution"]["execution_id"] == result["execution_id"]
    assert len(store.recent()) == 1


def test_manual_wager_rejects_unapproved_market_direction_stake_and_line(tmp_path):
    c, store, _ = client_for(tmp_path)
    store.set_unit_price(10)
    store.set_config("auto_betting_enabled", "true")
    base = {"game_id": "evt-canonical-001", "market": "TOTAL",
            "direction": "UNDER", "line": 193.5, "stake": 5,
            "idempotency_key": "manual-run-0002"}
    for change in ({"market": "MONEYLINE"}, {"direction": "HOME"},
                   {"stake": 10.01}, {"line": 190.5}):
        response = c.post("/api/v4/betting/manual", json={**base, **change})
        assert response.status_code in (400, 409)
    assert store.recent() == []


def test_manual_respects_global_switch_and_submission_boundary(tmp_path):
    c, store, _ = client_for(tmp_path)
    store.set_unit_price(10)
    payload = {"game_id": "evt-canonical-001", "market": "TOTAL",
               "direction": "UNDER", "line": 193.5, "stake": 10,
               "idempotency_key": "manual-run-0003"}
    blocked = c.post("/api/v4/betting/manual", json=payload).json()["execution"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["error_code"] == "GLOBAL_KILL_SWITCH"
    assert c.post("/api/v4/betting/manual", json=payload).json()["duplicate"] is True

    # Directly exercise the submission boundary: OFF means no provider call.
    store.set_config("auto_betting_enabled", "true")
    config = cfg(tmp_path, dry_run=False)
    g = game()
    candidate = evaluate(g, cfg=config, store=store, enabled=True,
                         unit_price=10, stats=store.today_stats())["candidate"]
    store.set_config("auto_betting_enabled", "false")
    called = []
    class Provider:
        def submit(self, **kwargs):
            called.append(kwargs)
            return {"status": "ACCEPTED"}
    result = execute(candidate, cfg=config, store=store, provider=Provider())
    assert result["status"] == "BLOCKED"
    assert called == []


def test_game_auto_bet_control_cannot_override_global_switch(tmp_path):
    c, store, games = client_for(tmp_path)
    store.set_unit_price(10)
    off = c.post("/api/v4/betting/game/evt-canonical-001/auto-bet",
                 json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["game_auto_bet_enabled"] is False
    assert off.json()["global_auto_bet_enabled"] is False
    state = c.get("/api/v4/betting/game/evt-canonical-001/state").json()
    assert state["eligible"] is False
    assert state["blocked_reason"] == "GLOBAL_KILL_SWITCH"

    store.set_config("auto_betting_enabled", "true")
    game_state = c.get("/api/v4/betting/game/evt-canonical-001/state").json()
    assert game_state["blocked_reason"] == "PER_GAME_AUTO_BET_OFF"


def test_unknown_manual_result_is_not_retried_by_same_logical_request(tmp_path, monkeypatch):
    from blm_v4.betting.provider import ProviderAmbiguous
    c, store, _ = client_for(tmp_path)
    config = cfg(tmp_path, dry_run=False)
    api.configure_betting(store, config, live_payload_fn=lambda: [_ for _ in [game()]])
    store.set_unit_price(10)
    store.set_config("auto_betting_enabled", "true")
    calls = []

    class Ambiguous:
        def submit(self, **kwargs):
            calls.append(kwargs)
            raise ProviderAmbiguous("response lost")

    monkeypatch.setattr(api, "provider_from_config", lambda _cfg: Ambiguous())
    payload = {"game_id": "evt-canonical-001", "market": "TOTAL",
               "direction": "UNDER", "line": 193.5, "stake": 10,
               "idempotency_key": "manual-unknown-01"}
    first = c.post("/api/v4/betting/manual", json=payload).json()
    second = c.post("/api/v4/betting/manual", json=payload).json()
    assert first["execution"]["status"] == "UNKNOWN"
    assert second["execution"]["status"] == "UNKNOWN"
    assert second["duplicate"] is True
    assert len(calls) == 1
    hist = c.get("/api/v4/betting/history?q=manual-unknown-01").json()
    assert hist["executions"][0]["execution_id"] == first["execution"]["execution_id"]
    assert hist["audit"]
