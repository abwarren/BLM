"""Auto-Bet COMMAND API + production wiring (gap G-07) — 2026-10-03 directive.

Proves the remaining in-process production link: ONE shared command path
(``blm_v4/betting/command.py``) that BOTH producers — the manual UI command and
the autonomous engine — run, wrapping the canonical rung validator
(``rung.validate_execution``) and the canonical stake authority
(``stake.resolve_stake``).

Covers the directive's requirements:
  B  executor preconditions (fail closed on any ambiguity)
  C  manual and autonomous use the SAME validator (one implementation)
  D  idempotency + the execution-state machine (a timeout is NEVER success)
  TESTING 4..9 (duplicate, stale/wrong-game/market/line, missing rung_size,
             invalid unit size, ZERO_STAKE !-> REAL_MONEY_TEST, R2.00 exact)

READ-ONLY w.r.t. production: no provider submission, no real money.  The
arm gate ``command.WIRED_INTO_PRODUCTION`` is monkeypatched per-test only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.betting import api as betting_api
from blm_v4.betting import command as C
from blm_v4.betting import stake as S
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.provider import DryRunProvider, ProviderAmbiguous
from blm_v4.betting.store import BettingStore

OBS = (190.0, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0, 193.5)  # 0.5 tick


def cfg(tmp_path, **over) -> BettingConfig:
    base = dict(dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
                max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
                max_unit_price=1000.0, alert_max_age_s=90.0,
                db_path=str(tmp_path / "betting.db"))
    base.update(over)
    return BettingConfig(**base)


def game(game_id="30990001", checkpoint=75, age_s=5.0, trigger=193.5,
         cur=193.5, obs=OBS, active=True) -> dict:
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id, "live": True, "live_reason": None,
        "market": {"total_line": cur, "market_status": "LIVE",
                   "observed_lines": list(obs) if obs is not None else None},
        "projector": {"progress_pct": 76.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": cur, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": active, "checkpoint": checkpoint,
                        "trigger_line": trigger, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": f"{game_id}|{checkpoint}"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }


@pytest.fixture
def armed(monkeypatch):
    """Arm the canonical command gate (production flip) for one test."""
    monkeypatch.setattr(C, "WIRED_INTO_PRODUCTION", True)
    return True


def _store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "betting.db"))


def _prod_kwargs(unit=10.0):
    return dict(mode=S.PRODUCTION_AUTO_BET, unit_size=unit,
                authorized=True, authorized_by="test")


# ══════════════════════════════════════════════════════════════════════
# C — ONE shared validator; manual == autonomous
# ══════════════════════════════════════════════════════════════════════

def test_manual_equals_autonomous_command():
    g = game()
    m = C.manual_command(g, **_prod_kwargs())
    a = C.autonomous_command(g, **_prod_kwargs())
    assert m["decision"] == a["decision"] == C.ALLOW
    assert m["rung"] == a["rung"]                 # same rung calculation
    assert m["stake"] == a["stake"]               # same stake resolution
    assert m["state"] == a["state"] == C.VALIDATED
    assert m["idempotency_key"] != a["idempotency_key"]  # source differs only


def test_validate_for_execution_is_the_same_call():
    g = game()
    assert (C.validate_for_execution(g, source=C.SOURCE_MANUAL, **_prod_kwargs())
            == C.build_command(g, source=C.SOURCE_MANUAL, **_prod_kwargs()))


def test_no_alert_means_no_command():
    # HARD RULE: no alert -> no Auto-Bet opportunity (and no derivable
    # command identity).  With no alert there is nothing to address.
    g = game()
    g.pop("under_alert")
    assert C.build_command(g, source=C.SOURCE_MANUAL,
                           **_prod_kwargs())["reason"] == "command_identity_missing"
    # an explicitly-addressed command with no alert still fails the identity gate
    assert C.build_command(g, source=C.SOURCE_MANUAL, idempotency_key="fixed",
                           **_prod_kwargs())["reason"] == "rung_identity_unverified"


# ══════════════════════════════════════════════════════════════════════
# B — the canonical decision + reasons (the ONE shared validator)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("mutate,reason", [
    (lambda g: g["market"].update(total_line=None), "rung_market_missing"),
    (lambda g: g["market"].update(observed_lines=[]), "rung_size_ambiguous"),
    (lambda g: g["market"].update(observed_lines=(1.0, 1.0, 1.0)),
     "rung_size_ambiguous"),
    (lambda g: g["under_alert"].update(trigger_line=None), "rung_market_missing"),
    # trigger 197.0 vs current 193.5 at a 0.5 tick => -7 rungs (> 1 down)
    (lambda g: g["under_alert"].update(trigger_line=197.0), "more_than_one_rung_down"),
    (lambda g: g.update(game_id=""), "rung_identity_unverified"),
    (lambda g: g.pop("under_alert"), "rung_identity_unverified"),
])
def test_command_layer_reasons(mutate, reason):
    g = game()
    mutate(g)
    cmd = C.build_command(g, source=C.SOURCE_MANUAL, idempotency_key="fixed",
                          **_prod_kwargs())
    assert cmd["decision"] == C.REJECT
    assert cmd["state"] == C.REJECTED
    assert cmd["reason"] == reason


# ══════════════════════════════════════════════════════════════════════
# B — preconditions fail closed (armed executor)
# ══════════════════════════════════════════════════════════════════════

def test_disarmed_executor_is_unchanged(tmp_path):
    """While disarmed the legacy ladder is byte-for-byte authoritative."""
    c, store = cfg(tmp_path), _store(tmp_path)
    assert C.WIRED_INTO_PRODUCTION is False
    # NOTE: no observed_lines — the legacy ladder does not need them
    res = evaluate(game(obs=None), cfg=c, store=store, enabled=True,
                   unit_price=10.0, stats=store.today_stats())
    assert res["decision"] == "WOULD_BET"


def test_armed_executor_allows_when_rung_ok(tmp_path, armed):
    c, store = cfg(tmp_path), _store(tmp_path)
    res = evaluate(game(), cfg=c, store=store, enabled=True,
                   unit_price=10.0, stats=store.today_stats())
    assert res["decision"] == "WOULD_BET"


@pytest.mark.parametrize("mutate", [
    lambda g: g["market"].update(total_line=None),
    lambda g: g["market"].update(observed_lines=[]),
    lambda g: g["under_alert"].update(trigger_line=None),
    lambda g: g["under_alert"].update(trigger_line=197.0),
    lambda g: g.update(game_id=""),
    lambda g: g.pop("under_alert"),
])
def test_armed_executor_fails_closed(tmp_path, armed, mutate):
    """With the gate armed the executor refuses ANY ambiguity.

    The legacy ladder above still runs first, so the named reason may be
    either ladder's; the contract this pins is DECISION-level fail-closed.
    The exact canonical reasons are pinned by test_command_layer_reasons.
    """
    c, store = cfg(tmp_path), _store(tmp_path)
    g = game()
    mutate(g)
    res = evaluate(g, cfg=c, store=store, enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"]
    assert store.recent() == []          # nothing claimed


def test_armed_executor_one_rung_down_allowed(tmp_path, armed):
    c, store = cfg(tmp_path), _store(tmp_path)
    res = evaluate(game(trigger=193.5, cur=193.0), cfg=c, store=store,
                   enabled=True, unit_price=10.0, stats=store.today_stats())
    assert res["decision"] == "WOULD_BET"


def test_armed_executor_duplicate_cannot_create_two_bets(tmp_path, armed):
    c, store = cfg(tmp_path), _store(tmp_path)
    first = evaluate(game(), cfg=c, store=store, enabled=True, unit_price=10.0,
                     stats=store.today_stats())
    assert first["decision"] == "WOULD_BET"
    second = evaluate(game(), cfg=c, store=store, enabled=True, unit_price=10.0,
                      stats=store.today_stats())
    assert second["decision"] == "NO_BET"
    assert second["reason"] == "duplicate_execution"
    assert len(store.recent()) == 1


# ══════════════════════════════════════════════════════════════════════
# C — the MANUAL endpoint routes through the SAME validator when armed
# ══════════════════════════════════════════════════════════════════════

def _client(tmp_path, g):
    store = BettingStore(str(tmp_path / "betting.db"))
    config = cfg(tmp_path)
    current = [g]
    betting_api.configure_betting(store, config, live_payload_fn=lambda: current)
    app = FastAPI()
    app.include_router(betting_api.router)
    store.set_unit_price(10.0)
    store.set_config("auto_betting_enabled", "true")
    return TestClient(app), store


def _manual_payload(g, key, line):
    return {"game_id": g["game_id"], "market": "TOTAL", "direction": "UNDER",
            "line": line, "stake": 10.0, "idempotency_key": key}


def test_armed_manual_under_uses_validator(tmp_path, armed):
    g = game(cur=193.5, trigger=193.5)
    c, _ = _client(tmp_path, g)
    r = c.post("/api/v4/betting/manual", json=_manual_payload(g, "ui-arm-ok", 193.5))
    assert r.status_code == 200, r.text
    assert r.json()["execution"]["selection"] == "UNDER"


def test_armed_manual_under_rejected_by_rung(tmp_path, armed):
    # current line 7 rungs BELOW the frozen trigger -> the canonical gate refuses
    g = game(trigger=193.5, cur=190.0)
    c, store = _client(tmp_path, g)
    r = c.post("/api/v4/betting/manual", json=_manual_payload(g, "ui-arm-bad", 190.0))
    assert r.status_code == 409
    assert "more_than_one_rung_down" in r.json()["detail"]
    assert store.recent() == []


def test_armed_manual_without_observed_lines_fails_closed(tmp_path, armed):
    g = game(obs=None)
    c, store = _client(tmp_path, g)
    r = c.post("/api/v4/betting/manual", json=_manual_payload(g, "ui-arm-noobs", 193.5))
    assert r.status_code == 409
    assert "rung_size_ambiguous" in r.json()["detail"]
    assert store.recent() == []


def test_disarmed_manual_legacy_contract_unchanged(tmp_path):
    """Without the arm gate the manual endpoint keeps its legacy behaviour."""
    g = game(obs=None)
    c, _ = _client(tmp_path, g)
    r = c.post("/api/v4/betting/manual", json=_manual_payload(g, "ui-dis-ok", 193.5))
    assert r.status_code == 200, r.text


# ══════════════════════════════════════════════════════════════════════
# D — execution-state machine + idempotency
# ══════════════════════════════════════════════════════════════════════

def test_state_vocabulary_and_ledger_mapping():
    from blm_v4.betting.store import _STATUSES
    assert C.STATES == ("CREATED", "VALIDATED", "SENT", "ACKNOWLEDGED",
                        "EXECUTED", "REJECTED", "FAILED", "UNKNOWN")
    assert set(C.LEDGER_STATUS) == set(C.STATES)
    for st, ledger in C.LEDGER_STATUS.items():
        assert ledger in _STATUSES, (st, ledger)
    # a timeout/ambiguous outcome maps to UNKNOWN, NEVER to success
    assert C.LEDGER_STATUS[C.UNKNOWN] == "UNKNOWN"
    assert C.LEDGER_STATUS[C.UNKNOWN] not in ("ACCEPTED", "SUBMITTED")


def test_timeout_is_never_success(tmp_path, monkeypatch):
    c, store = cfg(tmp_path, dry_run=False), _store(tmp_path)
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(game(), cfg=c, store=store, enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    class Ambiguous:
        name = "ambiguous"
        def submit(self, **kw):
            raise ProviderAmbiguous("timeout mid-flight")
    out = execute(res["candidate"], cfg=c, store=store, provider=Ambiguous())
    assert out["status"] == "UNKNOWN"
    assert store.get_execution(
        res["candidate"]["execution_id"])["status"] == "UNKNOWN"


def test_command_identity_is_stable_and_immutable():
    g = game()
    k1 = C.command_identity(g, C.SOURCE_AUTONOMOUS)
    g2 = game()
    g2["market"]["total_line"] = 999.0        # a price move must not change it
    g2["projector"]["captured_at"] = "2000-01-01T00:00:00Z"
    assert C.command_identity(g2, C.SOURCE_AUTONOMOUS) == k1


# ══════════════════════════════════════════════════════════════════════
# stake modes — ZERO_STAKE, R2.00 exact, production unit size
# ══════════════════════════════════════════════════════════════════════

def test_zero_stake_is_zero_and_never_real_money():
    cmd = C.build_command(game(), source=C.SOURCE_MANUAL, mode=S.ZERO_STAKE)
    assert cmd["decision"] == C.ALLOW
    assert cmd["stake"]["stake"] == 0.0
    assert cmd["mode"] == S.ZERO_STAKE != S.REAL_MONEY_TEST
    assert cmd["stake"]["stake"] != S.REAL_MONEY_TEST_STAKE


def test_real_money_test_is_exactly_2_00():
    assert S.is_authorized_test_stake(2.00, "ZAR") is True
    for bad in (1.99, 2.01, 5.00, 10.00, 0.0, -2.0, None):
        assert S.is_authorized_test_stake(bad, "ZAR") is False
    assert S.is_authorized_test_stake(2.00, "USD") is False
    # the command refuses an unauthorized real-money test
    assert C.build_command(game(), source=C.SOURCE_MANUAL,
                           mode=S.REAL_MONEY_TEST)["reason"] == "real_money_not_authorized"
    ok = C.build_command(game(), source=C.SOURCE_MANUAL, mode=S.REAL_MONEY_TEST,
                         authorized=True, authorized_by="op")
    assert ok["stake"]["stake"] == 2.00 and ok["decision"] == C.ALLOW


def test_production_never_substitutes_2_00_or_a_default():
    for bad in (None, 0, -1, float("nan"), float("inf")):
        cmd = C.build_command(game(), source=C.SOURCE_MANUAL,
                              mode=S.PRODUCTION_AUTO_BET, unit_size=bad,
                              authorized=True, authorized_by="op")
        assert cmd["decision"] == C.REJECT
        assert cmd["reason"] in ("unit_size_missing", "unit_size_invalid")
    good = C.build_command(game(), source=C.SOURCE_MANUAL,
                           mode=S.PRODUCTION_AUTO_BET, unit_size=25.0,
                           authorized=True, authorized_by="op")
    assert good["stake"]["stake"] == 25.0 != S.REAL_MONEY_TEST_STAKE


def test_unknown_mode_fails_closed():
    assert C.build_command(game(), source=C.SOURCE_MANUAL,
                           mode="LIVE_MONEY")["reason"] == "stake_mode_invalid"


def test_audit_record_carries_the_contract_fields():
    cmd = C.manual_command(game(), **_prod_kwargs(unit=25.0))
    rec = C.audit_record(cmd)
    for field in ("command_id", "idempotency_key", "game_id", "alert_id",
                  "trigger_checkpoint_percent", "trigger_line", "current_line",
                  "rung_size", "rungs_moved", "unit_size", "currency",
                  "execution_decision"):
        assert field in rec, field
    assert rec["trigger_line"] == 193.5 and rec["rung_size"] == 0.5
    assert rec["unit_size"] == 25.0 and rec["currency"] == "ZAR"
    assert rec["execution_decision"] == C.ALLOW


def test_payload_exposes_observed_lines_for_the_rung_rule():
    """The v4 market payload must carry the line series `infer_rung_size` reads.

    Binds the emitter (blm_v4/api.py) to the consumer (rung.py) by key name —
    a source assertion, the same shape PW-01 uses for the canonical formula.
    """
    import inspect
    from blm_v4 import api as v4_api
    from blm_v4.betting import rung
    src = inspect.getsource(v4_api)
    assert '"observed_lines": [l for l in lines if l is not None]' in src
    # the consumer reads market["observed_lines"] and derives the tick
    assert rung.infer_rung_size(game()["market"]["observed_lines"]) == 0.5


# ══════════════════════════════════════════════════════════════════════
# no real money executed anywhere in this suite
# ══════════════════════════════════════════════════════════════════════

def test_no_real_money_execution_in_this_suite(tmp_path):
    c, store = cfg(tmp_path), _store(tmp_path)     # dry_run=True
    assert c.live_money_enabled is False
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(game(), cfg=c, store=store, enabled=True, unit_price=10.0,
                   stats=store.today_stats())
    out = execute(res["candidate"], cfg=c, store=store,
                  provider=DryRunProvider())
    assert out["status"] == "ACCEPTED"
    assert out["provider_ref"].startswith("dryrun-")
    assert store.get_execution(res["candidate"]["execution_id"])["status"] == \
        "ACCEPTED"    # WOULD_BET recorded, nothing real submitted
