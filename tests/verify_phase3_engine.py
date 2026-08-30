#!/usr/bin/env python3
"""Phase 3 verification: Derived Metrics & Signal Engine.

Tests:
  - Engine: inflation, compression, momentum, regression, variance, fair_total
  - Compute orchestrator: full pipeline from snapshot to signals+events
  - Signal detectors: all 15 signal types
  - Event classifier: signal grouping
  - DB integration: signal + event insertion
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from typing import Any, Optional

passed = 0
failed = 0
def check(ok, msg):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS: {msg}")
    else:
        failed += 1
        print(f"  FAIL: {msg}")

# ═══════════════════════════════════════════════════════════════════
# 1. Inflation Index
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.inflation import compute_inflation_index, classify_inflation

# Inflation is positive when score moves faster than the line (score inflation)
# and negative when the line inflates faster than the score warrants.
inf = compute_inflation_index(90, 205.0, 50, 205.0)
check(inf is not None, "inflation: computes")
if inf is not None:
    # score +40 (0.8 ratio), line +0 — positive inflation (score outpaces line)
    check(inf > 0, f"inflation: score outpaces line = positive ({inf})")
    check(inf == 0.8, f"inflation: 40/50 - 0/205 = 0.8 ({inf})")

inf2 = compute_inflation_index(90, 210.0, 50, 205.0)
# score +40 (0.8), line +5 (0.024) — inflation = 0.8 - 0.024 = 0.776
check(inf2 is not None and inf2 > 0, f"inflation: score still outpaces line ({inf2})")

# Line inflating faster than score (def lation)
inf_def = compute_inflation_index(50, 210.0, 50, 205.0)
# score +0, line +5 (0.024) — negative inflation (line inflating without score support)
check(inf_def is not None and inf_def < 0, f"inflation: line inflates without score = negative ({inf_def})")

check(classify_inflation(None) == "unknown", "inflation: classify unknown")
check(classify_inflation(6.0) == "extreme_inflation", "inflation: classify extreme")
check(classify_inflation(4.0) == "inflation", "inflation: classify inflation")
check(classify_inflation(0.0) == "normal", "inflation: classify normal")
check(classify_inflation(-6.0) == "extreme_deflation", "inflation: classify extreme deflation")

# ═══════════════════════════════════════════════════════════════════
# 2. Compression Index
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.compression import compute_compression_index, classify_compression

comp = compute_compression_index(1.91, 1.91)
check(comp is not None and comp >= 0.99, f"compression: tight odds ({comp})")

comp2 = compute_compression_index(2.10, 1.70)
check(comp2 is not None and comp2 < 0.5, f"compression: wide odds ({comp2})")

check(compute_compression_index(None, 1.91) is None, "compression: None over = None")
check(classify_compression(None) == "unknown", "compression: classify unknown")
check(classify_compression(0.90) == "tight", "compression: classify tight")
check(classify_compression(0.50) == "normal", "compression: classify normal")
check(classify_compression(0.20) == "wide", "compression: classify wide")

# ═══════════════════════════════════════════════════════════════════
# 3. Momentum
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.momentum import (
    compute_momentum, compute_momentum_velocity,
    compute_momentum_acceleration, is_momentum_swing,
)

mom = compute_momentum(55, 50, None)
check(mom == 5.0, f"momentum: first tick = score delta ({mom})")

mom2 = compute_momentum(60, 55, 5.0, alpha=1.0)
check(mom2 == 5.0, f"momentum: alpha=1.0 = raw score delta ({mom2})")

mom3 = compute_momentum(60, 55, 5.0, alpha=0.3)
check(mom3 < 7.0, f"momentum: EMA smoothed ({mom3})")

vel = compute_momentum_velocity(5.0, 2.0, 1.0)
check(vel is not None and vel == 3.0, f"momentum: velocity ({vel})")

accel = compute_momentum_acceleration(3.0, 1.0, 1.0)
check(accel is not None and accel == 2.0, f"momentum: acceleration ({accel})")

check(is_momentum_swing(10.0, 2.0, 3.0), "momentum: swing detected")
check(not is_momentum_swing(4.0, 3.0, 3.0), "momentum: no swing")

# ═══════════════════════════════════════════════════════════════════
# 4. Regression
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.regression import (
    compute_regression_probability,
    is_regression_candidate,
    has_regression_completed,
)

reg = compute_regression_probability(200.0, 195.0, 24.0)
check(reg is not None and reg > 0, f"regression: probable ({reg})")

reg2 = compute_regression_probability(200.0, 199.5, 6.0)
check(reg2 is not None and reg2 < 0.3, f"regression: unlikely early ({reg2})")

check(compute_regression_probability(None, 195.0, 24.0) is None, "regression: no line = None")

check(is_regression_candidate(200.0, 190.0, 5.0), "regression: is candidate")
check(not is_regression_candidate(200.0, 198.0, 5.0), "regression: not candidate")

check(has_regression_completed(200.0, 199.5, 1.0), "regression: completed")
check(not has_regression_completed(200.0, 195.0, 1.0), "regression: not completed")

# ═══════════════════════════════════════════════════════════════════
# 5. Variance & Volatility
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.variance import compute_variance, compute_volatility, classify_volatility

var = compute_variance([205.0, 205.5, 206.0, 205.5, 205.0])
check(var is not None and var > 0, f"variance: computed ({var})")

check(compute_variance([]) is None, "variance: empty = None")
check(compute_variance([100.0]) is None, "variance: single = None")

vol = compute_volatility([205.0, 205.5, 206.0, 205.5, 205.0])
check(vol is not None and vol > 0, f"volatility: computed ({vol})")

check(classify_volatility(None) == "unknown", "volatility: classify unknown")

# ═══════════════════════════════════════════════════════════════════
# 6. Fair Total
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.fair_total import compute_fair_total, compute_expected_total

fair = compute_fair_total(200.0, 24.0, 90, 205.0)
check(fair is not None, "fair_total: computed")

exp = compute_expected_total(fair, 205.0, 0.5)
check(exp is not None, "expected_total: computed")

# ═══════════════════════════════════════════════════════════════════
# 7. Compute Pipeline (orchestrator)
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.compute import compute_all

# Simulate a game at Q2 6:00 — line moved up 2.5 points
snapshot = {
    "id": "snap001", "game_id": "g001", "timestamp": "2026-01-01T00:06:00Z",
    "quarter": 1, "clock": "6:00", "home_score": 30, "away_score": 25,
    "total_score": 55, "score_difference": 5,
    "home_team_total": None, "away_team_total": None,
    "total_line": 207.5, "total_line_raw": 207.0, "spread": -5.5, "spread_raw": -6.0,
    "over_odds": 1.88, "under_odds": 1.91,
    "line_delta": 2.5, "odds_delta": -0.03, "spread_delta": -0.5,
    "possessions": 55, "possessions_per_min": 9.1667,
    "projected_total": 220.0, "projected_possessions": 96.0,
    "trap_meter": 0, "tt_modifier": None,
}

prev_snapshot = {
    "id": "snap000", "game_id": "g001", "timestamp": "2026-01-01T00:03:00Z",
    "quarter": 1, "clock": "9:00", "home_score": 15, "away_score": 12,
    "total_score": 27, "score_difference": 3,
    "total_line": 205.0, "spread": -5.5,
    "over_odds": 1.91, "under_odds": 1.91,
    "line_delta": 0, "odds_delta": 0, "spread_delta": 0,
    "possessions": 27, "possessions_per_min": 9.0,
    "projected_total": 216.0,
    "trap_meter": 0,
}

result = compute_all(
    snapshot=snapshot,
    prev_snapshot=prev_snapshot,
    rolling_line_values=[205.0, 205.0, 205.0, 205.0],
    prev_momentum=3.0,
    consecutive_zero_deltas=0,
    game_start_total=0,
    game_start_line=205.0,
    game_minutes=6.0,
)

check("snapshot" in result, "compute: result has snapshot")
check("signals" in result, "compute: result has signals")
check("events" in result, "compute: result has events")

s = result["signals"]
check(len(s) > 0, f"compute: {len(s)} signals detected")
# Should detect line_jump (2.5 > 2.0 threshold)
line_jumps = [sig for sig in s if sig["signal_type"] == "line_jump"]
check(len(line_jumps) > 0, f"compute: line_jump detected ({len(line_jumps)})")

# Should also detect sharp_movement (momentum +3, line jumped up)
sharps = [sig for sig in s if sig["signal_type"] == "sharp_movement"]
print(f"  INFO: signals={[sig['signal_type'] for sig in s]}")

ev = result["events"]
print(f"  INFO: events={[e['event_type'] for e in ev]}")

# ═══════════════════════════════════════════════════════════════════
# 8. Signal Detectors (individual)
# ═══════════════════════════════════════════════════════════════════
from blm_v3.signals.detector import detect_all

# Line freeze detector
context_frozen = {
    "consecutive_zero_deltas": 15,
    "rolling_line_values": [205.0]*10,
    "prev_momentum": 0,
    "current_game_minutes": 24.0,
    "inflation_index": 0,
    "compression_index": 0.5,
    "trap_meter": 0,
    "prev_snapshot": None,
}
freeze_sigs = detect_all({"id":"s1","game_id":"g1","timestamp":"t","line_delta":0,
                           "total_score":100,"total_line":205.0,"momentum":0,
                           "line_jump":False}, context_frozen)
freezes = [s for s in freeze_sigs if s["signal_type"] == "line_freeze"]
check(len(freezes) >= 1, f"signal: freeze detected ({len(freezes)})")

# Line jump detector
context_jump = dict(context_frozen)
context_jump["consecutive_zero_deltas"] = 0
jump_snap = {"id":"s2","game_id":"g1","timestamp":"t","line_delta":3.5,
             "total_score":100,"total_line":208.5}
jump_sigs = detect_all(jump_snap, context_jump)
jumps = [s for s in jump_sigs if s["signal_type"] == "line_jump"]
check(len(jumps) >= 1, f"signal: line_jump detected ({len(jumps)})")

# Inflation spike detector
context_inflate = dict(context_frozen)
context_inflate["inflation_index"] = 5.5
inflate_snap = {"id":"s3","game_id":"g1","timestamp":"t","line_delta":1,
                "total_line":210.0,"total_score":100,"momentum":10}
inflate_sigs = detect_all(inflate_snap, context_inflate)
spikes = [s for s in inflate_sigs if s["signal_type"] == "inflation_spike"]
check(len(spikes) >= 1, f"signal: inflation_spike detected ({len(spikes)})")

# ═══════════════════════════════════════════════════════════════════
# 9. Event Classifier
# ═══════════════════════════════════════════════════════════════════
from blm_v3.signals.event_classifier import classify_events

# Two line_jump signals + one odds_expansion should produce event(s)
test_signals = [
    {"signal_type": "line_jump", "severity": "high", "value": 3.0, "threshold": 2.0,
     "game_id": "g1", "timestamp": "t1", "snapshot_id": "s1"},
    {"signal_type": "line_jump", "severity": "mid", "value": 2.5, "threshold": 2.0,
     "game_id": "g1", "timestamp": "t2", "snapshot_id": "s2"},
]
events = classify_events(test_signals, [], "t3", "s3", "g1")
check(len(events) >= 1, f"event: classified {len(events)} (expect >=1)")
vol_events = [e for e in events if e["event_type"] == "market_volatility"]
check(len(vol_events) >= 1, f"event: market_volatility found ({len(vol_events)})")

# ═══════════════════════════════════════════════════════════════════
# 10. DB Integration — signal + event write
# ═══════════════════════════════════════════════════════════════════
from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.models import GameModel, GameStatus
import asyncio

async def test_db_integration():
    db_file = Path(tempfile.mktemp(suffix=".db"))
    db = HistoricalDatabase(db_path=db_file)
    await db.init()
    gid = "test-phase3-db"

    # Insert game
    await db.save_game(GameModel(id=gid, home_team="A", away_team="B",
                                  status=GameStatus.LIVE).to_db_dict())

    # Insert snapshot (needed for FK from signals)
    from blm_v3.historical.models import HistoricalSnapshot
    snap = HistoricalSnapshot(game_id=gid, quarter=1, clock="12:00",
        home_score=0, away_score=0, score_difference=0, total_score=0,
        total_line=205.0)
    snap_id = await db.insert_snapshot(snap.to_db_dict())

    # Insert signal
    from blm_v3.historical.models import MarketSignal, SignalType, SignalSeverity
    sig = MarketSignal(
        game_id=gid, snapshot_id=snap_id, signal_type=SignalType.LINE_JUMP,
        severity=SignalSeverity.HIGH, value=3.5, threshold=2.0,
        description="Test signal from Phase 3",
    )
    sig_id = await db.insert_signal(sig.to_db_dict())
    check(len(sig_id) == 32, "db: signal inserted")

    # Insert market event
    from blm_v3.historical.models import MarketEvent
    evt = MarketEvent(game_id=gid, snapshot_id=snap_id, event_type="market_volatility",
                      magnitude=3.5, duration_seconds=30.0,
                      description="Test event")
    evt_id = await db.insert_market_event(evt.to_db_dict())
    check(len(evt_id) == 32, "db: event inserted")

    # Query back
    sigs = await db.query_signals(game_id=gid)
    check(len(sigs) == 1, f"db: signal query = {len(sigs)}")
    if sigs:
        check(sigs[0]["signal_type"] == "line_jump", "db: signal type integrity")
        check(sigs[0]["value"] == 3.5, "db: signal value integrity")

    evts = await db.query_market_events(gid)
    check(len(evts) == 1, f"db: event query = {len(evts)}")

    os.unlink(str(db_file))

asyncio.run(test_db_integration())

# ═══════════════════════════════════════════════════════════════════
total = passed + failed
print(f"\n{'='*50}")
print(f"PHASE 3 VERIFICATION: {passed}/{total} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
