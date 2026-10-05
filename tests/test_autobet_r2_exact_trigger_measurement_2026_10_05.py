"""R2 EXACT-TRIGGER EXECUTION MEASUREMENT — instrumentation tests (2026-10-05).

Proves the measurement layer answers ONE question — "did Auto-Bet get the money
down at the line the model identified?" — without touching the model, the
trigger or the execution policy.  Zero-stake / rehearsal only; no authenticated
real-money execution anywhere in this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.betting import measurement as M  # noqa: E402
from blm_v4.betting import rung as R  # noqa: E402
from blm_v4.betting.position import position_from_command  # noqa: E402
from blm_v4.betting.precheck import (  # noqa: E402
    PrecheckLimits,
    run_precheck,
)

GID = "31124212"
TRIGGER = 182.5
#: the live market's tick, DERIVED from observed lines (never hard-coded).
LINES_1_0 = [178.5, 179.5, 180.5, 181.5, 182.5, 183.5, 184.5]


def verdict(current, *, trigger=TRIGGER, observed=LINES_1_0):
    return R.validate_execution({
        "game_id": GID,
        "market": {"total_line": current, "observed_lines": observed},
        "under_alert": {"trigger_line": trigger, "alert_id": f"{GID}|75",
                        "trigger_progress": 75.0,
                        "trigger_captured_at": "2026-10-05T10:13:27Z"}})


def command(current, *, stake=10.0, **kw):
    v = verdict(current, **kw)
    return {"decision": v["decision"], "rung": v, "game_id": GID,
            "checkpoint": 75, "market": "TOTAL", "selection": "UNDER",
            "stake": {"stake": stake}, "alert_id": v["alert_id"],
            "source": "test", "mode": "REHEARSAL"}


def filled(current, *, line=None, odds=1.85, **kw):
    """A REHEARSAL command that produced a Position (a fill)."""
    cmd = command(current, **kw)
    pos = position_from_command(cmd, executable_line=line,
                                executable_odds=odds)
    return cmd, pos


# ══════════ the four measured fills (classification) ═════════════════

def test_exact_trigger_available_is_exact():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=182.5,
                             rung_size=1.0, executed=True)
    assert out["classification"] == M.CLASS_EXACT
    assert out["outcome"] == M.OUTCOME_FILLED and out["rungs_moved"] == 0.0


def test_one_rung_below_is_tolerance_down_1():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=181.5,
                             rung_size=1.0, executed=True)
    assert out["classification"] == M.CLASS_DOWN_1 and out["rungs_moved"] == -1.0


def test_one_rung_above_is_tolerance_up_1():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=183.5,
                             rung_size=1.0, executed=True)
    assert out["classification"] == M.CLASS_UP_1 and out["rungs_moved"] == 1.0


def test_two_rungs_above_is_tolerance_up_2():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=184.5,
                             rung_size=1.0, executed=True)
    assert out["classification"] == M.CLASS_UP_2 and out["rungs_moved"] == 2.0


def test_a_fill_beyond_the_measured_window_is_never_labelled_up_2():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=185.5,
                             rung_size=1.0, executed=True)
    assert out["classification"] == M.CLASS_UNMEASURED and out["rungs_moved"] == 3.0
    assert out["outcome"] == M.OUTCOME_FILLED       # allowed, just not measured


# ══════════ availability miss vs technical failure vs UNKNOWN ════════

def test_unavailable_trigger_is_a_market_availability_miss():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=None,
                             rung_size=1.0, executed=False)
    assert out["classification"] == M.CLASS_MISS
    assert out["outcome"] == M.OUTCOME_MARKET_MISS


def test_line_outside_the_window_is_a_market_availability_miss():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=179.5,
                             rung_size=1.0, executed=False)
    assert out["outcome"] == M.OUTCOME_MARKET_MISS
    assert out["reason"] == "no_qualifying_line"


def test_line_available_but_execution_failed_is_a_technical_failure():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=182.5,
                             rung_size=1.0, executed=False)
    assert out["outcome"] == M.OUTCOME_TECHNICAL_FAILURE
    assert out["outcome"] != M.OUTCOME_MARKET_MISS     # never collapsed


def test_ambiguous_confirmation_is_unknown_never_placed():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=182.5,
                             rung_size=1.0, executed=False, ambiguous=True)
    assert out["outcome"] == M.OUTCOME_UNKNOWN
    assert out["classification"] != M.CLASS_EXACT
    assert out["outcome"] != M.OUTCOME_FILLED


def test_ambiguous_rung_size_fails_closed():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=183.5,
                             rung_size=None, executed=True)
    assert out["outcome"] == M.OUTCOME_UNMEASURABLE
    assert out["classification"] == M.CLASS_MISS       # not a fill
    assert out["outcome"] != M.OUTCOME_MARKET_MISS     # not claimed either


# ══════════ odds are an independent axis ═════════════════════════════

def test_exact_line_with_bad_odds_is_an_odds_refusal():
    out = M.classify_attempt(trigger_line=TRIGGER, available_line=182.5,
                             rung_size=1.0, odds_ok=False, executed=False)
    assert out["classification"] == M.CLASS_ODDS_REJECTED
    assert out["outcome"] == M.OUTCOME_ODDS_REFUSED
    assert out["outcome"] != M.OUTCOME_MARKET_MISS
    assert "ODDS" in out["outcome"]                 # NOT a market-price miss


def test_odds_refusal_recognised_from_the_existing_price_gate():
    for reason in ("ODDS_UNAVAILABLE", "ODDS_TOLERANCE_EXCEEDED"):
        cmd = command(182.5)
        cmd["reason"] = reason
        m = M.measurement_from_command(cmd, position=None)
        assert m.classification == M.CLASS_ODDS_REJECTED
        assert m.outcome == M.OUTCOME_ODDS_REFUSED


# ══════════ the denominator: unique alert identities ═════════════════

def test_retry_does_not_add_another_denominator_entry():
    led = M.MeasurementLedger()
    cmd, pos = filled(182.5)
    first = M.measurement_from_command(cmd, position=pos)
    assert led.record(first) is True
    for _ in range(4):                      # four retries of game|75
        again = M.measurement_from_command(cmd, position=pos)
        assert led.record(again) is False
    assert led.report()["denominator"] == 1
    assert led.report()["attempts"] == 1


def test_duplicate_attempt_creates_no_duplicate_measurement():
    led = M.MeasurementLedger()
    cmd, pos = filled(183.5)
    m = M.measurement_from_command(cmd, position=pos)
    assert led.record_all([m, m, m]) == 1
    assert led.report()["attempts"] == 1


def test_two_checkpoints_are_two_identities():
    led = M.MeasurementLedger()
    cmd, pos = filled(182.5)
    a = M.measurement_from_command(cmd, position=pos)
    cmd2 = command(182.5)
    cmd2["checkpoint"] = "Q3_BREAK"
    cmd2["alert_id"] = f"{GID}|Q3_BREAK"
    cmd2["rung"]["alert_id"] = f"{GID}|Q3_BREAK"
    b = M.measurement_from_command(cmd2, position=pos)
    assert led.record_all([a, b]) == 2
    assert led.report()["denominator"] == 2


def test_alert_identity_matches_the_existing_canonical_form():
    assert M.alert_identity(GID, 75) == f"{GID}|75"
    assert M.alert_identity(GID, "Q3_BREAK") == f"{GID}|Q3_BREAK"


# ══════════ the existing facts are reused, never rewritten ═══════════

def test_trigger_line_stays_immutable_in_the_measurement():
    cmd, pos = filled(184.5, line=184.5, odds=1.72)
    m = M.measurement_from_command(cmd, position=pos)
    assert m.trigger_line == TRIGGER          # the FROZEN intent
    assert m.available_line == 184.5          # what the bookmaker offered
    assert m.trigger_line != m.available_line


def test_executable_line_and_odds_stay_independent():
    cmd, pos = filled(183.5, line=183.5, odds=1.88)
    m = M.measurement_from_command(cmd, position=pos)
    assert m.available_line == 183.5
    assert m.available_odds == 1.88
    assert m.available_odds != m.available_line


def test_measurement_reuses_the_position_rather_than_copying_it():
    cmd, pos = filled(182.5, odds=1.90)
    m = M.measurement_from_command(cmd, position=pos)
    assert m.available_odds == pos.executable_odds
    assert m.available_line == pos.executable_line
    assert m.trigger_line == pos.trigger_line
    # the ledger holds a VIEW; the Position remains the source of truth
    assert m.provider_ref == pos.provider_ref


def test_no_identity_means_no_measurement():
    assert M.measurement_from_command({"rung": {}}) is None
    assert M.MeasurementLedger().record(None) is False


# ══════════ non-0.5 market + fail-closed tick ═══════════════════════

def test_non_half_point_rung_size_is_supported():
    """A 2.0-point tick market: 170.0 is ONE rung above 168.0."""
    observed = [164.0, 166.0, 168.0, 170.0, 172.0]
    v = R.validate_execution({
        "game_id": GID,
        "market": {"total_line": 170.0, "observed_lines": observed},
        "under_alert": {"trigger_line": 168.0, "alert_id": f"{GID}|75"}})
    assert v["rung_size"] == 2.0
    out = M.classify_attempt(trigger_line=168.0, available_line=170.0,
                             rung_size=v["rung_size"], executed=True)
    assert out["classification"] == M.CLASS_UP_1


def test_ambiguous_tick_never_produces_a_fill_classification():
    observed = [160.0, 161.3, 162.7, 164.0]      # no consistent tick
    v = R.validate_execution({
        "game_id": GID,
        "market": {"total_line": 162.5, "observed_lines": observed},
        "under_alert": {"trigger_line": 162.5, "alert_id": f"{GID}|75"}})
    assert v["decision"] == R.REJECT
    out = M.classify_attempt(trigger_line=162.5, available_line=162.5,
                             rung_size=v["rung_size"], executed=True)
    assert out["classification"] == M.CLASS_MISS


# ══════════ roll-ups and the §9 report ══════════════════════════════

def _ledger_with(spec):
    led = M.MeasurementLedger()
    for i, (current, kind) in enumerate(spec):
        gid = f"{GID}{i}"
        if kind == "fill":
            cmd, pos = filled(current)
        else:
            cmd, pos = command(current), None
        cmd["game_id"] = gid
        cmd["rung"]["game_id"] = gid
        cmd["alert_id"] = f"{gid}|75"
        cmd["rung"]["alert_id"] = f"{gid}|75"
        if kind == "tech":
            cmd["reason"] = "CLICK_NOT_REGISTERED"
        if kind == "odds":
            cmd["reason"] = "ODDS_TOLERANCE_EXCEEDED"
        if kind == "unknown":
            cmd["_ambiguous"] = True
        m = M.measurement_from_command(
            cmd, position=pos, ambiguous=(kind == "unknown"))
        if kind == "missing":
            m = M.measurement_from_command(cmd, position=None)
            m = M.AttemptMeasurement(
                alert_id=m.alert_id, game_id=gid, checkpoint=75,
                trigger_line=m.trigger_line, available_line=None,
                classification=M.CLASS_MISS, outcome=M.OUTCOME_MARKET_MISS,
                rungs_moved=None, reason="line_unavailable")
        led.record(m)
    return led


def test_report_counts_and_rates_share_one_denominator():
    led = _ledger_with([
        (182.5, "fill"), (182.5, "fill"),            # 2 exact
        (181.5, "fill"),                             # -1
        (183.5, "fill"),                             # +1
        (184.5, "fill"),                             # +2
        (182.5, "tech"),                             # technical failure
        (182.5, "unknown"),                          # UNKNOWN
        (182.5, "odds"),                             # odds refusal
        (179.5, "miss"),                             # availability miss
    ])
    rep = led.report()
    assert rep["denominator"] == rep["attempts"] == 9
    assert rep["exact_fills"] == 2
    assert rep["tolerance_fills"] == 3
    assert rep["availability_misses"] == 1
    assert rep["technical_failures"] == 1
    assert rep["odds_refusals"] == 1
    assert rep["unknown"] == 1
    assert rep["exact_line_fill_rate"] == pytest.approx(2 / 9)
    assert rep["tolerance_down_1_rate"] == pytest.approx(1 / 9)
    assert rep["tolerance_up_1_rate"] == pytest.approx(1 / 9)
    assert rep["tolerance_up_2_rate"] == pytest.approx(1 / 9)
    assert rep["availability_miss_rate"] == pytest.approx(1 / 9)
    assert rep["technical_failure_rate"] == pytest.approx(1 / 9)
    assert rep["exact_line_odds_rejection_rate"] == pytest.approx(1 / 9)
    assert rep["unknown_rate"] == pytest.approx(1 / 9)


def test_report_distinguishes_signals_from_attempts():
    led = _ledger_with([(182.5, "fill")])
    led.note_signals([f"{GID}0|75", f"{GID}1|75", f"{GID}2|75"])
    rep = led.report()
    assert rep["signals"] == 3
    assert rep["attempts"] == 1               # not every signal was attempted


def test_empty_ledger_reports_none_not_zero():
    rep = M.MeasurementLedger().report()
    assert rep["attempts"] == 0 and rep["exact_line_fill_rate"] is None


# ══════════ zero-stake rehearsal (no wager, instrumentation observes) ══

class _Quote:
    def __init__(self, line, price):
        self.line, self.price = line, price


class _Adapter:
    def __init__(self, quote, status="OPEN"):
        self._q, self._s = quote, status

    def balance(self):
        return 1000.0

    def get_event(self, event_id):
        return {"status": "OPEN"}

    def get_market(self, event_id, market_id):
        return {"status": self._s}

    def get_selection(self, event_id, market_id, position):
        return self._q


class _Session:
    account_id = "rehearsal"

    def assert_submittable(self):
        return True


def test_zero_stake_rehearsal_places_nothing_and_is_measured():
    """The rehearsal path the eventual R2 execution uses, at zero stake:
    the gate refuses the stake, no Position exists, and the instrumentation
    still records the attempt (never a fill)."""
    cmd, pos = filled(182.5, stake=0.0)
    limits = PrecheckLimits(max_stake_per_bet=100.0, max_total_exposure=100.0,
                            current_exposure=0.0, max_line_movement=0.0,
                            max_odds_movement=0.05)
    res = run_precheck({"game_id": GID, "market": "TOTAL", "selection": "UNDER",
                        "line": 182.5, "price": 1.85, "stake_amount": 0.0,
                        "idempotency_key": "k1"},
                       adapter=_Adapter(_Quote(182.5, 1.85)),
                       session=_Session(), pre=limits)
    assert res.ok is False and res.reason == "STAKE_INVALID"   # no wager
    led = M.MeasurementLedger()
    cmd["reason"] = "STAKE_INVALID"          # the gate's own refusal
    m = M.measurement_from_command(cmd, position=None)
    led.record(m)
    assert led.report()["exact_fills"] == 0
    assert led.report()["exact_line_fill_rate"] == 0.0
    # the rehearsal must never have produced a FILL measurement
    assert led.attempts[0].outcome != M.OUTCOME_FILLED


def test_measurement_observes_the_same_command_the_execution_uses():
    """Instrumentation reads the SAME command/rung verdict the eventual R2
    execution path consumes — not a parallel reconstruction."""
    cmd, pos = filled(183.5, odds=1.83)
    assert cmd["rung"]["rungs_moved"] == 1.0          # canonical verdict
    m = M.measurement_from_command(cmd, position=pos)
    assert m.rungs_moved == cmd["rung"]["rungs_moved"]
    assert m.classification == M.CLASS_UP_1


# ══════════ the model and the policy must be untouched ══════════════

def test_measurement_layer_does_not_import_the_model():
    """The instrumentation depends on the EXECUTION rule only — it has no
    handle on under_alert_state or any part of the BLM trigger."""
    assert not hasattr(M, "under_alert_state")
    assert not hasattr(M, "under_trigger")
    assert M.R is R                     # the canonical rung rule, nothing else


def test_measurement_does_not_change_the_execution_policy():
    """The window is still exactly one rung down, unlimited up."""
    assert R.RUNG_ALLOW_MIN == -1.0
    assert verdict(181.5)["decision"] == R.ALLOW
    assert verdict(180.5)["decision"] == R.REJECT
    assert verdict(200.5)["decision"] == R.ALLOW
