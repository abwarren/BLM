"""Temporal-firewall + idempotency tests for the cohort rescore (doc §4.1, §8.2).

Synthetic fixtures only — no production data, no network, no service.

Run:  python -m pytest tests/test_tc_rescore_firewall_2026_10_26.py -q
"""
from __future__ import annotations

import sqlite3

import pytest

from blm_v4.cohorts import rescore

SCHEMA = """
CREATE TABLE games (id INTEGER PRIMARY KEY, source_game_id TEXT, competition_slug TEXT,
                    classification TEXT, first_seen_at TEXT);
CREATE TABLE snapshots (id INTEGER PRIMARY KEY, game_id INTEGER, captured_at TEXT,
                        quarter INTEGER, clock TEXT, home_score INTEGER, away_score INTEGER,
                        total_line REAL, total_under_odds REAL, classification TEXT);
CREATE TABLE game_results (id INTEGER PRIMARY KEY, source_game_id TEXT, classification TEXT,
                           final_home INTEGER, final_away INTEGER, final_total INTEGER,
                           result_at TEXT, final_result_status TEXT, result_source TEXT);
"""


def build(path, *, g1_result=180, g1_status="OK", closing_line=200.0, seed=True,
          cyber=False):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    if seed:  # a settled game in the same competition provides the PRIOR league pace
        con.execute("INSERT INTO games VALUES (9,'SEED','betual-euroleague','BETUAL_NBA','2026-10-22T08:00:00Z')")
        con.execute("INSERT INTO game_results VALUES (9,'SEED','BETUAL_NBA',80,80,160,"
                    "'2026-10-22T09:00:00Z','OK','results_page')")
    slug = "cyber-basketball-2k26-matches" if cyber else "betual-euroleague"
    cls = "CYBER_2K26" if cyber else "BETUAL_NBA"
    con.execute("INSERT INTO games VALUES (1,'G1',? ,?,'2026-10-22T11:00:00Z')", (slug, cls))
    rows = [
        # (captured_at, quarter, clock, home, away, line, under_price)
        ("2026-10-22T12:00:00Z", 3, "2:00", 70, 80, 200.0, 1.80),   # 55%  -> not stored
        ("2026-10-22T12:05:00Z", 4, "5:00", 75, 75, 200.0, 1.80),   # 87.5% -> FIRING
        ("2026-10-22T12:06:00Z", 4, "4:00", 80, 75, 200.0, 1.80),   # 90%  -> checkpoint
        ("2026-10-22T12:07:00Z", 4, "3:00", 85, 78, closing_line, 1.80),  # 92.5% out of band
        ("2026-10-22T12:08:00Z", 4, "0:00", 95, 95, closing_line, 1.80),  # 100% (final-ish)
    ]
    for i, (ts, q, clock, h, a, line, price) in enumerate(rows, start=1):
        con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (i, 1, ts, q, clock, h, a, line, price, cls))
    if g1_status is not None:
        con.execute(
            "INSERT INTO game_results (source_game_id, classification, final_home,"
            " final_away, final_total, result_at, final_result_status, result_source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("G1", cls, 95, 85, g1_result, "2026-10-22T12:30:00Z", g1_status, "results_page"))
    con.commit()
    con.close()
    return path


def ledger_rows(out):
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM cohort_ledger ORDER BY observed_at")]
    finally:
        con.close()


def test_firing_is_the_first_alert_condition_row_and_keeps_its_trigger(tmp_path):
    src = build(str(tmp_path / "src.db"))
    out = str(tmp_path / "out.db")
    rec = rescore.run(src, out, verbose=False)
    rows = ledger_rows(out)
    firings = [r for r in rows if r["source_type"] == "reconstructed_firing"]
    assert len(firings) == 1 and rec["fired"] == 1
    f = firings[0]
    assert f["observed_at"] == "2026-10-22T12:05:00Z"
    assert f["trigger_line"] == pytest.approx(200.0)      # the FIRING row's line
    assert f["under_price"] == pytest.approx(1.80)
    assert f["progress_pct"] == pytest.approx(87.5)
    assert f["pace_ratio"] == pytest.approx(2.5)          # (200-150)/5 = 10 ppm / 4.0
    assert f["tightest_cohort"] == "TC-X"
    assert f["outcome"] == "UNDER"                        # 180 < 200
    # Ledger = firing + qualifying checkpoint + data-quality rows in the alert band.
    # The 55 % row is excluded outright; the 0:00 row is UNKNOWN (no remaining time).
    assert [r["observed_at"] for r in rows] == [
        "2026-10-22T12:05:00Z", "2026-10-22T12:06:00Z", "2026-10-22T12:08:00Z"]
    by_ts = {r["observed_at"]: r for r in rows}
    assert by_ts["2026-10-22T12:06:00Z"]["status"] == "QUALIFIED"
    assert by_ts["2026-10-22T12:08:00Z"]["status"] == "UNKNOWN"
    assert "missing_required_pace" in by_ts["2026-10-22T12:08:00Z"]["reason_codes"]


def test_later_snapshots_and_closing_line_cannot_change_firing_evaluation(tmp_path):
    src_a = build(str(tmp_path / "a.db"), closing_line=200.0)
    src_b = build(str(tmp_path / "b.db"), closing_line=150.0)   # closing line moved
    out_a, out_b = str(tmp_path / "oa.db"), str(tmp_path / "ob.db")
    rescore.run(src_a, out_a, verbose=False)
    rescore.run(src_b, out_b, verbose=False)
    a = [r for r in ledger_rows(out_a) if r["source_type"] == "reconstructed_firing"][0]
    b = [r for r in ledger_rows(out_b) if r["source_type"] == "reconstructed_firing"][0]
    for k in ("observed_at", "trigger_line", "under_price", "progress_pct",
              "pace_ratio", "shortfall", "qualifying_cohorts", "tightest_cohort"):
        assert a[k] == b[k], k


def test_settlement_is_joined_after_evaluation_and_never_feeds_it(tmp_path):
    src_win = build(str(tmp_path / "w.db"), g1_result=180)      # UNDER
    src_lose = build(str(tmp_path / "l.db"), g1_result=210)     # OVER
    for src, expected in ((src_win, "UNDER"), (src_lose, "OVER")):
        out = str(tmp_path / f"{expected}.db")
        rescore.run(src, out, verbose=False)
        f = [r for r in ledger_rows(out) if r["source_type"] == "reconstructed_firing"][0]
        assert f["outcome"] == expected
        assert f["pace_ratio"] == pytest.approx(2.5)            # input unchanged


def test_push_is_reported_not_folded_into_a_win(tmp_path):
    src = build(str(tmp_path / "p.db"), g1_result=200)
    out = str(tmp_path / "o.db")
    rec = rescore.run(src, out, verbose=False)
    f = [r for r in ledger_rows(out) if r["source_type"] == "reconstructed_firing"][0]
    assert f["outcome"] == "PUSH" and rec["push"] == 1


def test_unsettled_game_is_pending_and_not_scored(tmp_path):
    src = build(str(tmp_path / "n.db"), g1_status=None)
    out = str(tmp_path / "o.db")
    rec = rescore.run(src, out, verbose=False)
    f = [r for r in ledger_rows(out) if r["source_type"] == "reconstructed_firing"][0]
    assert f["outcome"] is None and rec["pending"] == 1 and rec["settled_firings"] == 0


def test_rerun_is_idempotent(tmp_path):
    src = build(str(tmp_path / "s.db"))
    out = str(tmp_path / "o.db")
    rec1 = rescore.run(src, out, verbose=False, run_at="2026-10-26T00:00:00Z")
    n1 = len(ledger_rows(out))
    rec2 = rescore.run(src, out, verbose=False, run_at="2026-10-26T00:00:00Z")
    n2 = len(ledger_rows(out))
    assert n1 == n2
    assert rec1 == rec2


def test_checkpoints_are_not_counted_as_fired_alerts(tmp_path):
    src = build(str(tmp_path / "s.db"))
    out = str(tmp_path / "o.db")
    rec = rescore.run(src, out, verbose=False)
    assert rec["fired"] == 1
    assert rec["checkpoints"] >= 1
    assert rec["qualified_by_cohort"] == {"TC-X": 1, "TC-E": 1, "TC-S": 1,
                                          "TC-C": 1, "TC-W": 1, "TC-D": 1, "TC-C+": 1}


def test_cyber_feed_is_excluded_from_the_rescore(tmp_path):
    src = build(str(tmp_path / "c.db"), cyber=True)
    out = str(tmp_path / "o.db")
    rec = rescore.run(src, out, verbose=False)
    assert rec["excluded_cyber"] == 1 and rec["games"] == 0
    assert ledger_rows(out) == []


def test_unknown_inputs_are_stored_with_reason_codes_not_as_passes(tmp_path):
    src = build(str(tmp_path / "u.db"))
    con = sqlite3.connect(src)
    con.execute("UPDATE snapshots SET total_under_odds=NULL WHERE id IN (2,3)")
    con.commit()
    con.close()
    out = str(tmp_path / "o.db")
    rec = rescore.run(src, out, verbose=False)
    rows = {r["observed_at"]: r for r in ledger_rows(out)}
    # the two in-band rows with no price are UNKNOWN, never a pass
    for ts in ("2026-10-22T12:05:00Z", "2026-10-22T12:06:00Z"):
        assert rows[ts]["status"] == "UNKNOWN"
        assert "missing_price" in rows[ts]["reason_codes"]
    assert rec["qualified"] == 0
    # the alert can still reconstruct later, once a price exists — and that firing
    # legitimately sits OUTSIDE the cohort band (92.5 %), so it must be recorded as
    # a firing with status NOT_QUALIFIED (progress_out_of_band), not as a candidate.
    f = [r for r in rows.values() if r["source_type"] == "reconstructed_firing"][0]
    assert f["observed_at"] == "2026-10-22T12:07:00Z"
    assert f["status"] == "NOT_QUALIFIED"
    assert "progress_out_of_band" in f["reason_codes"]


def test_prior_only_league_reference_ignores_later_results(tmp_path):
    src = build(str(tmp_path / "s.db"))
    con = sqlite3.connect(src)
    # a LATER settled game in the same competition must not move the reference
    con.execute("INSERT INTO games VALUES (7,'LATER','betual-euroleague','BETUAL_NBA','2026-10-23T00:00:00Z')")
    con.execute("INSERT INTO game_results VALUES (7,'LATER','BETUAL_NBA',50,50,400,"
                "'2026-10-23T01:00:00Z','OK','results_page')")
    con.commit()
    con.close()
    out = str(tmp_path / "o.db")
    rescore.run(src, out, verbose=False)
    f = [r for r in ledger_rows(out) if r["source_type"] == "reconstructed_firing"][0]
    assert f["league_ppm"] == pytest.approx(4.0)     # SEED only, not the 400-point later game
    assert f["league_source"] == "prior_only"
