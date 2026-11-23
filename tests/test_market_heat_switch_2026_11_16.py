"""MARKET-HEAT OPERATOR SWITCH — one named test per requirement (2026-11-16).

Operator directive: "set TRADE_MARKET_HEAT_ENABLED=0 -- make a button on the
UI for it too".  The deployment env stays the DEFAULT; the dashboard switch is
a persisted betting_config row (``market_heat_enabled``) that OUTRANKS it, so
the operator can turn the guardrail back on without editing a systemd drop-in.

Rules proved here:
  1. the persisted switch wins over the env, in BOTH directions;
  2. with no row (or an unreadable store) the env governs — fail-open;
  3. the API validates, persists, audits and publishes it immediately;
  4. the status payload the button paints from carries the real verdict.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from blm_v4.betting import api as bapi
from blm_v4.betting import market_heat
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.store import BettingStore


def _cfg(tmp_path, **over) -> BettingConfig:
    base = dict(dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
                max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
                max_unit_price=1000.0, alert_max_age_s=90.0,
                db_path=str(tmp_path / "betting.db"))
    base.update(over)
    return BettingConfig(**base)


@pytest.fixture(autouse=True)
def _clean_switch(monkeypatch):
    """Every test starts with NO operator switch and a quiet env."""
    monkeypatch.delenv("TRADE_MARKET_HEAT_ENABLED", raising=False)
    monkeypatch.setattr(market_heat, "_override", None, raising=False)
    yield
    market_heat.apply_override(None)


def _store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "betting.db"))


# ── 1. the switch wins over the env, in both directions ───────────────
def test_operator_switch_can_arm_the_guardrail_a_disabled_env_refuses(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_MARKET_HEAT_ENABLED", "0")
    store = _store(tmp_path)
    store.set_config(market_heat.SWITCH_KEY, "true")
    assert market_heat.load_persisted_override(store.db_path) is True
    assert market_heat.enabled() is True


def test_operator_switch_can_disable_the_guardrail_an_enabled_env_arms(
        tmp_path):
    store = _store(tmp_path)
    store.set_config(market_heat.SWITCH_KEY, "false")
    assert market_heat.load_persisted_override(store.db_path) is False
    assert market_heat.enabled() is False


# ── 2. fail-open: no row, unknown value, or an unreadable store ───────
def test_with_no_row_the_deployment_env_governs(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("TRADE_MARKET_HEAT_ENABLED", "0")
    assert market_heat.load_persisted_override(store.db_path) is None
    assert market_heat.enabled() is False

    monkeypatch.delenv("TRADE_MARKET_HEAT_ENABLED")
    assert market_heat.enabled() is True          # code default stays ON


def test_an_unreadable_store_leaves_the_env_governing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("TRADE_MARKET_HEAT_ENABLED", "0")
    assert market_heat.load_persisted_override(missing) is None
    assert market_heat.enabled() is False


def test_apply_override_publishes_at_once_and_can_be_cleared():
    market_heat.apply_override(False)
    assert market_heat.enabled() is False
    market_heat.apply_override(True)
    assert market_heat.enabled() is True
    market_heat.apply_override(None)              # back to the env default
    assert market_heat.enabled() is True


def test_a_disabled_guardrail_never_trips_even_on_a_hot_market(monkeypatch):
    monkeypatch.setattr(market_heat, "samples",
                        lambda *a, **k: [(210.0, 200.0)] * 40)
    market_heat.apply_override(False)
    st = market_heat.state(refresh=True)
    assert st["enabled"] is False and st["tripped"] is False
    assert market_heat.refusal() is None
    assert "operator switch" in st["text"]


# ── 3. the API: validate, persist, audit, publish ─────────────────────
def _wired(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = BettingStore(cfg.db_path)
    monkeypatch.setattr(bapi, "_store", store, raising=False)
    monkeypatch.setattr(bapi, "_cfg", cfg, raising=False)
    monkeypatch.setattr(market_heat, "_override", None, raising=False)
    return store, cfg


def test_the_api_switch_off_persists_audits_and_takes_effect(tmp_path,
                                                             monkeypatch):
    store, _ = _wired(tmp_path, monkeypatch)
    bapi.betting_settings({"market_heat": "OFF"})
    assert store.get_config(market_heat.SWITCH_KEY, "") == "false"
    assert market_heat.enabled() is False
    events = [r["event"] for r in store.audit_recent(limit=10)]
    assert "market_heat_switch" in events


def test_the_api_switch_on_persists_and_takes_effect(tmp_path, monkeypatch):
    store, _ = _wired(tmp_path, monkeypatch)
    bapi.betting_settings({"market_heat": "ON"})
    assert store.get_config(market_heat.SWITCH_KEY, "") == "true"
    assert market_heat.enabled() is True


def test_the_api_refuses_anything_but_on_or_off(tmp_path, monkeypatch):
    store, _ = _wired(tmp_path, monkeypatch)
    for bad in ("maybe", 1.5, [], None):
        with pytest.raises(HTTPException) as err:
            bapi.betting_settings({"market_heat": bad})
        assert err.value.status_code == 400
    assert store.get_config(market_heat.SWITCH_KEY, "") == ""   # untouched


# ── 4. the switch survives a restart (the row is the authority) ───────
def test_the_persisted_row_survives_a_fresh_process(tmp_path):
    store = BettingStore(str(tmp_path / "betting.db"))
    store.set_config(market_heat.SWITCH_KEY, "false")
    market_heat.apply_override(None)              # simulate a process restart
    assert market_heat.load_persisted_override(store.db_path) is False
    assert market_heat.enabled() is False


# ── 5. the status payload the BUTTON paints from ──────────────────────
def test_status_payload_carries_the_verdict_the_button_paints(
        tmp_path, monkeypatch):
    store, _ = _wired(tmp_path, monkeypatch)
    market_heat.apply_override(False)
    payload = bapi.betting_status()
    assert payload["market_heat_enabled"] is False
    assert payload["market_heat"]["enabled"] is False


def test_dashboard_markup_has_the_heat_button():
    static = Path(__file__).resolve().parent.parent / "blm_v4/dashboard/static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "dashboard.js").read_text(encoding="utf-8")
    assert 'id="abHeat"' in html
    assert "abHeat" in js and "market_heat" in js
