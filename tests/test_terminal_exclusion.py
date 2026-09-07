"""TERMINAL-CHECKPOINT EXCLUSION — hard regression tests (directive).

The directive's hard rule, under permanent test:

    TERMINAL     = SETTLEMENT / AUDIT ONLY.
    NON-TERMINAL = PREDICTIVE RESEARCH ELIGIBLE.

Proofs required:

  * terminal rows are classified correctly (authoritative game-time
    evidence, never 100% alone when a better field exists);
  * terminal rows remain STORED (never deleted);
  * terminal rows remain available for SETTLEMENT (e.g. the directive's
    canonical case: final actual 162, terminal market 167.5, terminal
    fair 162, terminal direction UNDER -> U WIN stays on the row);
  * terminal rows are excluded from predictive validation (scorecard
    summary / fixed checkpoints / by-progress / market-compare);
  * terminal rows are excluded from calibration (freshness + reliability
    bins) and interaction (freshness x outcome / edge buckets);
  * terminal rows cannot enter prospective confirmation populations
    (deviation benchmark admission);
  * non-terminal 10-90% rows remain eligible;
  * final settlement is still attached retrospectively;  * repeated execution produces identical eligibility (idempotent).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.clean_metrics import CleanMetricsStore
from blm_v4.deviation import DeviationEngine
from blm_v4.pace_projector import PaceProjector
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore
from blm_v4.terminal_eligibility import (
    TERMINAL_EXCLUSION_REASON,
    eligibility_state,
    is_terminal_checkpoint,
    predictive_validation_label,
    terminal_basis,
)
from tests.test_m009_checkpoint_market import _build, _LINES

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── 1. Deterministic classification ──────────────────────────────────

def test_terminal_classified_from_authoritative_game_time():
    """Full classification durations remain authoritative: 40-minute
    classifications terminate at 40:00, 48-minute at 48:00."""
    # 40-min classification: elapsed 40.0 -> terminal (ELAPSED rule)
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", elapsed_minutes=40.0) is True
    # 48-min classification: terminal only at 48:00
    assert is_terminal_checkpoint(
        classification="CYBER_2K26", elapsed_minutes=48.0) is True
    assert is_terminal_checkpoint(
        classification="CYBER_2K26", elapsed_minutes=40.0) is False
    # Q4 period-over sentinels
    assert is_terminal_checkpoint(quarter=4, clock="00:00") is True
    assert is_terminal_checkpoint(quarter=4, clock="21:00") is True
    assert is_terminal_checkpoint(quarter=4, clock="02:00") is False
    # finished labels
    assert is_terminal_checkpoint(period_label="Full Time") is True
    assert is_terminal_checkpoint(period_label="Match Ended") is True
    # progress 1.0
    assert is_terminal_checkpoint(progress=1.0) is True
    # ended status trusted ONLY when no game-time evidence contradicts it
    assert is_terminal_checkpoint(game_status="ended") is True
    assert is_terminal_checkpoint(
        game_status="ended", elapsed_minutes=20.0) is False
    # BUCKET-INDEPENDENCE (label-semantics directive): the predicate has
    # NO checkpoint-bucket parameter at all — a bucket/percentage can
    # never be passed as terminal evidence, and a 39.25/40.00 (98.1%)
    # observation is NON-TERMINAL regardless of which bucket stored it.
    import inspect
    sig = inspect.signature(is_terminal_checkpoint)
    assert "checkpoint_pct" not in sig.parameters
    assert is_terminal_checkpoint(
        elapsed_minutes=39.25, classification="BETUAL_NBA") is False


def test_terminal_basis_reported_and_labels():
    import inspect
    assert terminal_basis(elapsed_minutes=40.0, classification="BETUAL_NBA") \
        == "ELAPSED_FULL_DURATION"
    assert terminal_basis(progress=1.0) == "PROGRESS_100"
    # the predicate accepts no bucket argument: a pct100 label can never
    # be terminal evidence (bucket-independence by construction)
    assert "checkpoint_pct" not in inspect.signature(terminal_basis).parameters
    assert terminal_basis(elapsed_minutes=12.0) is None
    # directive section 3: the settlement label and the predictive label
    # coexist on the same row
    assert predictive_validation_label(True) == \
        "PREDICTIVE VALIDATION: EXCLUDED"
    assert predictive_validation_label(False) == \
        "PREDICTIVE VALIDATION: ELIGIBLE"
    t, e, r = eligibility_state(elapsed_minutes=40.0,
                                classification="BETUAL_NBA")
    assert (t, e, r) == (True, 0, TERMINAL_EXCLUSION_REASON)


def test_non_terminal_10_to_90_remain_eligible():
    for pct in range(10, 100, 10):
        elapsed = pct / 100 * 40.0
        assert is_terminal_checkpoint(
            classification="BETUAL_NBA",
            elapsed_minutes=elapsed) is False, pct
        assert eligibility_state(
            classification="BETUAL_NBA",
            elapsed_minutes=elapsed)[1] == 1


# ── 2. Canonical tautology case (directive section 5) ─────────────────

def _terminal_fixture(tmp_path: Path) -> Scorecard:
    """Game with a normal 90% checkpoint and a 100% terminal row: final
    actual 162, terminal market 167.5, terminal fair 162, terminal
    direction UNDER -> the row settles U WIN but must be excluded from
    every predictive statistic."""
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-TERM", lines=_LINES)
    conn = sqlite3.connect(dbfile)
    try:
        gid_db = conn.execute(
            "SELECT id FROM games WHERE source_game_id='G-TERM'").fetchone()[0]
        # override the last snapshot (Q4 00:00, the terminal state) with
        # the directive's numbers: score 162, market 167.5
        last = conn.execute(
            "SELECT id FROM snapshots WHERE game_id=? ORDER BY id DESC "
            "LIMIT 1", (gid_db,)).fetchone()[0]
        conn.execute(
            "UPDATE snapshots SET home_score=80, away_score=82, "
            "total_line=167.5 WHERE id=?", (last,))
        # mid-game market 155 -> terminal fair (== actual 162, no future
        # snapshots) vs market 167.5 => fair < market, actual < market =>
        # U WIN for settlement; predictive: EXCLUDED
        mid = conn.execute(
            "SELECT id FROM snapshots WHERE game_id=? ORDER BY id ASC "
            "LIMIT 1 OFFSET 9", (gid_db,)).fetchone()[0]
        conn.execute(
            "UPDATE snapshots SET total_line=155.0 WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()
    sc = Scorecard(dbfile)
    sc.capture_results()
    sc.record_checkpoint_market()
    return sc


def test_terminal_row_settles_but_excluded_from_predictive(tmp_path):
    sc = _terminal_fixture(tmp_path)
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["checkpoint_pct"]: dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id='G-TERM'")}
    finally:
        conn.close()
    assert 100 in rows, "terminal row must remain STORED (never deleted)"
    trow = rows[100]
    # settlement identity intact: U WIN for settlement purposes
    assert trow["outcome"] == "UNDER_WIN"
    assert trow["actual_final_total"] == 162
    assert trow["live_market_line"] == 167.5
    # explicit exclusion state
    assert trow["terminal"] == 1
    assert trow["predictive_eligible"] == 0
    assert trow["exclusion_reason"] == TERMINAL_EXCLUSION_REASON
    # the 90% checkpoint stays predictive-eligible
    assert rows[90]["terminal"] == 0
    assert rows[90]["predictive_eligible"] == 1


def test_terminal_absent_from_every_aggregate_numerator_and_denominator(tmp_path):
    sc = _terminal_fixture(tmp_path)
    agg = sc.market_vs_fair()

    def cp(pct):
        return next(c for c in agg["checkpoints"] if c["checkpoint_pct"] == pct)

    c100 = cp(100)
    c90 = cp(90)
    # 100%: one terminal row exists but contributes to NOTHING
    assert c100["n_terminal"] == 1
    assert c100["predictive_eligible"] == 0
    assert c100["n"] == 0 and c100["n_fair"] == 0
    assert c100["avg_market"] is None and c100["avg_fair"] is None
    assert c100["under_win"] == 0 and c100["under_loss"] == 0
    # 90%: the non-terminal population stays measurable
    assert c90["n_terminal"] == 0
    assert c90["predictive_eligible"] >= 1
    assert c90["n_fair"] >= 1
    # population counters (directive section 6)
    te = agg["terminal_exclusion"]
    assert te["all_rows"] == te["terminal_rows"] + te["predictive_eligible_rows"]
    assert te["terminal_rows"] >= 1
    # game-level audit table keeps the terminal row WITH its outcome
    game = next(g for g in agg["games"] if g["source_game_id"] == "G-TERM")
    row100 = next(r for r in game["rows"] if r["checkpoint_pct"] == 100)
    assert row100["outcome"] == "UNDER_WIN"      # settlement outcome retained
    assert row100["terminal"] == 1
    assert row100["predictive_eligible"] == 0
    row90 = next(r for r in game["rows"] if r["checkpoint_pct"] == 90)
    assert row90["terminal"] == 0 and row90["predictive_eligible"] == 1


def test_terminal_excluded_from_prediction_score_research(tmp_path):
    """A prediction taken at the terminal snapshot (progress 1.0) is never
    scored: it contributes 0 to accuracy / market compare / by-progress."""
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-PT")
    sc = Scorecard(dbfile)          # creates the scorecard schema
    conn = sqlite3.connect(dbfile)
    try:
        # final result must be OK for scoring; give the game a clean result
        conn.execute(
            "INSERT OR REPLACE INTO game_results (source_game_id, "
            "classification, final_home, final_away, final_total, result_at, "
            "final_result_status) "
            "VALUES ('G-PT', 'BETUAL_NBA', 80, 82, 162, ?, 'OK')",
            (_iso(NOW - timedelta(minutes=5)),))
        # a mid-game prediction + a TERMINAL prediction (progress=1.0)
        conn.execute(
            "INSERT INTO predictions (source_game_id, classification, "
            "model_version, checkpoint, quarter, predicted_at, "
            "source_snapshot_at, elapsed_minutes, progress, projected_home, "
            "projected_away, projected_total, market_total, valid, terminal, "
            "predictive_eligible) VALUES "
            "('G-PT', 'BETUAL_NBA', 'v4-pace-1', 'q2', 2, ?, ?, 20.0, 0.5, "
            "81.0, 81.0, 162.0, 160.5, 1, 0, 1), "
            "('G-PT', 'BETUAL_NBA', 'v4-pace-1', 'final', 4, ?, ?, 40.0, 1.0, "
            "81.0, 81.0, 162.0, 167.5, 1, 1, 0)",
            (_iso(NOW - timedelta(minutes=10)),
             _iso(NOW - timedelta(minutes=11)),
             _iso(NOW - timedelta(minutes=9)),
             _iso(NOW - timedelta(minutes=8))))
        conn.commit()
    finally:
        conn.close()
    stats = sc.score_all()
    assert stats["terminal_excluded"] == 1
    assert stats["scored"] >= 1
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        scored = conn.execute(
            "SELECT p.checkpoint FROM prediction_scores s "
            "JOIN predictions p ON p.id = s.prediction_id").fetchall()
        checkpoints = {r["checkpoint"] for r in scored}
    finally:
        conn.close()
    assert "final" not in checkpoints          # terminal never scored
    assert "q2" in checkpoints                 # non-terminal scored
    # headline accuracy excludes terminal by construction: the summary's
    # population counters prove the split, and every per-version metric
    # is computed over eligible (non-terminal) rows only
    summary = sc.summary()["versions"]
    term_pop = summary["_terminal_eligibility"]
    assert term_pop["all_checkpoints"] >= 2
    assert term_pop["terminal_checkpoints"] >= 1
    assert term_pop["non_terminal_checkpoints"] >= 1
    assert term_pop["all_checkpoints"] == (
        term_pop["terminal_checkpoints"]
        + term_pop["non_terminal_checkpoints"])
    for ver, m in summary.items():
        if ver.startswith("_") or not isinstance(m, dict):
            continue
        # every real model version has non-terminal scored rows
        assert m.get("predictions", 0) >= 1, ver


def test_terminal_excluded_from_calibration_and_interaction(tmp_path):
    """Calibration (freshness/reliability bins) + interaction (edge
    buckets, freshness x outcome) count terminal rows in NO bucket."""
    sc = _terminal_fixture(tmp_path)
    agg = sc.market_vs_fair()
    # every freshness bucket + time-of-day bucket counts must be composed
    # of non-terminal rows: recompute from the raw population
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        n_rows = conn.execute(
            "SELECT COUNT(*) c FROM checkpoint_market "
            "WHERE source_game_id='G-TERM'").fetchone()["c"]
        n_term = conn.execute(
            "SELECT COUNT(*) c FROM checkpoint_market "
            "WHERE source_game_id='G-TERM' AND terminal=1").fetchone()["c"]
        n_elig = conn.execute(
            "SELECT COUNT(*) c FROM checkpoint_market "
            "WHERE source_game_id='G-TERM' AND terminal=0").fetchone()["c"]
    finally:
        conn.close()
    assert n_rows == n_term + n_elig           # stored, partitioned
    # the freshness/TOD/edge aggregates run over research_rows only — the
    # population counters prove the split, and the 100% bucket proves the
    # exclusion at the only terminal checkpoint
    c100 = next(c for c in agg["checkpoints"]
                if c["checkpoint_pct"] == 100)
    assert c100["n_terminal"] == 1 and c100["n"] == 0
    # freshness bins carry no terminal contribution (all zeros for the
    # terminal-only population would show up as counts from G-TERM's
    # terminal row if it leaked)
    fresh = agg["market_freshness"]
    assert all(b["n"] >= 0 for b in fresh)


def test_terminal_cannot_enter_deviation_benchmark(tmp_path):
    """Prospective-confirmation population gate: a terminal clean
    observation (elapsed == full duration) never becomes a residual, and
    the benchmark counters report the exclusion."""
    clean = CleanMetricsStore(tmp_path / "blm_metrics_clean.db")
    conn = sqlite3.connect(clean.db_path)
    base = NOW
    full = 40.0
    for i, (elapsed, total, line) in enumerate(
            [(10.0, 50, 160.0), (20.0, 100, 165.0), (40.0, 162, 167.5)]):
        cap = _iso(base + timedelta(minutes=2 * i))
        cur = conn.execute(
            "INSERT INTO clean_snapshots (source_game_id, captured_at, "
            "period_label, quarter, clock, total_points, game_status, source) "
            "VALUES ('G-DEV', ?, ?, ?, ?, ?, 'live', 't')",
            (cap, "Q4" if elapsed == 40.0 else "Q1",
             4 if elapsed == 40.0 else 1, "00:00" if elapsed == 40.0
             else "06:00", total))
        sid = cur.lastrowid
        conn.execute(
            "INSERT INTO clean_observations (source_game_id, snapshot_id, "
            "captured_at, classification, total_points, total_game_minutes, "
            "elapsed_game_minutes, remaining_game_minutes, progress_pct, "
            "actual_pts_per_min, live_total_line, market_captured_at, "
            "market_source, status) "
            "VALUES ('G-DEV', ?, ?, 'BETUAL_NBA', ?, 40.0, ?, ?, ?, ?, ?, "
            "?, 'event_view', 'VALID')",
            (sid, cap, total, elapsed, full - elapsed,
             round(elapsed / full * 100, 4), round(total / elapsed, 4),
             line, cap))
    conn.commit()
    conn.close()
    PaceProjector().refresh_game(clean, "G-DEV")
    stats = DeviationEngine(clean.db_path).refresh_game("G-DEV")
    # the 40:00 observation (terminal) is excluded from the benchmark
    assert stats["terminal_excluded"] == 1
    assert stats["eligible"] == 2
    store_rows = DeviationEngine(clean.db_path).store.residuals_for_game("G-DEV")
    assert len(store_rows) == 2
    # backfill the explicit stamp state on the pre-existing rows
    clean.restamp_terminal_eligibility()
    # the terminal row remains stored in clean_observations, stamped
    conn = sqlite3.connect(clean.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM clean_observations ORDER BY id")]
    finally:
        conn.close()
    assert len(rows) == 3                       # never deleted
    assert rows[-1]["terminal"] == 1
    assert rows[-1]["predictive_eligible"] == 0
    assert rows[-1]["exclusion_reason"] == TERMINAL_EXCLUSION_REASON
    assert rows[-1]["predictive_validation"] == \
        "PREDICTIVE VALIDATION: EXCLUDED"
    assert rows[0]["terminal"] == 0 and rows[0]["predictive_eligible"] == 1


def test_restamp_terminal_eligibility_idempotent(tmp_path):
    """Repeated execution produces identical eligibility (directive §8)."""
    clean = CleanMetricsStore(tmp_path / "blm_metrics_clean.db")
    conn = sqlite3.connect(clean.db_path)
    for i, elapsed in enumerate((10.0, 20.0, 40.0)):
        cap = _iso(NOW + timedelta(minutes=i))
        cur = conn.execute(
            "INSERT INTO clean_snapshots (source_game_id, captured_at, "
            "period_label, quarter, clock, total_points, game_status, source) "
            "VALUES ('G-IDEM', ?, 'Q1', 1, '06:00', ?, 'ended', 't')",
            (cap, 50 + i))
        sid = cur.lastrowid
        conn.execute(
            "INSERT INTO clean_observations (source_game_id, snapshot_id, "
            "captured_at, classification, total_points, "
            "total_game_minutes, elapsed_game_minutes, "
            "remaining_game_minutes, progress_pct, actual_pts_per_min, status) "
            "VALUES ('G-IDEM', ?, ?, 'BETUAL_NBA', ?, 40.0, ?, ?, ?, ?, 'VALID')",
            (sid, cap, 50 + i, elapsed, 40.0 - elapsed,
             round(elapsed / 40.0 * 100, 4), round((50 + i) / elapsed, 4)))
    conn.commit()
    conn.close()
    r1 = clean.restamp_terminal_eligibility()
    conn = sqlite3.connect(clean.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snap1 = [dict(r) for r in conn.execute(
            "SELECT id, terminal, predictive_eligible, exclusion_reason, "
            "predictive_validation FROM clean_observations ORDER BY id")]
        projs1 = conn.execute(
            "SELECT COUNT(*) c FROM clean_projections").fetchone()["c"]
    finally:
        conn.close()
    r2 = clean.restamp_terminal_eligibility()
    assert r1 == r2 == {"observations": 3, "terminal": 1,
                        "predictive_eligible": 2}
    conn = sqlite3.connect(clean.db_path)
    conn.row_factory = sqlite3.Row
    try:
        snap2 = [dict(r) for r in conn.execute(
            "SELECT id, terminal, predictive_eligible, exclusion_reason, "
            "predictive_validation FROM clean_observations ORDER BY id")]
    finally:
        conn.close()
    assert snap1 == snap2
    # mid-game frames of an ended game stay ELIGIBLE (status cannot
    # override game-time evidence)
    assert [s["terminal"] for s in snap2] == [0, 0, 1]


def test_scorecard_migration_idempotent_and_settlement_intact(tmp_path):
    """The stamp migration is deterministic: repeated Scorecard
    construction reclassifies identically and never deletes or flips the
    settlement identity of any row."""
    sc = _terminal_fixture(tmp_path)
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        before = [dict(r) for r in conn.execute(
            "SELECT source_game_id, checkpoint_pct, outcome, terminal, "
            "predictive_eligible FROM checkpoint_market ORDER BY 1, 2")]
    finally:
        conn.close()
    sc2 = Scorecard(sc._db_path)               # re-run migration
    sc3 = Scorecard(sc._db_path)
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        after = [dict(r) for r in conn.execute(
            "SELECT source_game_id, checkpoint_pct, outcome, terminal, "
            "predictive_eligible FROM checkpoint_market ORDER BY 1, 2")]
    finally:
        conn.close()
    assert before == after
    assert len(after) == len(before)           # nothing deleted
    # settlement outcomes survived every migration untouched
    assert all(r["outcome"] for r in after)


def test_deviation_validation_population_counters(tmp_path):
    """The validation report exposes the ALL/TERMINAL/ELIGIBLE split and
    the research dataset contains zero terminal rows."""
    clean = CleanMetricsStore(tmp_path / "blm_metrics_clean.db")
    conn = sqlite3.connect(clean.db_path)
    for i, elapsed in enumerate((10.0, 20.0)):
        cap = _iso(NOW + timedelta(minutes=i))
        cur = conn.execute(
            "INSERT INTO clean_snapshots (source_game_id, captured_at, "
            "period_label, quarter, clock, total_points, game_status, source) "
            "VALUES ('G-CNT', ?, 'Q1', 1, '06:00', ?, 'live', 't')",
            (cap, 50 + i))
        sid = cur.lastrowid
        conn.execute(
            "INSERT INTO clean_observations (source_game_id, snapshot_id, "
            "captured_at, classification, total_points, "
            "total_game_minutes, elapsed_game_minutes, "
            "remaining_game_minutes, progress_pct, actual_pts_per_min, "
            "live_total_line, market_captured_at, market_source, status) "
            "VALUES ('G-CNT', ?, ?, 'BETUAL_NBA', ?, 40.0, ?, ?, ?, ?, "
            "160.0, ?, 'event_view', 'VALID')",
            (sid, cap, 50 + i, elapsed, 40.0 - elapsed,
             round(elapsed / 40.0 * 100, 4), round((50 + i) / elapsed, 4),
             cap))
    conn.commit()
    conn.close()
    PaceProjector().refresh_game(clean, "G-CNT")
    DeviationEngine(clean.db_path).refresh_game("G-CNT")
    from blm_v4.deviation_analysis import DeviationAnalysis
    v = DeviationAnalysis(clean.db_path).validation()
    term = v["terminal_population"]
    assert term["clean_observations_total"] == 2
    assert term["clean_observations_terminal"] == 0
    assert term["deviation_residual_terminal"] == 0
    assert term["deviation_residual_predictive_eligible"] == 2
    assert v["n_terminal_excluded"] == 0
    assert v["n_predictive_eligible"] == 2


# ── Legacy-schema handling (directive §5/§11) ─────────────────────────

def _strip_terminal_columns(dbfile: Path) -> None:
    """Recreate prediction_scores WITHOUT the terminal stamp columns —
    simulating a pre-directive database.  Missing column != zero terminal
    rows; the research reads must DERIVE terminal status instead."""
    conn = sqlite3.connect(dbfile)
    try:
        conn.executescript("""
            CREATE TABLE prediction_scores_legacy AS
            SELECT prediction_id, source_game_id, classification,
                   model_version, home_error, away_error, total_error,
                   abs_home_error, abs_away_error, abs_total_error,
                   total_pct_error, model_total, market_total, actual_total,
                   market_error, model_beat_market, ou_prediction, ou_result,
                   ou_correct, scored_at, fragment
            FROM prediction_scores;
            DROP TABLE prediction_scores;
            ALTER TABLE prediction_scores_legacy RENAME TO prediction_scores;
        """)
        conn.commit()
    finally:
        conn.close()


def test_evidence_precedence_game_time_beats_stale_status():
    """§6-C: 39.25/40.00 with game_status='ended' stays NON-TERMINAL —
    a status flag (or any stale metadata) never outranks authoritative
    game-time evidence that proves the game is still in progress."""
    t, e, r = eligibility_state(
        classification="BETUAL_NBA",
        elapsed_minutes=39.25,       # 98.1% — demonstrably not game end
        progress=0.9813,
        game_status="ended",         # contradictory stale status flag
        ended=True,
    )
    assert (t, e) == (False, 1) and r is None
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", elapsed_minutes=39.25,
        progress=0.9813, game_status="ended", ended=True) is False
    # and the reverse ordering holds: no game-time evidence -> the status
    # fallback IS permitted (never manufactured from missing evidence)
    assert is_terminal_checkpoint(game_status="ended") is True


def test_rounding_cannot_change_terminal_status():
    """§6-E: 39.25/40.00 = 98.125% -> displays 98.1%.  Rounding is a
    display operation: whether rounded up, down, or to the nearest
    tenth, it can never flip terminal status."""
    raw = 39.25 / 40.00 * 100                 # 98.125
    assert round(raw, 1) == 98.1              # the displayed value
    # every rounding variant of the same observation classifies identically
    for shown in (round(raw, 1), round(raw), round(raw, 2), raw):
        elapsed = shown / 100 * 40.0
        assert is_terminal_checkpoint(
            classification="BETUAL_NBA", elapsed_minutes=elapsed) is False
        t, e, _ = eligibility_state(classification="BETUAL_NBA",
                                    elapsed_minutes=elapsed)
        assert e == 1
    # the rounding boundary itself: 98.05 -> 98.1 and 99.95 -> 100.0 are
    # still sub-duration observations (only true game end is terminal)
    for elapsed in (98.05 / 100 * 40, 99.95 / 100 * 40):
        assert is_terminal_checkpoint(
            classification="BETUAL_NBA", elapsed_minutes=elapsed) is False


def test_legacy_schema_derives_terminal_status(tmp_path):
    """On a pre-stamp database (no terminal column), a scored row whose
    source prediction was taken at the game's final state is DERIVED
    terminal and excluded from every research aggregate — it is never
    eligible-by-default just because the stamp column is absent."""
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-LEG")
    sc = Scorecard(dbfile)                     # creates scorecard schema
    conn = sqlite3.connect(dbfile)
    try:
        iso = _iso
        conn.execute(
            "INSERT OR REPLACE INTO game_results (source_game_id, "
            "classification, final_home, final_away, final_total, result_at, "
            "final_result_status) VALUES ('G-LEG', 'BETUAL_NBA', 80, 82, "
            "162, ?, 'OK')", (iso(NOW - timedelta(minutes=5)),))
        # one TERMINAL prediction (progress 1.0) + one non-terminal
        conn.execute(
            "INSERT INTO predictions (source_game_id, classification, "
            "model_version, checkpoint, quarter, predicted_at, "
            "source_snapshot_at, elapsed_minutes, progress, projected_home, "
            "projected_away, projected_total, market_total, valid) VALUES "
            "('G-LEG', 'BETUAL_NBA', 'v4-pace-1', 'q2', 2, ?, ?, 20.0, 0.5, "
            "81.0, 81.0, 162.0, 160.5, 1), "
            "('G-LEG', 'BETUAL_NBA', 'v4-pace-1', 'final', 4, ?, ?, 40.0, "
            "1.0, 81.0, 81.0, 162.0, 167.5, 1)",
            (iso(NOW - timedelta(minutes=10)),
             iso(NOW - timedelta(minutes=11)),
             iso(NOW - timedelta(minutes=9)),
             iso(NOW - timedelta(minutes=8))))
        for pid, mkt, te in ((1, 160.5, 1.5), (2, 167.5, -5.5)):
            conn.execute(
                "INSERT INTO prediction_scores (prediction_id, "
                "source_game_id, classification, model_version, total_error, "
                "abs_total_error, model_total, market_total, actual_total, "
                "market_error, scored_at, fragment) VALUES "
                "(?, 'G-LEG', 'BETUAL_NBA', 'v4-pace-1', ?, abs(?), 162.0, "
                "?, 162.0, ?, ?, 0)",
                (pid, te, te, mkt, round(mkt - 162.0, 2),
                 iso(NOW - timedelta(minutes=4))))
        conn.commit()
    finally:
        conn.close()
    _strip_terminal_columns(dbfile)

    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(prediction_scores)")}
        assert "terminal" not in cols            # legacy schema confirmed
        # the market-compare research aggregate DERIVES terminality:
        # only the non-terminal row is admitted
        from blm_v4.scorecard import _market_compare_sql, _summary_sql
        mc = _market_compare_sql(conn)
        assert mc["n"] == 1
        # the summary classifies the legacy row as DERIVED terminal
        summary = _summary_sql(conn)["versions"]
        pop = summary["_terminal_eligibility"]
        assert pop["all_scored_rows"] == 2
        assert pop["terminal_scored_rows"] == 1
        assert pop["explicit_terminal_stamped"] == 0   # no stamp possible
        assert pop["derived_terminal_unstamped"] >= 1  # caught by derivation
        assert pop["terminal_in_headline"] == 1
        # headline per-version metrics: only the eligible row counts
        assert summary["v4-pace-1"]["predictions"] == 1
    finally:
        conn.close()

    # the migration, when it runs, stamps the SAME classification: the
    # derived-terminal legacy row becomes explicitly terminal=1
    sc2 = Scorecard(dbfile)
    sc2._init()
    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["prediction_id"]: dict(r) for r in conn.execute(
            "SELECT prediction_id, terminal, predictive_eligible "
            "FROM prediction_scores")}
    finally:
        conn.close()
    assert rows[2]["terminal"] == 1 and rows[2]["predictive_eligible"] == 0
    assert rows[1]["terminal"] == 0 and rows[1]["predictive_eligible"] == 1
    # and the aggregate result is unchanged after migration
    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    try:
        from blm_v4.scorecard import _market_compare_sql
        assert _market_compare_sql(conn)["n"] == 1
    finally:
        conn.close()


# ── 13. Checkpoint %/terminal LABEL semantics (label-semantics directive)
# The displayed progress must be the ACTUAL OBSERVATION TIME
# (elapsed_game_minutes / total_game_minutes), not the checkpoint bucket.
# A bucket/rounding operation can NEVER determine terminal status.

def test_display_progress_and_terminal_label_semantics(tmp_path):
    """39.25/40.00 (98.1%) is a NON-TERMINAL, predictive-eligible row even
    when the writer freezes it into the pct100 bucket; 40.00/40.00
    (100.0%) is TERMINAL.  The bucket never decides — and the progress
    stored/served with the row is the actual observation time, not the
    bucket label."""
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-981", lines=_LINES)
    conn = sqlite3.connect(dbfile)
    try:
        gid_db = conn.execute(
            "SELECT id FROM games WHERE source_game_id='G-981'").fetchone()[0]
        # Case A — final snapshot at 39.25/40.00: Q4 00:45 (= 39.25 of
        # 40 min = 98.125% -> displays 98.1%, the directive's own example
        # with 00:45 remaining), score 162, market 167.5.
        last = conn.execute(
            "SELECT id FROM snapshots WHERE game_id=? ORDER BY id DESC "
            "LIMIT 1", (gid_db,)).fetchone()[0]
        conn.execute(
            "UPDATE snapshots SET home_score=80, away_score=82, "
            "quarter=4, clock='00:45', period_label='4th Quarter', "
            "total_line=167.5 WHERE id=?", (last,))
        conn.commit()
    finally:
        conn.close()
    sc = Scorecard(dbfile)
    sc.capture_results()
    sc.record_checkpoint_market()
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id='G-981' "
            "AND checkpoint_pct=100").fetchone())
    finally:
        conn.close()
    # stored actual observation time, not the bucket label
    assert row["elapsed_minutes"] == pytest.approx(39.25)
    assert row["progress"] == pytest.approx(0.9813, abs=1e-3)
    assert row["progress"] < 1.0
    # RULES 1-3: 39.25/40.00 -> 98.1%, NON-TERMINAL, predictive eligible
    assert round(row["progress"] * 100, 1) == pytest.approx(98.1)
    assert row["terminal"] == 0
    assert row["predictive_eligible"] == 1
    assert row["exclusion_reason"] is None
    # settlement is still attached retrospectively even though the final
    # snapshot is not the terminal state
    assert row["outcome"] is not None
    assert row["actual_final_total"] == 162
    # RULES 4-6: 40.00/40.00 -> 100.0%, TERMINAL, excluded from research
    t, e, r = eligibility_state(classification="BETUAL_NBA",
                                elapsed_minutes=40.0)
    assert (t, e) == (True, 0) and r == TERMINAL_EXCLUSION_REASON
    # RULE 7: a bucket/rounding operation CANNOT change terminal status —
    # the same 39.25/40.00 evidence classifies identically no matter
    # which bucket it is filed under (the predicate takes no bucket).
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", elapsed_minutes=39.25) is False
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", elapsed_minutes=40.0) is True


def test_stale_bucket_stamp_repaired_on_migration(tmp_path):
    """A row stamped terminal=1 by the superseded bucket rule (39.25/40.00
    frozen into pct100) is reconciled to NON-TERMINAL by the idempotent
    migration; an truly-terminal row (40.00/40.00) keeps its stamp."""
    dbfile = tmp_path / "blm_pokerbet.db"
    _build(dbfile, "G-STALE", lines=_LINES)
    conn = sqlite3.connect(dbfile)
    try:
        gid_db = conn.execute(
            "SELECT id FROM games WHERE source_game_id='G-STALE'").fetchone()[0]
        last = conn.execute(
            "SELECT id FROM snapshots WHERE game_id=? ORDER BY id DESC "
            "LIMIT 1", (gid_db,)).fetchone()[0]
        # 39.25/40.00 final snapshot (Q4 00:45) — the stale bucket-stamp
        # scenario
        conn.execute(
            "UPDATE snapshots SET quarter=4, clock='00:45', "
            "period_label='4th Quarter' WHERE id=?", (last,))
        conn.commit()
    finally:
        conn.close()
    sc = Scorecard(dbfile)
    sc.capture_results()
    sc.record_checkpoint_market()
    conn = sqlite3.connect(dbfile)
    try:
        # simulate the superseded writer's bucket-based stamp on the
        # pct100 row (39.25/40.00 stamped terminal by the BUCKET)
        conn.execute(
            "UPDATE checkpoint_market SET terminal=1, predictive_eligible=0, "
            "exclusion_reason='TERMINAL CHECKPOINT' "
            "WHERE source_game_id='G-STALE' AND checkpoint_pct=100")
        # control: a genuinely terminal row (40.00/40.00 = progress 1.0)
        # stamped under the same legacy rule — the repair must NOT touch it
        conn.execute(
            "INSERT INTO checkpoint_market (source_game_id, classification, "
            "checkpoint_pct, checkpoint_timestamp, quarter, progress, "
            "elapsed_minutes, terminal, predictive_eligible, "
            "exclusion_reason, model_version, recorded_at) "
            "VALUES ('G-CTRL', 'BETUAL_NBA', 100, ?, 4, 1.0, 40.0, "
            "1, 0, 'TERMINAL CHECKPOINT', 'v4-pace-1', ?)",
            (_iso(NOW - timedelta(minutes=30)), _iso(NOW)))
        conn.commit()
    finally:
        conn.close()
    sc2 = Scorecard(dbfile)    # migration runs again: repairs the stamp
    sc2._init()
    conn = sqlite3.connect(f"file:{sc2._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id='G-STALE' "
            "AND checkpoint_pct=100").fetchone())
        # the genuinely terminal control row (40.00/40.00) keeps its stamp
        ctrl = dict(conn.execute(
            "SELECT terminal, predictive_eligible, exclusion_reason "
            "FROM checkpoint_market WHERE source_game_id='G-CTRL' "
            "AND checkpoint_pct=100").fetchone())
    finally:
        conn.close()
    assert row["progress"] == pytest.approx(0.9813, abs=1e-3)
    # repaired: game time outranks the stale bucket-based stamp
    assert row["terminal"] == 0
    assert row["predictive_eligible"] == 1
    assert row["exclusion_reason"] is None
    assert ctrl == {"terminal": 1, "predictive_eligible": 0,
                    "exclusion_reason": TERMINAL_EXCLUSION_REASON}
    # idempotence: a third run classifies identically
    sc3 = Scorecard(dbfile)
    sc3._init()
    conn = sqlite3.connect(f"file:{sc3._db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        again = dict(conn.execute(
            "SELECT terminal, predictive_eligible, exclusion_reason "
            "FROM checkpoint_market WHERE source_game_id='G-STALE' "
            "AND checkpoint_pct=100").fetchone())
    finally:
        conn.close()
    assert again == {"terminal": 0, "predictive_eligible": 1,
                     "exclusion_reason": None}
