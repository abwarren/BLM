"""AUTO-BET BROWSER/EXTENSION EXECUTION BRIDGE — 2026-10-04 directive.

Proves the bridge INTERFACE (``blm_v4/execution/browser_bridge.py``) against the
deterministic fake browser adapter that already exists
(``tests/fake_browser_adapter.py`` — a faithful ``SelectionResolver`` contract
double that records every click/placement and can simulate success, timeout
(unconfirmed), reject, duplicate and error).

ONE NAMED TEST PER REQUIRED SAFETY PROPERTY (no bundling):

  * test_zero_stake_execution_submits_nothing          (zero-stake)
  * test_fail_closed_on_missing_or_ambiguous_inputs    (fail-closed)
  * test_idempotency_command_claimed_and_executed_once (idempotency)
  * test_timeout_is_never_success                      (timeout ≠ success)
  * test_exact_r2_00_stake_validation                  (exact R2.00)
  * test_duplicate_command_rejected                    (duplicate-command)
  * test_ledger_state_transitions_are_auditable        (ledger transitions)
  * test_execution_failure_leaves_consistent_state     (failure handling)

Plus two guards: the shared manual/autonomous validator still converges
(PW-13), and the bridge is driven by the EXISTING fake adapter unchanged.

SAFETY: no provider is constructed, no credentials are read, nothing touches a
real site.  A ZERO real-money bet is placed anywhere in this file.  (The single
R2.00 assertion is a pure stake-authority check — no submission.)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import (  # noqa: E402
    ACCEPTED as FAKE_ACCEPTED,
    FakeBrowserAdapter,
)

from blm_v4.betting import command as C  # noqa: E402
from blm_v4.betting import rung as R  # noqa: E402
from blm_v4.betting import stake as S  # noqa: E402
from blm_v4.execution import browser_bridge as B  # noqa: E402

OBS = (190.0, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0, 193.5)  # 0.5 tick
EVENT = "Team A vs Team B"
GID = "30990001"


# ── builders ────────────────────────────────────────────────────────────────

def game(game_id=GID, checkpoint=75, age_s=5.0, trigger=193.5, cur=193.5,
         obs=OBS, active=True) -> dict:
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


def prod_command(unit=10.0, key="ik-bridge", g=None, source=C.SOURCE_MANUAL):
    cmd = C.build_command(g or game(), source=source,
                          mode=S.PRODUCTION_AUTO_BET, unit_size=unit,
                          authorized=True, authorized_by="test",
                          idempotency_key=key)
    cmd["event"] = EVENT          # the browser-resolvable event label
    return cmd


def repl_command(key="ik-test-2"):
    cmd = C.build_command(game(), source=C.SOURCE_MANUAL,
                          mode=S.REAL_MONEY_TEST, authorized=True,
                          authorized_by="op", idempotency_key=key)
    cmd["event"] = EVENT
    return cmd


def zero_command(key="ik-zero"):
    cmd = C.build_command(game(), source=C.SOURCE_MANUAL,
                          mode=S.ZERO_STAKE, idempotency_key=key)
    cmd["event"] = EVENT
    return cmd


def adapter_ok(line=193.5, under=1.90, over=1.95, *, place_status=FAKE_ACCEPTED,
               confirmation=None, adapt_down=False) -> FakeBrowserAdapter:
    a = FakeBrowserAdapter()
    a.set_market(EVENT, line, over, under)
    a.place_status = place_status
    a.confirmation = confirmation
    a.adapt_down = adapt_down
    return a


def bridge(a: FakeBrowserAdapter) -> B.ExecutionBridge:
    return B.ExecutionBridge(B.ResolverBrowserBridge(a))


# ══════════════════════════════════════════════════════════════════════════
# 1. ZERO-STAKE EXECUTION SUBMITS NOTHING
# ══════════════════════════════════════════════════════════════════════════

def test_zero_stake_execution_submits_nothing():
    a = adapter_ok()
    cmd = zero_command()
    assert cmd["decision"] == C.ALLOW and cmd["state"] == C.VALIDATED
    assert cmd["stake"]["stake"] == 0.0

    rec = bridge(a).execute(cmd)

    assert rec.state == B.NO_OP
    assert rec.reason == B.R_ZERO_STAKE
    assert rec.submitted is False
    # the browser was NEVER clicked and NEVER asked to place a stake
    assert a.clicks == []
    assert a.placements == []
    assert a.slip == []


# ══════════════════════════════════════════════════════════════════════════
# 2. FAIL-CLOSED WHEN INPUTS ARE MISSING / STALE / AMBIGUOUS
# ══════════════════════════════════════════════════════════════════════════

def test_fail_closed_on_missing_or_ambiguous_inputs():
    a = adapter_ok()

    # (a) a REJECTED command (rung rule: 7 rungs below the frozen trigger)
    stale = C.build_command(game(trigger=193.5, cur=190.0),
                            source=C.SOURCE_MANUAL,
                            mode=S.PRODUCTION_AUTO_BET, unit_size=10.0,
                            authorized=True, authorized_by="t",
                            idempotency_key="ik-bad")
    stale["event"] = EVENT
    assert stale["decision"] == C.REJECT
    r1 = bridge(a).execute(stale)
    assert r1.state == B.BRIDGE_REJECTED and r1.reason == B.R_NOT_VALIDATED

    # (b) missing stake entirely (ambiguous — never default to allow)
    no_stake = prod_command(key="ik-nostake")
    no_stake["stake"] = None
    r2 = bridge(a).execute(no_stake)
    assert r2.state == B.BRIDGE_REJECTED and r2.reason == B.R_STAKE_INVALID

    # (c) non-finite stake
    nan_stake = prod_command(key="ik-nan")
    nan_stake["stake"] = dict(nan_stake["stake"])
    nan_stake["stake"]["stake"] = float("nan")
    r3 = bridge(a).execute(nan_stake)
    assert r3.state == B.BRIDGE_REJECTED and r3.reason == B.R_STAKE_INVALID

    # (d) missing command identity
    no_id = prod_command(key="ik-noid")
    no_id.pop("idempotency_key")
    r4 = bridge(a).execute(no_id)
    assert r4.state == B.BRIDGE_REJECTED and r4.reason == B.R_IDENTITY

    # nothing was ever placed, and no slot was consumed
    assert a.placements == []
    assert a.clicks == []


# ══════════════════════════════════════════════════════════════════════════
# 3. IDEMPOTENCY — SAME COMMAND ID CLAIMED/EXECUTED EXACTLY ONCE
# ══════════════════════════════════════════════════════════════════════════

def test_idempotency_command_claimed_and_executed_once():
    a = adapter_ok(confirmation={"reference": "PB-1"})
    cmd = prod_command(key="ik-idem")
    br = bridge(a)

    first = br.execute(cmd)
    assert first.state == B.EXECUTED
    assert first.submitted is True
    assert len(a.placements) == 1

    # the SAME command id delivered again must never reach the browser
    second = br.execute(cmd)
    assert second.state == B.BRIDGE_REJECTED
    assert second.reason == B.R_DUPLICATE
    assert second.submitted is False
    assert len(a.placements) == 1              # still EXACTLY one submission
    assert br.ledger.state(cmd["command_id"]) == B.EXECUTED


# ══════════════════════════════════════════════════════════════════════════
# 4. TIMEOUT ≠ SUCCESS
# ══════════════════════════════════════════════════════════════════════════

def test_timeout_is_never_success():
    # the browser SUBMITTED the bet but never confirmed it (a slow/absent
    # receipt is the browser's timeout semantics) → UNKNOWN, never success
    a = adapter_ok(place_status=B.SUBMITTED, confirmation=None)
    cmd = prod_command(key="ik-timeout")
    rec = bridge(a).execute(cmd)

    assert rec.state == B.BRIDGE_UNKNOWN
    assert rec.status == B.UNKNOWN
    assert rec.reason and rec.reason.startswith(B.R_TIMEOUT)
    assert rec.submitted is False
    assert rec.state not in (B.EXECUTED, B.ACKNOWLEDGED)
    assert rec.provider_ref is None

    # also: ACCEPTED without the bookmaker's OWN receipt is still not success
    a2 = adapter_ok(place_status=B.ACCEPTED, confirmation=None)
    rec2 = bridge(a2).execute(prod_command(key="ik-accepted-noconf"))
    assert rec2.state == B.BRIDGE_UNKNOWN


# ══════════════════════════════════════════════════════════════════════════
# 5. EXACT R2.00 STAKE VALIDATION (no rounding drift)
# ══════════════════════════════════════════════════════════════════════════

def test_exact_r2_00_stake_validation():
    # the stake authority is exact: 2.00 passes, 1.99 / 2.01 do NOT
    assert S.is_authorized_test_stake(2.00, "ZAR") is True
    assert S.is_authorized_test_stake(2.01, "ZAR") is False
    assert S.is_authorized_test_stake(1.99, "ZAR") is False

    # the exact 2.00 command executes and the browser is told EXACTLY 2.00
    a = adapter_ok(confirmation={"reference": "PB-R2"})
    ok = bridge(a).execute(repl_command(key="ik-r2-ok"))
    assert ok.state == B.EXECUTED
    assert len(a.placements) == 1
    assert a.placements[0]["stake"] == 2.00          # 2.00 exactly, no drift
    assert a.placements[0]["stake"] == S.REAL_MONEY_TEST_STAKE

    # a drifting amount (above AND below) is refused BEFORE the browser
    for bad in (2.01, 1.99, 2.005):
        b = adapter_ok(confirmation={"reference": "x"})
        cmd = repl_command(key=f"ik-r2-{bad}")
        cmd["stake"] = dict(cmd["stake"])
        cmd["stake"]["stake"] = bad
        rec = bridge(b).execute(cmd)
        assert rec.state == B.BRIDGE_REJECTED, bad
        assert rec.reason == B.R_TEST_STAKE, bad
        assert b.placements == [], bad              # never submitted


# ══════════════════════════════════════════════════════════════════════════
# 6. DUPLICATE-COMMAND REJECTION
# ══════════════════════════════════════════════════════════════════════════

def test_duplicate_command_rejected():
    a = adapter_ok(confirmation={"reference": "PB-DUP"})
    br = bridge(a)
    cmd = prod_command(key="ik-dup")
    first = br.execute(cmd)
    assert first.state == B.EXECUTED

    # an exact re-delivery of the same command (same immutable command id)
    replay = dict(cmd)
    dup = br.execute(replay)
    assert dup.state == B.BRIDGE_REJECTED
    assert dup.reason == B.R_DUPLICATE
    assert dup.submitted is False
    # the browser saw the submission EXACTLY once
    assert len(a.placements) == 1
    # a duplicate NEVER occupies a second slot — still one claim row
    assert br.ledger.is_terminal(cmd["command_id"]) is True
    history = br.ledger.history(cmd["command_id"])
    assert sum(1 for e in history if e["state"] == B.EXECUTED) == 1


# ══════════════════════════════════════════════════════════════════════════
# 7. LEDGER STATE TRANSITIONS — AUDITABLE, NO SILENT LOSS
# ══════════════════════════════════════════════════════════════════════════

def test_ledger_state_transitions_are_auditable():
    a = adapter_ok(confirmation={"reference": "PB-LED"})
    br = bridge(a)
    cmd = prod_command(key="ik-ledger")
    rec = br.execute(cmd)

    states = [e["state"] for e in br.ledger.history(cmd["command_id"])]
    # claimed → SENT → ACKNOWLEDGED → EXECUTED, in order, no gaps
    assert states == [B.CREATED, B.SENT, B.ACKNOWLEDGED, B.EXECUTED]
    assert rec.state == B.EXECUTED
    assert br.ledger.is_terminal(cmd["command_id"])

    # a REJECTED command also ends in a recorded terminal state (no loss)
    bad = adv_reject()
    br2 = bridge(adapter_ok())
    r2 = br2.execute(bad)
    assert r2.state == B.BRIDGE_REJECTED
    hist = br2.ledger.history(bad["command_id"])
    assert hist and hist[-1]["state"] == B.BRIDGE_REJECTED


def adv_reject():
    cmd = C.build_command(game(trigger=193.5, cur=190.0),
                          source=C.SOURCE_MANUAL, mode=S.PRODUCTION_AUTO_BET,
                          unit_size=10.0, authorized=True, authorized_by="t",
                          idempotency_key="ik-ledger-bad")
    cmd["event"] = EVENT
    return cmd


# ══════════════════════════════════════════════════════════════════════════
# 8. EXECUTION FAILURE HANDLING — CONSISTENT STATE + SURFACED FAILURE
# ══════════════════════════════════════════════════════════════════════════

def test_execution_failure_leaves_consistent_state():
    # the browser is unreachable at the moment of execution (tab closed / CDP)
    a = adapter_ok(adapt_down=True)
    cmd = prod_command(key="ik-fail")
    br = bridge(a)
    rec = br.execute(cmd)

    assert rec.state == B.BRIDGE_FAILED
    assert rec.reason and rec.reason.startswith(B.R_ADAPTER_DOWN)   # SURFACED
    assert rec.submitted is False
    assert rec.provider_ref is None
    # consistent, terminal ledger state — nothing left dangling
    assert br.ledger.state(cmd["command_id"]) == B.BRIDGE_FAILED
    assert br.ledger.is_terminal(cmd["command_id"]) is True
    # and no money moved: neither a placement nor a click was recorded
    assert a.placements == []
    assert a.clicks == []


# ══════════════════════════════════════════════════════════════════════════
# GUARD — manual/autonomous still share ONE validator (PW-13)
# ══════════════════════════════════════════════════════════════════════════

def test_manual_and_autonomous_still_share_one_validator():
    g = game()
    m = C.manual_command(g, mode=S.PRODUCTION_AUTO_BET, unit_size=10.0,
                         authorized=True, authorized_by="t",
                         idempotency_key="ik-shared")
    a = C.autonomous_command(g, mode=S.PRODUCTION_AUTO_BET, unit_size=10.0,
                             authorized=True, authorized_by="t",
                             idempotency_key="ik-shared")
    assert m["rung"] == a["rung"]
    assert m["stake"] == a["stake"]
    assert m["decision"] == a["decision"] == C.ALLOW
    # the canonical rung producers converge too (rung.py)
    assert (R.manual_execution_command(g)
            == R.autonomous_execution_command(g)
            == R.validate_execution(g))


# ══════════════════════════════════════════════════════════════════════════
# GUARD — the bridge drives the EXISTING fake adapter (no new abstraction)
# ══════════════════════════════════════════════════════════════════════════

def test_bridge_is_satisfied_by_the_existing_fake_browser_adapter():
    from blm_v4.execution.adapter import SelectionResolver
    a = adapter_ok(confirmation={"reference": "PB-OK"})
    assert isinstance(a, SelectionResolver)                 # the real contract
    b = B.ResolverBrowserBridge(a)
    assert isinstance(b, B.BrowserBridge)
    rec = b.place(command={"game_id": GID, "event": EVENT, "market": "TOTAL",
                           "selection": "UNDER"},
                  stake_amount=10.0)
    assert rec["status"] == "ACCEPTED" and rec["provider_ref"]
    # every call the bridge made is recorded by the fake (auditability)
    assert len(a.clicks) == 1 and a.clicks[0]["position"] == "UNDER"
    assert len(a.placements) == 1 and a.placements[0]["stake"] == 10.0
