#!/usr/bin/env python3
"""Phase 6 verification: ML Pipeline & Final Integration.

Tests:
  - MlPipeline.compute_labels() for all 4 label types
  - MlPipeline.build_dataset() for flat training rows
  - ML export endpoint with advanced labels
  - ADR exists and is well-formed
  - All 6 phases verify (cross-phase integration)
  - git log shows all phases committed
"""
import sys, os, json, tempfile, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from typing import Any, Optional

passed = 0; failed = 0
def check(ok, msg):
    global passed, failed
    if ok: passed += 1; print(f"  PASS: {msg}")
    else: failed += 1; print(f"  FAIL: {msg}")

# ═══════════════════════════════════════════════════════════════════
# 1. MlPipeline — label computation
# ═══════════════════════════════════════════════════════════════════
from blm_v3.engine.ml_pipeline import (
    MlPipeline, compute_final_result_label, compute_over_under_label,
    compute_clv_label, compute_trap_success_label,
    _get_opening_line, _get_closing_line, _get_final_total,
)

# Label computation (from snapshot sequences)
# Game where opening line=200, final total=195 → under
snaps_under = [
    {"total_line": 200.0, "total_score": 0, "home_score": 0, "away_score": 0, "trap_meter": 0, "inflation_index": 0},
    {"total_line": 199.5, "total_score": 48, "home_score": 25, "away_score": 23, "trap_meter": 20, "inflation_index": 1.0},
    {"total_line": 198.5, "total_score": 95, "home_score": 50, "away_score": 45, "trap_meter": 40, "inflation_index": 2.0},
    {"total_line": 197.0, "total_score": 140, "home_score": 72, "away_score": 68, "trap_meter": 60, "inflation_index": 3.0},
    {"total_line": 196.0, "total_score": 195, "home_score": 100, "away_score": 95, "trap_meter": 85, "inflation_index": -1.0},
]

check(compute_final_result_label(snaps_under) == 0, "ml: final_result=0 for under")
check(compute_over_under_label(snaps_under) == "under", "ml: over_under=under")
clv = compute_clv_label(snaps_under)
check(clv is not None and clv == 4.0, f"ml: CLV={clv} (expected 200-196=4)")

# Game with bear trap (meter>80, inflation negative early → bear trap, OVER wins)
snaps_bear_trap = [
    {"total_line": 200.0, "total_score": 0, "home_score": 0, "away_score": 0, "trap_meter": 0, "inflation_index": 0},
    {"total_line": 198.0, "total_score": 55, "home_score": 28, "away_score": 27, "trap_meter": 30, "inflation_index": -2.5},
    {"total_line": 196.0, "total_score": 105, "home_score": 53, "away_score": 52, "trap_meter": 50, "inflation_index": -3.0},
    {"total_line": 195.0, "total_score": 160, "home_score": 82, "away_score": 78, "trap_meter": 70, "inflation_index": -3.5},
    {"total_line": 194.0, "total_score": 210, "home_score": 108, "away_score": 102, "trap_meter": 85, "inflation_index": -2.0},
]
# Line deflated from 200 to 194, final total 210 > opening 200 = OVER won
check(compute_final_result_label(snaps_bear_trap) == 1, "ml: bear trap final_result=1")
check(compute_trap_success_label(snaps_bear_trap) == 1, "ml: bear trap success=1 (UNDER was attractive, OVER won)")

# Game where opening line=200, final total=210 → over
snaps_over = [
    {"total_line": 200.0, "total_score": 0, "home_score": 0, "away_score": 0, "trap_meter": 0, "inflation_index": 0},
    {"total_line": 201.0, "total_score": 55, "home_score": 28, "away_score": 27, "trap_meter": 10, "inflation_index": 0.5},
    {"total_line": 202.0, "total_score": 105, "home_score": 53, "away_score": 52, "trap_meter": 30, "inflation_index": 1.0},
    {"total_line": 203.0, "total_score": 160, "home_score": 82, "away_score": 78, "trap_meter": 50, "inflation_index": 1.5},
    {"total_line": 204.0, "total_score": 210, "home_score": 108, "away_score": 102, "trap_meter": 70, "inflation_index": 2.0},
]
check(compute_final_result_label(snaps_over) == 1, "ml: final_result=1 for over")
check(compute_over_under_label(snaps_over) == "over", "ml: over_under=over")
clv2 = compute_clv_label(snaps_over)
check(clv2 is not None and clv2 == -4.0, f"ml: CLV={clv2} (expected 200-204=-4)")

# No trap (meter never >80)
check(compute_trap_success_label(snaps_over) is None, "ml: no trap = None")

# Helper functions
check(_get_opening_line(snaps_over) == 200.0, "ml: opening_line=200")
check(_get_closing_line(snaps_over) == 204.0, "ml: closing_line=204")
check(_get_final_total(snaps_over) == 210.0, "ml: final_total=210")
check(_get_opening_line([]) is None, "ml: opening_line empty=None")

# ═══════════════════════════════════════════════════════════════════
# 2. Db-backed MlPipeline integration
# ═══════════════════════════════════════════════════════════════════
from blm_v3.historical.database import HistoricalDatabase
from blm_v3.historical.models import GameModel, GameStatus, HistoricalSnapshot
from blm_v3.historical.config import ML_DEFAULT_FEATURES
import asyncio

