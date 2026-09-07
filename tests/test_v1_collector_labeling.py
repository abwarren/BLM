"""Labeling-contract tests for the V1 SnapshotCollector raw store.

Proven data-integrity defect (2026-09-03 audit of blm_ts.db +
blm.db): the collector froze a game_id from its FIRST capture and wrote
every later capture under it, but the scraped PokerBet page is a
multi-game view whose DOM order changes — consecutive captures show
DIFFERENT games.  Result: rows for game X stored under game Y's id
(e.g. game_id 'Memphis Grizzlies Cyber-vs-Orlando Magic Cyber-2026-09-02'
contains 5 different team-pairs; 'Oklahoma City...' contains 11).

The only identity the text-parse can attest is the parsed team pair
itself, so every row must be labeled by its OWN capture.  These tests
pin the raw-store (blm.db) and latest_state contracts without a browser
or the production DB (DB_PATH is monkeypatched to a temp file).
"""

from __future__ import annotations

import sqlite3

from blm_v1 import database as v1_database
from blm_v1.collector import SnapshotCollector


def _isolate_db(monkeypatch, tmp_path) -> None:
    """Point blm_v1.database at a temp file.

    blm_v1/database.py calls init_db() at IMPORT time, so the module
    already holds a cached connection to the production blm.db before
    any monkeypatch runs — DB_PATH alone is not enough.  Drop the cached
    per-thread connection so the next get_connection() opens the temp
    path.
    """
    monkeypatch.setattr(v1_database, "DB_PATH", tmp_path / "test_blm.db")
    v1_database._local.conn = None
    v1_database.init_db()


def _capture(collector, home, away, score=0):
    collector._store_snapshot({
        "home_team": home, "away_team": away,
        "home_score": score, "away_score": score,
        "quarter": 1, "clock": "10:00",
        "total_line": 200.5, "spread": -1.5,
    })


def test_each_capture_is_labeled_by_its_own_teams(monkeypatch, tmp_path):
    """Two captures showing DIFFERENT games must produce two different,
    self-consistent game_ids — the second capture must NOT inherit the
    first capture's frozen label."""
    _isolate_db(monkeypatch, tmp_path)

    c = SnapshotCollector(headless=True)
    _capture(c, "Alpha Cyber", "Beta Cyber")
    first_gid = c.latest_state["game_id"]
    assert first_gid.startswith("Alpha Cyber-vs-Beta Cyber-"), first_gid

    _capture(c, "Gamma Cyber", "Delta Cyber")
    second_gid = c.latest_state["game_id"]
    assert second_gid.startswith("Gamma Cyber-vs-Delta Cyber-"), second_gid
    assert second_gid != first_gid

    # The raw store rows carry their own labels too.
    conn = sqlite3.connect(str(tmp_path / "test_blm.db"))
    rows = conn.execute(
        "SELECT game_id FROM snapshots ORDER BY id"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == [first_gid, second_gid]


def test_consistent_captures_share_one_game_id(monkeypatch, tmp_path):
    """Consecutive captures of the SAME game stay under one game_id."""
    _isolate_db(monkeypatch, tmp_path)

    c = SnapshotCollector(headless=True)
    _capture(c, "Alpha Cyber", "Beta Cyber")
    _capture(c, "Alpha Cyber", "Beta Cyber", score=2)
    assert c.latest_state["game_id"].startswith("Alpha Cyber-vs-Beta Cyber-")

    conn = sqlite3.connect(str(tmp_path / "test_blm.db"))
    n = conn.execute("SELECT COUNT(DISTINCT game_id) FROM snapshots").fetchone()[0]
    conn.close()
    assert n == 1
