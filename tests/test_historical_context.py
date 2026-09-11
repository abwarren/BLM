"""Historical-context layer — league/state-relative UNDER context.

Pins the verified forensic finding as descriptive infrastructure:
  * analytical eligibility: remaining_game_minutes >= 2.5 (2.50 INCLUDED;
    2.49/2.0/0 excluded) — an analytical rule, terminal semantics untouched
  * the canonical benchmark key (provider|competition|period|progress) —
    never a global average, never cross-competition
  * primary UNDER state: actual_pace < own avg AND required_pace >= own avg
  * explicit no_mature_historical_context state (N < 30 / no observation /
    unresolved competition) — NO fallback of any kind
All fixtures build isolated tmp DBs; the production DBs are never touched.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blm_v4.live_analytics.historical_context import (
    ANALYTICAL_MIN_REMAINING_MINUTES,
    HistoricalContextEngine,
)

# ── fixtures ────────────────────────────────────────────────────────────

def _make_dbs(tmp_path: Path):
    clean = tmp_path / "blm_metrics_clean.db"
    main = tmp_path / "blm_pokerbet.db"
    side = Path(str(clean) + ".live_analytics.db")
    c = sqlite3.connect(clean)
    c.executescript("""
    CREATE TABLE clean_projections (
      id INTEGER PRIMARY KEY, source_game_id TEXT, classification TEXT,
      captured_at TEXT, period_label TEXT, quarter INTEGER, clock TEXT,
      progress_pct REAL, elapsed_game_minutes REAL,
      remaining_game_minutes REAL, current_total_points REAL,
      live_total_line REAL, actual_pts_per_min REAL,
      required_pts_per_min REAL, pace_gap REAL, status TEXT
    );
    """)
    c.commit()
    s = sqlite3.connect(side)
    s.executescript("""
    CREATE TABLE IF NOT EXISTS competition_ledger (
      source_game_id TEXT PRIMARY KEY, provider TEXT, competition TEXT,
      competition_id TEXT, source_classification TEXT, status TEXT,
      reason TEXT, resolved_at TEXT);
    CREATE TABLE IF NOT EXISTS pace_benchmark_cache (
      benchmark_key TEXT NOT NULL, cutoff_captured_at TEXT NOT NULL,
      cutoff_observation_id INTEGER, n INTEGER NOT NULL,
      mean_pace REAL, std_pace REAL, computed_at TEXT NOT NULL,
      PRIMARY KEY (benchmark_key, cutoff_captured_at, cutoff_observation_id));
    """)
    s.commit()
    m = sqlite3.connect(main)
    m.executescript("""
    CREATE TABLE games (id INTEGER PRIMARY KEY, source_game_id TEXT,
      classification TEXT, competition_slug TEXT, competition_id TEXT,
      status TEXT);
    CREATE TABLE game_results (source_game_id TEXT, final_total REAL);
    """)
    m.commit()
    s.close()
    return clean, main, c, m


def _seed_archive(c: sqlite3.Connection, comp: str, period: str,
                  prog: float, n: int, under_share: float = 0.9,
                  rem: float = 6.0, actual: float = 4.0, line: float = 200.5,
                  side: "sqlite3.Connection | None" = None,
                  m: "sqlite3.Connection | None" = None):
    """Seed n settled archival observations in ONE benchmark key."""
    fam = {"betual-nba": "BETUAL_NBA", "betual-kbl": "BETUAL_NBA",
           "cyber-basketball-2k26-matches": "CYBER_2K26"}[comp]
    prov = "CYBER" if comp.startswith("cyber") else "BETUAL"
    rows = []
    for i in range(n):
        # Competition-scoped game id: seeding TWO competitions through this
        # helper must never collide on source_game_id — a shared id space
        # would let the second seed's INSERT OR REPLACE re-map the first
        # seed's ledger rows (one competition silently swallowing the other,
        # producing a single merged cache key).  The full slug keeps every
        # fixture competition in its own id namespace.
        gid = f"{comp}-{i // 2:04d}"    # 2 observations per game
        total = 100
        elapsed = 40 * (1 - rem / 40)
        rows.append((gid, fam, f"2026-01-01T10:00:{i % 60:02d}.{i:06d}Z",
                     period, prog, elapsed, rem, total, line,
                     actual, 5.0, 5.0 - actual, "VALID"))
    c.executemany(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    c.commit()
    # ledger registration — the benchmark population join REQUIRES every
    # game to be ledger-classified (provider AND competition separate)
    if side is not None:
        gids = sorted({r[0] for r in rows})
        # Fixture hardening: a game id must never be re-seeded under a
        # DIFFERENT competition.  INSERT OR REPLACE would silently re-map
        # the earlier seed's ledger rows and corrupt the fixture, so this
        # class of bug fails loudly here instead of producing a misleading
        # test result downstream.
        for gid in gids:
            row = side.execute(
                "SELECT competition FROM competition_ledger "
                "WHERE source_game_id=?", (gid,)).fetchone()
            assert row is None or row[0] == comp, (
                f"fixture bug: game id {gid!r} already seeded for "
                f"competition {row[0]!r}; re-seeding under {comp!r} "
                f"would clobber the ledger")
        side.executemany(
            "INSERT OR REPLACE INTO competition_ledger VALUES (?,?,?,?,?,?,?,?)",
            [(gid, prov, comp, None, fam, "classified", None,
              "2026-01-01T00:00:00Z") for gid in gids])
        side.commit()
    # settlement — outcome statistics join prod.game_results.final_total;
    # under_share of the GAMES finish UNDER (final < line)
    if m is not None:
        games = sorted({r[0] for r in rows})
        n_under = int(round(len(games) * under_share))
        for i, gid in enumerate(games):
            final = line - 20.5 if i < n_under else line + 19.5
            m.execute("INSERT OR REPLACE INTO game_results "
                      "(source_game_id, final_total) VALUES (?,?)",
                      (gid, final))
        m.commit()


@pytest.fixture
def env(tmp_path):
    clean, main, c, m = _make_dbs(tmp_path)
    yield clean, main, c, m
    c.close()
    m.close()


# ── eligibility rule ────────────────────────────────────────────────────

def test_eligibility_constant_is_inclusive():
    assert ANALYTICAL_MIN_REMAINING_MINUTES == 2.5
    # boundary semantics: 2.5 included, 2.49 excluded
    assert 2.5 >= ANALYTICAL_MIN_REMAINING_MINUTES
    assert not (2.49 >= ANALYTICAL_MIN_REMAINING_MINUTES)


def test_remaining_boundary_in_engine(env):
    clean, main, c, m = env
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    # live game exactly at 2.5 → eligible; at 2.49 → excluded
    for rem, expect_match in ((2.5, True), (2.49, False), (2.0, False),
                              (6.0, True), (5.0, True), (4.0, True),
                              (3.0, True)):
        c.execute(
            """INSERT INTO clean_projections (source_game_id, classification,
               captured_at, period_label, progress_pct, elapsed_game_minutes,
               remaining_game_minutes, current_total_points, live_total_line,
               actual_pts_per_min, required_pts_per_min, pace_gap, status)
               VALUES ('LIVE1','BETUAL_NBA',?, '4th Quarter', 93.75, 36.5,
                       ?, 150, 200.5, 4.0, 5.0, 1.0, 'VALID')""",
            (f"2026-02-01T10:00:00.{int(rem*100):06d}Z", rem))
        c.commit()
        m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
                  " competition_slug, status) VALUES ('LIVE1','BETUAL_NBA',"
                  "'betual-nba','live')")
        m.execute("INSERT OR REPLACE INTO game_results (source_game_id,"
                  " final_total) VALUES ('LIVE1', 180)")
        m.commit()
        eng = HistoricalContextEngine(clean)
        ctx = eng.context_for(m, "LIVE1")
        if expect_match:
            assert ctx["status"] == "matched", (rem, ctx)
            assert ctx["eligible"] is True
        else:
            assert ctx["status"] == "no_mature_historical_context", (rem, ctx)
            assert ctx["reason"] == "below_analytical_eligibility"
        c.execute("DELETE FROM clean_projections WHERE source_game_id='LIVE1'")
        c.commit()


# ── primary state classification ────────────────────────────────────────

def test_primary_under_state_and_mirror(env):
    clean, main, c, m = env
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    # live obs: actual BELOW own avg (4.0 < 4.x from archive), required ABOVE
    c.execute(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES ('LIVE2','BETUAL_NBA','2026-02-01T10:00:00.000000Z',
                   '4th Quarter', 93.75, 36.5, 3.5, 150, 200.5,
                   3.0, 14.43, 11.43, 'VALID')""")
    c.commit()
    m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
              " competition_slug, status) VALUES ('LIVE2','BETUAL_NBA',"
              "'betual-nba','live')")
    m.commit()
    eng = HistoricalContextEngine(clean)
    eng.refresh_stats(m)
    ctx = eng.context_for(m, "LIVE2")
    assert ctx["status"] == "matched"
    assert ctx["benchmark_key"].startswith("BETUAL|betual-nba|Q4|P090")
    assert ctx["under_state"] is True
    assert ctx["mirror_over_state"] is False
    # required vs own avg positive, actual vs own avg negative
    assert ctx["required_vs_avg"] >= 0
    assert ctx["actual_vs_avg"] < 0
    # outcome stats joined via game_results: 40 archival obs, 90% UNDER —
    # served under the explicit key_hindsight_* names (the audit's AUDIT J
    # finding: key-level hindsight frequencies must never be conflated with
    # the primary-cell qualifying rates, which are served as the frozen
    # PRIMARY_* display constants)
    assert ctx["key_hindsight_under_pct"] == 90.0
    assert ctx["key_hindsight_obs"] == 40
    assert ctx["key_hindsight_equal_game_under_pct"] == 90.0
    # the qualifying (frozen whole-archive) rates travel separately
    assert ctx["qualifying_under_pct"] == 69.75
    assert ctx["qualifying_equal_game_under_pct"] == 73.02
    assert ctx["qualifying_games"] == 1515
    assert ctx["qualifying_observations"] == 12444
    assert ctx["archive_baseline_under_pct"] == 49.79
    # the two comparisons are served separately evaluated
    assert ctx["actual_below_state_mean"] is True
    assert ctx["required_ge_state_mean"] is True
    assert ctx["both_conditions_true"] is True


