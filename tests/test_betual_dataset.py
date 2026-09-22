"""Betual-only dataset tests (directive 2026-09-22, DATA COLLECTION
ONLY).

Covers the directive's §17 matrix: internal timer (init, progression,
restart, recovery, drift), quarter transitions incl. halftime + Q3→Q4,
score + line movement fields, line returning to a previous value,
duplicates, missing data, game end / final-state recovery, the §1
BETUAL-only gate, and the §18 metrics.  NOTHING here touches alert
logic, fingerprints, betting or thresholds — these tables feed no
decision path.
"""
import json
import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace

import pytest

from blm_v4 import betual_timer as bt
from blm_v4.betual_dataset import (
    BetualDataset,
    BetualDatasetError,
    BetualGameRecord,
)
from blm_v4.storage import PokerBetStore


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def store():
    tmp = tempfile.mkdtemp()
    return PokerBetStore(os.path.join(tmp, "b.db"))


@pytest.fixture()
def dataset(store):
    return BetualDataset(store)


def rec_for(ds, gid="B1", cls="BETUAL_NBA"):
    rec = ds.game(gid, cls)
    assert rec is not None
    return rec


# ── 1. internal timer (§3/§4) ────────────────────────────────────────

def test_timer_unanchored_reports_not_anchored():
    it = bt.derive_internal_time(bt.TimerAnchors(), wall_now=1000.0)
    assert it.anchored is False and it.quarter is None
    assert it.internal_elapsed_seconds == 0.0
    assert it.source == "internal"          # §2 invariant


def test_timer_progression_from_start_anchor():
    a = bt.TimerAnchors(game_start_wall=1000.0, observed_at_wall=1000.0,
                        monotonic_anchor=0.0, monotonic_elapsed=0.0)
    # monotonic path: 130s after start → Q2 (q=600 default), 130s remaining
    it = bt.derive_internal_time(a, monotonic_now=130.0)
    assert it.anchored and it.quarter == 1
    assert it.internal_elapsed_seconds == 130.0
    assert it.quarter_remaining_seconds == 470.0
    it2 = bt.derive_internal_time(a, monotonic_now=700.0)   # into Q2
    assert it2.quarter == 2
    assert it2.quarter_elapsed_seconds == 100.0
    assert it2.internal_game_time == "11:40"


def test_timer_mid_game_join_does_not_restart_at_zero():
    """A game joined 20 min after its authoritative start keeps its
    elapsed time (§13: never reset to zero)."""
    rec = BetualGameRecord("B-JOIN")
    rec.adopt_start_evidence(
        game_start_wall=time.time() - 1200.0,
        observed_at_wall=time.time() - 60.0,
        elapsed_at_adoption=1140.0)
    it = bt.derive_internal_time(rec.anchors, monotonic_now=time.monotonic())
    assert it.internal_elapsed_seconds >= 1140.0
    assert it.quarter == 2


def test_timer_calibration_from_observed_transitions():
    """§4: quarter lengths are CALIBRATED from observed Q1→Q2 spans, not
    assumed.  Observed 720s step → model switches to calibrated 720."""
    obs = [
        bt.PhaseObservation(phase=1, observed_at_wall=1000.0),
        bt.PhaseObservation(phase=2, observed_at_wall=1720.0),
        bt.PhaseObservation(phase=3, observed_at_wall=2440.0),
    ]
    a = bt.settle_phase(None, obs)
    assert a.model == "calibrated"
    assert a.quarter_seconds == 720.0
    assert a.break_seconds == 0.0            # no-timeout model


def test_timer_calibration_median_robust_to_lag():
    obs = [
        bt.PhaseObservation(phase=1, observed_at_wall=0.0),
        bt.PhaseObservation(phase=2, observed_at_wall=600.0),
        bt.PhaseObservation(phase=3, observed_at_wall=1200.0),
        bt.PhaseObservation(phase=4, observed_at_wall=2400.0),  # lagged
    ]
    a = bt.settle_phase(None, obs)
    assert a.quarter_seconds == 600.0        # median, not the lagged 1200


def test_timer_calibration_without_anchor_still_calibrates():
    """A game joined mid-stream with NO start evidence still calibrates
    its quarter model from observed transitions."""
    a = bt.settle_phase(None, [
        bt.PhaseObservation(phase=2, observed_at_wall=100.0),
        bt.PhaseObservation(phase=3, observed_at_wall=700.0),
    ])
    assert a.model == "calibrated" and a.quarter_seconds == 600.0
    assert a.game_start_wall is None          # start not invented (§2)


