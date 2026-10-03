"""Auto-Bet RUNG Production Wiring Gates PW-01..PW-13, PW-15 (2026-10-03).

Proves the VERIFIED rung rule actually CONTROLS execution — that the manual
path, the autonomous path and the pre-execution validation all call the ONE
canonical implementation in ``blm_v4/betting/rung.py`` and cannot bypass it.

NOT provable in this environment (reported as open gates, see the gate doc):
  PW-02 / PW-14  the BLM UI -> PokerBet "extension" end-to-end hop (no browser
                 extension exists in-repo; the PokerBet executor is a Playwright
                 adapter, not a runnable extension)
  PW-03          the live autonomous engine against a real PokerBet window
"""
from __future__ import annotations

import pathlib
import re

import pytest

from blm_v4.betting import rung as R
from blm_v4.betting.store import BettingStore

REPO = pathlib.Path(__file__).resolve().parent.parent
BETTING = REPO / "blm_v4" / "betting"


def game(trigger_line, current_line, observed_lines, *, game_id="G1",
         trigger_progress=0.77, alert_id="G1|75"):
    return {
        "game_id": game_id,
        "under_alert": {"trigger_line": trigger_line, "checkpoint": 75,
                        "trigger_progress": trigger_progress,
                        "trigger_captured_at": "2026-10-03T19:32:21Z",
                        "alert_id": alert_id},
        "market": {"total_line": current_line, "observed_lines": observed_lines},
    }


# ── PW-01 canonical implementation (exactly one) ──────────────────────────
def test_pw01_one_canonical_rungs_definition():
    hits = []
    for p in BETTING.rglob("*.py"):
        src = p.read_text()
        if "def rungs_moved" in src:
            hits.append(p.name)
    assert hits == ["rung.py"], hits


def test_pw01_one_canonical_decision_definition():
    hits = []
    for p in BETTING.rglob("*.py"):
        src = p.read_text()
        if "def under_rung_decision" in src:
            hits.append(p.name)
    assert hits == ["rung.py"], hits


def test_pw01_no_duplicated_formula_or_boundary_elsewhere():
    """The formula and the >= -1 boundary appear ONLY in rung.py."""
    offenders = []
    for p in BETTING.rglob("*.py"):
        if p.name == "rung.py":
            continue
        src = p.read_text()
        if "(current_line - trigger_line)" in src or "rungs_moved >= -1" in src:
            offenders.append(p.name)
    assert offenders == []


# ── PW-02 / PW-03 both producers call the canonical validator ─────────────
def test_pw02_manual_calls_canonical(monkeypatch):
    calls = {"n": 0}
    orig = R.under_rung_decision

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(R, "under_rung_decision", spy)
    r = R.manual_execution_command(game(48.5, 49.5, [48.5, 49.0, 49.5]))
    assert calls["n"] == 1
    assert r["rungs_moved"] == 2.0 and r["decision"] == "ALLOW"


def test_pw03_autonomous_calls_canonical(monkeypatch):
    calls = {"n": 0}
    orig = R.under_rung_decision

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(R, "under_rung_decision", spy)
    r = R.autonomous_execution_command(game(48.5, 49.5, [48.5, 49.0, 49.5]))
    assert calls["n"] == 1
    assert r["rungs_moved"] == 2.0 and r["decision"] == "ALLOW"


# ── PW-04 pre-execution validation is independent + mandatory ────────────
def test_pw04_validation_reports_every_required_field():
    r = R.validate_execution(game(48.5, 48.0, [48.5, 48.0, 47.5]))
    for key in ("game_id", "market", "selection", "trigger_line",
                "current_line", "rung_size", "rungs_moved", "decision",
                "reason"):
        assert key in r
    assert r["trigger_line"] == 48.5 and r["current_line"] == 48.0
    assert r["rung_size"] == 0.5 and r["rungs_moved"] == -1.0
    assert r["decision"] == "ALLOW"


def test_pw04_no_allow_without_identity():
    g = game(48.5, 48.0, [48.5, 48.0, 47.5], game_id="")
    r = R.validate_execution(g)
    assert r["decision"] == "REJECT" and r["reason"] == R.R_IDENTITY


def test_pw04_no_allow_without_an_alert():
    """HARD ARCHITECTURAL RULE: no alert -> no opportunity."""
    g = {"game_id": "G1", "market": {"total_line": 48.0,
                                     "observed_lines": [48.5, 48.0, 47.5]}}
    r = R.validate_execution(g)
    assert r["decision"] == "REJECT" and r["reason"] == R.R_IDENTITY