def test_benchmark_key_is_competition_and_state_specific(env):
    clean, main, c, m = env
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    _seed_archive(c, "betual-kbl", "4th Quarter", 93.75, 40,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    c.execute(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES ('LIVE3','BETUAL_NBA','2026-02-01T10:00:00.000000Z',
                   '4th Quarter', 93.75, 36.5, 3.5, 150, 200.5,
                   3.0, 14.43, 11.43, 'VALID')""")
    c.commit()
    m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
              " competition_slug, status) VALUES ('LIVE3','BETUAL_NBA',"
              "'betual-nba','live')")
    m.commit()
    eng = HistoricalContextEngine(clean)
    eng.refresh_stats(m)
    ctx = eng.context_for(m, "LIVE3")
    assert ctx["benchmark_key"] == "BETUAL|betual-nba|Q4|P090"
    assert ctx["competition"] == "betual-nba"
    # kbl population must not leak: keys are distinct in the cache
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    keys = {r[0] for r in side.execute(
        "SELECT benchmark_key FROM historical_context_cache")}
    assert "BETUAL|betual-nba|Q4|P090" in keys
    assert "BETUAL|betual-kbl|Q4|P090" in keys
    side.close()


# ── no-fallback states ──────────────────────────────────────────────────

def test_no_mature_context_when_population_thin(env):
    clean, main, c, m = env
    _seed_archive(c, "cyber-basketball-2k26-matches", "4th Quarter",
                  93.75, 10,                       # below N=30
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    c.execute(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES ('LIVE4','CYBER_2K26','2026-02-01T10:00:00.000000Z',
                   '4th Quarter', 93.75, 36.5, 3.5, 150, 200.5,
                   3.0, 14.43, 11.43, 'VALID')""")
    c.commit()
    m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
              " competition_slug, status) VALUES ('LIVE4','CYBER_2K26',"
              "'cyber-basketball-2k26-matches','live')")
    m.commit()
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "LIVE4")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx["reason"] == "insufficient_mature_population"
    # NO fabricated stats
    assert "key_hindsight_under_pct" not in ctx
    assert "qualifying_under_pct" not in ctx