def test_timer_default_model_reported_honestly():
    a = bt.settle_phase(None, [bt.PhaseObservation(phase=1,
                                                   observed_at_wall=5.0)])
    assert a.model == "default"
    assert a.quarter_seconds == bt.DEFAULT_QUARTER_SECONDS


def test_wall_path_is_ntp_safe_for_restart():
    """§13: both terms of the wall difference move together under NTP;
    elapsed derived wall-to-wall cannot go backwards."""
    a = bt.TimerAnchors(game_start_wall=1000.0, observed_at_wall=1000.0)
    assert bt.derive_internal_time(a, wall_now=1100.0) \
        .internal_elapsed_seconds == 100.0
    assert bt.derive_internal_time(a, wall_now=1250.5) \
        .internal_elapsed_seconds == 250.5
    assert bt.derive_internal_time(a, wall_now=900.0) \
        .internal_elapsed_seconds == 0.0     # clamped, never negative


def test_post_q4_no_invented_game_time():
    a = bt.TimerAnchors(game_start_wall=0.0, observed_at_wall=0.0,
                        quarter_seconds=600.0)
    it = bt.derive_internal_time(a, wall_now=3000.0)
    assert it.internal_elapsed_seconds == 2400.0   # capped at 4x600
    assert it.quarter == 4


# ── 2. clock diagnostics (§14) — flags, never steering ───────────────

def test_backward_clock_movement_flagged():
    prev = bt.PrevState(phase=3, observed_at_wall=100.0, remaining=300.0,
                        captured_at="T1")
    diags = bt.clock_diagnostics(prev, 3, 130.0, 330.0, "T2", 3)
    kinds = {d.kind for d in diags}
    assert "backward_clock_movement" in kinds


def test_duplicate_timestamp_flagged():
    prev = bt.PrevState(phase=2, observed_at_wall=100.0, remaining=100.0,
                        captured_at="SAME")
    diags = bt.clock_diagnostics(prev, 2, 100.0, 99.0, "SAME", 2)
    assert any(d.kind == "duplicate_timestamp" for d in diags)


def test_impossible_elapsed_time_flagged():
    prev = bt.PrevState(phase=1, observed_at_wall=0.0, remaining=500.0,
                        captured_at="T1")
    diags = bt.clock_diagnostics(prev, 1, 700.0, 100.0, "T2", 1)
    assert any(d.kind == "impossible_elapsed_time" for d in diags)


def test_transition_gap_flagged():
    prev = bt.PrevState(phase=1, observed_at_wall=0.0, remaining=10.0,
                        captured_at="T1")
    diags = bt.clock_diagnostics(prev, 2, 500.0, 600.0, "T2", None)
    assert any(d.kind == "quarter_transition_anomaly" for d in diags)


def test_phase_quarter_mismatch_only_when_calibrated():
    prev = bt.PrevState(phase=3, observed_at_wall=100.0, remaining=100.0,
                        captured_at="T1")
    # default model: internal lag is a known property, NOT an anomaly
    a = bt.clock_diagnostics(prev, 3, 130.0, 90.0, "T2", 4, model="default")
    assert not any(d.kind == "phase_quarter_mismatch" for d in a)
    b = bt.clock_diagnostics(prev, 3, 130.0, 90.0, "T2", 4,
                             model="calibrated")
    assert any(d.kind == "phase_quarter_mismatch" for d in b)


# ── 3. score observations (§1/§5) ────────────────────────────────────

def test_score_observation_roundtrip_with_internal_time(store, dataset):
    rec = rec_for(dataset)
    row = dataset.record_score_observation(
        rec, source_game_id="B1", classification="BETUAL_NBA",
        captured_at="2026-09-22T10:00:00.000000Z",
        home=61, away=55, quarter=3, displayed_clock="05:00",
        period_label="3rd Quarter", q_scores=[(18, 14), (39, 33)],
        raw={"k": "v"})
    assert row["internal_game_time"] is not None
    assert row["total_score"] == 116
    assert row["source_quarter"] == 3 and row["quarter"] is None  # unanchored
    assert row["betual_displayed_clock"] == "05:00"
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM betual_time_observations").fetchone())
    finally:
        con.close()
    assert r["q1_home_score"] == 18 and r["q1_away_score"] == 14
    assert r["q2_home_score"] == 21 and r["q2_away_score"] == 19
    assert r["q3_home_score"] is None      # cumulative for Q3 not exposed
    assert json.loads(r["raw_json"]) == {"k": "v"}


