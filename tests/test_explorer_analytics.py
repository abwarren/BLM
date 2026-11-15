"""Evidence-bounded DB Explorer performance calculation tests."""
import json
import sqlite3

from blm_v4.db_explorer_analytics import load_execution_page
from blm_v4.explorer_analytics import (
    execution_state, performance, settlement, theoretical_unit_result,
)


def test_theoretical_flat_unit_uses_decimal_price_and_push_rules():
    assert theoretical_unit_result("Won", 1.85) == 0.85
    assert theoretical_unit_result("Lost", 1.85) == -1.0
    assert theoretical_unit_result("Push", 2.10) == 0.0
    assert theoretical_unit_result("Void", 2.10) == 0.0
    assert theoretical_unit_result("Won", None) is None
    assert theoretical_unit_result("Won", 1.0) is None


def test_points_margin_is_direction_normalized_and_push_safe():
    assert settlement("UNDER", 180.5, 178)["margin"] == 2.5
    assert settlement("OVER", 180.5, 183)["margin"] == 2.5
    assert settlement("UNDER", 180, 180)["status"] == "Push"
    assert settlement("OVER", None, 183)["status"] == "Unknown"
    assert settlement("UNDER", 180, None)["status"] == "Pending"


def test_execution_unknown_and_dry_run_are_not_confirmed_bets():
    dry = execution_state({"status": "ACCEPTED", "provider_ref": "dryrun-x"})
    assert dry["execution"] == "Not attempted"
    unknown = execution_state({"status": "UNKNOWN"})
    assert unknown["execution"] == "Unknown"
    eligible = execution_state(
        {"status": "FAILED"},
        [{"event": "claimed", "reason": "all ten conditions satisfied"}],
    )
    assert eligible["eligibility"] == "Eligible"


def test_actual_ledger_uses_confirmed_stakes_and_sampled_win_rate():
    rows = [
        {"execution": "Confirmed placed", "settlement": "Won",
         "stake_amount": 100, "price": 1.85, "unit_price": 100},
        {"execution": "Confirmed placed", "settlement": "Lost",
         "stake_amount": 50, "price": 2.1, "unit_price": 50},
        {"execution": "Confirmed placed", "settlement": "Push",
         "stake_amount": 30, "price": 2.0, "unit_price": 30},
        {"execution": "Not attempted", "settlement": "Won",
         "stake_amount": 1000, "price": 2.0, "unit_price": 1000},
    ]
    out = performance(rows)
    assert (out["wins"], out["losses"], out["pushes"]) == (1, 1, 1)
    assert out["win_rate_pct"] == 50.0
    assert out["win_rate_n"] == 2
    assert out["net_amount"] == 35.0
    assert out["total_stake"] == 180.0
    assert out["roi_pct"] == 19.44


def test_unknown_or_missing_price_never_creates_profit():
    out = performance([{
        "execution": "Confirmed placed", "settlement": "Won",
        "stake_amount": 20, "price": None, "unit_price": 20,
    }])
    assert out["missing_price_wins"] == 1
    assert out["net_amount"] is None
    assert out["roi_pct"] is None


def test_read_only_execution_query_uses_recorded_line_and_authoritative_final(tmp_path):
    betting = tmp_path / "betting.db"
    pipeline = tmp_path / "pipeline.db"
    b = sqlite3.connect(betting)
    b.executescript("""
      CREATE TABLE bet_executions (
        id INTEGER PRIMARY KEY, execution_id TEXT, idempotency_key TEXT,
        game_id TEXT, alert_id TEXT, checkpoint TEXT, market TEXT, selection TEXT,
        triggered_line REAL, price REAL, unit_price REAL, stake_units REAL,
        stake_amount REAL, accepted_amount REAL, status TEXT, execution_state TEXT,
        provider_ref TEXT, error_code TEXT, error_message TEXT, rejection_reason TEXT,
        requested_at_utc TEXT, submitted_at_utc TEXT, resolved_at_utc TEXT, day_utc TEXT
      );
      CREATE TABLE bet_audit (
        id INTEGER PRIMARY KEY, execution_id TEXT, event TEXT, reason TEXT,
        details TEXT, at_utc TEXT
      );
    """)
    b.execute("""INSERT INTO bet_executions VALUES
      (1,'exec-1','key-1','12345','alert-1','75','TOTAL','UNDER',180.5,
       1.85,100,1,100,100,'ACCEPTED','ACCEPTED','ticket-1',NULL,NULL,NULL,
       '2026-11-14T12:00:00Z','2026-11-14T12:00:01Z','2026-11-14T12:00:02Z','2026-11-14')""")
    b.execute("INSERT INTO bet_audit VALUES(1,'exec-1','claimed',?, ?,?)",
              ("all ten conditions satisfied", json.dumps({"dry_run": False}),
               "2026-11-14T12:00:00Z"))
    b.commit()
    b.close()

    p = sqlite3.connect(pipeline)
    p.executescript("""
      CREATE TABLE games (
        source_game_id TEXT, competition TEXT, competition_slug TEXT,
        home_team TEXT, away_team TEXT
      );
      CREATE TABLE game_results (
        source_game_id TEXT, final_home INTEGER, final_away INTEGER,
        final_total INTEGER, final_result_status TEXT, result_at TEXT
      );
    """)
    p.execute("INSERT INTO games VALUES('12345','NBA','betual-nba','A','B')")
    p.execute("INSERT INTO game_results VALUES('12345',90,88,178,'OK','2026-11-14T13:00:00Z')")
    p.commit()
    p.close()

    check = sqlite3.connect(betting)
    before = check.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    check.close()
    data = load_execution_page(
        betting, pipeline, from_day="2026-11-14", to_day="2026-11-14")
    check = sqlite3.connect(betting)
    after = check.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    check.close()
    row = data["rows"][0]
    assert row["execution"] == "Confirmed placed"
    assert row["eligibility"] == "Eligible"
    assert row["settlement"] == "Won"
    assert row["points_margin"] == 2.5
    assert row["actual_profit"] == 85.0
    assert row["actual_units"] == 0.85
    assert row["signal_qualification"] == "Unknown"
    assert row["cohort"] == "UNCLASSIFIED"
    assert data["actual_performance"]["win_rate_n"] == 1
    assert data["theoretical_signal_performance_available"] is False
    assert before == after