def test_no_context_when_competition_unresolved(env):
    clean, main, c, m = env
    c.execute(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES ('LIVE5','BETUAL_NBA','2026-02-01T10:00:00.000000Z',
                   '4th Quarter', 93.75, 36.5, 3.5, 150, 200.5,
                   3.0, 14.43, 11.43, 'VALID')""")
    c.commit()
    # game NOT registered in the ledger path (no games row)
    m.commit()
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "LIVE5")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx["reason"] in ("competition_unresolved", "no_clean_observation")


def test_no_context_when_no_clean_observation(env):
    clean, main, c, m = env
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "MISSING")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx["reason"] == "no_clean_observation"


# ── the two conditions independently exposed (frontend contract) ─────────

def _insert_live(c, m, gid, act, req, rem=3.5):
    c.execute(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES (?,'BETUAL_NBA','2026-02-01T10:00:00.000000Z',
                   '4th Quarter', 93.75, 36.5, ?, 150, 200.5,
                   ?, ?, ?, 'VALID')""",
        (gid, rem, act, req, (req - act) if (req is not None and act is not None)
         else None))
    c.commit()
    m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
              " competition_slug, status) VALUES (?,'BETUAL_NBA',"
              "'betual-nba','live')", (gid,))
    m.commit()


def test_two_conditions_are_exposed_independently(env):
    """Each relationship must be served as its OWN boolean: every quadrant
    of the 2x2 (actual-vs-mean, required-vs-mean) must be reachable, and
    ``both_conditions_true`` must be exactly their conjunction.  The UI
    reads these flags verbatim — it never re-derives them."""
    clean, main, c, m = env
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    # archive key mean = 4.0 (all 40 seeded observations carry actual=4.0)
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40, actual=4.0,
                  side=side, m=m)
    cases = {
        "LIVE_TT": (3.0, 5.0, True, True),
        "LIVE_TF": (3.0, 3.0, True, False),
        "LIVE_FT": (5.0, 5.0, False, True),
        "LIVE_FF": (5.0, 3.0, False, False),
    }
    for gid, (act, req, _a, _r) in cases.items():
        _insert_live(c, m, gid, act, req)
    eng = HistoricalContextEngine(clean)
    for gid, (act, req, a_below, r_ge) in cases.items():
        ctx = eng.context_for(m, gid)
        assert ctx["status"] == "matched", (gid, ctx)
        assert ctx["historical_avg_pace"] == 4.0, (gid, ctx)
        # the two comparisons are served SEPARATELY (never collapsed)
        assert ctx["actual_below_state_mean"] is a_below, (gid, ctx)
        assert ctx["required_ge_state_mean"] is r_ge, (gid, ctx)
        # ... and the combined flag is exactly their conjunction
        assert ctx["both_conditions_true"] is (a_below and r_ge), (gid, ctx)
        assert ctx["under_state"] is (a_below and r_ge), (gid, ctx)


