"""The Q3 pace reference must be driven by settled finals, not by a full
GROUP BY over every snapshot, and must still produce the SAME numbers.

Measured 2026-11-02: the old plan (aggregate all snapshots, then join) did not
finish inside the stats worker's budget on the live DB (>5 min).  The rewrite
drives from game_results and lets idx_snapshots_game_captured serve the
per-game lookup.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blm_v4.live_analytics.fingerprint_c5 import q3_pace_reference
from blm_v4.projection import duration_for


SCHEMA = """
CREATE TABLE games (source_game_id TEXT PRIMARY KEY, competition_slug TEXT,
                    classification TEXT);
CREATE TABLE game_results (source_game_id TEXT, final_result_status TEXT,
                           final_total REAL);
CREATE TABLE snapshots (source_game_id TEXT, period_label TEXT,
                        home_score INTEGER, away_score INTEGER,
                        captured_at TEXT);
CREATE INDEX idx_snapshots_game_captured
    ON snapshots (source_game_id, captured_at);
"""

# game -> [(period, home, away)] ; two settled games, one unsettled, one with
# a missing Q2 (must contribute nothing)
ROWS = {
    "g1": [("1st Quarter", 20, 18), ("2nd Quarter", 40, 36),
           ("3rd Quarter", 60, 55), ("4th Quarter", 80, 74)],
    "g2": [("1st Quarter", 25, 20), ("2nd Quarter", 50, 44),
           ("3rd Quarter", 72, 66), ("4th Quarter", 95, 90)],
    "g3": [("1st Quarter", 10, 10), ("2nd Quarter", 30, 30),
           ("3rd Quarter", 50, 48), ("4th Quarter", 70, 68)],   # unsettled
    "g4": [("3rd Quarter", 44, 40), ("4th Quarter", 60, 55)],   # no Q2
}
RESULTS = {"g1": ("OK", 154.0), "g2": ("OK", 185.0),
           "g3": ("NEEDS_RECONCILIATION", None), "g4": ("OK", 115.0)}


def _db(tmp_path: Path, classification="BETUAL_NBA") -> Path:
    p = tmp_path / "fixture.db"
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    for gid in ROWS:
        c.execute("INSERT INTO games VALUES (?,?,?)", (gid, "betual-nba", classification))
        st, ft = RESULTS[gid]
        c.execute("INSERT INTO game_results VALUES (?,?,?)", (gid, st, ft))
        for i, (period, h, a) in enumerate(ROWS[gid]):
            c.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)",
                      (gid, period, h, a, f"2026-11-01T00:0{i}:00Z"))
    c.commit()
    c.close()
    return p


def test_reference_is_the_per_game_q3_segment_over_quarter_minutes(tmp_path):
    p = _db(tmp_path)
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    ref = q3_pace_reference(c)
    c.close()

    qmin = duration_for("BETUAL_NBA") / 4.0 if False else None  # noqa: F841
    # the module's own quarter minutes for this classification
    from blm_v4.live_analytics.fingerprint_c5 import _quarter_minutes
    q = _quarter_minutes("BETUAL_NBA")
    seg1 = (60 + 55) - (40 + 36)
    seg2 = (72 + 66) - (50 + 44)
    expected = round((seg1 / q + seg2 / q) / 2, 4)
    assert ref["betual-nba"]["games"] == 2          # g1, g2 only
    assert ref["betual-nba"]["avg_q3_pace"] == expected


def test_unsettled_and_incomplete_games_are_excluded(tmp_path):
    p = _db(tmp_path)
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    ref = q3_pace_reference(c)
    c.close()
    # g3 (not OK) and g4 (no Q2) contribute nothing: n stays at 2, never 4
    assert ref["betual-nba"]["games"] == 2


def test_an_empty_database_yields_an_empty_reference_not_an_error(tmp_path):
    p = tmp_path / "empty.db"
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.commit()
    assert q3_pace_reference(c) == {}
    c.close()
