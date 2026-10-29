"""The population statistic must be computed IN SQL, not by fetching the rows.

WHY (2026-10-29): _stats used to pull every matching pace into Python — a
4th-quarter population is ~30 000 rows — measured at 4-70 s PER CALL on prod.
The live payload asks for one benchmark per game per quarter, so a betting pass
took 10-30 minutes and the worker was blind between passes, missing most TC-W
candidates.  Aggregating (COUNT/SUM/SUM(x²)) keeps the same statistic at one
tenth of the cost.

This pins both halves: the numbers still match a direct computation, and the
function must not go back to transferring the population.
"""
from __future__ import annotations

import math
import sqlite3

import blm_v4.live_analytics.benchmark as B

KEY = "BETUAL|betual-tbsl|Q3|P075"
CUTOFF = "2026-10-29T00:00:00.000000Z"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE clean_projections(
            id INTEGER PRIMARY KEY, source_game_id TEXT, status TEXT,
            classification TEXT, period_label TEXT, progress_pct REAL,
            captured_at TEXT, actual_pts_per_min REAL);
        CREATE TABLE competition_ledger(
            source_game_id TEXT PRIMARY KEY, provider TEXT, competition TEXT,
            status TEXT);
        CREATE TABLE pace_benchmark_cache(
            benchmark_key TEXT, cutoff_captured_at TEXT,
            cutoff_observation_id INTEGER, n INTEGER, mean_pace REAL,
            std_pace REAL, computed_at TEXT);
    """)
    paces = [3.5, 3.9, 4.1, 4.4, 3.7, 4.8, 3.3, 4.0]
    for i, p in enumerate(paces):
        gid = f"g{i}"
        conn.execute("INSERT INTO competition_ledger VALUES (?,?,?,'classified')",
                     (gid, "BETUAL", "betual-tbsl"))
        conn.execute(
            "INSERT INTO clean_projections(source_game_id, status, classification,"
            " period_label, progress_pct, captured_at, actual_pts_per_min)"
            " VALUES (?, 'VALID', 'BETUAL_NBA', '3rd Quarter', 77.0, ?, ?)",
            (gid, "2026-10-28T00:00:00Z", p))
    # a row from ANOTHER competition must never enter this population
    conn.execute("INSERT INTO competition_ledger VALUES ('x','BETUAL','betual-nba','classified')")
    conn.execute(
        "INSERT INTO clean_projections(source_game_id, status, classification,"
        " period_label, progress_pct, captured_at, actual_pts_per_min)"
        " VALUES ('x','VALID','BETUAL_NBA','3rd Quarter',77.0,"
        "'2026-10-28T00:00:00Z', 99.0)")
    conn.commit()
    return conn, paces


def test_benchmark_matches_the_direct_computation():
    conn, paces = _db()
    b = B._stats(conn, KEY, CUTOFF, None, None)

    mean = sum(paces) / len(paces)
    var = sum((x - mean) ** 2 for x in paces) / (len(paces) - 1)

    assert b.n == len(paces), "the other competition leaked into the population"
    assert abs(b.mean - mean) < 1e-6   # PaceBenchmark rounds to 6 dp
    assert abs(b.std - math.sqrt(var)) < 1e-6


def test_the_population_is_never_fetched_into_python(monkeypatch):
    """Guard the fix itself: one aggregate row back, not n rows."""
    conn, _ = _db()
    seen = []

    class Spy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if sql.lstrip().upper().startswith("SELECT") and "actual_pts_per_min" in sql:
                seen.append(" ".join(sql.split())[:90])
            return self._real.execute(sql, *a, **kw)

    B._stats(Spy(conn), KEY, CUTOFF, None, None)

    assert seen, "no population query ran"
    for sql in seen:
        assert "COUNT(*)" in sql and "SUM(" in sql, (
            "the population is being fetched row-by-row again: " + sql)


def test_cache_round_trip_uses_the_same_statistic():
    conn, paces = _db()
    first = B.benchmark_for(conn, "BETUAL", "betual-tbsl", 77.0, CUTOFF,
                            period_label="3rd Quarter")
    second = B.benchmark_for(conn, "BETUAL", "betual-tbsl", 77.0, CUTOFF,
                             period_label="3rd Quarter")   # cache hit
    assert (first.n, first.mean, first.std) == (second.n, second.mean, second.std)
    assert first.n == len(paces)