def test_quarter_derivation_only_from_valid_inputs(store, dataset):
    """§5: cumulative→quarter subtraction only when BOTH cumulatives are
    observed; a missing earlier cumulative leaves later legs NULL."""
    rec = rec_for(dataset, "B-DERIVE")
    dataset.record_score_observation(
        rec, source_game_id="B-DERIVE", classification="BETUAL_NBA",
        captured_at="T1", home=86, away=75, quarter=4,
        displayed_clock="00:30",
        q_scores=[(18, 14), None, (61, 55)])   # Q2 cumulative missing
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM betual_time_observations").fetchone())
    finally:
        con.close()
    assert r["q1_home_score"] == 18
    assert r["q2_home_score"] is None          # cannot derive across a gap
    assert r["q3_home_score"] is None


def test_missing_scores_store_null_never_fabricated(store, dataset):
    rec = rec_for(dataset, "B-NULL")
    row = dataset.record_score_observation(
        rec, source_game_id="B-NULL", classification="BETUAL_NBA",
        captured_at="T1", home=None, away=None, quarter=2,
        displayed_clock=None, q_scores=[])
    assert row["total_score"] is None
    assert row["clock_difference"] is None     # never guessed
    assert row["betual_displayed_clock"] is None


# ── 4. line observations + movement model (§6/§7) ────────────────────

def test_line_movement_fields_calculated(store, dataset):
    rec = rec_for(dataset, "B-LINE")
    r1 = dataset.record_line_observation(
        rec, source_game_id="B-LINE", classification="BETUAL_NBA",
        captured_at="T1", market_id="M1", market_name="3rd Quarter Total",
        period="Q3", line=45.5, home=40, away=37, quarter=3,
        displayed_clock="06:00")
    assert r1["line_previous"] is None         # first observation
    assert r1["line_change"] is None
    time.sleep(0.05)
    r2 = dataset.record_line_observation(
        rec, source_game_id="B-LINE", classification="BETUAL_NBA",
        captured_at="T2", market_id="M1", market_name="3rd Quarter Total",
        period="Q3", line=47.5, home=42, away=37, quarter=3,
        displayed_clock="05:00")
    assert r2["line_previous"] == 45.5
    assert r2["line_change"] == 2.0
    assert r2["seconds_since_previous_line"] > 0
    assert r2["line_velocity"] > 0
    assert r2["score_at_observation"] == 77    # previous obs total


def test_line_returning_to_previous_value_retained(store, dataset):
    """§10: change-and-return is a genuine market event — three rows."""
    rec = rec_for(dataset, "B-RETURN")
    for ts, line in (("T1", 45.5), ("T2", 47.5), ("T3", 45.5)):
        dataset.record_line_observation(
            rec, source_game_id="B-RETURN", classification="BETUAL_NBA",
            captured_at=ts, market_id="M1", market_name="Total",
            period="full_game", line=line, home=50, away=50, quarter=3,
            displayed_clock=None)
    con = store._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT captured_at, line, line_previous, line_change "
            "FROM betual_line_observations ORDER BY captured_at")]
    finally:
        con.close()
    assert [r["line"] for r in rows] == [45.5, 47.5, 45.5]
    assert rows[2]["line_previous"] == 47.5 and rows[2]["line_change"] == -2.0


def test_line_dedup_only_exact_logical_duplicate(store, dataset):
    """§10: SAME market+line+timestamp collapses; a new timestamp for the
    same line is a fresh observation (the book re-showing a price)."""
    rec = rec_for(dataset, "B-DEDUP")
    for ts in ("T1", "T1", "T2"):
        dataset.record_line_observation(
            rec, source_game_id="B-DEDUP", classification="BETUAL_NBA",
            captured_at=ts, market_id="M1", market_name="Total",
            period="full_game", line=155.5, home=40, away=37, quarter=2,
            displayed_clock=None)
    con = store._connect()
    try:
        rows = con.execute(
            "SELECT captured_at FROM betual_line_observations "
            "ORDER BY captured_at").fetchall()
    finally:
        con.close()
    assert [r["captured_at"] for r in rows] == ["T1", "T2"]


def test_missing_line_stores_null_with_no_movement(store, dataset):
    """§6/§9: a NULL line is stored verbatim; deltas stay NULL."""
    rec = rec_for(dataset, "B-NOLINE")
    row = dataset.record_line_observation(
        rec, source_game_id="B-NOLINE", classification="BETUAL_NBA",
        captured_at="T1", market_id="M1", market_name="Total",
        period="full_game", line=None, home=10, away=8, quarter=1,
        displayed_clock=None)
    assert row["line"] is None and row["line_previous"] is None
    assert row["line_velocity"] is None


