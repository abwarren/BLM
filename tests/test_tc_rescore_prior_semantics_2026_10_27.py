"""Strictly-prior league reference + timestamp normalization (reviewer follow-up).

Covers the 2026-10-27 review action: a settled result stamped at EXACTLY the
observation time must not enter that observation's league reference, and the
comparison must be on parsed UTC instants — never on lexical strings.

Run:  python -m pytest tests/test_tc_rescore_prior_semantics_2026_10_27.py -q
"""
from __future__ import annotations

import sqlite3

import pytest

from blm_v4.cohorts.rescore import parse_utc_epoch

BASE = "2026-10-22T12:05:00Z"          # the firing observation in the fixture
BASE_EPOCH = 1792670700.0              # 2026-10-22T12:05:00Z


# ── unit: timestamp normalization ────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("2026-10-22T12:05:00Z", BASE_EPOCH),
    ("2026-10-22T12:05:00", BASE_EPOCH),               # naive -> UTC
    ("2026-10-22 12:05:00", BASE_EPOCH),               # space separator
    ("2026-10-22T14:05:00+02:00", BASE_EPOCH),         # offset form
    ("2026-10-22T14:05:00+0200", BASE_EPOCH),          # compact offset
    ("2026-10-22T12:05:00.000000Z", BASE_EPOCH),       # microseconds
    ("2026-10-22T12:05:00.123Z", BASE_EPOCH + 0.123),  # fractional
    (BASE_EPOCH, BASE_EPOCH),                          # epoch number
])
def test_parse_utc_epoch_variants(value, expected):
    got = parse_utc_epoch(value)
    assert got is not None and abs(got - expected) < 1e-6


@pytest.mark.parametrize("value", [None, "", "not-a-timestamp", True, [], {},
                                    float("nan"), float("inf")])
def test_parse_utc_epoch_rejects_unparseable(value):
    assert parse_utc_epoch(value) is None


def test_lexical_order_would_differ_from_instant_order():
    """Proof the old lexical comparison was wrong: '...T12:05:00.5Z' sorts BEFORE
    '...T12:05:00Z' lexically but is LATER in time."""
    a, b = "2026-10-22T12:05:00.5Z", "2026-10-22T12:05:00Z"
    assert (a < b) is True                      # lexical says earlier  (wrong)
    assert parse_utc_epoch(a) > parse_utc_epoch(b)   # instants say later


# ── integration: the reference itself ────────────────────────────────────

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


def build_with_result(path, *, extra_result_at: str | None, extra_total: int = 400):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    # baseline settled game: result well BEFORE the observations -> pace 4.0
    con.execute("INSERT INTO games VALUES (9,'SEED','betual-euroleague','BETUAL_NBA','2026-10-22T08:00:00Z')")
    con.execute("INSERT INTO game_results VALUES (9,'SEED','BETUAL_NBA',80,80,160,"
                "'2026-10-22T10:00:00Z','OK','results_page')")
    if extra_result_at is not None:
        con.execute("INSERT INTO games VALUES (8,'EDGE','betual-euroleague','BETUAL_NBA','2026-10-22T08:00:00Z')")
        con.execute("INSERT INTO game_results VALUES (8,'EDGE','BETUAL_NBA',100,100,?,?,"
                    "'OK','results_page')", (extra_total, extra_result_at))
    con.execute("INSERT INTO games VALUES (1,'G1','betual-euroleague','BETUAL_NBA','2026-10-22T11:00:00Z')")
    con.execute("INSERT INTO snapshots VALUES (1,1,?,4,'5:00',75,75,200.0,1.80,'BETUAL_NBA')", (BASE,))
    con.commit()
    con.close()
    return path


def _league(tmp_path, name, **kw):
    from blm_v4.cohorts import rescore
    src = build_with_result(str(tmp_path / f"{name}.db"), **kw)
    out = str(tmp_path / f"{name}-out.db")
    rescore.run(src, out, verbose=False)
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM cohort_ledger WHERE source_type='reconstructed_firing'").fetchone()
    finally:
        con.close()
    return dict(row)


def test_result_before_the_observation_contributes(tmp_path):
    row = _league(tmp_path, "before", extra_result_at="2026-10-22T12:04:59Z")
    # (4.0 + 400/40=10.0) / 2 = 7.0
    assert row["league_ppm"] == pytest.approx(7.0)
    assert row["league_source"] == "prior_only"


def test_result_exactly_at_the_observation_is_excluded(tmp_path):
    row = _league(tmp_path, "equal", extra_result_at=BASE)
    assert row["league_ppm"] == pytest.approx(4.0)     # SEED only


def test_same_instant_in_another_format_is_still_excluded(tmp_path):
    row = _league(tmp_path, "equal-offset", extra_result_at="2026-10-22T14:05:00+02:00")
    assert row["league_ppm"] == pytest.approx(4.0)


def test_result_after_the_observation_is_excluded(tmp_path):
    row = _league(tmp_path, "after", extra_result_at="2026-10-22T12:05:01Z")
    assert row["league_ppm"] == pytest.approx(4.0)


def test_unparseable_result_timestamp_is_never_used_and_is_counted(tmp_path):
    from blm_v4.cohorts import rescore
    src = build_with_result(str(tmp_path / "bad-ts.db"), extra_result_at="not-a-time")
    out = str(tmp_path / "bad-out.db")
    rec = rescore.run(src, out, verbose=False)
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM cohort_ledger WHERE source_type='reconstructed_firing'").fetchone()
    finally:
        con.close()
    assert dict(row)["league_ppm"] == pytest.approx(4.0)
    assert rec["results_unparseable_ts"] == 1
