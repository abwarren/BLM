"""RELATIVE-PACE / HISTORICAL STATE MEAN benchmark — its OWN N.

The relative-pace state mean and the Z-score benchmark are separate
calculations.  They share ONE population definition
(``benchmark.benchmark_for``: same canonical key, strictly prior
observations, the observation's own row excluded, same-game observations
excluded) but each resolves at its OWN cutoff observation row, so the two
N's are separate values that must be exposed separately and never assumed
equal.

``state_mean_n`` is the N of the relative-pace computation ONLY.  These
tests prove it is the real population count under the documented rules —
never copied from the Z-score payload, never fabricated when the population
is immature.
"""
from __future__ import annotations

import sqlite3

import pytest

from blm_v4.live_analytics.historical_context import HistoricalContextEngine
from blm_v4.live_analytics.service import pace_z_payload
from tests.test_historical_context import _make_dbs, _seed_archive

PERIOD = "4th Quarter"
PROG = 93.75            # -> P090
P_LO, P_HI = 90.0, 95.0


def _live_row(c, gid, act, req, captured, prog=PROG, period=PERIOD,
              rem=3.5, cls="BETUAL_NBA"):
    c.execute(
        "INSERT INTO clean_projections (source_game_id, classification,"
        " captured_at, period_label, progress_pct, elapsed_game_minutes,"
        " remaining_game_minutes, current_total_points, live_total_line,"
        " actual_pts_per_min, required_pts_per_min, pace_gap, status)"
        " VALUES (?,?,?,?,?,?,?,150,200.5,?,?,?,'VALID')",
        (gid, cls, captured, period, prog, 40 - rem, rem, act, req,
         (req - act) if (act is not None and req is not None) else None))
    c.commit()


def _live_game(m, gid, slug="betual-nba", cls="BETUAL_NBA"):
    m.execute("INSERT OR REPLACE INTO games (source_game_id, classification,"
              " competition_slug, status) VALUES (?,?,?,'live')",
              (gid, cls, slug))
    m.commit()


def _ledger(clean, gids, comp="betual-nba"):
    """Register game ids as ledger-classified — the benchmark population
    join requires it, exactly as the live engine does."""
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    try:
        fam = "BETUAL_NBA" if comp.startswith("betual") else "CYBER_2K26"
        prov = "BETUAL" if comp.startswith("betual") else "CYBER"
        side.executemany(
            "INSERT OR REPLACE INTO competition_ledger VALUES "
            "(?,?,?,NULL,?,'classified',NULL,'2026-01-01T00:00:00Z')",
            [(g, prov, comp, fam) for g in gids])
        side.commit()
    finally:
        side.close()


def _independent_n(clean, gid, comp, cutoff, own_id, period=PERIOD,
                   lo=P_LO, hi=P_HI):
    """A SECOND, independent implementation of the population count.  Used
    only to cross-check the engine — the benchmark itself is untouched."""
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    try:
        side.execute("ATTACH DATABASE 'file:%s?mode=ro' AS clean" % clean)
        fam = "BETUAL_NBA" if comp.startswith("betual") else "CYBER_2K26"
        prov = "BETUAL" if comp.startswith("betual") else "CYBER"
        q = ("SELECT COUNT(*) FROM clean.clean_projections p "
             "JOIN competition_ledger l ON l.source_game_id=p.source_game_id "
             "WHERE p.status='VALID' AND p.actual_pts_per_min IS NOT NULL "
             "AND p.classification=? AND l.provider=? AND l.competition=? "
             "AND l.status='classified' AND p.period_label=? "
             "AND p.progress_pct>=? AND p.progress_pct<? "
             "AND p.captured_at < ? AND p.id != ? AND p.source_game_id != ?")
        return side.execute(q, (fam, prov, comp, period, lo, hi,
                                cutoff, own_id, gid)).fetchone()[0]
    finally:
        side.close()


@pytest.fixture
def base_env(tmp_path):
    """40 settled archive observations in ONE benchmark key (BETUAL NBA Q4
    P090, mean actual = 4.0), fully ledger-classified."""
    clean, main, c, m = _make_dbs(tmp_path)
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    _seed_archive(c, "betual-nba", PERIOD, PROG, 40, actual=4.0,
                  side=side, m=m)
    side.close()
    yield clean, main, c, m
    c.close()
    m.close()


# ── (a) the relative-pace N is present and is the real population count ──

