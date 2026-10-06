"""SUBMIT GATE + LATENCY — R2 pre-execution hardening tests (2026-10-05).

Covers the two code workstreams of the hardening directive:

  * the SUBMIT GATE — the control must APPEAR and be ENABLED, the stake must
    read back EXACTLY, and the betslip leg must be re-read and still be the
    intended bet — all BEFORE any click, and a refusal means NO CLICK;
  * the LATENCY trace — measurement only, never a gate.

Zero-stake / mocked-DOM only.  Nothing here touches a live bookmaker.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pokerbet_live_adapter import (  # noqa: E402
    GAME_A,
    GID_A,
    FakePage,
    make_adapter,
)
from blm_v4.execution.pokerbet.latency import (  # noqa: E402
    STAGES,
    LatencyTrace,
)


def ready_adapter(page=None, *, stake="2.00", **kw):
    """An adapter on a resolved page with the stake ALREADY ENTERED — the gate
    is a verification, so the entry (step 6) happens before it (step 7)."""
    kw.setdefault("submit_gate_timeout_ms", 150)
    adapter = make_adapter(page or FakePage(), **kw)
    adapter.find_event(GAME_A)          # attaches the page (and hydrates)
    adapter.find_market(GAME_A, "TOTAL")
    adapter._page.stake_value = stake   # step 6 happened before the gate
    return adapter


def with_pending(adapter, *, position="UNDER", line=164.5):
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": position, "line": line,
                              "price": 1.90, "game_id": GID_A}]
    return adapter


# ═══════════════ the gate's happy path ══════════════════════════════

def test_gate_ready_when_control_enabled_and_everything_matches():
    adapter = with_pending(ready_adapter())
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is True
    assert gate["reason"] == "submit_ready"
    assert gate["stake"] == 2.00
    assert gate["leg"]["position"] == "UNDER"
    assert adapter._submit_button is not None      # the element to click


def test_gate_does_not_click_by_itself():
    adapter = with_pending(ready_adapter())
    adapter.prepare_submit(2.00)
    assert adapter._page.clicks == []              # NOTHING was clicked


# ═══════════════ the gate's refusals (each → NO CLICK) ══════════════

def test_gate_refuses_when_control_is_absent():
    page = FakePage()
    page.place_button_present = False
    adapter = with_pending(ready_adapter(page))
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is False
    assert gate["reason"] == "submit_control_unavailable"


def test_gate_refuses_when_control_is_disabled():
    """The live state that aborted the supervised run: the event was deleted,
    so the control existed but was DISABLED."""
    page = FakePage()
    page.place_button_disabled = True
    adapter = with_pending(ready_adapter(page))
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is False
    assert gate["reason"] == "submit_control_unavailable"
    assert adapter._submit_button is None


def test_gate_refuses_stake_mismatch():
    adapter = with_pending(ready_adapter(stake="5"))   # field holds 5, not 2
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is False and gate["reason"] == "stake_mismatch"
    assert gate["stake_read"] == 5.0


def test_gate_refuses_wrong_direction_leg():
    adapter = with_pending(ready_adapter(), position="OVER")   # slip holds UNDER
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is False
    assert gate["reason"] == "leg_direction_mismatch"


def test_gate_accepts_when_the_line_moved_under_the_bet():
    """Production rule 2026-10-06: a moved line is accepted (take the UNDER)."""
    adapter = with_pending(ready_adapter(), line=166.5)        # slip holds 164.5
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is True


def test_gate_refuses_an_unreadable_betslip():
    page = FakePage(betslip_text="")
    adapter = with_pending(ready_adapter(page))
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is False and gate["reason"] == "betslip_unreadable"


def test_gate_refuses_a_non_positive_stake():
    adapter = with_pending(ready_adapter())
    for bad in (0, -1, None, "x"):
        assert adapter.prepare_submit(bad)["reason"] == "stake_invalid"


# ═══════════════ place_parlay honours the gate ══════════════════════

def test_place_parlay_withholds_the_click_when_the_gate_refuses():
    page = FakePage()
    page.place_button_disabled = True
    adapter = with_pending(ready_adapter(page))
    res = adapter.place_parlay(2.00)
    assert res["status"] == "FAILED"
    assert res["error_code"] == "SUBMIT_CONTROL_UNAVAILABLE"
    assert page.clicks == []                       # NEVER clicked


def test_place_parlay_still_submits_when_the_gate_passes():
    adapter = with_pending(ready_adapter())
    res = adapter.place_parlay(2.00)
    assert res["status"] == "ACCEPTED" or res["status"] == "SUBMITTED"
    assert any(c["role"] == "submit" for c in adapter._page.clicks)


# ═══════════════ latency trace (measurement only) ═══════════════════

def test_latency_trace_records_every_stage_of_the_path():
    trace = LatencyTrace(alert_id=f"{GID_A}|75")
    trace.mark("trigger_detected")
    trace.mark("trigger_line_frozen")
    adapter = ready_adapter(trace=trace)
    with_pending(adapter)
    obs = adapter.find_position(GAME_A, "TOTAL", "UNDER")
    adapter.click_selection(obs)
    adapter.place_parlay(2.00)
    rep = trace.report()
    for stage in ("trigger_detected", "trigger_line_frozen", "market_observed",
                  "selection_added", "stake_entered", "stake_verified",
                  "submit_enabled", "submit_clicked"):
        assert stage in rep["stages"], stage
    assert rep["stages"]["trigger_detected"]["offset_s"] >= 0
    assert rep["total_s"] is not None and rep["total_s"] >= 0
    # every stage carries a monotonic wall stamp for the collector comparison
    assert all(k in rep["wall"] for k in rep["stages"])


def test_latency_deltas_are_non_negative_and_ordered():
    trace = LatencyTrace()
    for s in STAGES:
        trace.mark(s)
    rep = trace.report()
    offs = [rep["stages"][s]["offset_s"] for s in STAGES]
    assert offs == sorted(offs)


def test_latency_first_mark_wins():
    trace = LatencyTrace()
    trace.mark("market_observed")
    first = trace.stages["market_observed"]
    trace.mark("market_observed")
    assert trace.stages["market_observed"] == first


def test_latency_trace_is_optional_and_changes_nothing():
    """No trace → the adapter behaves identically (no gate, no delay)."""
    plain = with_pending(ready_adapter())
    traced = with_pending(ready_adapter(trace=LatencyTrace()))
    assert plain.prepare_submit(2.00)["ready"] is True
    assert traced.prepare_submit(2.00)["ready"] is True


def test_report_without_marks_is_empty_not_misleading():
    rep = LatencyTrace().report()
    assert rep["stages"] == {} and "total_s" not in rep


# ════════ BOUNDED ODDS-CHANGE ACCEPTANCE (live-verified 2026-10-05) ══════

#: observed/pending offer 1.90; the slip shows the bookmaker's updated 1.95
_DRIFT_SLIP = "Team A vs Team B\nTotal Points\nUnder 164.5 @ 1.95\n"


def test_bounded_odds_change_is_accepted_within_the_window():
    """The bookmaker moved 1.90 -> 1.95 (drift 0.05).  Within the bound it is
    ACCEPTED and recorded — not a hard failure."""
    page = FakePage(betslip_text=_DRIFT_SLIP)
    adapter = with_pending(ready_adapter(page, max_odds_drift=0.25))
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is True
    assert gate["odds_accepted"], gate
    assert gate["odds_accepted"][0]["offered"] == 1.90
    assert gate["odds_accepted"][0]["accepted"] == 1.95


def test_odds_change_beyond_the_window_is_also_accepted():
    """Production rule 2026-10-06: line/price movement is IRRELEVANT — even a
    price move beyond the old bounded window is ACCEPTED and recorded."""
    page = FakePage(betslip_text=_DRIFT_SLIP)
    adapter = with_pending(ready_adapter(page, max_odds_drift=0.0))
    gate = adapter.prepare_submit(2.00)
    assert gate["ready"] is True
    assert gate["odds_accepted"], gate
    assert gate["odds_accepted"][0]["offered"] == 1.90
    assert gate["odds_accepted"][0]["accepted"] == 1.95
