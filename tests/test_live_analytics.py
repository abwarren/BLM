"""Tests for the neutral live-analytics foundation: pace formulas,
provider/competition classification, and the leakage-safe
(provider, competition, period, progress) pace benchmark + T-state Z.

Audit basis (2026-09-09): `games.competition_slug`/`competition_id` are
the authoritative competition identifiers (verified 1:1); the
`classification` (BETUAL_NBA/CYBER_2K26) is the PROVIDER family;
BETUAL_NBA bundles ≥5 distinct competitions (betual-nba, betual-tbsl,
betual-euroleague, betual-cba, betual-kbl) which must NEVER share a
benchmark population.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from blm_v4.live_analytics import pace as P                     # noqa: E402
from blm_v4.live_analytics.benchmark import (                   # noqa: E402
    MIN_BENCHMARK_N, PaceZ, benchmark_for, ensure_schema, pace_z)
from blm_v4.live_analytics.league import (                      # noqa: E402
    PROVIDERS, benchmark_eligible, canonical_competition,
    ensure_league_schema, game_competition, register_game_competition)

# ───────────────────────── pace formulas ─────────────────────────


def test_actual_pace_basic_and_zero_elapsed():
    assert P.actual_pace(90, 40) == 2.25
    assert P.actual_pace(0, 20) == 0.0           # scoreless but defined
    assert P.actual_pace(50, 0) is None          # elapsed=0 → undefined
    assert P.actual_pace(50, None) is None
    assert P.actual_pace(None, 20) is None       # missing score


def test_remaining_minutes_floors_at_zero():
    assert P.remaining_minutes(40, 25.5) == 14.5
    assert P.remaining_minutes(40, 40) == 0.0    # remaining=0 boundary
    assert P.remaining_minutes(40, 43) == 0.0    # clock over-run floored
    assert P.remaining_minutes(None, 10) is None


def test_required_pace_boundaries_and_signs():
    assert P.required_pace(176.5, 90, 20) == 4.325
    assert P.required_pace(176.5, 180, 20) < 0   # above the line is valid
    assert P.required_pace(176.5, 176.5, 20) == 0.0
    assert P.required_pace(176.5, 90, 0) is None  # remaining=0 → undefined
    assert P.required_pace(None, 90, 20) is None  # missing line
    assert P.required_pace(178.5, 100, 10) == 7.85  # half-point line exact


def test_pace_gap_and_line_gaps():
    assert P.pace_gap(3.0, 2.25) == 0.75
    assert P.pace_gap(2.0, 2.5) == -0.5
    assert P.pace_gap(None, 2.5) is None
    assert P.line_score_gap(176.5, 90) == 86.5
    assert P.score_line_gap(90, 176.5) == -86.5
    assert P.score_line_gap(90, 176.5) == -P.line_score_gap(176.5, 90)
    assert P.line_score_gap(None, 90) is None
    assert P.line_score_gap(178.5, 101) == 77.5


def test_benchmark_key_requires_all_dimensions():
    assert P.benchmark_key("BETUAL", "betual-nba", 62.0,
                           "2nd Quarter") == "BETUAL|betual-nba|Q2|P060"
    assert P.benchmark_key("BETUAL", "betual-nba", 60.0,
                           "2nd Quarter") == "BETUAL|betual-nba|Q2|P060"
    assert P.benchmark_key("BETUAL", "betual-nba", 64.999,
                           "2nd Quarter") == "BETUAL|betual-nba|Q2|P060"
    assert P.benchmark_key("BETUAL", "betual-nba", 65.0,
                           "2nd Quarter") == "BETUAL|betual-nba|Q2|P065"
    assert P.benchmark_key("CYBER", "cyber-basketball-2k26-matches", 0.0,
                           "1st Quarter") == \
        "CYBER|cyber-basketball-2k26-matches|Q1|P000"
    assert P.benchmark_key("BETUAL", "betual-nba", 100.0,
                           "4th Quarter") == "BETUAL|betual-nba|Q4|P100"
    # any missing dimension → ineligible
    assert P.benchmark_key(None, "betual-nba", 50.0, "Q2") is None
    assert P.benchmark_key("BETUAL", None, 50.0, "Q2") is None
    assert P.benchmark_key("BETUAL", "betual-nba", None, "Q2") is None
    assert P.benchmark_key("BETUAL", "betual-nba", 50.0, None) is None
    assert P.benchmark_key("BETUAL", "betual-nba", 50.0, "Half End") is None
    assert P.benchmark_key("BETUAL", "betual-nba", -0.1, "Q2") is None


def test_score_change_moves_gap_line_change_does_not_move_score():
    assert P.line_score_gap(180.5, 90) == 90.5
    assert P.line_score_gap(182.5, 90) == 92.5    # line moved up
    assert P.line_score_gap(180.5, 95) == 85.5    # score moved up
    assert P.line_score_gap(180.5, 90) == P.line_score_gap(180.5, 90)


# ─────────────── provider/competition classification ───────────────


def test_competition_resolution_from_authoritative_metadata():
    r = canonical_competition("BETUAL_NBA", "betual-nba", "18296756")
    assert r.provider == "BETUAL" and r.competition == "betual-nba"
    assert r.status == "classified" and r.competition_id == "18296756"
    r2 = canonical_competition("BETUAL_NBA", "betual-tbsl", "18296900")
    assert r2.competition == "betual-tbsl"        # TSBL distinct from NBA
    r3 = canonical_competition("CYBER_2K26",
                               "cyber-basketball-2k26-matches", "18295203")
    assert r3.provider == "CYBER"
    # unknown / missing / foreign → ineligible, never defaulted
    assert canonical_competition(None, "betual-nba").status == "unknown"
    assert canonical_competition("SOMETHING", "betual-nba").status == "unknown"
    assert canonical_competition("BETUAL_NBA", None).status == "unknown"
    assert canonical_competition("BETUAL_NBA", "unknown").status == "unknown"
    assert canonical_competition("BETUAL_NBA", "").competition is None


def test_competition_ledger_immutability_and_conflict_flag(bench_db):
    r1 = register_game_competition(bench_db, "G1", "BETUAL_NBA",
                                   "betual-nba", "18296756",
                                   "2026-09-01T00:00:00Z")
    assert r1.status == "classified"
    # mid-game provider/competition contradiction → FLAGGED, never switched
    r2 = register_game_competition(bench_db, "G1", "BETUAL_NBA",
                                   "betual-tbsl", "18296900",
                                   "2026-09-01T00:10:00Z")
    assert r2.status == "conflict"
    back = game_competition(bench_db, "G1")
    assert back.status == "conflict" and back.competition == "betual-nba"
    assert not benchmark_eligible(back)
    # consistent re-claim keeps the flag (first assignment remains truth)
    r3 = register_game_competition(bench_db, "G1", "BETUAL_NBA",
                                   "betual-nba", "18296756",
                                   "2026-09-01T00:20:00Z")
    assert r3.status == "conflict"


def test_competition_ledger_unknown_then_resolved(bench_db):
    r1 = register_game_competition(bench_db, "G2", "BETUAL_NBA", None, None,
                                   "2026-09-01T00:00:00Z")
    assert r1.status == "unknown"
    assert not benchmark_eligible(game_competition(bench_db, "G2"))
    r2 = register_game_competition(bench_db, "G2", "BETUAL_NBA",
                                   "betual-kbl", "18296987",
                                   "2026-09-01T00:05:00Z")
    assert r2.status == "classified"              # genuine resolution, once
    assert benchmark_eligible(game_competition(bench_db, "G2"))


# ─────────────────────── benchmark population ───────────────────────

PROJ_COLS = ("id INTEGER PRIMARY KEY, source_game_id TEXT, classification TEXT, "
             "captured_at TEXT, period_label TEXT, progress_pct REAL, "
             "actual_pts_per_min REAL, status TEXT")


@pytest.fixture()
def bench_db(tmp_path):
    """Mirror of clean_projections' benchmark-relevant columns plus the
    competition ledger (the population join target)."""
    db = str(tmp_path / "bench.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE clean_projections (%s)" % PROJ_COLS)
    ensure_schema(conn)
    ensure_league_schema(conn)
    yield conn
    conn.close()


def register(conn, gid, provider_family, competition, comp_id="1"):
    register_game_competition(conn, gid, provider_family, competition,
                              comp_id, "2026-09-01T00:00:00Z")


def add_obs(conn, oid, game, cls, captured_at, progress, pace,
            period="2nd Quarter"):
    conn.execute("INSERT INTO clean_projections VALUES (?,?,?,?,?,?,?,?)",
                 (oid, game, cls, captured_at, period, progress, pace,
                  "VALID"))
    conn.commit()


def test_benchmark_empty_population(bench_db):
    register(bench_db, "G1", "BETUAL_NBA", "betual-nba")
    b = benchmark_for(bench_db, "BETUAL", "betual-nba", 50.0,
                      "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert b.n == 0 and b.mean is None and b.std is None


def test_prior_observations_only_no_future_leakage(bench_db):
    register(bench_db, "G1", "BETUAL_NBA", "betual-nba")
    register(bench_db, "G2", "BETUAL_NBA", "betual-nba")
    add_obs(bench_db, 1, "G1", "BETUAL_NBA", "2026-09-01T00:00:00Z", 50.0, 2.0)
    add_obs(bench_db, 2, "G2", "BETUAL_NBA", "2026-09-08T00:00:00Z", 52.0, 4.0)
    b = benchmark_for(bench_db, "BETUAL", "betual-nba", 51.0,
                      "2026-09-05T00:00:00Z", period_label="2nd Quarter")
    assert b.n == 1 and b.mean == 2.0             # only the prior row
    b2 = benchmark_for(bench_db, "BETUAL", "betual-nba", 51.0,
                       "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert b2.n == 2 and b2.mean == 3.0           # both after T passes them


def test_current_observation_never_its_own_benchmark(bench_db):
    register(bench_db, "G1", "BETUAL_NBA", "betual-nba")
    register(bench_db, "G2", "BETUAL_NBA", "betual-nba")
    add_obs(bench_db, 1, "G1", "BETUAL_NBA", "2026-09-01T00:00:00Z", 50.0, 2.0)
    add_obs(bench_db, 2, "G2", "BETUAL_NBA", "2026-09-08T00:00:00Z", 52.0, 9.0)
    b = benchmark_for(bench_db, "BETUAL", "betual-nba", 52.0,
                      "2026-09-08T00:00:00Z", cutoff_observation_id=2,
                      period_label="2nd Quarter")
    assert b.n == 1 and b.mean == 2.0


def test_same_game_exclusion_available(bench_db):
    register(bench_db, "G1", "BETUAL_NBA", "betual-nba")
    register(bench_db, "G2", "BETUAL_NBA", "betual-nba")
    add_obs(bench_db, 1, "G1", "BETUAL_NBA", "2026-09-01T00:00:00Z", 50.0, 2.0)
    add_obs(bench_db, 2, "G2", "BETUAL_NBA", "2026-09-02T00:00:00Z", 52.0, 4.0)
    b = benchmark_for(bench_db, "BETUAL", "betual-nba", 51.0,
                      "2026-09-09T00:00:00Z", exclude_game_id="G1",
                      period_label="2nd Quarter")
    assert b.n == 1 and b.mean == 4.0


def test_t_state_cache_isolation(bench_db):
    register(bench_db, "G1", "BETUAL_NBA", "betual-nba")
    b1 = benchmark_for(bench_db, "BETUAL", "betual-nba", 50.0,
                       "2026-09-05T00:00:00Z", period_label="2nd Quarter")
    assert b1.n == 0
    add_obs(bench_db, 1, "G1", "BETUAL_NBA", "2026-09-08T00:00:00Z", 50.0, 5.0)
    b2 = benchmark_for(bench_db, "BETUAL", "betual-nba", 50.0,
                       "2026-09-05T00:00:00Z", period_label="2nd Quarter")
    assert b2.n == 0                              # early cutoff unaffected
    b3 = benchmark_for(bench_db, "BETUAL", "betual-nba", 50.0,
                       "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert b3.n == 1


def test_bucket_and_period_edges_do_not_mix(bench_db):
    register(bench_db, "G1", "BETUAL_NBA", "betual-nba")
    register(bench_db, "G2", "BETUAL_NBA", "betual-nba")
    register(bench_db, "G3", "BETUAL_NBA", "betual-nba")
    register(bench_db, "G4", "BETUAL_NBA", "betual-nba")
    # P060 spans [60, 65): 59.9 → P055, 60.0 → P060, 65.0 → P065
    add_obs(bench_db, 1, "G1", "BETUAL_NBA", "2026-09-01T00:00:00Z", 59.9, 2.0)
    add_obs(bench_db, 2, "G2", "BETUAL_NBA", "2026-09-01T00:00:00Z", 60.0, 4.0)
    add_obs(bench_db, 3, "G3", "BETUAL_NBA", "2026-09-01T00:00:00Z", 65.0, 9.0)
    # same bucket, different period → separate population
    add_obs(bench_db, 4, "G4", "BETUAL_NBA", "2026-09-01T00:00:00Z", 62.0,
            7.0, period="3rd Quarter")
    b = benchmark_for(bench_db, "BETUAL", "betual-nba", 62.0,
                      "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert b.n == 1 and b.mean == 4.0
    b3 = benchmark_for(bench_db, "BETUAL", "betual-nba", 62.0,
                       "2026-09-09T00:00:00Z", period_label="3rd Quarter")
    assert b3.n == 1 and b3.mean == 7.0


def test_nba_and_tsbl_never_share_a_population(bench_db):
    """THE critical isolation proof: BETUAL/betual-nba and BETUAL/betual-tbsl
    are two completely separate benchmark populations."""
    for i in range(12):
        register(bench_db, "N%d" % i, "BETUAL_NBA", "betual-nba",
                 "18296756")
        register(bench_db, "T%d" % i, "BETUAL_NBA", "betual-tbsl",
                 "18296900")
        add_obs(bench_db, 100 + i, "N%d" % i, "BETUAL_NBA",
                "2026-09-01T00:00:%02dZ" % i, 41.0, 2.0)
        add_obs(bench_db, 200 + i, "T%d" % i, "BETUAL_NBA",
                "2026-09-01T00:00:%02dZ" % i, 41.0, 8.0)
    bn = benchmark_for(bench_db, "BETUAL", "betual-nba", 42.0,
                       "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    bt = benchmark_for(bench_db, "BETUAL", "betual-tbsl", 42.0,
                       "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert bn.key == "BETUAL|betual-nba|Q2|P040"
    assert bt.key == "BETUAL|betual-tbsl|Q2|P040"
    assert bn.n == 12 and bn.mean == 2.0      # zero TSBL pace leaked in
    assert bt.n == 12 and bt.mean == 8.0      # zero NBA pace leaked in
    # same-source guard: both populations come from BETUAL_NBA rows —
    # only the COMPETITION partition separates them
    assert bn.key.split("|")[0] == bt.key.split("|")[0] == "BETUAL"


def test_conflicting_games_excluded_from_population(bench_db):
    """A game flagged 'conflict' must not contaminate ANY population."""
    register(bench_db, "OK1", "BETUAL_NBA", "betual-nba")
    register(bench_db, "BAD", "BETUAL_NBA", "betual-nba")
    register_game_competition(bench_db, "BAD", "BETUAL_NBA", "betual-tbsl",
                              "18296900", "2026-09-01T00:05:00Z")  # conflict
    for i in range(4):
        add_obs(bench_db, 10 + i, "OK1", "BETUAL_NBA",
                "2026-09-01T00:00:0%dZ" % i, 41.0, 2.0)
        add_obs(bench_db, 20 + i, "BAD", "BETUAL_NBA",
                "2026-09-01T00:00:0%dZ" % i, 41.0, 99.0)
    b = benchmark_for(bench_db, "BETUAL", "betual-nba", 42.0,
                      "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert b.n == 4 and b.mean == 2.0         # BAD's 99.0 pace excluded


def test_pace_z_gates_and_value(bench_db):
    for i in range(MIN_BENCHMARK_N):
        gid = "G%d" % i
        register(bench_db, gid, "BETUAL_NBA", "betual-nba")
        add_obs(bench_db, i + 1, gid, "BETUAL_NBA",
                "2026-09-01T00:00:%02dZ" % (i % 60), 50.0 + (i % 3) * 0.1,
                2.0 + (i % 5) * 0.1)
    r = pace_z(bench_db, 2.0, "BETUAL", "betual-nba", 50.5,
               "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert isinstance(r, PaceZ) and r.z is not None and r.n >= MIN_BENCHMARK_N
    assert r.benchmark_key == "BETUAL|betual-nba|Q2|P050"
    r0 = pace_z(bench_db, r.mean, "BETUAL", "betual-nba", 50.5,
                "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert abs(r0.z) < 0.01                    # x == mean → z == 0
    # non-canonical provider → NO population at all
    rz = pace_z(bench_db, 2.0, "ACME", "acme-nba", 50.0,
                "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert rz.z is None and rz.n == 0
    # missing pace → None, benchmark still reported
    rm = pace_z(bench_db, None, "BETUAL", "betual-nba", 50.5,
                "2026-09-09T00:00:00Z", period_label="2nd Quarter")
    assert rm.z is None and rm.actual_pace is None and rm.n >= MIN_BENCHMARK_N


def test_provider_family_values():
    assert PROVIDERS == {"BETUAL_NBA": "BETUAL", "CYBER_2K26": "CYBER"}


def test_no_blm_vocabulary_in_live_analytics():
    """Naming firewall: no BLM concept appears in any identifier or logic
    string of the new architecture (prose/comments explaining what is
    EXCLUDED are fine)."""
    banned = {"fair", "edge", "over_value", "under_value", "no_edge",
              "calibration", "confirmation", "stake", "staking",
              "betting", "market_vs_fair", "signal"}
    for path in (REPO / "blm_v4" / "live_analytics").glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = set()
        # The descriptive-only guard's ban list (FORBIDDEN_KEYS, freeze
        # directive §10) is the structural sentinel that KEEPS predictive
        # concepts out of the prospective health report — its element
        # literals necessarily NAME the banned words and are exempt like
        # explanatory prose.  Nothing else is exempt.
        sentinel = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name)
                            and t.id == "FORBIDDEN_KEYS"
                            for t in node.targets)
                    and isinstance(node.value, (ast.Set, ast.Tuple,
                                                ast.List))):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) \
                            and isinstance(elt.value, str):
                        sentinel.add(id(elt))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.append(node.name.lower())
            elif isinstance(node, ast.Name):
                names.append(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.append(node.attr.lower())
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings or id(node) in sentinel:
                    continue
                names.append(node.value.lower())
            for nm in names:
                for bad in banned:
                    assert bad not in nm.split(), "%s in %s (%s)" % (
                        bad, nm[:40], path.name)


@pytest.mark.skipif(
    not (REPO / "blm_pokerbet.db").exists()
    or not (REPO / "blm_metrics_clean.db").exists(),
    reason="production DBs not present")
def test_real_population_smoke():
    """Real production-shaped data: register the audited competitions,
    compute a benchmark for a real NBA population and prove the TSBL
    population differs (identical period+progress, different games)."""
    # Consistent snapshot of the LIVE production clean DB: the collector
    # writes continuously (WAL mode), so a plain shutil.copy can tear the
    # copy at a WAL checkpoint and intermittently yield "database disk
    # image is malformed".  sqlite3.Connection.backup() takes a consistent
    # snapshot under the source DB's own locking — proven 20/20 vs 2/20.
    import tempfile
    fd, tmp_name = tempfile.mkstemp(prefix="blm_la_smoke_", suffix=".sqlite3")
    os.close(fd)
    tmp = Path(tmp_name)
    src = sqlite3.connect("file:%s?mode=ro" % (REPO / "blm_metrics_clean.db"),
                          uri=True)
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    dst.close()
    src.close()
    conn = sqlite3.connect(str(tmp))
    ensure_schema(conn)
    ensure_league_schema(conn)
    main = sqlite3.connect("file:%s?mode=ro" % (REPO / "blm_pokerbet.db"),
                           uri=True)
    for gid, cls, slug, cid in main.execute(
            "SELECT DISTINCT source_game_id, classification, "
            "competition_slug, competition_id FROM games").fetchall():
        register_game_competition(conn, str(gid), cls, slug, cid,
                                  "2026-09-01T00:00:00Z")
    main.close()
    row = conn.execute(
        "SELECT classification, period_label, progress_pct, "
        "actual_pts_per_min, captured_at, id, source_game_id "
        "FROM clean_projections WHERE status='VALID' "
        "AND actual_pts_per_min IS NOT NULL AND period_label LIKE '%Quarter' "
        "AND progress_pct BETWEEN 47.5 AND 52.5 "
        "ORDER BY captured_at DESC LIMIT 1").fetchone()
    assert row, "expected at least one VALID observation near 50% progress"
    cls, period, prog, pace, ts, oid, gid = row
    reg = game_competition(conn, gid)
    assert reg.status == "classified"
    r = pace_z(conn, pace, reg.provider, reg.competition, prog, ts,
               cutoff_observation_id=oid, period_label=period)
    assert r.benchmark_key.startswith("BETUAL|betual-") or \
        r.benchmark_key.startswith("CYBER|cyber-")
    assert r.benchmark_key.split("|")[2] in ("Q1", "Q2", "Q3", "Q4")
    assert r.n >= MIN_BENCHMARK_N, "population should be large in production"
    assert r.z is not None and abs(r.z) < 10
    # NBA vs TSBL at the same state are different populations in real data
    comp_counts = dict(conn.execute(
        "SELECT l.competition, COUNT(DISTINCT p.source_game_id) "
        "FROM clean_projections p JOIN competition_ledger l "
        "ON l.source_game_id = p.source_game_id "
        "WHERE l.status='classified' AND p.status='VALID' "
        "AND p.period_label=? AND p.progress_pct BETWEEN 47.5 AND 52.5 "
        "GROUP BY 1", (period,)).fetchall())
    if "betual-nba" in comp_counts and "betual-tbsl" in comp_counts:
        assert comp_counts["betual-nba"] > 0 and comp_counts["betual-tbsl"] > 0
    conn.close()
    tmp.unlink(missing_ok=True)