@pytest.mark.parametrize("market,selection", [("SPREAD", "UNDER"),
                                              ("TOTAL", "OVER")])
def test_pw04_rejects_ambiguous_market_or_selection(market, selection):
    r = R.validate_execution(game(48.5, 48.0, [48.5, 48.0, 47.5]),
                             market=market, selection=selection)
    assert r["decision"] == "REJECT" and r["reason"] == R.R_IDENTITY


# ── PW-05 immutable trigger line ──────────────────────────────────────────
def test_pw05_trigger_line_immutable_across_market_moves():
    g = game(48.5, 49.5, [48.5, 49.0, 49.5])
    first = R.validate_execution(g)["trigger_line"]
    # the market moves a lot; the trigger used is still the frozen alert line
    g["market"] = {"total_line": 45.0, "observed_lines": [48.5, 49.0, 49.5]}
    second = R.validate_execution(g)["trigger_line"]
    assert first == second == 48.5


def test_pw05_rerender_does_not_reset_trigger():
    """A DOM re-render that changes observed_lines must not change the trigger."""
    g = game(48.5, 48.0, [48.5, 48.0, 47.5])
    t0 = R.validate_execution(g)["trigger_line"]
    g["market"]["observed_lines"] = [50.0, 50.5, 51.0]   # a completely new series
    t1 = R.validate_execution(g)["trigger_line"]
    assert t0 == t1 == 48.5


# ── PW-06 live rung size (never a hard-coded 0.5) ─────────────────────────
def test_pw06_rung_from_live_market_1_0_tick():
    # tick 1.0 market: a -1.0 move is ONE rung (ALLOW), not a point judgement
    g = game(48.5, 47.5, [49.5, 48.5, 47.5])
    r = R.validate_execution(g)
    assert r["rung_size"] == 1.0 and r["rungs_moved"] == -1.0
    assert r["decision"] == "ALLOW"


def test_pw06_hardcoded_0_5_would_change_the_answer():
    """Same move, judged by a hard-coded 0.5 rung, would REJECT. Proves the
    production value is the live market tick, not the 0.5 example."""
    g = game(48.5, 47.5, [49.5, 48.5, 47.5])   # live tick 1.0
    live = R.validate_execution(g)
    hardcoded = R.under_rung_decision(47.5, 48.5, 0.5)   # -2.0 rungs
    assert live["decision"] == "ALLOW"
    assert hardcoded["decision"] == "REJECT"


# ── PW-07 fail closed ─────────────────────────────────────────────────────
@pytest.mark.parametrize("cur,trig,series,reason", [
    (None, 48.5, [48.5, 48.0, 47.5], R.R_MARKET_MISSING),   # current missing
    (48.0, None, [48.5, 48.0, 47.5], R.R_MARKET_MISSING),   # trigger missing
    (48.0, 48.5, None, R.R_RUNG_AMBIGUOUS),                 # rung missing
    (48.0, 48.5, [48.5, 48.0], R.R_RUNG_AMBIGUOUS),         # <2 increments
    (48.0, 48.5, [48.5, 47.0, 45.0], R.R_RUNG_AMBIGUOUS),   # inconsistent steps
])
def test_pw07_fail_closed(cur, trig, series, reason):
    g = game(trig, cur, series)
    r = R.validate_execution(g)
    assert r["decision"] == "REJECT" and r["reason"] == reason


def test_pw07_zero_or_negative_rung_size_rejects():
    assert R.under_rung_decision(48.0, 48.5, 0)["decision"] == "REJECT"
    assert R.under_rung_decision(48.0, 48.5, -0.5)["decision"] == "REJECT"
    assert R.under_rung_decision(48.0, 48.5, float("inf"))["decision"] == "REJECT"


# ── PW-08 fresh market state ──────────────────────────────────────────────
def test_pw08_stale_current_line_rejects():
    g = game(48.5, None, [48.5, 48.0, 47.5])   # nothing reacquired
    r = R.validate_execution(g)
    assert r["decision"] == "REJECT" and r["reason"] == R.R_MARKET_MISSING


# ── PW-09 movement rule truth table ───────────────────────────────────────
@pytest.mark.parametrize("cur,expected", [
    (50.5, "ALLOW"),   # up 4 rungs
    (48.5, "ALLOW"),   # unchanged
    (48.0, "ALLOW"),   # down exactly 1
    (47.5, "REJECT"),  # down 2
    (46.5, "REJECT"),  # down 4
])
def test_pw09_movement_rule(cur, expected):
    g = game(48.5, cur, [48.5, 48.0, 47.5])
    assert R.validate_execution(g)["decision"] == expected


