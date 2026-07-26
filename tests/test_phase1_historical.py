#!/usr/bin/env python3
"""Phase 1 verification: models, config, schema, database."""
import sys, os, json, tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # project root

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

# 1. Config
from blm_v3.historical.config import (
    DEFAULT_DB_PATH, DEFAULT_COLLECT_INTERVAL_MS,
    INFLATION_HIGH, MOMENTUM_ALPHA, ML_DEFAULT_FEATURES,
)
check(DEFAULT_DB_PATH.name == "blm_historical.db", "config: DB filename")
check(DEFAULT_COLLECT_INTERVAL_MS == 250, "config: collect interval")
check(INFLATION_HIGH == 6.0, "config: inflation high")
check(MOMENTUM_ALPHA == 0.3, "config: momentum alpha")
check(len(ML_DEFAULT_FEATURES) >= 20, f"config: {len(ML_DEFAULT_FEATURES)} ML features")

# 2. Schema
from blm_v3.historical.schema import FULL_DDL, get_table_names
tables = get_table_names()
check(len(tables) == 7, f"schema: {len(tables)} tables ({', '.join(tables)})")
check(len(FULL_DDL) > 5000, f"schema: DDL {len(FULL_DDL)} chars")
for t in tables:
    check(t in FULL_DDL, f"schema: table '{t}' in DDL")

# 3. Models
from blm_v3.historical.models import (
    HistoricalSnapshot, MarketSignal, MarketEvent, GameModel,
    SignalType, SignalSeverity, GameStatus,
)
snap = HistoricalSnapshot(
    game_id="test-g-001", quarter=2, clock="05:30",
    home_score=48, away_score=42, score_difference=6, total_score=90,
    total_line=187.5,
)
check(len(snap.id) == 32, f"model: snapshot UUID v7 length {len(snap.id)}")
check(snap.home_score == 48, "model: snapshot home_score")
check(snap.away_score == 42, "model: snapshot away_score")
check(snap.total_line == 187.5, "model: snapshot total_line")
check(str(snap.timestamp).endswith("Z"), f"model: snapshot timestamp ends with Z ({snap.timestamp[-20:]})")

sig = MarketSignal(
    game_id="test-g-001",
    signal_type=SignalType.LINE_JUMP,
    severity=SignalSeverity.HIGH,
    value=3.5, threshold=2.0,
    description="test signal",
)
check(sig.signal_type == SignalType.LINE_JUMP, "model: signal type")
check(sig.severity == SignalSeverity.HIGH, "model: signal severity")
dbd = sig.to_db_dict()
check(dbd["signal_type"] == "line_jump", "model: signal to_db_dict type")
check(dbd["severity"] == "high", "model: signal to_db_dict severity")
check(dbd["confirmed"] == 0, "model: signal to_db_dict confirmed is int")

game = GameModel(id="test-g-001", home_team="CyberDogs", away_team="RoboHawks")
check(game.home_team == "CyberDogs", "model: game home_team")
check(game.status == GameStatus.LIVE, "model: game default status")
dbd_g = game.to_db_dict()
check(dbd_g["status"] == "live", "model: game to_db_dict status")

evt = MarketEvent(
    game_id="test-g-001", event_type="trap_formation",
    magnitude=0.85, duration_seconds=30.0,
)
check(evt.event_type == "trap_formation", "model: market event type")
check(evt.magnitude == 0.85, "model: market event magnitude")

# 4. Database
from blm_v3.historical.database import HistoricalDatabase
import asyncio

async def test_db():
    db_file = Path(tempfile.mktemp(suffix=".db"))
    db = HistoricalDatabase(db_path=db_file)
    try:
        await db.init()
        info = db.get_table_info()
        for t in tables:
            check(info.get(t, False), f"db: table '{t}' exists")
        # Insert game
        await db.save_game(game.to_db_dict())
        games = await db.list_games()
        check(len(games) == 1, f"db: {len(games)} game after insert")
        # Insert snapshot
        snap_dict = snap.to_db_dict()
        snap_dict["raw_json"] = json.dumps(snap_dict)
        sid = await db.insert_snapshot(snap_dict)
        check(len(sid) == 32, f"db: snapshot insert returns ID ({len(sid)} chars)")
        # Query back
        snaps = await db.query_snapshots("test-g-001")
        check(len(snaps) == 1, f"db: {len(snaps)} snapshot queried")
        if snaps:
            check(snaps[0]["home_score"] == 48, "db: snapshot data integrity")
        # Count
        cnt = await db.count_snapshots("test-g-001")
        check(cnt == 1, f"db: snapshot count = {cnt}")
        # Insert signal
        sig_dict = sig.to_db_dict()
        sig_dict["snapshot_id"] = sid
        await db.insert_signal(sig_dict)
        sigs = await db.query_signals(game_id="test-g-001")
        check(len(sigs) == 1, f"db: signal query = {len(sigs)}")
        if sigs:
            check(sigs[0]["signal_type"] == "line_jump", "db: signal type integrity")
        # Health
        health = await db.get_health()
        check(health["status"] == "ok", "db: health OK")
        check(health["game_count"] >= 1, f"db: health games={health['game_count']}")
        check(health["snapshot_count"] >= 1, f"db: health snapshots={health['snapshot_count']}")
        check(health["signal_count"] >= 1, f"db: health signals={health['signal_count']}")
        print(f"\n  DB path: {db_file}")
        print(f"  Health: {health}")
    finally:
        if os.path.exists(db_file):
            os.unlink(db_file)

asyncio.run(test_db())

# Summary
total = passed + failed
print(f"\n{'='*50}")
print(f"PHASE 1 VERIFICATION: {passed}/{total} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
