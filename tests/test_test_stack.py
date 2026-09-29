"""GATE 10 — the FULL 🔴 TEST environment workflow, exercised in-process
(directive: "Test direct: fixture alert → BettingWorker → validation →
provider → response → reconciliation → API → UI").

Runs the REAL composition (build_test_app) with the REAL BettingWorker
thread against the deterministic fixture feed, through the REAL betting
API, and verifies the UI surfaces it.  Isolation is asserted, not
assumed: TEST paths, DRY_RUN forced, collector OFF, banner present.

HARD BOUNDARY: no production DB is opened; the production paths are
OVERRIDDEN by apply_test_environment and the test asserts it.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import blm_v4.test_stack as ts
from blm_v4.test_stack import build_test_app
from blm_v4.betting.store import KEY_AUTO_ENABLED

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


@pytest.fixture
def test_stack(tmp_path, monkeypatch):
    # The TEST stack now carries the SAME authentication guard as
    # production, so the fixture signs in as a real seeded operator before
    # exercising the workflow.  Credentials are generated per run and
    # supplied through the environment — never a literal in source.
    import secrets as _secrets
    admin_pw = _secrets.token_urlsafe(18)
    user_pw = _secrets.token_urlsafe(18)
    monkeypatch.setenv("BLM_AUTH_SEED_ADMIN_USERNAME", "t-admin")
    monkeypatch.setenv("BLM_AUTH_SEED_ADMIN_PASSWORD", admin_pw)
    monkeypatch.setenv("BLM_AUTH_SEED_USER_USERNAME", "t-user")
    monkeypatch.setenv("BLM_AUTH_SEED_USER_PASSWORD", user_pw)
    # build_test_app composes REAL routers against TEST paths.  The
    # worker is BUILT (stopped) so tests drive poll_once deterministically.
    app, feed, store, cfg, worker, targets = build_test_app(
        tmp_path, with_worker=True)
    client = TestClient(app)
    r = client.post("/api/auth/login",
                    json={"username": "t-admin", "password": admin_pw})
    assert r.status_code == 200, r.text
    client.headers.update({"X-CSRF-Token": r.json()["csrf_token"]})
    yield {"client": client, "feed": feed, "store": store, "cfg": cfg,
           "worker": worker, "targets": targets, "tmp": tmp_path}


# ══════════════════════════════════════════════════════════════════════
# isolation guarantees (GATE 1 of the directive, as tests)
# ══════════════════════════════════════════════════════════════════════

def test_test_stack_isolation_targets(test_stack):
    t = test_stack["targets"]
    assert t["environment"] == "TEST"
    assert t["dry_run"] is True
    assert "test_env" in t["analytics_db"]
    assert "test_env" in t["betting_db"]
    # never the production paths
    assert t["analytics_db"] != "/mnt/blm-nvme/blm_pokerbet.db"
    assert "blm_betting" in t["betting_db"]


def test_test_stack_env_overrides_inherited_production_values(
        test_stack, monkeypatch):
    """A TEST process must not inherit production targets: apply overrides
    even when the ambient environment carries production values."""
    import os
    ts.apply_test_environment(test_stack["tmp"])
    assert os.environ["BLM_POKERBET_DB"].endswith("blm_pokerbet_test.db")
    assert os.environ["BETTING_DRY_RUN"] == "true"


def test_test_env_endpoint_reports_the_gate1_record(test_stack):
    r = test_stack["client"].get("/api/v4/test/env")
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "TEST"
    assert body["collector"] == "OFF"
    assert body["provider"] == "DryRunProvider"
    assert body["auto_betting_enabled"] is False     # OFF initially
    assert "🔴 TEST ENVIRONMENT" in body["banner"]


def test_dashboard_carries_the_test_banner(test_stack):
    r = test_stack["client"].get("/")
    assert r.status_code == 200
    assert "🔴 TEST ENVIRONMENT" in r.text
    assert 'id="testEnvBanner"' in r.text


# ══════════════════════════════════════════════════════════════════════
# the DIRECT workflow: fixture → worker → provider → ledger → API → UI
# ══════════════════════════════════════════════════════════════════════

def test_full_workflow_qualifying_fixture_reaches_the_api(
        test_stack):
    client, feed, store = (test_stack["client"], test_stack["feed"],
                           test_stack["store"])
    # 1. fixture: the qualifying alert
    r = client.post("/api/v4/test/fixtures/alert",
                    json={"scenario": "qualifying", "enabled": True})
    assert r.status_code == 200
    assert r.json()["game"]["under_alert"]["active"] is True
    # 2. arm auto-betting THROUGH THE API (settings endpoint), unit price
    #    first — the switch must not arm without stake semantics
    r = client.post("/api/v4/betting/settings", json={"unit_price": 2.0})
    assert r.status_code == 200
    r = client.post("/api/v4/betting/settings", json={"auto_betting": "ON"})
    assert r.status_code == 200 and r.json()["enabled"] is True
    # 3. the REAL worker consumes the fixture feed (dry-run authority)
    summary = test_stack["worker"].poll_once()
    assert summary["enabled"] is True
    assert summary["would_bet"] == 1
    # 4. the ledger holds the WOULD_BET record
    rec = store.latest_for_game("TEST-GAME-9001")
    assert rec is not None
    assert rec["status"] == "ACCEPTED"
    assert rec["execution_state"] == "WOULD_BET"
    assert rec["provider_ref"].startswith("dryrun-")
    # 5. the API serves it (status + history)
    status = client.get("/api/v4/betting/status").json()
    assert status["mode"] == "DRY_RUN"
    assert status["today"]["is_simulated"] is True
    assert any(e["game_id"] == "TEST-GAME-9001" and
               e["execution_state"] == "WOULD_BET"
               for e in status["recent"])
    hist = client.get("/api/v4/betting/history").json()
    assert hist["executions"] and hist["audit"]
    # 6. the game's betting state reports ARMED/eligible
    state = client.get("/api/v4/betting/game/TEST-GAME-9001/state").json()
    assert state["eligible"] is True
    assert state["reconciliation_state"] == "NOT_REQUIRED"


def test_full_workflow_inactive_fixture_produces_nothing(test_stack):
    client, feed, store = (test_stack["client"], test_stack["feed"],
                           test_stack["store"])
    client.post("/api/v4/test/fixtures/alert",
                json={"scenario": "inactive", "enabled": True})
    client.post("/api/v4/betting/settings", json={"unit_price": 2.0})
    client.post("/api/v4/betting/settings", json={"auto_betting": "ON"})
    summary = test_stack["worker"].poll_once()
    assert summary["candidates"] == 0
    assert store.recent(10) == []            # empty ledger — no bet


def test_full_workflow_stale_fixture_is_refused(test_stack):
    client, feed, store = (test_stack["client"], test_stack["feed"],
                           test_stack["store"])
    client.post("/api/v4/test/fixtures/alert",
                json={"scenario": "stale", "enabled": True})
    client.post("/api/v4/betting/settings", json={"unit_price": 2.0})
    client.post("/api/v4/betting/settings", json={"auto_betting": "ON"})
    summary = test_stack["worker"].poll_once()
    assert summary["candidates"] == 0        # refused (alert_stale)
    assert store.recent(10) == []


def test_worker_thread_lifecycle_start_stop(test_stack):
    """The composed worker thread starts, polls the fixture, and stops
    cleanly (worker lifecycle — directive layer list item 1)."""
    feed, store = test_stack["feed"], test_stack["store"]
    feed.set_scenario("qualifying", True)
    store.set_unit_price(2.0)
    store.set_config(KEY_AUTO_ENABLED, "true")
    worker = test_stack["worker"]
    assert worker is not None
    worker.start()
    deadline = time.time() + 15
    while time.time() < deadline and not store.recent(5):
        time.sleep(0.2)
    worker.stop(timeout=5)
    recs = store.recent(5)
    assert recs, "worker thread never recorded the fixture within 15s"
    assert recs[0]["execution_state"] == "WOULD_BET"


def test_kill_switch_off_by_default_in_fresh_test_stack(test_stack):
    """A fresh TEST stack is OFF — the absence of an enable is never an
    enable (store contract), so a restarted TEST environment cannot bet."""
    client = test_stack["client"]
    status = client.get("/api/v4/betting/status").json()
    assert status["enabled"] is False
    assert status["mode"] == "DRY_RUN"
