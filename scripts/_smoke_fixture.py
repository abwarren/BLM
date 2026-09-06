"""Smoke fixture: synthetic blm_pokerbet.db for walk_forward_validation.

Creates minimal games/snapshots/game_results/game_quality/market_observations
tables plus a handful of checkpoint_market rows, then removes the DB.
NOT analytical data — pipeline plumbing test only.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "blm_v4"))

DB = "/tmp/blm_smoke.db"
if os.path.exists(DB):
    os.remove(DB)
conn = sqlite3.connect(DB)
conn.executescript("""
CREATE TABLE games (id INTEGER PRIMARY KEY, source_game_id TEXT UNIQUE,
  classification TEXT, home_team TEXT, away_team TEXT, status TEXT);
CREATE TABLE snapshots (id INTEGER PRIMARY KEY, game_id INTEGER,
  captured_at TEXT, home_score INTEGER, away_score INTEGER, total_line REAL,
  market_type TEXT);
CREATE TABLE market_observations (id INTEGER PRIMARY KEY, source_game_id TEXT,
  market_type TEXT, line_value REAL, captured_at TEXT);
CREATE TABLE game_results (source_game_id TEXT PRIMARY KEY, classification TEXT,
  final_home INTEGER, final_away INTEGER, final_total INTEGER,
  result_at TEXT, final_result_status TEXT);
CREATE TABLE game_quality (source_game_id TEXT PRIMARY KEY, classification TEXT,
  status TEXT, reason TEXT, checked_at TEXT);
CREATE TABLE checkpoint_market (id INTEGER PRIMARY KEY, source_game_id TEXT,
  classification TEXT, checkpoint_pct INTEGER, checkpoint_timestamp TEXT,
  quarter INTEGER, progress REAL, elapsed_minutes REAL, opening_line REAL,
  live_market_line REAL, market_timestamp TEXT, blm_fair_value REAL,
  closing_line REAL, actual_final_total INTEGER, market_vs_fair REAL,
  signal TEXT, outcome TEXT, model_version TEXT, recorded_at TEXT);
""")

# two games on two dates, clean histories; market lines observed ~10s before checkpoints
games = [
    ("30001", "2026-09-01", 190, [(4, 191.0, 196.0), (10, 195.0, 200.0)]),
    ("30002", "2026-09-02", 180, [(4, 183.0, 178.0), (10, 179.0, 174.0)]),
]
for i, (gid, date, final_total, rows) in enumerate(games, start=1):
    conn.execute("INSERT INTO games VALUES (?,?,'CYBER_2K26','H','A','ended')",
                 (i, gid))
    conn.execute("INSERT INTO snapshots VALUES (?, ?, '2026-08-31T10:00:00Z',0,0,NULL,NULL)",
                 (i, gid))
    conn.execute("INSERT INTO game_results VALUES (?, 'CYBER_2K26',%d,%d,%d,"
                 " '2026-08-31T12:00:00Z','OK')" % (final_total // 2,
                                                    final_total - final_total // 2,
                                                    final_total), (gid,))
    for pct, live, fair in rows:
        ts = f"{date}T10:{pct:02d}:00Z"
        mt = f"{date}T09:{pct:02d}:50Z"
        conn.execute(
            "INSERT INTO checkpoint_market (source_game_id, classification,"
            " checkpoint_pct, checkpoint_timestamp, live_market_line,"
            " market_timestamp, blm_fair_value, actual_final_total,"
            " market_vs_fair, closing_line, model_version, recorded_at)"
            " VALUES (?, 'CYBER_2K26', ?, ?, ?, ?, ?, ?, ?, 189.0,"
            " 'v4-pace-1', '2026-09-02T00:00:00Z')",
            (gid, pct, ts, live, mt, fair, final_total, round(live - fair, 2)))
conn.commit()
conn.close()
print("fixture ready:", DB)
