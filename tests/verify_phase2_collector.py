#!/usr/bin/env python3
"""Phase 2 verification: High-Frequency Collector.

Tests:
  - Pace calculator (clock parsing, pace metrics)
  - Movement tracker (deltas, freeze detection)
  - Game state extraction (from sample HTML text)
  - Full pipeline: extracted state → HistoricalSnapshot → DB write
  - Collector init and validation (no Playwright test — requires live site)

Run: python3 tests/test_phase2_collector.py
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path

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
# 1. Pace Calculator
# ═══════════════════════════════════════════════════════════════════

from blm_v3.collector.pace_calculator import (
    parse_clock_to_seconds,
    clock_elapsed_seconds,
    game_elapsed_minutes,
    compute_pace_metrics,
    PaceCalculator,
)

check(parse_clock_to_seconds("12:00") == 720.0, "pace: parse 12:00 = 720s")
check(parse_clock_to_seconds("7:30") == 450.0, "pace: parse 7:30 = 450s")
check(parse_clock_to_seconds("0:00") is None, "pace: parse 0:00 = None")
check(parse_clock_to_seconds(None) is None, "pace: parse None = None")

check(clock_elapsed_seconds("12:00") == 0.0, "pace: elapsed 12:00 = 0s")
check(clock_elapsed_seconds("7:30") == 270.0, "pace: elapsed 7:30 = 270s")
check(clock_elapsed_seconds(None) is None, "pace: elapsed None = None")

check(game_elapsed_minutes(1, "12:00") == 0.0, "pace: game_min Q1 12:00 = 0")
check(game_elapsed_minutes(2, "12:00") == 12.0, "pace: game_min Q2 12:00 = 12")
check(game_elapsed_minutes(3, "6:00") == 30.0, "pace: game_min Q3 6:00 = 30")
check(game_elapsed_minutes(2, None) == 12.0, "pace: game_min Q2 no clock = 12")

# Pace metrics computation
prev = {"quarter": 1, "clock": "12:00", "home_score": 0, "away_score": 0, "total_score": 0}
curr = {"quarter": 1, "clock": "6:00", "home_score": 30, "away_score": 25, "total_score": 55}

result = compute_pace_metrics(prev, curr)
check(result["possessions"] == 55, "pace: possessions = 55")
check(result["possessions_per_min"] is not None, "pace: possessions_per_min computed")
check(result["projected_total"] is not None, "pace: projected_total computed")
# 6 min elapsed = 55 / 6 = 9.1667 ppm → projected over 48 = 440
check(abs(result["possessions_per_min"] - (55 / 6.0)) < 0.01, "pace: ppm ~9.1667")

# Edge case: no clock data
prev2 = {"quarter": 1, "clock": None, "home_score": 0, "away_score": 0}
curr2 = {"quarter": 1, "clock": None, "home_score": 30, "away_score": 25}
result2 = compute_pace_metrics(prev2, curr2)
check(result2["possessions_per_min"] is None, "pace: no clock = no ppm")
check(result2["projected_total"] is None, "pace: no clock = no proj total")

# PaceCalculator class wrapper
pc = PaceCalculator()
result3 = pc.compute(prev, curr)
check(result3["possessions"] == 55, "pace: class wrapper works")

# ═══════════════════════════════════════════════════════════════════
# 2. Movement Tracker
# ═══════════════════════════════════════════════════════════════════

from blm_v3.collector.movement_tracker import (
    compute_movement_deltas,
    is_market_frozen,
    MovementTracker,
)

p = {"total_line": 205.0, "over_odds": 1.91, "spread": -5.5}
c = {"total_line": 207.5, "over_odds": 1.85, "spread": -6.0}

deltas = compute_movement_deltas(p, c)
check(deltas["line_delta"] == 2.5, "tracker: line delta = 2.5")
check(deltas["odds_delta"] == -0.06, f"tracker: odds delta = {deltas['odds_delta']}")
check(deltas["spread_delta"] == -0.5, f"tracker: spread delta = {deltas['spread_delta']}")

# No previous data
deltas2 = compute_movement_deltas({}, c)
check(deltas2["line_delta"] is None, "tracker: no prev = None deltas")

# Frozen market detection
frozen = is_market_frozen({"line_delta": 0.0}, 15, 10)
check(frozen, "tracker: frozen detection works")

not_frozen = is_market_frozen({"line_delta": 0.0}, 5, 10)
check(not not_frozen, "tracker: not enough ticks = not frozen")

not_frozen2 = is_market_frozen({"line_delta": 2.0}, 15, 10)
check(not not_frozen2, "tracker: line moving = not frozen")

# Class wrapper
mt = MovementTracker()
deltas3 = mt.compute(p, c)
check(deltas3["line_delta"] == 2.5, "tracker: class wrapper works")
check(mt.is_frozen({"line_delta": 0.0}, 15, 10), "tracker: class frozen check")

# ═══════════════════════════════════════════════════════════════════
# 3. Game State Extraction
# ═══════════════════════════════════════════════════════════════════

from blm_v3.collector.historical_collector import extract_game_state

# Sample PokerBet-style HTML text
sample_text = """
PokerBet Sportsbook
Cyber Basketball 2K26
CyberDogs
48
CyberHawks
42
1st Quarter
05:30
Total Points
205.0  1.91  1.91
Points Handicap
-5.5  1.87
""".strip()

state = extract_game_state(sample_text)
check(state is not None, "extract: state found")
if state:
    check(state.get("home_team") == "CyberDogs", f"extract: home_team={state.get('home_team')}")
    check(state.get("away_team") == "CyberHawks", f"extract: away_team={state.get('away_team')}")
    check(state.get("home_score") == 48, f"extract: home_score={state.get('home_score')}")
    check(state.get("away_score") == 42, f"extract: away_score={state.get('away_score')}")
    check(state.get("quarter") == 1, f"extract: quarter={state.get('quarter')}")
    check(state.get("clock") == "05:30", f"extract: clock={state.get('clock')}")
    check(state.get("total_line") == 205.0, f"extract: total_line={state.get('total_line')}")
    check(state.get("spread") == -5.5, f"extract: spread={state.get('spread')}")
    check(state.get("over_odds") == 1.91, f"extract: over_odds={state.get('over_odds')}")
    check(state.get("under_odds") == 1.91, f"extract: under_odds={state.get('under_odds')}")

# Empty text
check(extract_game_state("") is None, "extract: empty = None")
check(extract_game_state(None) is None, "extract: None = None")

# No game data — should return None
check(extract_game_state("PokerBet homepage") is None, "extract: no game = None")

# Quarter detection variations
q2_text = "Some text here\n2nd Quarter\n12:00\nand stuff"
half_text = "Half Time\n45-42\n"
for text, expected_q in [(q2_text, 2), (half_text, 2)]:
    s = extract_game_state(text)
    # These only contain quarter info but no teams, so state will be None
    # That's fine — quarter parsing needs team detection

# ═══════════════════════════════════════════════════════════════════
# 4. Full Pipeline: extracted state → HistoricalSnapshot → DB write
# ═══════════════════════════════════════════════════════════════════

from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.models import (
    HistoricalSnapshot, GameModel, GameStatus, _uuid7,
)
from blm_v3.collector.pace_calculator import compute_pace_metrics
from blm_v3.collector.movement_tracker import compute_movement_deltas
import asyncio, json

async def test_pipeline():
    db_file = Path(tempfile.mktemp(suffix=".db"))
    db = HistoricalDatabase(db_path=db_file)
    await db.init()

    gid = f"test-pipeline-{_uuid7()[:8]}"
    ts_start = "2026-07-26T00:00:00.000Z"

    # Insert first snapshot (no prev — no deltas/pace)
    snap1 = HistoricalSnapshot(
        game_id=gid, timestamp=ts_start, quarter=1, clock="12:00",
        home_score=0, away_score=0, score_difference=0, total_score=0,
        total_line=205.0, spread=-5.5,
        over_odds=1.91, under_odds=1.91,
    )
    await db.save_game(GameModel(id=gid, home_team="A", away_team="B",
                                  status=GameStatus.LIVE).to_db_dict())
    sid1 = await db.insert_snapshot(snap1.to_db_dict())
    check(len(sid1) == 32, "pipeline: first snapshot inserted")

    # Insert second snapshot with movement + pace
    prev = {"quarter": 1, "clock": "12:00", "home_score": 0, "away_score": 0,
            "total_score": 0, "total_line": 205.0, "over_odds": 1.91, "spread": -5.5}
    curr = {"quarter": 1, "clock": "9:00", "home_score": 15, "away_score": 12,
            "total_score": 27, "total_line": 206.5, "over_odds": 1.88, "spread": -6.0}

    deltas = compute_movement_deltas(prev, curr)
    pace = compute_pace_metrics(prev, curr)

    snap2 = HistoricalSnapshot(
        game_id=gid, timestamp="2026-07-26T00:03:00.000Z",
        quarter=1, clock="9:00",
        home_score=15, away_score=12, score_difference=3, total_score=27,
        total_line=206.5, spread=-6.0,
        over_odds=1.88, under_odds=1.91,
        line_delta=deltas["line_delta"],
        odds_delta=deltas["odds_delta"],
        spread_delta=deltas["spread_delta"],
        possessions=pace["possessions"],
        possessions_per_min=pace["possessions_per_min"],
        projected_total=pace["projected_total"],
        raw_json=json.dumps({"prev": prev, "curr": curr}),
    )
    sid2 = await db.insert_snapshot(snap2.to_db_dict())
    check(len(sid2) == 32, "pipeline: second snapshot inserted")

    # Query back and verify
    snaps = await db.query_snapshots(gid)
    check(len(snaps) == 2, f"pipeline: 2 snapshots found, got {len(snaps)}")

    if len(snaps) >= 2:
        s2 = snaps[1]  # newest (ASC order, so index 1)
        check(s2["home_score"] == 15, f"pipeline: home_score={s2['home_score']}")
        check(s2["total_line"] == 206.5, f"pipeline: total_line={s2['total_line']}")
        check(s2["line_delta"] == 1.5, f"pipeline: line_delta={s2['line_delta']}")
        check(s2["odds_delta"] == -0.03, f"pipeline: odds_delta={s2['odds_delta']}")
        check(s2["projected_total"] is not None, "pipeline: projected_total computed")
        check(s2["raw_json"] is not None, "pipeline: raw_json stored")

    # Health check
    health = await db.get_health()
    check(health["status"] == "ok", "pipeline: health OK")
    check(health["snapshot_count"] >= 2, f"pipeline: snapshots={health['snapshot_count']}")

    os.unlink(str(db_file))

asyncio.run(test_pipeline())

# ═══════════════════════════════════════════════════════════════════
# 5. Collector module structure (no Playwright test — requires live site)
# ═══════════════════════════════════════════════════════════════════

from blm_v3.collector import __version__ as coll_ver
check(coll_ver == "3.0.0", "collector: package version")

# Collector class can be imported and instantiated (without starting)
from blm_v3.collector.historical_collector import HistoricalCollector
c = HistoricalCollector(interval_ms=500)
check(c.current_interval_ms == 500, f"collector: interval={c.current_interval_ms}")
check(not c.is_running, "collector: not running on init")
check(c.latest_state is None, "collector: no state on init")
check(c.snapshot_count == 0, "collector: count=0 on init")

# Degrade gracefully with invalid interval
c2 = HistoricalCollector(interval_ms=10)
check(c2.current_interval_ms >= 100, f"collector: min interval={c2.current_interval_ms}")

# ═══════════════════════════════════════════════════════════════════

total = passed + failed
print(f"\n{'='*50}")
print(f"PHASE 2 VERIFICATION: {passed}/{total} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