# ── PW-10 adversarial: fixed-point rule disagrees with rung rule ──────────
def test_pw10_directive_examples():
    # trigger 48.5, current 47.0, live tick 1.0 -> -1.5 rungs -> REJECT
    g1 = game(48.5, 47.0, [49.5, 48.5, 47.5])
    r1 = R.validate_execution(g1)
    assert r1["rung_size"] == 1.0 and r1["rungs_moved"] == -1.5
    assert r1["decision"] == "REJECT"
    # trigger 48.5, current 47.5, live tick 1.0 -> -1.0 rung exactly -> ALLOW
    g2 = game(48.5, 47.5, [49.5, 48.5, 47.5])
    r2 = R.validate_execution(g2)
    assert r2["rung_size"] == 1.0 and r2["rungs_moved"] == -1.0
    assert r2["decision"] == "ALLOW"


def test_pw10_fixed_point_rule_disagrees_with_rung_rule():
    """A fixed POINT-distance tolerance is not the rule. On a 0.5-tick market a
    -2.0-point move is FOUR rungs (REJECT); a naive '<=2 points' rule ALLOWs it."""
    g = game(48.5, 46.5, [48.5, 48.0, 47.5])   # tick 0.5; move -2.0 pts
    r = R.validate_execution(g)
    assert r["rung_size"] == 0.5 and r["rungs_moved"] == -4.0
    assert r["decision"] == "REJECT"
    # a fixed-point implementation would ALLOW this — the two DISAGREE:
    point_rule = "ALLOW" if abs(46.5 - 48.5) <= 2.0 else "REJECT"
    assert point_rule == "ALLOW"
    assert point_rule != r["decision"]


def test_pw10_boundary_one_rung_allows():
    g = game(48.5, 47.5, [49.5, 48.5, 47.5])   # -1.0 rung exactly
    r = R.validate_execution(g)
    assert r["rungs_moved"] == -1.0 and r["decision"] == "ALLOW"


# ── PW-11 DOM reacquisition retains the trigger ───────────────────────────
def test_pw11_disappear_then_reappear_recalculates_reusing_trigger():
    g = game(48.5, 48.0, [48.5, 48.0, 47.5])
    # 1) market disappears
    g["market"] = {"total_line": None, "observed_lines": None}
    gone = R.validate_execution(g)
    assert gone["decision"] == "REJECT" and gone["reason"] == R.R_MARKET_MISSING
    # 2) market reappears, selectors reacquired, current refreshed, trigger kept
    g["market"] = {"total_line": 48.0, "observed_lines": [48.5, 48.0, 47.5]}
    back = R.validate_execution(g)
    assert back["trigger_line"] == 48.5            # original trigger retained
    assert back["rungs_moved"] == -1.0 and back["decision"] == "ALLOW"


# ── PW-12 idempotency (same command id never executes twice) ──────────────
def test_pw12_idempotent_claim_once(tmp_path):
    store = BettingStore(str(tmp_path / "betting.db"))
    rec = {"execution_id": "bet-x", "idempotency_key": "G1|75|G1|75",
           "game_id": "G1", "alert_id": "G1|75", "checkpoint": "75",
           "market": "TOTAL", "selection": "UNDER", "triggered_line": 48.5,
           "price": 48.0, "unit_price": 1.0, "stake_units": 1.0,
           "stake_amount": 1.0, "status": "PENDING"}
    first, _ = store.claim(rec)
    second, existing = store.claim(rec)          # DOM rerender / retry / re-delivery
    assert first is True and second is False
    assert existing["idempotency_key"] == "G1|75|G1|75"


# ── PW-13 manual/autonomous equivalence ───────────────────────────────────
@pytest.mark.parametrize("cur", [49.5, 48.5, 48.0, 47.5, 47.0])
def test_pw13_manual_equals_autonomous(cur):
    g = game(48.5, cur, [48.5, 48.0, 47.5])
    m = R.manual_execution_command(g)
    a = R.autonomous_execution_command(g)
    assert m == a == R.validate_execution(g)


# ── PW-15 real-money separation (no real money here) ──────────────────────
def test_pw15_no_real_money_execution_in_this_suite():
    from blm_v4.betting import stake as S
    # PRODUCTION without a configured unit size must never default to R2.00
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=None,
                        currency="ZAR", authorized=True)
    assert r["ok"] is False and r["stake"] != 2.00
