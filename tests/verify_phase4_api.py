#!/usr/bin/env python3
"""Phase 4 verification: Historical Research API.

Tests:
  - Router creation
  - All endpoint response shapes (via FastAPI TestClient)
  - Export routes (CSV, JSON, ML)
  - Error handling (404, 400)
  - DB-backed integration
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from typing import Any, Optional

# FastAPI test dependencies
from fastapi.testclient import TestClient
from fastapi import FastAPI

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
# Setup: create a real HistoricalDatabase with test data
# ═══════════════════════════════════════════════════════════════════
from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.models import (
    GameModel, GameStatus, HistoricalSnapshot,
    MarketSignal, SignalType, SignalSeverity, MarketEvent,
)
from blm_v3.api.historical_routes import create_historical_router
from blm_v3.api.export_routes import create_export_router
import asyncio, json

db_file = Path(tempfile.mktemp(suffix=".db"))
db = HistoricalDatabase(db_path=db_file)
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
try:
    loop.run_until_complete(db.init())

    # Insert test games
    gid1 = "test-game-001"
    gid2 = "test-game-002"
    loop.run_until_complete(db.save_game(GameModel(
        id=gid1, home_team="CyberDogs", away_team="RoboHawks",
        status=GameStatus.ENDED, final_home=95, final_away=88,
        final_total=183, final_margin=7, total_snapshots=50,
    ).to_db_dict()))
    loop.run_until_complete(db.save_game(GameModel(
        id=gid2, home_team="MetalCats", away_team="PixelBears",
        status=GameStatus.ENDED, final_home=102, final_away=96,
        final_total=198, final_margin=6, total_snapshots=60,
    ).to_db_dict()))

    # Insert test snapshots
    for gid, scores in [(gid1, [(0,0),(15,12),(30,25),(48,42),(65,58),(80,72),(95,88)]),
                         (gid2, [(0,0),(18,14),(36,28),(54,42),(72,56),(90,70),(102,96)])]:
        for i, (h, a) in enumerate(scores):
            snap = HistoricalSnapshot(
                game_id=gid, quarter=(i//2)+1, clock=f"{12-i*2}:00",
                home_score=h, away_score=a, score_difference=h-a,
                total_score=h+a, total_line=200.0 + i*0.5,
                trap_meter=min(i*12, 72), confidence=max(0.9-i*0.1, 0.3),
                inflation_index=i*0.8, momentum=i*2.0,
                projected_total=195.0 + i,
                possessions=h+a, possessions_per_min=(h+a)/max((i*2)+1, 1),
            )
            loop.run_until_complete(db.insert_snapshot(snap.to_db_dict()))

    # Insert test signals
    for gid in [gid1, gid2]:
        sig = MarketSignal(
            game_id=gid, signal_type=SignalType.LINE_JUMP,
            severity=SignalSeverity.HIGH, value=3.5, threshold=2.0,
            description=f"Test jump in {gid}",
        )
        loop.run_until_complete(db.insert_signal(sig.to_db_dict()))

    # Insert test event
    evt = MarketEvent(
        game_id=gid1, event_type="market_volatility",
        magnitude=3.5, duration_seconds=30.0,
        description="Volatility event",
    )
    loop.run_until_complete(db.insert_market_event(evt.to_db_dict()))

    # Create router + app
    router = create_historical_router(db=db)
    export_router = create_export_router(db=db)

    app = FastAPI()
    app.include_router(router, prefix="/api/v2/historical")
    app.include_router(export_router, prefix="/api/v2/historical/export")

    # Override startup (already initialized)
    @app.on_event("startup")
    async def _noop():
        pass

    client = TestClient(app)

except Exception as e:
    print(f"SETUP FAILED: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════
# 1. Health
# ═══════════════════════════════════════════════════════════════════
r = client.get("/api/v2/historical/health")
check(r.status_code == 200, f"health: status {r.status_code}")
data = r.json()
check(data["status"] == "ok", f"health: status=ok ({data['status']})")
check(data["game_count"] >= 2, f"health: games={data['game_count']}")
check(data["snapshot_count"] >= 12, f"health: snapshots={data['snapshot_count']}")
check(data["version"] == "3.0.0", f"health: version={data['version']}")

# ═══════════════════════════════════════════════════════════════════
# 2. Games List
# ═══════════════════════════════════════════════════════════════════
r = client.get("/api/v2/historical/games")
check(r.status_code == 200, f"games list: status {r.status_code}")
data = r.json()
check(data["total"] >= 2, f"games list: total={data['total']}")
check(len(data["games"]) >= 2, f"games list: {len(data['games'])} items")
if data["games"]:
    g = data["games"][0]
    check("game_id" in g, "games list: has game_id")
    check("home_team" in g, "games list: has home_team")
    check("total_snapshots" in g, "games list: has total_snapshots")

# Games with filter
r = client.get("/api/v2/historical/games?status=ended")
check(r.status_code == 200, "games list: filter=ended")

# ═══════════════════════════════════════════════════════════════════
# 3. Game Detail
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/games/{gid1}")
check(r.status_code == 200, f"game detail: status {r.status_code}")
data = r.json()
check("game" in data, "game detail: has game")
check("snapshot_count" in data, "game detail: has snapshot_count")
check("signal_count" in data, "game detail: has signal_count")

# 404
r = client.get("/api/v2/historical/games/nonexistent")
check(r.status_code == 404, "game detail: 404 for missing")

# ═══════════════════════════════════════════════════════════════════
# 4. Snapshots
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/snapshots/{gid1}")
check(r.status_code == 200, f"snapshots: status {r.status_code}")
data = r.json()
check(data["game_id"] == gid1, f"snapshots: game_id={data['game_id']}")
check(data["total"] >= 5, f"snapshots: total={data['total']}")
check(len(data["snapshots"]) >= 5, f"snapshots: {len(data['snapshots'])} items")
if data["snapshots"]:
    s = data["snapshots"][0]
    check("home_score" in s, "snapshots: has home_score")
    check("total_line" in s, "snapshots: has total_line")
    check("trap_meter" in s, "snapshots: has trap_meter")

# With limit
r = client.get(f"/api/v2/historical/snapshots/{gid1}?limit=2")
check(r.status_code == 200, "snapshots: with limit")
check(len(r.json()["snapshots"]) <= 2, "snapshots: limit applied")

# ═══════════════════════════════════════════════════════════════════
# 5. Aggregated
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/snapshots/{gid1}/aggregated?interval=30")
check(r.status_code == 200, f"aggregated: status {r.status_code}")
data = r.json()
check(data["game_id"] == gid1, "aggregated: game_id")
check("intervals" in data, "aggregated: has intervals")

# ═══════════════════════════════════════════════════════════════════
# 6. Metrics
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/metrics/{gid1}?metrics=trap_meter,confidence,total_line")
check(r.status_code == 200, f"metrics: status {r.status_code}")
data = r.json()
check(data["game_id"] == gid1, "metrics: game_id")
check("trap_meter" in data["series"], "metrics: has trap_meter series")
check("confidence" in data["series"], "metrics: has confidence series")

# ═══════════════════════════════════════════════════════════════════
# 7. Signals
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/signals?game_id={gid1}")
check(r.status_code == 200, f"signals: status {r.status_code}")
data = r.json()
check(data["total"] >= 1, f"signals: total={data['total']}")
if data["signals"]:
    s = data["signals"][0]
    check(s["signal_type"] == "line_jump", f"signals: type={s['signal_type']}")
    check(s["severity"] == "high", f"signals: severity={s['severity']}")

# ═══════════════════════════════════════════════════════════════════
# 8. Events
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/events/{gid1}")
check(r.status_code == 200, f"events: status {r.status_code}")
data = r.json()
check(data["total"] >= 1, f"events: total={data['total']}")
if data["events"]:
    e = data["events"][0]
    check(e["event_type"] == "market_volatility", f"events: type={e['event_type']}")

# ═══════════════════════════════════════════════════════════════════
# 9. Compare
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/compare?game_ids={gid1},{gid2}&metrics=trap_meter,confidence")
check(r.status_code == 200, f"compare: status {r.status_code}")
data = r.json()
check(len(data["game_ids"]) == 2, f"compare: {len(data['game_ids'])} games")
check(gid1 in data["series"], "compare: game1 in series")
check(gid2 in data["series"], "compare: game2 in series")

# ═══════════════════════════════════════════════════════════════════
# 10. Compare by Filters (POST)
# ═══════════════════════════════════════════════════════════════════
r = client.post(
    "/api/v2/historical/compare/query",
    json={"trap_min": 0, "result": "under"},
)
check(r.status_code == 200, f"compare query: status {r.status_code}")
data = r.json()
check("matched_games" in data, "compare query: has matched_games")
check("count" in data, "compare query: has count")

# ═══════════════════════════════════════════════════════════════════
# 11. CSV Export
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/export/csv?game_ids={gid1}")
check(r.status_code == 200, f"csv export: status {r.status_code}")
check("text/csv" in r.headers["content-type"], f"csv export: content-type={r.headers['content-type']}")
check('Content-Disposition' in r.headers, "csv export: has Content-Disposition")
csv_text = r.text
check(len(csv_text) > 100, f"csv export: {len(csv_text)} chars")
check("home_score" in csv_text, "csv export: has headers")

# ═══════════════════════════════════════════════════════════════════
# 12. JSON Export
# ═══════════════════════════════════════════════════════════════════
r = client.get(f"/api/v2/historical/export/json?game_ids={gid1}")
check(r.status_code == 200, f"json export: status {r.status_code}")
check(r.headers["content-type"] == "application/json", "json export: content-type")
json_data = r.json()
check(len(json_data) >= 5, f"json export: {len(json_data)} rows")
if json_data:
    check("home_score" in json_data[0], "json export: has home_score")

# ═══════════════════════════════════════════════════════════════════
# 13. ML Export
# ═══════════════════════════════════════════════════════════════════
r = client.get(
    f"/api/v2/historical/export/ml?game_ids={gid1}"
    "&features=total_line,trap_meter,confidence,possessions_per_min"
    "&label=total_score"
)
check(r.status_code == 200, f"ml export: status {r.status_code}")
ml_text = r.text
check("total_line" in ml_text, "ml export: has total_line feature")
check("trap_meter" in ml_text, "ml export: has trap_meter feature")
check("total_score" in ml_text, "ml export: has label")

# ═══════════════════════════════════════════════════════════════════
# 14. Error Handling
# ═══════════════════════════════════════════════════════════════════
r = client.get("/api/v2/historical/export/csv?game_ids=")
check(r.status_code == 400, "csv export: 400 for empty game_ids")

r = client.get("/api/v2/historical/export/json?game_ids=nonexistent")
check(r.status_code == 404, "json export: 404 for nonexistent game")

# ═══════════════════════════════════════════════════════════════════
total = passed + failed
print(f"\n{'='*50}")
print(f"PHASE 4 VERIFICATION: {passed}/{total} passed, {failed} failed")
print(f"{'='*50}")

# Cleanup
db_file.unlink(missing_ok=True)
loop.close()

sys.exit(0 if failed == 0 else 1)