def test_benchmark_identity_is_served_for_the_frontend(env):
    """The UI must show PROVIDER / COMPETITION / PERIOD / PROGRESS / STATE /
    HISTORICAL N — every one of them travels in the payload."""
    clean, main, c, m = env
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    _insert_live(c, m, "LIVE_ID", 3.0, 5.0)
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "LIVE_ID")
    assert ctx["status"] == "matched"
    assert ctx["provider"] == "BETUAL"
    assert ctx["competition"] == "betual-nba"
    assert ctx["period"] == "4th Quarter"
    assert ctx["progress_pct"] == 93.75
    assert ctx["state"] == "P090"
    assert ctx["benchmark_key"] == "BETUAL|betual-nba|Q4|P090"
    assert ctx["benchmark_n"] >= 30


def test_missing_actual_pace_never_raises_and_never_classifies_true(env):
    """A clean row can carry a live line and a resolvable clock yet a
    missing actual pace.  Such a row must classify as an explicit
    non-TRUE state — it must never raise inside the engine."""
    clean, main, c, m = env
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40, actual=4.0,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    c.execute(
        """INSERT INTO clean_projections (source_game_id, classification,
           captured_at, period_label, progress_pct, elapsed_game_minutes,
           remaining_game_minutes, current_total_points, live_total_line,
           actual_pts_per_min, required_pts_per_min, pace_gap, status)
           VALUES ('LIVE_NA','BETUAL_NBA','2026-02-01T10:00:00.000000Z',
                   '4th Quarter', 93.75, 36.5, 3.5, 150, 200.5,
                   NULL, 5.0, NULL, 'VALID')""")
    c.commit()
    m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
              " competition_slug, status) VALUES ('LIVE_NA','BETUAL_NBA',"
              "'betual-nba','live')")
    m.commit()
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "LIVE_NA")     # must not raise
    assert ctx["status"] == "matched"
    assert ctx["actual_below_state_mean"] is False
    assert ctx["both_conditions_true"] is False
    assert ctx["actual_vs_avg"] is None
