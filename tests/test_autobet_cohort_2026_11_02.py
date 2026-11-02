"""The STATS cohort is the AUTO-BET fired set, not the fired-alert population.

Operator directive 2026-11-02: the headline card must answer "of all the games
where auto-bet was fired".  These pin the one seam that decides membership —
``autobet_game_ids`` — because the cohort is built by intersecting it with the
alert rows, and a wrong id set silently answers a different question.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blm_v4.live_analytics.fingerprint_stats import autobet_game_ids


def _ledger(path: Path, rows) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE bet_executions (id INTEGER PRIMARY KEY,"
                 " game_id TEXT, status TEXT)")
    conn.executemany("INSERT INTO bet_executions (game_id, status) VALUES (?, ?)",
                     rows)
    conn.commit()
    conn.close()
    return path


def test_missing_ledger_is_empty_not_the_alert_population(tmp_path):
    assert autobet_game_ids(tmp_path / "nope.db") == set()


def test_ledger_without_the_table_is_empty(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()
    assert autobet_game_ids(p) == set()


def test_ids_are_deduped_and_instance_suffixes_are_stripped(tmp_path):
    p = _ledger(tmp_path / "bet.db", [
        ("31429632", "SUBMITTING"),
        ("31429632", "FAILED"),          # same game, second attempt
        ("31429096#i1", "SUBMITTED"),    # instance sibling
        (None, "FAILED"),                # unreadable id must not crash
    ])
    assert autobet_game_ids(p) == {"31429632", "31429096"}


def test_a_game_with_a_ledger_row_is_a_member_and_one_without_is_not(tmp_path):
    p = _ledger(tmp_path / "bet.db", [("31429632", "SUBMITTED")])
    ids = autobet_game_ids(p)
    assert "31429632" in ids
    assert "31429632#i7".split("#")[0] in ids     # sibling resolves to the base
    assert "31400000" not in ids
