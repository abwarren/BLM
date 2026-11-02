"""Fixture-context join for the DB EXPLORER execution ledger.

The explorer asks one question of each ledger row: who played, when did it
start, how did it finish, and how far did the total land from the line.
These tests pin that contract — including the honest 'unknown' answers —
against a purpose-built deployment directory.
"""
import sqlite3
from datetime import datetime, timezone

from blm_v4.betting.fixture_context import CONTEXT_FIELDS, fixture_context


def _deployment(tmp_path):
    root = tmp_path
    clean = sqlite3.connect(root / "blm_metrics_clean.db")
    clean.execute(
        "CREATE TABLE clean_games (source_game_id TEXT, base_game_id TEXT, "
        "classification TEXT, home_team TEXT, away_team TEXT, status TEXT, "
        "first_seen_at TEXT, last_seen_at TEXT, final_home INTEGER, "
        "final_away INTEGER, final_total INTEGER, final_result_status TEXT, "
        "finalized_at TEXT)")
    clean.execute(
        "INSERT INTO clean_games VALUES ('evt-1','evt-1','BETUAL_NBA',"
        "'Denver Nuggets','Cleveland Cavaliers','live','2026-11-02T12:01:30Z',"
        "'2026-11-02T14:00:00Z',NULL,NULL,NULL,NULL,NULL)")
    clean.execute(
        "INSERT INTO clean_games VALUES ('evt-2','evt-2','CYBER_2K26',"
        "'Memphis Grizzlies Cyber','Orlando Magic Cyber','live',"
        "'2026-11-02T14:49:40Z','2026-11-02T14:50:27Z',NULL,NULL,NULL,NULL,NULL)")
    clean.commit()
    clean.close()

    pokerbet = sqlite3.connect(root / "blm_pokerbet.db")
    pokerbet.execute(
        "CREATE TABLE betual_game_timers (source_game_id TEXT, "
        "game_start_wall REAL, observed_at_wall REAL, quarter_seconds REAL, "
        "break_seconds REAL, timer_model TEXT, last_home INTEGER, "
        "last_away INTEGER, last_line REAL, last_line_at REAL, "
        "last_capture_at TEXT, updated_at TEXT)")
    pokerbet.execute("INSERT INTO betual_game_timers (source_game_id, "
                     "game_start_wall) VALUES ('evt-1', ?)",
                     (datetime(2026, 11, 2, 12, 1, 30,
                               tzinfo=timezone.utc).timestamp(),))
    pokerbet.execute(
        "CREATE TABLE game_results (id INTEGER PRIMARY KEY, "
        "source_game_id TEXT, classification TEXT, final_home INTEGER, "
        "final_away INTEGER, final_total INTEGER, result_at TEXT, "
        "final_result_status TEXT, result_source TEXT)")
    pokerbet.execute("INSERT INTO game_results VALUES (1,'evt-1','BETUAL_NBA',"
                     "117,136,253,'2026-11-02T14:00:00Z','OK','live_page')")
    # a stale earlier row and a later authoritative row for the same game
    pokerbet.execute("INSERT INTO game_results VALUES (2,'evt-2','CYBER_2K26',"
                     "NULL,NULL,NULL,'2026-11-02T14:51:00Z',"
                     "'NEEDS_RECONCILIATION',NULL)")
    pokerbet.commit()
    pokerbet.close()
    return root


def test_joins_teams_start_and_final(tmp_path):
    ctx = fixture_context(_deployment(tmp_path), ["evt-1"])
    assert set(ctx["evt-1"]) == set(CONTEXT_FIELDS)
    row = ctx["evt-1"]
    assert row["home_team"] == "Denver Nuggets"
    assert row["away_team"] == "Cleveland Cavaliers"
    assert row["final_total"] == 253
    assert row["final_result_status"] == "OK"
    assert row["fixture_start_utc"].startswith("2026-11-02T12:01:30")
    assert row["fixture_start_source"] == "betual_timer"


def test_missing_result_is_none_not_invented(tmp_path):
    ctx = fixture_context(_deployment(tmp_path), ["evt-2"])
    assert ctx["evt-2"]["final_total"] is None
    assert ctx["evt-2"]["final_result_status"] == "NEEDS_RECONCILIATION"
    # no timer row -> falls back to first observation, and says so
    assert ctx["evt-2"]["fixture_start_source"] == "first_seen"
    assert ctx["evt-2"]["fixture_start_utc"] == "2026-11-02T14:49:40Z"


def test_instance_suffix_resolves_to_base_fixture(tmp_path):
    ctx = fixture_context(_deployment(tmp_path), ["evt-1#i2"])
    assert ctx["evt-1#i2"]["home_team"] == "Denver Nuggets"
    assert ctx["evt-1#i2"]["final_total"] == 253


def test_unknown_and_manual_ids_are_blank_not_dropped(tmp_path):
    root = _deployment(tmp_path)
    ctx = fixture_context(root, ["manual:ui-abc", "evt-404"])
    assert set(ctx) == {"manual:ui-abc", "evt-404"}
    assert all(v is None for v in ctx["evt-404"].values())


def test_missing_database_yields_blank_context(tmp_path):
    """No DBs at all: still one entry per requested id, every field None."""
    ctx = fixture_context(tmp_path, ["evt-1"])
    assert set(ctx) == {"evt-1"}
    assert all(v is None for v in ctx["evt-1"].values())