# ── 5. quarter transitions (§11) ─────────────────────────────────────

def _transition(store, dataset, gid, prev_q, new_q, captured_at="T9"):
    rec = rec_for(dataset, gid)
    return dataset.maybe_transition(
        rec, source_game_id=gid, classification="BETUAL_NBA",
        captured_at=captured_at, prev_quarter=prev_q, new_quarter=new_q,
        home=61, away=55, full_game_line=155.5, quarter_line=38.5,
        line_previous=154.5, displayed_clock="12:00",
        q_scores=[(18, 14), (39, 33), (61, 55)], raw={})


def test_q3_to_q4_transition_captured(store, dataset):
    row = _transition(store, dataset, "B-T34", 3, 4)
    assert row is not None and row["transition"] == "Q3->Q4"
    con = store._connect()
    try:
        r = dict(con.execute("SELECT * FROM betual_transitions").fetchone())
    finally:
        con.close()
    assert r["prev_quarter_home"] == 61 and r["prev_quarter_away"] == 55
    assert r["full_game_line"] == 155.5 and r["line_previous"] == 154.5
    assert r["line_change"] == 1.0
    assert r["new_quarter"] == 4


def test_non_adjacent_quarter_jump_is_not_a_transition(store, dataset):
    assert _transition(store, dataset, "B-JUMP", 1, 3) is None
    assert _transition(store, dataset, "B-JUMP2", 3, 2) is None
    assert _transition(store, dataset, "B-JUMP3", None, 2) is None


def test_transition_recorded_once_per_game(store, dataset):
    _transition(store, dataset, "B-ONCE", 3, 4, captured_at="T9")
    _transition(store, dataset, "B-ONCE", 3, 4, captured_at="T10")
    con = store._connect()
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM betual_transitions").fetchone()[0]
    finally:
        con.close()
    assert n == 1                              # UNIQUE(game, transition)


# ── 6. game end + final-state recovery (§12) ─────────────────────────

def test_game_end_observed_final_with_quarter_scores(store, dataset):
    rec = rec_for(dataset, "B-END")
    row = dataset.record_game_end(
        rec, source_game_id="B-END", classification="BETUAL_NBA",
        captured_at="T-END", home=86, away=75, quarter=4,
        displayed_clock="00:00", full_game_line=155.5,
        settlement_state="fulltime", end_evidence="observed_final",
        q_scores=[(18, 14), (21, 19), (22, 22), (25, 20)], raw={})
    assert row["final_total"] == 161
    assert row["q4_home_score"] == 25
    con = store._connect()
    try:
        r = dict(con.execute("SELECT * FROM betual_game_ends").fetchone())
    finally:
        con.close()
    assert r["end_evidence"] == "observed_final"
    assert r["settlement_state"] == "fulltime"


def test_game_end_disappeared_is_never_final(store, dataset):
    """§12: a game that left the panel is recorded with
    end_evidence='disappeared' — it must NOT read as final."""
    rec = rec_for(dataset, "B-GONE")
    dataset.record_game_end(
        rec, source_game_id="B-GONE", classification="BETUAL_NBA",
        captured_at="T-GONE", home=None, away=None, quarter=None,
        end_evidence="disappeared", q_scores=[])
    con = store._connect()
    try:
        r = dict(con.execute("SELECT * FROM betual_game_ends").fetchone())
    finally:
        con.close()
    assert r["end_evidence"] == "disappeared"
    assert r["final_total"] is None            # nothing fabricated


def test_game_end_dedup_per_timestamp(store, dataset):
    rec = rec_for(dataset, "B-END2")
    for _ in range(2):
        dataset.record_game_end(
            rec, source_game_id="B-END2", classification="BETUAL_NBA",
            captured_at="T-END2", home=100, away=90, quarter=4,
            end_evidence="observed_final", q_scores=[])
    con = store._connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM betual_game_ends").fetchone()[0]
    finally:
        con.close()
    assert n == 1


# ── 7. restart / recovery (§13) ──────────────────────────────────────

