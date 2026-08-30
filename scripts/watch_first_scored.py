#!/usr/bin/env python3
"""Watch for the first clean completed BLM game passing the quality gate.

Polls the scorecard tables read-only every 60s for up to ~20 minutes.
Prints the first scored game's details (id, checkpoint results, metrics)
when it appears, then exits 0.  If nothing scores, prints a summary of
the newest ended games' status and exits 1.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB = "/home/gdi/BLM/blm_pokerbet.db"
DEADLINE = time.time() + 20 * 60
first = True


def check():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        scored = conn.execute(
            "SELECT COUNT(*) c FROM prediction_scores").fetchone()["c"]
        ok_games = conn.execute(
            "SELECT source_game_id FROM game_results WHERE final_result_status='OK'"
        ).fetchall()
        if scored > 0 and ok_games:
            gid = ok_games[0]["source_game_id"]
            game = conn.execute(
                "SELECT * FROM games WHERE source_game_id=?", (gid,)).fetchone()
            nsnaps = conn.execute(
                "SELECT COUNT(*) c FROM snapshots WHERE game_id=?", (game["id"],)).fetchone()["c"]
            preds = conn.execute(
                """SELECT p.checkpoint, p.checkpoint_percent, p.progress,
                          p.projected_total, p.market_total, p.source_snapshot_at,
                          s.total_error, s.abs_total_error, s.home_error, s.away_error
                   FROM predictions p JOIN prediction_scores s ON s.prediction_id = p.id
                   WHERE p.source_game_id=? ORDER BY p.checkpoint""", (gid,)).fetchall()
            res = conn.execute(
                "SELECT * FROM game_results WHERE source_game_id=?", (gid,)).fetchone()
            summ = conn.execute(
                """SELECT model_version, COUNT(*) n, COUNT(DISTINCT source_game_id) games,
                          AVG(abs_total_error) mae, AVG(total_error) bias,
                          AVG(total_pct_error) mape
                   FROM prediction_scores GROUP BY model_version""").fetchall()
            out = {
                "scored_game": {
                    "source_game_id": gid,
                    "instance": gid,
                    "home": game["home_team"], "away": game["away_team"],
                    "classification": game["classification"],
                    "snapshot_count": nsnaps,
                    "final": {"home": res["final_home"], "away": res["final_away"],
                              "total": res["final_total"]},
                    "predictions": [dict(p) for p in preds],
                },
                "scorecard": [dict(s) for s in summ],
            }
            print(json.dumps(out, indent=1, default=str))
            return True
        # no scored game yet — show newest ended games
        ended = conn.execute(
            """SELECT g.source_game_id, g.classification, g.status,
                      (SELECT COUNT(*) FROM snapshots s WHERE s.game_id=g.id) snaps,
                      (SELECT r.final_result_status FROM game_results r WHERE r.source_game_id=g.source_game_id) result_status
               FROM games g WHERE g.status='ended' ORDER BY g.last_seen_at DESC LIMIT 5""").fetchall()
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%SZ')}] scored={scored} ok_games={len(ok_games)} "
              f"recent_ended={[dict(r) for r in ended]}")
        return False
    finally:
        conn.close()


while time.time() < DEADLINE:
    try:
        if check():
            sys.exit(0)
    except Exception as e:
        print(f"check error: {e}")
    time.sleep(60)

print("NO_CLEAN_GAME_YET within 20min watch window")
sys.exit(1)