def test_state_mean_fields_present_and_correct(base_env):
    clean, main, c, m = base_env
    _live_row(c, "SM1", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM1")
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM1")

    assert ctx["status"] == "matched"
    # the explicit contract fields exist
    for k in ("state_mean_provider", "state_mean_competition",
              "state_mean_period", "state_mean_state", "state_mean_key",
              "state_mean_n", "state_mean_pace"):
        assert k in ctx, k
    assert ctx["state_mean_provider"] == "BETUAL"
    assert ctx["state_mean_competition"] == "betual-nba"
    assert ctx["state_mean_period"] == "Q4"
    assert ctx["state_mean_state"] == "P090"
    assert ctx["state_mean_key"] == "BETUAL|betual-nba|Q4|P090"
    # documented aliases agree (one computation, two names)
    assert ctx["state_mean_n"] == ctx["benchmark_n"]
    assert ctx["state_mean_pace"] == ctx["historical_avg_pace"]
    assert ctx["state_mean_key"] == ctx["benchmark_key"]

    # the N is the ACTUAL population of this computation — cross-checked
    # against an independent count under the same documented rules
    own_id = c.execute(
        "SELECT id FROM clean_projections WHERE source_game_id='SM1' "
        "ORDER BY captured_at DESC, id DESC LIMIT 1").fetchone()[0]
    assert ctx["state_mean_n"] == _independent_n(
        clean, "SM1", "betual-nba", "2026-02-01T10:00:00.000000Z", own_id)
    assert ctx["state_mean_n"] == 40
    assert ctx["state_mean_pace"] == 4.0


# ── (e) the observation's OWN row is never its own population ───────────

def test_own_observation_row_is_excluded(base_env):
    clean, main, c, m = base_env
    _live_row(c, "SM2", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM2")
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM2")
    own_id = c.execute(
        "SELECT id FROM clean_projections WHERE source_game_id='SM2' "
        "ORDER BY captured_at DESC, id DESC LIMIT 1").fetchone()[0]
    # a row for ANOTHER game captured at exactly the cutoff is excluded too
    # (the bound is STRICT: captured_at < T)
    _live_row(c, "SM2B", 4.0, 5.0, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM2B")
    ctx2 = eng.context_for(m, "SM2")
    assert ctx["state_mean_n"] == ctx2["state_mean_n"] == 40, (
        ctx["state_mean_n"], ctx2["state_mean_n"])
    # and the own id is never counted
    assert ctx["state_mean_n"] == _independent_n(
        clean, "SM2", "betual-nba", "2026-02-01T10:00:00.000000Z", own_id)


# ── (f) future observations are excluded ───────────────────────────────

def test_future_observations_are_excluded(base_env):
    clean, main, c, m = base_env
    _live_row(c, "SM3", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM3")
    eng = HistoricalContextEngine(clean)
    before = eng.context_for(m, "SM3")
    # 25 observations in the SAME key captured AFTER the cutoff must not
    # enter this observation's benchmark
    for i in range(25):
        _live_row(c, "SM3FUT-%02d" % i, 4.0, 5.0,
                  "2026-06-01T10:%02d:00.000000Z" % i)
        _live_game(m, "SM3FUT-%02d" % i)
    after = eng.context_for(m, "SM3")
    assert after["state_mean_n"] == before["state_mean_n"] == 40


# ── (g) same-game observations are excluded ────────────────────────────

def test_same_game_observations_are_excluded(base_env):
    clean, main, c, m = base_env
    _live_game(m, "SM4")
    # 12 PRIOR observations of the SAME game in the same key — they carry
    # the game's own information and must not define its benchmark
    for i in range(12):
        _live_row(c, "SM4", 4.0, 5.0,
                  "2026-01-15T10:%02d:00.000000Z" % i)
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    side.execute("INSERT OR REPLACE INTO competition_ledger VALUES "
                 "(?,'BETUAL','betual-nba',NULL,'BETUAL_NBA','classified',"
                 "NULL,'2026-01-01T00:00:00Z')", ("SM4",))
    side.commit()
    side.close()
    # the cutoff row
    _live_row(c, "SM4", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM4")
    own_id = c.execute(
        "SELECT id FROM clean_projections WHERE source_game_id='SM4' "
        "ORDER BY captured_at DESC, id DESC LIMIT 1").fetchone()[0]
    assert ctx["state_mean_n"] == 40, ctx["state_mean_n"]
    assert ctx["state_mean_n"] == _independent_n(
        clean, "SM4", "betual-nba", "2026-02-01T10:00:00.000000Z", own_id)


# ── (b) the two N's are SEPARATE — proven where the populations differ ──

def test_relative_pace_n_differs_from_z_n_when_populations_differ(base_env):
    """A game whose latest VALID observation carries NO actual pace: the
    Z-score path finds no population of its own (n=0, no benchmark) while
    the relative-pace path still resolves its benchmark.  If state_mean_n
    were copied from the Z payload it would be 0 here — it is not."""
    clean, main, c, m = base_env
    _live_row(c, "SM5", None, 4.5, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM5")
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM5")
    pz = pace_z_payload(m, clean, "SM5")
    assert ctx["status"] == "matched"
    assert ctx["state_mean_n"] == 40
    assert ctx["state_mean_pace"] == 4.0
    # the Z-score benchmark for the SAME game is a different population
    assert pz["n"] == 0 and pz["z"] is None
    assert ctx["state_mean_n"] != pz["n"]


def test_each_path_resolves_its_own_cutoff_row(base_env):
    """The relative-pace path uses the LATEST VALID observation; the Z path
    requires a non-null actual pace and so falls back to an EARLIER row.
    The two therefore legitimately produce different cutoffs (and N's)."""
    clean, main, c, m = base_env
    _live_game(m, "SM6")
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    side.execute("INSERT OR REPLACE INTO competition_ledger VALUES "
                 "(?,'BETUAL','betual-nba',NULL,'BETUAL_NBA','classified',"
                 "NULL,'2026-01-01T00:00:00Z')", ("SM6",))
    side.commit()
    side.close()
    # Z picks the EARLIER actual-carrying row (P090, 12:00)
    _live_row(c, "SM6", 3.0, 5.0, "2026-02-01T12:00:00.000000Z")
    # ctx picks the LATER VALID row (P090, 12:05) which has no actual pace
    _live_row(c, "SM6", None, 4.5, "2026-02-01T12:05:00.000000Z")
    # 6 further archive observations land between the two cutoffs
    mids = ["SM6MID-%d" % i for i in range(6)]
    for i, gid in enumerate(mids):
        _live_row(c, gid, 4.0, 5.0, "2026-02-01T12:0%d:00.000000Z" % i)
        _live_game(m, gid)
    _ledger(clean, mids)
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM6")
    pz = pace_z_payload(m, clean, "SM6")
    z_row = c.execute(
        "SELECT id, captured_at FROM clean_projections "
        "WHERE source_game_id='SM6' AND actual_pts_per_min IS NOT NULL "
        "ORDER BY captured_at DESC, id DESC LIMIT 1").fetchone()
    ctx_row = c.execute(
        "SELECT id, captured_at FROM clean_projections "
        "WHERE source_game_id='SM6' ORDER BY captured_at DESC, id DESC "
        "LIMIT 1").fetchone()
    assert z_row[1] != ctx_row[1]          # different cutoff rows resolved
    assert ctx["state_mean_n"] != pz["n"], (ctx["state_mean_n"], pz["n"])


# ── the direction labels are exactly the comparison predicates ──────────

@pytest.mark.parametrize("act,req", [
    (3.5, 5.0),    # actual BELOW, required AT/ABOVE  -> TRUE / TRUE
    (3.5, 3.5),    # actual BELOW, required BELOW    -> TRUE / FALSE
    (5.5, 5.5),    # actual ABOVE, required AT/ABOVE -> FALSE / TRUE
    (5.5, 3.5),    # actual ABOVE, required BELOW    -> FALSE / FALSE
])
def test_direction_labels_match_the_booleans(base_env, act, req):
    """ACTUAL "BELOW" iff actual_below_state_mean; REQUIRED "AT/ABOVE" iff
    required_ge_state_mean.  The tie case (== mean) is asymmetric by design:
    5.5 > 4.0 so ACTUAL is ABOVE; required == mean is the AT/ABOVE boundary
    only reachable exactly — verified separately below."""
    clean, main, c, m = base_env
    _live_row(c, "SM9", act, req, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM9")
    ctx = HistoricalContextEngine(clean).context_for(m, "SM9")
    mean = ctx["state_mean_pace"]
    assert ctx["actual_below_state_mean"] == (act < mean)
    assert ctx["required_ge_state_mean"] == (req >= mean)
    assert ctx["actual_vs_state_mean"] == ("BELOW" if act < mean else "ABOVE")
    assert ctx["required_vs_state_mean"] == (
        "BELOW" if req < mean else "AT/ABOVE")
    assert ctx["both_conditions_true"] == (
        ctx["actual_below_state_mean"] and ctx["required_ge_state_mean"])


def test_tie_handling_is_asymmetric_at_the_boundary(base_env):
    """required == mean is AT/ABOVE (>=) while actual == mean is NOT BELOW
    (<).  Pins the exact predicate direction so neither can be reversed."""
    clean, main, c, m = base_env
    mean = 4.0
    _live_row(c, "SM10", mean, mean, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM10")
    ctx = HistoricalContextEngine(clean).context_for(m, "SM10")
    assert ctx["state_mean_pace"] == mean
    assert ctx["actual_below_state_mean"] is False        # <  is strict
    assert ctx["actual_vs_state_mean"] == "ABOVE"         # not below
    assert ctx["required_ge_state_mean"] is True          # >= is inclusive
    assert ctx["required_vs_state_mean"] == "AT/ABOVE"
    assert ctx["both_conditions_true"] is False


# ── (i) benchmark maturity rules remain intact ─────────────────────────

def test_maturity_rule_is_min_benchmark_n(tmp_path):
    from blm_v4.live_analytics.benchmark import MIN_BENCHMARK_N
    assert MIN_BENCHMARK_N == 30
    clean, main, c, m = _make_dbs(tmp_path)     # no archive seeded
    # exactly MIN_BENCHMARK_N-1 prior observations in the target key
    n_prior = MIN_BENCHMARK_N - 1
    gids = ["SM11-%02d" % i for i in range(n_prior)]
    for i, gid in enumerate(gids):
        _live_row(c, gid, 4.0, 5.0, "2026-01-10T10:%02d:00.000000Z" % i)
        _live_game(m, gid)
    _ledger(clean, gids)
    _live_row(c, "SM11", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM11")
    ctx = HistoricalContextEngine(clean).context_for(m, "SM11")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx["state_mean_pace"] is None
    assert ctx["state_mean_n"] == n_prior           # true count, no fabrication
    assert ctx["state_mean_n"] < MIN_BENCHMARK_N
    # one more prior observation crosses the maturity line: the mean lands
    _live_row(c, "SM11-X", 4.0, 5.0, "2026-01-11T10:00:00.000000Z")
    _ledger(clean, ["SM11-X"])
    ctx2 = HistoricalContextEngine(clean).context_for(m, "SM11")
    assert ctx2["status"] == "matched"
    assert ctx2["state_mean_n"] == MIN_BENCHMARK_N
    assert ctx2["state_mean_pace"] == 4.0


# ── (j) missing benchmark returns null, never a fabricated value ────────

def test_no_clean_observation_returns_no_benchmark(tmp_path):
    clean, main, c, m = _make_dbs(tmp_path)
    _live_game(m, "SM12")
    ctx = HistoricalContextEngine(clean).context_for(m, "SM12")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx["reason"] == "no_clean_observation"
    assert ctx.get("state_mean_n") is None
    assert ctx.get("state_mean_pace") is None


def test_immature_population_does_not_fabricate_an_n(tmp_path):
    clean, main, c, m = _make_dbs(tmp_path)
    side = sqlite3.connect(str(clean) + ".live_analytics.db")
    # only 5 settled observations — below the N >= 30 maturity rule
    _seed_archive(c, "betual-nba", PERIOD, PROG, 5, actual=4.0,
                  side=side, m=m)
    side.close()
    _live_row(c, "SM7", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    _live_game(m, "SM7")
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM7")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx["reason"] == "insufficient_mature_population"
    # the real (small) population size is reported — the true count, not a
    # fabricated one, and the mean stays absent
    assert ctx["state_mean_n"] == 5
    assert ctx["state_mean_pace"] is None
    # no mean is served at all when the population is immature
    assert "historical_avg_pace" not in ctx
    assert "state_mean_pace" in ctx


def test_unresolved_competition_returns_no_fabricated_n(tmp_path):
    clean, main, c, m = _make_dbs(tmp_path)
    _live_row(c, "SM8", 3.0, 5.0, "2026-02-01T10:00:00.000000Z")
    # NOT registered in games -> competition unresolvable
    eng = HistoricalContextEngine(clean)
    ctx = eng.context_for(m, "SM8")
    assert ctx["status"] == "no_mature_historical_context"
    assert ctx.get("state_mean_n") is None
    assert ctx.get("state_mean_pace") is None