def test_timer_survives_collector_restart(store, dataset):
    gid = "B-RESTART"
    rec = rec_for(dataset, gid)
    start_wall = time.time() - 1500.0           # game began 25 min ago
    rec.adopt_start_evidence(start_wall, time.time() - 60.0,
                             elapsed_at_adoption=1440.0)
    dataset.record_score_observation(
        rec, source_game_id=gid, classification="BETUAL_NBA",
        captured_at="T-PRE", home=50, away=48, quarter=3,
        displayed_clock="05:00", q_scores=[])
    dataset.record_line_observation(
        rec, source_game_id=gid, classification="BETUAL_NBA",
        captured_at="T-PRE", market_id="M1", market_name="Total",
        period="full_game", line=155.5, home=50, away=48, quarter=3,
        displayed_clock=None)
    store.upsert_betual_timer(
        source_game_id=gid, game_start_wall=start_wall,
        observed_at_wall=start_wall, quarter_seconds=None,
        break_seconds=None, timer_model="default",
        last_home=50, last_away=48, last_line=155.5,
        last_line_at=time.time() - 10.0, last_capture_at="T-PRE")

    # ── the restart: a NEW dataset instance over the SAME store ──
    ds2 = BetualDataset(store)
    r = store.get_betual_timer(gid)
    rec2 = ds2.restore(
        gid, "BETUAL_NBA", game_start_wall=r["game_start_wall"],
        observed_at_wall=r["observed_at_wall"],
        quarter_seconds=r["quarter_seconds"],
        timer_model=r["timer_model"], last_home=r["last_home"],
        last_away=r["last_away"], last_line=r["last_line"],
        last_line_at=r["last_line_at"])
    assert rec2 is not None and rec2.is_anchored()
    it = rec2.internal_time()
    assert it["internal_elapsed_seconds"] >= 1500.0   # NOT reset to zero
    # movement deltas continue across the restart boundary
    row = ds2.record_line_observation(
        rec2, source_game_id=gid, classification="BETUAL_NBA",
        captured_at="T-POST", market_id="M1", market_name="Total",
        period="full_game", line=157.5, home=55, away=50, quarter=3,
        displayed_clock=None)
    assert row["line_previous"] == 155.5 and row["line_change"] == 2.0


def test_restore_is_noop_for_non_betual(store, dataset):
    assert dataset.restore("X1", "CYBER_2K26", 1.0, 1.0) is None


# ── 8. the §1 BETUAL-only gate ───────────────────────────────────────

def test_non_betual_classification_refused(store, dataset):
    with pytest.raises(BetualDatasetError):
        dataset.record_score_observation(
            rec=None, source_game_id="C1", classification="CYBER_2K26",
            captured_at="T", home=1, away=1, quarter=1,
            displayed_clock=None)
    with pytest.raises(BetualDatasetError):
        dataset.record_line_observation(
            BetualGameRecord("C1"), source_game_id="C1",
            classification="CYBER_2K26", captured_at="T", market_id="M",
            market_name="Total", period="full_game", line=1.0,
            home=1, away=1, quarter=1, displayed_clock=None)
    assert dataset.game("C1", "CYBER_2K26") is None


# ── 9. parse failures (§9) + metrics (§18) ───────────────────────────

def test_parse_failure_raw_retained_error_recorded(store):
    store.record_betual_parse_failure("B-PF", '{"broken":', "bad json")
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM betual_parse_failures").fetchone())
    finally:
        con.close()
    assert r["raw_json"] == '{"broken":' and r["error"] == "bad json"


def test_collection_metrics_cover_directive_18(store, dataset):
    from blm_v4.models import PokerBetGame
    store.upsert_game(PokerBetGame(source_game_id="B-M1",
                                   classification="BETUAL_NBA",
                                   competition="Betual NBA",
                                   game_family="betual"))
    _transition(store, dataset, "B-M1", 3, 4)
    rec = rec_for(dataset, "B-M1")
    dataset.record_score_observation(
        rec, source_game_id="B-M1", classification="BETUAL_NBA",
        captured_at="T1", home=61, away=55, quarter=4,
        displayed_clock="11:30", q_scores=[(18, 14), (39, 33), (61, 55)])
    dataset.record_game_end(
        rec, source_game_id="B-M1", classification="BETUAL_NBA",
        captured_at="T2", home=80, away=70, quarter=4,
        end_evidence="observed_final", q_scores=[])
    store.record_betual_parse_failure("", "{}", "e")
    m = store.betual_collection_metrics()
    assert m["betual_games"] >= 1
    assert m["games_with_score_data"] >= 1
    assert m["games_with_q1"] >= 1
    assert m["q3_q4_transitions"] >= 1
    assert m["finalization_success"] >= 1
    assert m["parse_failures"] >= 1