async def test_pipeline():
    db_file = Path(tempfile.mktemp(suffix=".db"))
    db = HistoricalDatabase(db_path=db_file)
    await db.init()
    gid = "test-ml-pipeline"

    # Insert game with ending data
    await db.save_game(GameModel(
        id=gid, home_team="A", away_team="B", status=GameStatus.ENDED,
        final_home=108, final_away=102, final_total=210, final_margin=6,
    ).to_db_dict())

    # Insert snapshots with data matching `snaps_over`
    for i, s in enumerate(snaps_over):
        snap = HistoricalSnapshot(
            game_id=gid, quarter=(i//2)+1, clock=f"{12-i*2}:00",
            home_score=s["home_score"], away_score=s["away_score"],
            score_difference=s["home_score"] - s["away_score"],
            total_score=s["total_score"],
            total_line=s["total_line"],
            trap_meter=s["trap_meter"],
            inflation_index=s["inflation_index"],
            confidence=0.5 + i*0.1,
            momentum=i*1.5,
            projected_total=200.0 + i*2,
            possessions=s["total_score"],
            possessions_per_min=s["total_score"]/max((i+1)*2, 1),
        )
        await db.insert_snapshot(snap.to_db_dict())

    # Compute labels via pipeline
    pipeline = MlPipeline(db)
    labels = await pipeline.compute_labels(gid)
    check(labels.get("final_result") == 1, f"pipeline: final_result={labels.get('final_result')}")
    check(labels.get("over_under") == "over", f"pipeline: over_under={labels.get('over_under')}")
    check(labels.get("clv") is not None, "pipeline: clv computed")
    check(labels.get("max_trap_meter") == 70, f"pipeline: max_trap={labels.get('max_trap_meter')}")
    check(labels.get("snapshot_count") == 5, f"pipeline: count={labels.get('snapshot_count')}")

    # Build dataset with advanced label
    rows = await pipeline.build_dataset(
        game_ids=[gid],
        features=["total_line", "trap_meter", "confidence", "inflation_index"],
        label="final_result",
    )
    check(len(rows) == 5, f"pipeline: {len(rows)} rows")
    if rows:
        check("final_result" in rows[0], "pipeline: has final_result label")
        check("total_line" in rows[0], "pipeline: has total_line feature")
        check(rows[0]["final_result"] == 1, f"pipeline: row[0] label={rows[0]['final_result']}")

    # Build dataset with raw label
    rows2 = await pipeline.build_dataset(
        game_ids=[gid],
        features=["total_line"],
        label="total_score",
    )
    check(len(rows2) == 5, f"pipeline: raw {len(rows2)} rows")
    if rows2:
        check("total_score" in rows2[0], "pipeline: raw has label")

    # ML export via HTTP (FastAPI TestClient)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from blm_v3.api.export_routes import create_export_router
    app = FastAPI()
    router = create_export_router(db=db)
    app.include_router(router, prefix="/api/v2/historical/export")
    client = TestClient(app)

    # ML export with advanced label
    r = client.get(f"/api/v2/historical/export/ml?game_ids={gid}&label=final_result&features=total_line,trap_meter")
    check(r.status_code == 200, f"export ml: status={r.status_code}")
    csv_lines = r.text.strip().split("\n")
    check(len(csv_lines) > 1, f"export ml: {len(csv_lines)} rows")
    check("final_result" in csv_lines[0], "export ml: has label header")
    if len(csv_lines) > 1:
        check(csv_lines[1].endswith("1") or ",1" in csv_lines[1], "export ml: first row label=1")

    os.unlink(str(db_file))

asyncio.run(test_pipeline())

# ═══════════════════════════════════════════════════════════════════
# 3. ADR exists and is well-formed
# ═══════════════════════════════════════════════════════════════════
adr_path = "/private/tmp/BLM/docs/ADR-003-historical-engine.md"
check(os.path.exists(adr_path), "adr: file exists")
with open(adr_path) as f:
    adr = f.read()
check("# ADR-003:" in adr, "adr: has title")
check("**Status:** Implemented" in adr, "adr: has status")
check("## Context" in adr, "adr: has context")
check("## Decision" in adr, "adr: has decision")
check("## Consequences" in adr, "adr: has consequences")
check(len(adr) > 2000, f"adr: {len(adr)} chars (substantive)")

# ═══════════════════════════════════════════════════════════════════
# 4. Git log shows all phases
# ═══════════════════════════════════════════════════════════════════
r = subprocess.run(
    ["git", "log", "--oneline", "--all"],
    capture_output=True, text=True, timeout=10,
    cwd="/private/tmp/BLM",
)
log = r.stdout
# Check that Phase 1-5 are committed (Phase 6 committed after this test)
phases_expected = ["Phase 1:", "Phase 2:", "Phase 3:", "Phase 4:", "Phase 5:"]
for phase in phases_expected:
    check(phase in log, f"git log: {phase} committed")

# ═══════════════════════════════════════════════════════════════════
total = passed + failed
print(f"\n{'='*50}")
print(f"PHASE 6 VERIFICATION: {passed}/{total} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
