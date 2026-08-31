"""Tests for the BLM V4 projection-accuracy scorecard + fixed checkpoints.

Covers:
  1. prediction stored (quarter checkpoints)
  2. final result stored (OK + UNKNOWN)
  3. total error calculated correctly
  4. home error correct
  5. away error correct
  6. absolute error correct
  7. bias correct
  8. MAE correct
  9. RMSE correct
 10. market comparison correct
 11. OVER/UNDER result correct
 12. missing market handled correctly
 13. missing final result not scored
 14. model versions remain separated
 15. server restart preserves history (persistence)
 16. duplicate snapshots do not corrupt scoring
 17. prediction made after final result rejected
plus: fixed 10%..90% checkpoints, data-quality gate (contaminated games
excluded), collector virtual-replay instance split.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.collector import PokerBetCollector
from blm_v4.discovery import RowGame
from blm_v4.models import MarketObservation, PokerBetGame, utcnow_iso
from blm_v4.projection import MODEL_VERSION, project
from blm_v4.scorecard import (
    MAX_DISTANCE_PCT,
    FIXED_CHECKPOINT_PCTS,
    Scorecard,
    _checkpoint_for,
    _market_history_sql,
    _per_version_metrics,
    _progress_of,
    _snapshot_history_quality,
)
from blm_v4.storage import PokerBetStore


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_store(tmp_path) -> PokerBetStore:
    return PokerBetStore(tmp_path / "blm.db")


def _add_game(st: PokerBetStore, gid: str, cls: str, home: str, away: str,
              status: str = "live") -> int:
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp", competition_slug=cls.lower(),
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification=cls, sport="basketball",
        home_team=home, away_team=away,
        game_slug=f"{home}-{away}".lower().replace(" ", "-"),
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=30)),
        last_seen_at=_iso(datetime.now(timezone.utc)),
    )
    return st.upsert_game(game)


def _snap(st: PokerBetStore, gid_db: int, gid: str, cls: str,
          t: datetime, hs: int, as_: int, q: int | None, clock: str,
          total: float | None = None, period: str | None = None) -> None:
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid, classification=cls,
        captured_at=_iso(t), home_team="H", away_team="A",
        home_score=hs, away_score=as_,
        period_label=period if period is not None else (f"{q}th Quarter" if q is not None else ""),
        quarter=q, clock=clock,
        game_status="live", total_line=total,
        markets_json=json.dumps({"total": {"first_line": total}}) if total else "{}",
    )
    st.insert_snapshot(gid_db, obs, force=True)


def _ended_game_snapshots(st: PokerBetStore, gid: str = "9001",
                          cls: str = "BETUAL_NBA",
                          t0: datetime | None = None) -> int:
    """Full monotonic game: Q1..Q4, finishes 96-88 (total 184)."""
    t0 = t0 or (datetime.now(timezone.utc) - timedelta(minutes=20))
    gid_db = _add_game(st, gid, cls, "Home Virtual", "Away Virtual", status="ended")
    snaps = [
        (0, 0, 1, "09:00"), (8, 6, 1, "06:00"), (16, 12, 1, "03:00"),
        (24, 20, 1, "00:00"), (30, 26, 2, "09:00"), (40, 34, 2, "06:00"),
        (52, 42, 2, "03:00"), (60, 50, 2, "00:00"), (66, 58, 3, "09:00"),
        (76, 66, 3, "06:00"), (82, 72, 3, "03:00"), (86, 78, 3, "00:00"),
        (88, 80, 4, "09:00"), (92, 84, 4, "06:00"), (96, 88, 4, "00:00"),
    ]
    for i, (hs, as_, q, clock) in enumerate(snaps):
        _snap(st, gid_db, gid, cls, t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, 190.0 if i % 3 == 0 else None)
    return gid_db


# ═══════════ 1-2: prediction + result storage ═════════════════════

def test_prediction_stored(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.run()
    assert stats["recorded"]["recorded"] > 0
    # q1..q4 + final checkpoints recorded at least
    conn = sc._connect()
    try:
        rows = conn.execute("SELECT DISTINCT checkpoint FROM predictions").fetchall()
        cps = {r["checkpoint"] for r in rows}
        assert {"q1", "q2", "q3", "q4", "final"} <= cps
    finally:
        conn.close()


def test_final_result_stored_ok_and_unknown(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)                       # OK
    gid2 = _add_game(st, "9002", "BETUAL_NBA", "H2", "A2", status="ended")
    _snap(st, gid2, "9002", "BETUAL_NBA",
          datetime.now(timezone.utc) - timedelta(minutes=5), 45, 40, 2, "08:00")  # half-time stub
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.capture_results()
    conn = sc._connect()
    try:
        ok = conn.execute("SELECT COUNT(*) c FROM game_results WHERE final_result_status='OK'").fetchone()["c"]
        unk = conn.execute("SELECT COUNT(*) c FROM game_results WHERE final_result_status='UNKNOWN'").fetchone()["c"]
        assert ok == 1
        assert unk == 1
    finally:
        conn.close()


# ═══════════ 3-6: error metrics ═════════════════════════════════

def test_errors_calculated_correctly(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        row = conn.execute(
            """SELECT p.projected_home, p.projected_away, p.projected_total,
                      p.market_total, s.*, r.final_home, r.final_away, r.final_total
               FROM prediction_scores s
               JOIN predictions p ON p.id = s.prediction_id
               JOIN game_results r ON r.source_game_id = s.source_game_id
               ORDER BY s.abs_total_error LIMIT 1""").fetchone()
        assert row["final_home"] == 96 and row["final_away"] == 88 and row["final_total"] == 184
        # errors are against the actual final, not the model
        assert row["total_error"] == round(row["projected_total"] - 184, 2)
        assert row["home_error"] == round(row["projected_home"] - 96, 2)
        assert row["away_error"] == round(row["projected_away"] - 88, 2)
        assert row["abs_total_error"] == abs(row["total_error"])
        assert row["abs_home_error"] == abs(row["home_error"])
        assert row["abs_away_error"] == abs(row["away_error"])
    finally:
        conn.close()


# ═══════════ 7-9: MAE / RMSE / bias ════════════════════════════

def test_mae_rmse_bias_correct(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    summ = sc.summary()
    ver = summ["versions"][MODEL_VERSION]
    assert ver["predictions"] > 0
    assert ver["mae"] is not None and ver["mae"] > 0
    assert ver["rmse"] is not None and ver["rmse"] >= ver["mae"]  # RMSE >= MAE
    assert ver["bias"] is not None
    assert ver["median_abs_error"] is not None
    assert ver["completed_games"] >= 1


# ═══════════ 10-12: market comparison + O/U + missing market ═════

def test_market_compare_and_over_under(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)  # final 184; market lines 190 present
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    mc = sc.market_compare()
    assert mc["n"] > 0
    assert mc["model_mae"] is not None
    assert mc["market_mae"] is not None
    assert mc["ou_predictions"] > 0
    assert mc["ou_hit_rate"] is not None and 0 <= mc["ou_hit_rate"] <= 1
    assert mc["ou_over"] + mc["ou_under"] + mc["ou_push"] == mc["ou_predictions"]


def test_missing_market_handled(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st, gid="9101")
    # strip all market lines from this game
    conn = st._connect()
    try:
        conn.execute("UPDATE snapshots SET total_line=NULL WHERE game_id=?", (gid_db,))
        conn.commit()
    finally:
        conn.close()
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        n_scores = conn.execute("SELECT COUNT(*) c FROM prediction_scores").fetchone()["c"]
        n_mkt = conn.execute(
            "SELECT COUNT(*) c FROM prediction_scores WHERE market_total IS NOT NULL").fetchone()["c"]
        assert n_scores > 0            # still scored (total error valid)
        assert n_mkt == 0              # but no market-based fields
    finally:
        conn.close()
    mc = sc.market_compare()
    assert mc["n"] == 0                # no manufactured edge


# ═══════════ 13-14: no final result / version separation ════════

def test_missing_final_result_not_scored(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "9201", "BETUAL_NBA", "H", "A", status="live")
    for i in range(8):
        _snap(st, gid_db, "9201", "BETUAL_NBA",
              datetime.now(timezone.utc) - timedelta(minutes=8 - i),
              i * 6, i * 5, 1 + i // 3, "05:00")
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.run()
    conn = sc._connect()
    try:
        n_scores = conn.execute("SELECT COUNT(*) c FROM prediction_scores").fetchone()["c"]
        assert n_scores == 0
    finally:
        conn.close()


def test_model_versions_separated(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    # inject a second version's prediction manually (sourced BEFORE result)
    conn = sc._connect()
    try:
        # the game ended at ~t0 + 14*1.2 min; use a snapshot at ~6 min in
        t_pred = datetime.now(timezone.utc) - timedelta(minutes=14)
        conn.execute(
            """INSERT INTO predictions (source_game_id, classification, model_version,
                checkpoint, quarter, predicted_at, source_snapshot_at,
                home_score, away_score, combined, projected_home, projected_away,
                projected_total, market_total, valid)
               VALUES ('9001', 'BETUAL_NBA', 'v4-pace-OLD', 'q2', 2, ?, ?, 40, 34, 74,
                       100.0, 95.0, 195.0, 190.0, 1)""",
            (utcnow_iso(), _iso(t_pred)),
        )
        conn.commit()
    finally:
        conn.close()
    sc.score_all()
    summ = sc.summary()
    vers = summ["versions"]
    assert MODEL_VERSION in vers and "v4-pace-OLD" in vers
    assert vers[MODEL_VERSION]["predictions"] > 0
    assert vers["v4-pace-OLD"]["predictions"] == 1
    # not combined
    assert vers[MODEL_VERSION]["predictions"] != vers["v4-pace-OLD"]["predictions"]


# ═══════════ 15-16: persistence + duplicate safety ══════════════

def test_history_persists_across_restart(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)
    db = tmp_path / "blm.db"
    Scorecard(db).run()
    n1 = Scorecard(db).summary()["versions"][MODEL_VERSION]["predictions"]
    # "restart": new Scorecard instance on the same DB; run again is idempotent
    stats = Scorecard(db).run()
    n2 = Scorecard(db).summary()["versions"][MODEL_VERSION]["predictions"]
    assert n1 == n2 > 0
    assert stats["recorded"]["recorded"] == 0  # nothing new to record


def test_duplicate_snapshots_do_not_corrupt(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st)
    # duplicate the last snapshot (same captured_at)
    t_last = datetime.now(timezone.utc) - timedelta(minutes=20) + timedelta(minutes=14 * 1.2)
    _snap(st, gid_db, "9001", "BETUAL_NBA", t_last, 96, 88, 4, "00:00", 190.0)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    summ = sc.summary()
    assert summ["versions"][MODEL_VERSION]["predictions"] > 0


# ═══════════ 17: post-result prediction rejected ════════════════

def test_prediction_after_final_rejected(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        # remove the legitimately-recorded q4 prediction so the injected
        # post-result one (same game/checkpoint/version) can be inserted
        # (delete its score row first — FK from prediction_scores)
        conn.execute(
            """DELETE FROM prediction_scores WHERE prediction_id IN (
                SELECT p.id FROM predictions p
                WHERE p.source_game_id='9001' AND p.checkpoint='q4' AND p.model_version=?)""",
            (MODEL_VERSION,),
        )
        conn.execute(
            "DELETE FROM predictions WHERE source_game_id='9001' AND checkpoint='q4' AND model_version=?",
            (MODEL_VERSION,),
        )
        # inject a prediction whose source snapshot is AFTER the result
        conn.execute(
            """INSERT INTO predictions (source_game_id, classification, model_version,
                checkpoint, quarter, predicted_at, source_snapshot_at,
                home_score, away_score, combined, projected_home, projected_away,
                projected_total, market_total, valid)
               VALUES ('9001', 'BETUAL_NBA', ?, 'q4', 4, ?, ?, 96, 88, 184,
                       100.0, 95.0, 195.0, 190.0, 1)""",
            (MODEL_VERSION, _iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc))),
        )
        conn.commit()
    finally:
        conn.close()
    stats = sc.score_all()
    assert stats["rejected"] >= 1


# ═══════════ Fixed checkpoints ═════════════════════════════════

def test_fixed_checkpoints_recorded(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.record_fixed_checkpoints()
    assert stats["recorded"] >= 8  # 9 targets, all within ±5pp for a full game
    fxs = sc.fixed_checkpoints()
    assert len(fxs) == len(FIXED_CHECKPOINT_PCTS)
    for fx in fxs:
        assert fx["percent"] in FIXED_CHECKPOINT_PCTS
        if fx["n"] > 0:
            assert fx["mae"] is not None and fx["mae"] > 0


def test_fixed_checkpoint_selection_uses_correct_snapshot(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    sc.record_fixed_checkpoints()
    conn = sc._connect()
    try:
        # the 50% checkpoint prediction must have progress near 0.50
        row = conn.execute(
            "SELECT checkpoint_percent, distance_pct, progress FROM predictions WHERE checkpoint='pct50'"
        ).fetchone()
        assert row is not None
        assert row["progress"] is not None
        assert abs(row["progress"] - 0.50) <= MAX_DISTANCE_PCT / 100 + 0.01
        assert row["distance_pct"] <= MAX_DISTANCE_PCT
    finally:
        conn.close()


def test_fixed_checkpoint_no_lookahead(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st)
    sc = Scorecard(tmp_path / "blm.db")
    sc.record_fixed_checkpoints()
    conn = sc._connect()
    try:
        row = conn.execute(
            "SELECT home_score, away_score, progress FROM predictions WHERE checkpoint='pct30'"
        ).fetchone()
        # the snapshot used must be at ~30% of the game — NOT the final (96-88)
        assert row["progress"] < 0.4
        assert row["home_score"] < 90
    finally:
        conn.close()


# ═══════════ Data-quality gate ═════════════════════════════════

def test_contaminated_history_excluded(tmp_path):
    st = _make_store(tmp_path)
    gid_db = _ended_game_snapshots(st, gid="9301")  # clean, ends 96-88
    # contaminate: inject a regression (new virtual replay mixed in)
    t = datetime.now(timezone.utc) - timedelta(minutes=20) + timedelta(minutes=7 * 1.2)
    _snap(st, gid_db, "9301", "BETUAL_NBA", t, 31, 24, 2, "20:00")
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        q = conn.execute("SELECT status, reason FROM game_quality WHERE source_game_id='9301'").fetchone()
        assert q is not None and q["status"] == "INVALID"
        assert "regression" in q["reason"]
        # and it was never scored
        n = conn.execute("SELECT COUNT(*) c FROM prediction_scores WHERE source_game_id='9301'").fetchone()["c"]
        assert n == 0
    finally:
        conn.close()
    summ = sc.summary()
    assert summ["versions"]["_quality"]["invalid"] >= 1


def test_quality_gate_all_checks(tmp_path):
    rows = [
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:00:00Z",
         "home_score": 10, "away_score": 8},
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:01:00Z",
         "home_score": 9, "away_score": 10},  # regression
    ]
    status, reason = _snapshot_history_quality(rows)
    assert status == "INVALID" and "regression" in reason
    # timestamp ordering
    rows2 = [
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:01:00Z",
         "home_score": 10, "away_score": 8},
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:00:00Z",
         "home_score": 12, "away_score": 10},
    ]
    status2, reason2 = _snapshot_history_quality(rows2)
    assert status2 == "INVALID" and "out of order" in reason2
    # clean
    rows3 = [
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:00:00Z",
         "home_score": 10, "away_score": 8},
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:01:00Z",
         "home_score": 12, "away_score": 10},
    ]
    status3, _ = _snapshot_history_quality(rows3)
    assert status3 == "OK"


def test_quality_gate_cross_event(tmp_path):
    rows = [
        {"source_game_id": "1", "classification": "A", "captured_at": "2026-01-01T00:00:00Z",
         "home_score": 10, "away_score": 8},
        {"source_game_id": "2", "classification": "A", "captured_at": "2026-01-01T00:01:00Z",
         "home_score": 12, "away_score": 10},
    ]
    status, reason = _snapshot_history_quality(rows)
    assert status == "INVALID" and "contamination" in reason


# ═══════════ M008-SCORE-M1: forensic metric accounting ═══════════

def _sample_scores_conn(tmp_path):
    """DB with 2 valid games, each with a scored prediction:
    game 9001: BLM 180 vs actual 184 (signed -4), market line 190 (market
               err +6) -> BLM wins (4<6), O/U UNDER for both.
    game 9002: BLM 170 vs actual 172 (signed -2), market line 180 (market
               err +8) -> BLM wins (2<8), O/U UNDER for both.
    """
    import sqlite3
    from blm_v4.scorecard import SCORECARD_SCHEMA
    db = tmp_path / "sc.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCORECARD_SCHEMA)
    conn.executescript("""
        INSERT INTO predictions (id, source_game_id, classification, model_version,
            checkpoint, predicted_at, source_snapshot_at, projected_home,
            projected_away, projected_total, market_total, valid)
        VALUES
          (1,'9001','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 90,90,180,190,1),
          (2,'9002','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 85,85,170,180,1);
        INSERT INTO prediction_scores (prediction_id, source_game_id, classification,
            model_version, home_error, away_error, total_error,
            abs_home_error, abs_away_error, abs_total_error, total_pct_error,
            model_total, market_total, actual_total, market_error,
            model_beat_market, ou_prediction, ou_result, ou_correct, scored_at, fragment)
        VALUES
          (1,'9001','BETUAL_NBA','v4-pace-1', -4,-4,-4, 4,4,4, 2.17, 180,190,184, 6, 1, -1,-1,1, '2026-01-01T00:00:00Z', 0),
          (2,'9002','BETUAL_NBA','v4-pace-1', -2,-2,-2, 2,2,2, 1.16, 170,180,172, 8, 1, -1,-1,1, '2026-01-01T00:00:00Z', 0);
    """)
    conn.commit()
    return conn


def test_m008_mae_never_negative(tmp_path):
    """MAE = mean(|prediction - actual|) >= 0.  A 'MAE' of -8.37 is a
    mislabeled bias — the aggregate must NEVER emit a negative MAE."""
    import blm_v4.scorecard as sc
    conn = _sample_scores_conn(Path(tmp_path))
    try:
        m = sc._per_version_metrics(conn, "fragment = 0", ())
        assert m["v4-pace-1"]["mae"] == 3.0, "MAE must be mean of ABS errors"
        assert m["v4-pace-1"]["mae"] >= 0
        assert m["v4-pace-1"]["bias"] == -3.0, "signed bias separate from MAE"
    finally:
        conn.close()


def test_m008_market_compare_mae_and_denominator(tmp_path):
    """model_mae in market_compare must be a REAL MAE (>=0) computed from
    absolute errors, and every rate must expose numerator + denominator."""
    import blm_v4.scorecard as sc
    conn = _sample_scores_conn(Path(tmp_path))
    try:
        out = sc._market_compare_sql(conn)
        assert out["model_mae"] == 3.0, "model MAE must be mean(abs BLM error)"
        assert out["model_mae"] >= 0
        assert out["market_mae"] == 7.0
        assert out["n"] == 2
        assert out["model_beat_market_rate"] == 1.0
        # numerator + denominator
        assert out["model_beat_market_n"] == 2
        assert out["model_beat_market_d"] == 2
        assert out["market_beat_blm_n"] == 0
        assert out["ties_n"] == 0
        # O/U accounting identifies the line: checkpoint market (this slice)
        assert out["ou_over"] == 0 and out["ou_under"] == 2 and out["ou_push"] == 0
    finally:
        conn.close()


def test_m008_olv_clv_separate(tmp_path):
    """OLV and CLV are distinct fields; missing OLV/CLV is NULL, never
    substituted; a checkpoint market is never the closing line."""
    import sqlite3
    from blm_v4.scorecard import SCORECARD_SCHEMA, _market_history_sql
    db = tmp_path / "sc.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCORECARD_SCHEMA)
    # game 9003 has OLV=180 and CLV=190 (distinct); 9004 has no market
    conn.executescript("""
        INSERT INTO market_history (source_game_id, classification, started_at,
            analytics_tz, opening_total, closing_total, final_home, final_away,
            final_total, recorded_at)
        VALUES
          ('9003','BETUAL_NBA','2026-01-01T00:00:00Z','UTC',180,190, 90,92,182,'2026-01-01T00:11:00Z'),
          ('9004','BETUAL_NBA','2026-01-01T00:00:00Z','UTC',NULL,NULL, 80,80,160,'2026-01-01T00:11:00Z');
    """)
    conn.commit()
    rows = _market_history_sql(conn)
    d = {r["source_game_id"]: r for r in rows}
    assert d["9003"]["opening_total"] == 180 and d["9003"]["closing_total"] == 190
    assert d["9003"]["opening_total"] != d["9003"]["closing_total"]
    assert d["9004"]["opening_total"] is None and d["9004"]["closing_total"] is None
    conn.close()


def test_m008_every_percentage_has_denominator(tmp_path):
    """No bare percentage — every rate carries numerator/denominator."""
    import blm_v4.scorecard as sc
    conn = _sample_scores_conn(Path(tmp_path))
    try:
        out = sc._market_compare_sql(conn)
        # beat + lost + ties reconcile exactly to n
        assert (out["model_beat_market_n"] + out["market_beat_blm_n"] + out["ties_n"]) == out["n"]
        assert "model_beat_market_d" in out and out["model_beat_market_d"] == out["n"]
    finally:
        conn.close()


def test_m008_invalid_game_zero_headline(tmp_path):
    """An INVALID game contributes nothing to headline metrics."""
    import sqlite3
    from blm_v4.scorecard import SCORECARD_SCHEMA, _per_version_metrics
    db = tmp_path / "sc.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCORECARD_SCHEMA)
    conn.executescript("""
        INSERT INTO game_quality (source_game_id, classification, status, reason, checked_at)
        VALUES ('9999','BETUAL_NBA','INVALID','contamination','2026-01-01T00:00:00Z');
        INSERT INTO predictions (id, source_game_id, classification, model_version,
            checkpoint, predicted_at, source_snapshot_at, projected_home,
            projected_away, projected_total, market_total, valid)
        VALUES (3,'9999','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 100,100,200,195,1);
        INSERT INTO prediction_scores (prediction_id, source_game_id, classification,
            model_version, home_error, away_error, total_error,
            abs_home_error, abs_away_error, abs_total_error, total_pct_error,
            model_total, market_total, actual_total, market_error,
            model_beat_market, ou_prediction, ou_result, ou_correct, scored_at, fragment)
        VALUES (3,'9999','BETUAL_NBA','v4-pace-1', 10,10,20, 10,10,20, 10.0, 200,195,180, 15, 1, 1,1,1, '2026-01-01T00:00:00Z', 1);
    """)
    conn.commit()
    m = _per_version_metrics(conn, "fragment = 0", ())
    assert m.get("v4-pace-1", {}).get("games", 0) == 0
    conn.close()


def test_m008_negative_disparity_retained(tmp_path):
    """disparity = BLM prediction - market line keeps its sign (negative
    matters for UNDER); the scorecard must expose signed disparity."""
    import blm_v4.scorecard as sc
    conn = _sample_scores_conn(Path(tmp_path))
    try:
        out = sc._market_compare_sql(conn)
        # both sample games: BLM 180 vs market 190 -> disparity -10; 170 vs 180 -> -10
        assert out.get("disparity_min") == -10.0
        assert out.get("disparity_max") == -10.0
        assert out.get("disparity_abs_max") == 10.0
    finally:
        conn.close()

def test_legacy_ok_contaminated_game_zero_headline(tmp_path):
    """A game with an OK result recorded under an OLDER, laxer gate but
    whose tracking history FAILS the CURRENT quality rules must contribute
    ZERO to headline scorecard metrics (MAE/RMSE/etc.).

    Unit of validity = game + complete valid tracking history, not
    'prediction rows exist' nor 'a final score exists'.
    """
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "9302", "BETUAL_NBA", "Home Virtual", "Away Virtual",
                       status="ended")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=20)
    # 15 snaps from Q1 (fragment=0 by count/Q1) but with an impossible
    # jump at index 9->10 (+64 home pts in a 72s gap) = contamination.
    snaps = [
        (0, 0, 1, "09:00"), (8, 6, 1, "06:00"), (16, 12, 1, "03:00"),
        (24, 20, 1, "00:00"), (30, 26, 2, "09:00"), (40, 34, 2, "06:00"),
        (52, 42, 2, "03:00"), (60, 50, 2, "00:00"), (66, 58, 3, "09:00"),
        (76, 66, 3, "06:00"), (140, 130, 3, "03:00"), (86, 78, 3, "00:00"),
        (88, 80, 4, "09:00"), (92, 84, 4, "06:00"), (96, 88, 4, "00:00"),
    ]
    for i, (hs, as_, q, clock) in enumerate(snaps):
        _snap(st, gid_db, "9302", "BETUAL_NBA", t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, 190.0 if i % 3 == 0 else None)
    sc = Scorecard(tmp_path / "blm.db")
    # simulate an OK result recorded BEFORE the current quality gate existed
    conn = sc._connect()
    conn.execute(
        "INSERT INTO game_results (source_game_id, classification, final_home,"
        " final_away, final_total, result_at, final_result_status)"
        " VALUES ('9302','BETUAL_NBA',96,88,184,?,'OK')",
        (_iso(t0 + timedelta(minutes=15 * 1.2)),))
    conn.commit()
    conn.close()
    sc.run()
    conn = sc._connect()
    try:
        q = conn.execute(
            "SELECT status, reason FROM game_quality WHERE source_game_id='9302'"
        ).fetchone()
        scored = conn.execute(
            "SELECT COUNT(*) c FROM prediction_scores WHERE source_game_id='9302'"
        ).fetchone()["c"]
        head = conn.execute(
            "SELECT COUNT(*) c FROM prediction_scores"
            " WHERE source_game_id='9302' AND fragment=0"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert q is not None and q["status"] == "INVALID", \
        f"quality gate must flag the contaminated history, got {q}"
    assert scored == 0, "an invalid-history game must never be scored"
    assert head == 0, "an invalid-history game must contribute ZERO headline"
    summ = sc.summary()
    qb = summ["versions"]["_quality"]
    assert qb["invalid"] >= 1
    # per-game audit trace exposes eligibility
    audit = {g["source_game_id"]: g for g in summ.get("eligible_games", [])}
    assert audit["9302"]["eligible"] == 0
    assert audit["9302"]["predictions_used"] == 0


def test_m008_ou_hit_rate_excludes_pushes(tmp_path):
    """M008-SCORE-M1 item 6: pushes must be EXCLUDED from the O/U hit-rate
    denominator (hit rate = hits / (hits + misses)).  A push is not a miss.
    """
    import sqlite3
    import blm_v4.scorecard as sc
    from blm_v4.scorecard import SCORECARD_SCHEMA
    db = tmp_path / "sc.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCORECARD_SCHEMA)
    # 3 decided rows (2 hits, 1 miss) + 1 push row
    conn.executescript("""
        INSERT INTO predictions (id, source_game_id, classification, model_version,
            checkpoint, predicted_at, source_snapshot_at, projected_home,
            projected_away, projected_total, market_total, valid)
        VALUES
          (1,'9101','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 90,90,180,190,1),
          (2,'9102','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 85,85,170,180,1),
          (3,'9103','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 88,88,176,176,1),
          (4,'9104','BETUAL_NBA','v4-pace-1','q1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z', 95,95,190,180,1);
        INSERT INTO prediction_scores (prediction_id, source_game_id, classification,
            model_version, home_error, away_error, total_error,
            abs_home_error, abs_away_error, abs_total_error, total_pct_error,
            model_total, market_total, actual_total, market_error,
            model_beat_market, ou_prediction, ou_result, ou_correct, scored_at, fragment)
        VALUES
          (1,'9101','BETUAL_NBA','v4-pace-1', -4,-4,-4, 4,4,4, 2.17, 180,190,184, 6, 1, -1,-1,1, '2026-01-01T00:00:00Z', 0),
          (2,'9102','BETUAL_NBA','v4-pace-1', -2,-2,-2, 2,2,2, 1.16, 170,180,172, 8, 1, -1,-1,1, '2026-01-01T00:00:00Z', 0),
          (3,'9103','BETUAL_NBA','v4-pace-1', -1,-1,-1, 1,1,1, 0.57, 176,176,176, 0, 0, 0, 0,0, '2026-01-01T00:00:00Z', 0),
          (4,'9104','BETUAL_NBA','v4-pace-1',  5, 5, 5, 5,5,5, 2.78, 190,180,184, 4, 0, 1, 1,0, '2026-01-01T00:00:00Z', 0);
    """)
    conn.commit()
    try:
        out = sc._market_compare_sql(conn)
        assert out["ou_push"] == 1
        assert out["ou_hit_n"] == 2
        # denominator EXCLUDES the push: 2 hits + 1 miss = 3 decided
        assert out["ou_hit_d"] == 3, f"push must be excluded from denominator, got {out['ou_hit_d']}"
        assert out["ou_hit_rate"] == round(2 / 3, 3)
    finally:
        conn.close()


def test_quality_gate_jump_is_gap_aware():
    """A >50pt hop in under 90s is physically impossible (foreign state)
    and rejects; the same hop across a multi-minute capture gap is a
    legitimate fast virtual game (~7x speed) and must PASS."""
    t0 = "2026-01-01T00:00:00Z"
    short_gap = [
        {"source_game_id": "1", "classification": "A", "captured_at": t0,
         "home_score": 19, "away_score": 14},
        {"source_game_id": "1", "classification": "A",
         "captured_at": "2026-01-01T00:00:11Z", "home_score": 62, "away_score": 71},
    ]
    status, reason = _snapshot_history_quality(short_gap)
    assert status == "INVALID" and "jump" in reason
    long_gap = [
        {"source_game_id": "1", "classification": "A", "captured_at": t0,
         "home_score": 15, "away_score": 2},
        {"source_game_id": "1", "classification": "A",
         "captured_at": "2026-01-01T00:02:27Z", "home_score": 109, "away_score": 99},
    ]
    status, reason = _snapshot_history_quality(long_gap)
    assert status == "OK", reason
    # monotonicity still enforced regardless of gap
    reg = [
        {"source_game_id": "1", "classification": "A", "captured_at": t0,
         "home_score": 100, "away_score": 66},
        {"source_game_id": "1", "classification": "A",
         "captured_at": "2026-01-01T00:05:00Z", "home_score": 31, "away_score": 24},
    ]
    status, reason = _snapshot_history_quality(reg)
    assert status == "INVALID" and "regression" in reason


# ═══════════ Collector virtual-replay instance split ═══════════

def _collector(st: PokerBetStore) -> PokerBetCollector:
    c = PokerBetCollector(store=st)
    # mark one tracked game so _store_list_snapshot has a target
    game = PokerBetGame(
        source="PokerBet", source_game_id="5001",
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Home Virtual", away_team="Away Virtual",
        game_slug="h-a", source_url="https://x/5001", status="live",
    )
    st.upsert_game(game)
    c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"] = game
    c._market_queue.append("5001")
    return c


def _row(hs: int, as_: int, q: int = 4, clock: str = "05:00") -> RowGame:
    return RowGame(
        home_team="Home Virtual", away_team="Away Virtual",
        home_score=hs, away_score=as_,
        period_label=f"{q}th Quarter", clock=clock,
        w1_odds=None, w2_odds=None, spread_indicator=None,
    )


def test_collector_detects_instance_reset(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st, gid="5001")  # game 5001 ends 96-88
    c = _collector(st)
    assert c._detect_instance_reset(c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"],
                                    _row(31, 24, 2, "20:00")) == "score_drop"
    assert c._detect_instance_reset(c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"],
                                    _row(100, 92, 4, "01:00")) is None  # continuation


def test_collector_detects_event_view_reset(tmp_path):
    """The event-view capture path must detect the same replay reset."""
    st = _make_store(tmp_path)
    _ended_game_snapshots(st, gid="5001")  # last snapshot 96-88
    c = _collector(st)
    game = c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"]
    assert c._detect_event_reset(game, 25, 32) == "score_drop"   # new replay, big drop
    assert c._detect_event_reset(game, 97, 89) is None           # continuation


def test_collector_splits_instance(tmp_path):
    st = _make_store(tmp_path)
    _ended_game_snapshots(st, gid="5001")
    c = _collector(st)
    game = c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"]
    new_game = c._split_instance(game, _row(31, 24, 2, "20:00"),
                                 __import__("blm_v4.classifications", fromlist=["Classification"]).Classification.BETUAL_NBA)
    assert new_game.source_game_id == "5001#i1"
    assert game.status == "ended"
    assert c._instances["5001"] == "5001#i1"
    assert "5001" not in c._market_queue and "5001#i1" in c._market_queue
    # DB: two game rows, snapshots still belong to the old id
    conn = st._connect()
    try:
        n_games = conn.execute("SELECT COUNT(*) c FROM games WHERE source_game_id IN ('5001','5001#i1')").fetchone()["c"]
        assert n_games == 2
        n_snaps = conn.execute("SELECT COUNT(*) c FROM snapshots WHERE source_game_id='5001'").fetchone()["c"]
        assert n_snaps == 15  # old game untouched
    finally:
        conn.close()


def test_base_id_strips_suffix():
    assert PokerBetCollector._base_id("30739645") == "30739645"
    assert PokerBetCollector._base_id("30739645#i3") == "30739645"


def test_collector_detects_reset_via_clock_regression(tmp_path):
    """At 20s ticks the new replay's first row can already carry a score
    above the 50% drop threshold — the game-clock regression (Q4 -> Q1)
    must still trigger the split."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "5002", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="live")
    # stored history ends late Q4 with a high total
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    _snap(st, gid_db, "5002", "BETUAL_NBA", t0, 41, 52, 4, "00:45")
    c = PokerBetCollector(store=st)
    game = PokerBetGame(
        source="PokerBet", source_game_id="5002",
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Home Virtual", away_team="Away Virtual",
        game_slug="h-a", source_url="https://x/5002", status="live",
    )
    # new replay first row: Q1 01:30, 28-28 (56 total — ABOVE 50% of 93)
    assert c._detect_event_reset(game, 28, 28, "1st Quarter", "01:30") == "clock_regression"
    # same-phase continuation must NOT trigger
    assert c._detect_event_reset(game, 44, 54, "4th Quarter", "00:30") is None
    # clock regression alone (even with a rising score) triggers
    assert c._detect_event_reset(game, 50, 55, "2nd Quarter", "05:00") == "clock_regression"
    # a forward score jump (even a big one in a short window) is NOT a new
    # replay — the live list feed lags the true ~7x game by minutes, so the
    # event view legitimately shows a much later state of the SAME game.
    # Splitting on forward jumps fragmented every game into #iN churn
    # (2026-08-30 forensics: #i3 -> #i4 -> #i5 on one legit game).
    _snap(st, gid_db, "5002", "BETUAL_NBA", datetime.now(timezone.utc), 19, 14, 1, "03:45")
    assert c._detect_event_reset(game, 62, 71, "4th Quarter", "21:00") is None
    # same-phase continuation must NOT trigger
    assert c._detect_event_reset(game, 44, 54, "4th Quarter", "00:30") is None


def test_collector_event_view_final_ends_game_not_split(tmp_path):
    """A VERIFIED event view showing the tracked game's own final state
    (4th Quarter + sentinel clock, forward score) ENDS the game — it is the
    same replay finishing while the lagging list feed still shows mid-game,
    NOT a new replay.  Before this rule the event view's final split the
    game and the stale list split it AGAIN (the #i3->#i4->#i5 churn)."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "5101", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="live")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=2)
    _snap(st, gid_db, "5101", "BETUAL_NBA", t0, 34, 37, 2, "00:45")
    c = _collector(st)
    game = c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"]
    assert c._is_final_state({"period_label": "4th Quarter", "clock": "21:00"}) is True
    assert c._is_final_state({"period_label": "4th Quarter", "clock": "05:00"}) is False
    assert c._is_final_state({"period_label": "Half End", "clock": "21:00"}) is False
    # the final-state branch in _capture_next_market: forward final ends,
    # no split.  (The branch logic is exercised via _is_final_state + the
    # cur >= prev guard; the full navigation path needs a live page.)
    assert c._is_final_state({"period_label": "Full Time", "clock": "21:00"}) is True


def test_capture_event_state_rejects_foreign_teams(tmp_path):
    """The identity guard must live INSIDE the shared capture so the
    _resolve_new_game path (which calls it directly, bypassing
    _capture_next_market) can never store lobby/foreign content."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "5201", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="live")
    c = _collector(st)
    game = c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"]
    parsed_foreign = {"home_team": "Minnesota Lynx", "away_team": "Atlanta Dream",
                      "home_score": 66, "away_score": 79,
                      "period_label": "4th Quarter", "clock": "21:00",
                      "total": None, "handicap": None, "team_totals": {},
                      "match_winner": None, "markets_json": "{}", "raw_json": "{}"}
    assert c._verified_event_view(game, parsed_foreign) is False
    assert c._capture_event_state(None, __import__("blm_v4.classifications",
                                                   fromlist=["Classification"]).Classification.BETUAL_NBA,
                                  game, "ignored") is False
    conn = st._connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM snapshots WHERE source_game_id='5201'").fetchone()["c"] == 0
    finally:
        conn.close()


def test_restart_split_suffix_ignores_foreign_text(tmp_path, monkeypatch):
    """A lobby/foreign page must never drive a restart split — only the
    fixture's own verified event view can (the resolve path previously
    split every fixture on the lobby's first-listed game)."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "5301", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="ended")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    _snap(st, gid_db, "5301", "BETUAL_NBA", t0, 100, 66, 4, "02:00")
    c = PokerBetCollector(store=st)
    game = PokerBetGame(
        source="PokerBet", source_game_id="5301",
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Home Virtual", away_team="Away Virtual",
        game_slug="h-a", source_url="https://x/5301", status="live",
    )
    tax = {"game_id": "5301"}
    # foreign lobby text (other teams) with a big drop -> NO split
    monkeypatch.setattr(
        "blm_v4.collector.parse_event_view",
        lambda text: {"home_team": "Minnesota Lynx", "away_team": "Atlanta Dream",
                      "home_score": 2, "away_score": 2,
                      "period_label": "1st Quarter", "clock": "12:00"},
    )
    assert c._restart_split_suffix("ignored", tax, game) is None
    # the fixture's OWN event view (teams match) with a drop -> split
    monkeypatch.setattr(
        "blm_v4.collector.parse_event_view",
        lambda text: {"home_team": "Home Virtual", "away_team": "Away Virtual",
                      "home_score": 28, "away_score": 28,
                      "period_label": "1st Quarter", "clock": "01:30"},
    )
    assert c._restart_split_suffix("ignored", tax, game) == "5301#i1"


def test_split_instance_writes_audit_row(tmp_path):
    """Every split persists an instance_splits audit row with the tracked
    instance's last state vs the triggering observation."""
    st = _make_store(tmp_path)
    _ended_game_snapshots(st, gid="5001")
    c = _collector(st)
    game = c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"]
    c._split_instance(game, _row(31, 24, 2, "20:00"),
                      __import__("blm_v4.classifications", fromlist=["Classification"]).Classification.BETUAL_NBA,
                      signal="score_drop", path="event")
    conn = st._connect()
    try:
        rows = conn.execute("SELECT * FROM instance_splits").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["base_id"] == "5001" and r["new_id"] == "5001#i1"
        assert r["path"] == "event" and r["signal"] == "score_drop"
        assert r["prev_home"] is not None and r["new_home"] == 31
    finally:
        conn.close()


def test_collector_restart_safe_split(tmp_path, monkeypatch):
    """After a collector restart, _tracked is empty — a fixture re-resolved
    from an event URL whose DB row holds a finished game must start a fresh
    #iN instance, not append the new replay to the finished history."""
    st = _make_store(tmp_path)
    gid_db = _add_game(st, "5003", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="ended")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    _snap(st, gid_db, "5003", "BETUAL_NBA", t0 + timedelta(seconds=0), 100, 66, 4, "02:00")
    c = PokerBetCollector(store=st)
    game = PokerBetGame(
        source="PokerBet", source_game_id="5003",
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Home Virtual", away_team="Away Virtual",
        game_slug="h-a", source_url="https://x/5003", status="live",
    )
    tax = {"game_id": "5003"}
    # new replay event text: Q1 01:30, 28-28 (score above the 50% drop
    # threshold but clock regressed from Q4) → must split
    monkeypatch.setattr(
        "blm_v4.collector.parse_event_view",
        lambda text: {"home_team": "Home Virtual", "away_team": "Away Virtual",
                      "home_score": 28, "away_score": 28,
                      "period_label": "1st Quarter", "clock": "01:30"},
    )
    assert c._restart_split_suffix("ignored", tax, game) == "5003#i1"
    # existing instances must be skipped — #i1 already recorded (collision
    # seen live: a restarted collector re-created base#i1 and contaminated
    # the finished game) → next free id is #i3
    _add_game(st, "5003#i1", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="ended")
    _add_game(st, "5003#i2", "BETUAL_NBA", "Home Virtual", "Away Virtual", status="ended")
    assert c._restart_split_suffix("ignored", tax, game) == "5003#i3"
    assert c._next_instance_id("5003") == "5003#i3"
    # continuation (Q4, rising score) → no split
    monkeypatch.setattr(
        "blm_v4.collector.parse_event_view",
        lambda text: {"home_team": "Home Virtual", "away_team": "Away Virtual",
                      "home_score": 103, "away_score": 68,
                      "period_label": "4th Quarter", "clock": "01:00"},
    )
    assert c._restart_split_suffix("ignored", tax, game) is None
    # unknown game id → no split
    assert c._restart_split_suffix("ignored", {"game_id": "9999"}, game) is None


def test_final_result_accepts_list_stub_late_q4():
    """List-row snapshots carry quarter=NULL — a '4th Quarter' label within
    the final 2 game-minutes must verify OK, or no virtual game can score."""
    from blm_v4.scorecard import Scorecard
    stub = {"home_score": 75, "away_score": 77,
            "period_label": "4th Quarter", "clock": "00:30", "quarter": None}
    assert Scorecard._final_result(stub, 58)[0] == "OK"
    stub2 = dict(stub, clock="00:00")
    assert Scorecard._final_result(stub2, 58)[0] == "OK"
    stub3 = dict(stub, clock="")
    assert Scorecard._final_result(stub3, 58)[0] == "OK"
    # mid-Q4 loss (clock 05:00 = 35 game-min) is NOT a verified finish
    early = dict(stub, clock="05:00")
    assert Scorecard._final_result(early, 58)[0] == "UNKNOWN"
    # event-view row with quarter=4 but the "21:00" sentinel clock (panel's
    # finished-period placeholder, unparseable) — clean history + 4th-quarter
    # label IS a verified finish
    ev = {"home_score": 90, "away_score": 86,
          "period_label": "4th Quarter", "clock": "21:00", "quarter": 4}
    assert Scorecard._final_result(ev, 58)[0] == "OK"
    # event-view row with a parseable late clock -> OK
    ev2 = dict(ev, clock="00:15")
    assert Scorecard._final_result(ev2, 58)[0] == "OK"
    # too few snapshots stays UNKNOWN
    assert Scorecard._final_result(stub, 4)[0] == "UNKNOWN"


# ═══════════ Market-total path (stub snapshots must not null market) ═

def test_market_total_survives_list_stubs(tmp_path, monkeypatch):
    """List-level stubs (total_line=None) interleaved with event-view
    captures must not wipe the market total on the card or the scorecard."""
    st = _make_store(tmp_path)
    monkeypatch.setenv("BLM_POKERBET_DB", str(tmp_path / "blm.db"))
    gid_db = _add_game(st, "9501", "BETUAL_NBA", "H", "A", status="live")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    # event-view captures WITH market
    _snap(st, gid_db, "9501", "BETUAL_NBA", t0, 20, 18, 3, "06:00", 187.5)
    # interleaved list stubs WITHOUT market (quarter unknown, label present)
    _snap(st, gid_db, "9501", "BETUAL_NBA", t0 + timedelta(seconds=20), 22, 20, None, "05:45", None, "3rd Quarter")
    _snap(st, gid_db, "9501", "BETUAL_NBA", t0 + timedelta(seconds=40), 24, 22, None, "05:30", None, "3rd Quarter")
    # another event capture with a moved line
    _snap(st, gid_db, "9501", "BETUAL_NBA", t0 + timedelta(seconds=60), 26, 24, 3, "05:00", 189.5)
    _snap(st, gid_db, "9501", "BETUAL_NBA", t0 + timedelta(seconds=80), 28, 26, None, "04:45", None, "3rd Quarter")

    # API card: market must reflect the last REAL line, not the stub
    import blm_v4.api as v4api
    from blm_v4.api import _analyze_game
    conn = v4api._connect()
    try:
        game = conn.execute("SELECT * FROM games WHERE source_game_id='9501'").fetchone()
        rows = v4api._load_snapshots(conn, "9501")
    finally:
        conn.close()
    card = _analyze_game(dict(game), rows, datetime.now(timezone.utc))
    mkt = card["market"]
    assert mkt["total_line"] == 189.5          # last real line, not None
    assert card["model"]["expected_total"] is not None
    assert card["market_momentum"] == 2.0      # 189.5 - 187.5 (market rows only)

    # scorecard: market_total at a stub checkpoint = nearest prior real line
    sc = Scorecard(tmp_path / "blm.db")
    sc.record_fixed_checkpoints()
    conn = sc._connect()
    try:
        row = conn.execute(
            """SELECT market_total, checkpoint, progress FROM predictions
               WHERE source_game_id='9501' ORDER BY progress DESC LIMIT 1""").fetchone()
        assert row is not None
        assert row["market_total"] in (187.5, 189.5)
    finally:
        conn.close()


# ═══════════ Live-score floor (final projection >= live score) ═══════

def _proj_rows(scores, quarters, clocks, span_min=30.0,
               start="2026-08-30T20:00:00Z", total_line=None):
    """Build ascending snapshot rows; scores [(h, a), ...] parallel to
    quarters/clocks, spread evenly over span_min wall-clock minutes."""
    from datetime import timedelta
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    n = len(scores)
    rows = []
    for i, ((h, a), q, c) in enumerate(zip(scores, quarters, clocks)):
        rows.append({
            "captured_at": (t0 + timedelta(minutes=span_min * i / max(n - 1, 1))).isoformat(),
            "home_score": h, "away_score": a,
            "quarter": q, "clock": c, "period_label": None,
            "total_line": total_line, "spread": None,
            "home_total_line": None, "away_total_line": None,
            "w1_odds": None, "w2_odds": None,
            "total_over_odds": None, "total_under_odds": None,
            "spread_indicator": None,
        })
    return rows


def test_projection_never_below_live_score_109_114():
    """The live case: Boston 109-114 in Q4 with a slow pace window — the raw
    rate split would print home 89 / away 95 while the board already shows
    109-114.  The floor must start the projection FROM the live score:
    home >= 109, away >= 114, total >= 223, margin re-derived coherently."""
    rows = _proj_rows(
        [(40, 45), (52, 58), (61, 66), (73, 79), (88, 92), (98, 103), (109, 114)],
        [1, 1, 2, 2, 3, 3, 4],
        ["06:00", "05:00", "09:00", "07:30", "05:00", "03:30", "02:00"],
        span_min=30.0,
    )
    p = project(rows)
    assert p["home_score"] == 109 and p["away_score"] == 114
    assert p["home_projection"] >= 109
    assert p["away_projection"] >= 114
    assert p["expected_total"] >= 223
    assert abs(p["home_projection"] + p["away_projection"] - p["expected_total"]) < 0.11
    assert abs(p["expected_margin"] - (p["home_projection"] - p["away_projection"])) < 0.11


def test_projection_floor_all_quarters():
    """The invariant must hold at every game phase: Q1..Q4 and near-final."""
    cases = [
        (1, "08:00", 18, 16),
        (2, "06:00", 42, 39),
        (3, "04:00", 68, 71),
        (4, "02:00", 95, 97),
        (4, "00:30", 109, 114),
    ]
    for q, c, h, a in cases:
        rows = _proj_rows([(h - 14, a - 12), (h, a)], [max(q - 1, 1), q],
                          ["08:30", c], span_min=8.0)
        p = project(rows)
        assert p["home_projection"] >= h, (q, c, p)
        assert p["away_projection"] >= a, (q, c, p)
        assert p["expected_total"] >= h + a, (q, c, p)
        assert abs(p["home_projection"] + p["away_projection"] - p["expected_total"]) < 0.11


def test_projection_floor_one_team_only():
    """When only ONE team's raw split sits below the board (the other is
    already covered by the model), only that team is floored; total lifts
    to the sum of floored teams and the margin stays coherent."""
    # home 160 - away 120 at Q3 start; pace 272 -> raw home 176 (survives),
    # raw away 96 < 120 (trips).
    rows = _proj_rows(
        [(70, 40), (160, 120)],
        [1, 3], ["06:00", "10:00"], span_min=25.0,
    )
    p = project(rows)
    assert p["home_projection"] >= 160
    assert p["away_projection"] >= 120
    assert p["expected_total"] >= 280
    assert abs(p["home_projection"] + p["away_projection"] - p["expected_total"]) < 0.11
    assert abs(p["expected_margin"] - (p["home_projection"] - p["away_projection"])) < 0.11


def test_api_card_parity_with_projection():
    """api.py must NOT re-implement the model: the dashboard card's model
    block is exactly projection.project() (single source of truth), and the
    score shown on the card is the same snapshot the projection was built
    from."""
    from blm_v4.api import _analyze_game
    rows = _proj_rows(
        [(40, 45), (52, 58), (61, 66), (73, 79), (88, 92), (98, 103), (109, 114)],
        [1, 1, 2, 2, 3, 3, 4],
        ["06:00", "05:00", "09:00", "07:30", "05:00", "03:30", "02:00"],
        span_min=30.0, total_line=230.0,
    )
    p = project(rows)
    game = {"source_game_id": "t1", "source": "PokerBet", "classification": "BETUAL_NBA",
            "competition": "Betual NBA", "region": "Virtual Matches", "sport": "basketball",
            "home_team": "Boston Celtics Virtual", "away_team": "Sacramento Kings Virtual",
            "status": "live", "last_seen_at": rows[-1]["captured_at"], "id": 1}
    card = _analyze_game(dict(game), rows, datetime.now(timezone.utc))
    m = card["model"]
    assert m["home_projection"] == p["home_projection"]
    assert m["away_projection"] == p["away_projection"]
    assert m["expected_total"] == p["expected_total"]
    assert m["expected_margin"] == p["expected_margin"]
    assert m["pace"] == p["pace"]
    assert card["home_score"] == 109 and card["away_score"] == 114


# ═══════════ Prediction rebase (current code always wins) ═════════

def test_predictions_rebased_onto_current_code(tmp_path):
    """A prediction stored by an OLDER model build (pre live-score-floor)
    must be recomputed from the same snapshots on the next run — the
    scorecard measures v4-pace-1 AS DEFINED TODAY, never a dead build.

    Legacy rows like 'projected 58.0/85.1/143.2 while the board read
    95-104' violate the floor; rebase lifts them to the floored split."""
    st = _make_store(tmp_path)
    _ended_game_snapshots(st)                     # final 96-88 (184 total)
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.run()
    assert stats["recorded"]["recorded"] > 0
    conn = sc._connect()
    try:
        # simulate an old-build row: floor-violating total on the final cp
        conn.execute(
            "UPDATE predictions SET projected_home=58.0, projected_away=85.1,"
            " projected_total=143.2 WHERE checkpoint='final'")
        conn.commit()
    finally:
        conn.close()
    # rebase on next run: same snapshots, current code → floor-compliant
    stats = sc.run()
    assert stats["recorded"]["rebased"] >= 1
    conn = sc._connect()
    try:
        row = conn.execute(
            "SELECT projected_home, projected_away, projected_total, home_score, away_score"
            " FROM predictions WHERE checkpoint='final'").fetchone()
        # floor: projected_home >= home_score, projected_away >= away_score
        assert row["projected_home"] >= row["home_score"]
        assert row["projected_away"] >= row["away_score"]
        assert row["projected_total"] >= row["projected_home"] + row["projected_away"] - 0.05
        assert row["projected_total"] != 143.2
    finally:
        conn.close()
    # idempotent: a second rebase run changes nothing more
    stats = sc.run()
    assert stats["recorded"]["rebased"] == 0


# ═══════════ Historical market history + trends ═════════════════

def _clean_game(st: PokerBetStore, gid: str, t0: datetime | None = None,
                olvc: float = 190.0, clv: float = 190.0,
                cls: str = "BETUAL_NBA") -> int:
    """Full monotonic Q1..Q4 game ending 96-88 (total 184).  First snap
    carries OLVC, last carries CLV; middle snaps are line-less stubs."""
    t0 = t0 or (datetime.now(timezone.utc) - timedelta(minutes=20))
    gid_db = _add_game(st, gid, cls, "Home Virtual", "Away Virtual", status="ended")
    snaps = [
        (0, 0, 1, "09:00"), (8, 6, 1, "06:00"), (16, 12, 1, "03:00"),
        (24, 20, 1, "00:00"), (30, 26, 2, "09:00"), (40, 34, 2, "06:00"),
        (52, 42, 2, "03:00"), (60, 50, 2, "00:00"), (66, 58, 3, "09:00"),
        (76, 66, 3, "06:00"), (82, 72, 3, "03:00"), (86, 78, 3, "00:00"),
        (88, 80, 4, "09:00"), (92, 84, 4, "06:00"), (96, 88, 4, "00:00"),
    ]
    n = len(snaps)
    for i, (hs, as_, q, clock) in enumerate(snaps):
        line = olvc if i == 0 else clv if i == n - 1 else None
        _snap(st, gid_db, gid, cls, t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, line)
    return gid_db


def test_market_history_recorded_for_clean_game(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_ANALYTICS_TZ", "UTC")
    st = _make_store(tmp_path)
    t0 = datetime(2026, 8, 30, 1, 30, tzinfo=timezone.utc)
    _clean_game(st, "9701", t0=t0, olvc=190.0, clv=195.0)  # final 184 -> UNDER
    sc = Scorecard(tmp_path / "blm.db")
    stats = sc.run()
    assert stats["market"]["recorded"] == 1
    conn = sc._connect()
    try:
        row = conn.execute(
            "SELECT * FROM market_history WHERE source_game_id='9701'").fetchone()
        assert row["opening_total"] == 190.0
        assert row["closing_total"] == 195.0          # OLVC never overwritten
        assert row["total_line_move"] == 5.0
        assert row["market_move"] == "UP"
        assert row["outcome_olvc"] == "UNDER"
        assert row["outcome_clv"] == "UNDER"
        assert row["opening_total_edge"] == -6.0      # 184 - 190
        assert row["closing_total_edge"] == -11.0     # 184 - 195
        assert row["started_hour"] == 1
        assert row["analytics_tz"] == "UTC"
        assert row["started_date"] == "2026-08-30"
        assert row["started_dow"] is not None
        assert row["duration_min"] > 0
        assert "v4-pace-1" in (row["model_versions"] or "")
    finally:
        conn.close()
    # idempotent: re-run never duplicates
    sc.run()
    conn = sc._connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM market_history "
                         "WHERE source_game_id='9701'").fetchone()["c"]
        assert n == 1
    finally:
        conn.close()


def test_market_history_timezone_basis(tmp_path, monkeypatch):
    """The analytical timezone is explicit: 23:30 UTC = 01:30 SAST next
    day — hour AND date must reflect the configured basis."""
    monkeypatch.setenv("BLM_ANALYTICS_TZ", "Africa/Johannesburg")
    st = _make_store(tmp_path)
    t0 = datetime(2026, 8, 30, 23, 30, tzinfo=timezone.utc)
    _clean_game(st, "9905", t0=t0)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        row = conn.execute(
            "SELECT * FROM market_history WHERE source_game_id='9905'").fetchone()
        assert row["analytics_tz"] == "Africa/Johannesburg"
        assert row["started_hour"] == 1
        assert row["started_date"] == "2026-08-31"
    finally:
        conn.close()


def test_market_history_skips_fragment_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_ANALYTICS_TZ", "UTC")
    st = _make_store(tmp_path)
    # fragment: 6 snaps, all 4th quarter (the 30739952#i2 shape)
    gid_db = _add_game(st, "9702", "BETUAL_NBA", "H", "A", status="ended")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=2)
    for i, (hs, as_, clock) in enumerate([(60, 66, "02:00"), (64, 70, "01:30"),
                                          (70, 74, "01:00"), (78, 80, "00:45"),
                                          (86, 84, "00:30"), (92, 88, "00:15")]):
        _snap(st, gid_db, "9702", "BETUAL_NBA",
              t0 + timedelta(seconds=20 * i), hs, as_, 4, clock, 185.0)
    # invalid: score regression (away 6 -> 5)
    gid_db2 = _add_game(st, "9703", "BETUAL_NBA", "H", "A", status="ended")
    t0b = datetime.now(timezone.utc) - timedelta(minutes=20)
    snaps = [(0, 0, 1, "09:00"), (8, 6, 1, "06:00"), (10, 5, 1, "03:00"),
             (24, 20, 1, "00:00"), (30, 26, 2, "09:00"), (40, 34, 2, "06:00"),
             (52, 42, 2, "03:00"), (60, 50, 2, "00:00"), (66, 58, 3, "09:00"),
             (76, 66, 3, "06:00"), (82, 72, 3, "03:00"), (86, 78, 3, "00:00"),
             (88, 80, 4, "09:00"), (92, 84, 4, "06:00"), (96, 88, 4, "00:00")]
    for i, (hs, as_, q, clock) in enumerate(snaps):
        _snap(st, gid_db2, "9703", "BETUAL_NBA",
              t0b + timedelta(minutes=i * 1.2), hs, as_, q, clock,
              190.0 if i in (0, len(snaps) - 1) else None)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM market_history").fetchone()["c"]
        assert n == 0  # neither fragments nor invalid games enter the base
    finally:
        conn.close()


def test_market_history_single_line_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_ANALYTICS_TZ", "UTC")
    st = _make_store(tmp_path)
    _clean_game(st, "9704", olvc=184.0, clv=184.0)  # final 184 -> PUSH, no move
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        row = conn.execute(
            "SELECT * FROM market_history WHERE source_game_id='9704'").fetchone()
        assert row["outcome_olvc"] == "PUSH" and row["outcome_clv"] == "PUSH"
        assert row["total_line_move"] == 0.0
        assert row["market_move"] == "UNCHANGED"
        assert row["opening_total_edge"] == 0.0
    finally:
        conn.close()


def test_trends_market_performance_and_time_of_day(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_ANALYTICS_TZ", "UTC")
    st = _make_store(tmp_path)
    # hour 1: 184 vs 190/195 -> UNDER both (edges -6 / -11)
    _clean_game(st, "9801", t0=datetime(2026, 8, 30, 1, 30, tzinfo=timezone.utc),
                olvc=190.0, clv=195.0)
    # hour 12: 184 vs 175/178 -> OVER both (edges +9 / +6)
    _clean_game(st, "9802", t0=datetime(2026, 8, 30, 12, 30, tzinfo=timezone.utc),
                olvc=175.0, clv=178.0)
    # hour 12: 184 vs 184/184 -> PUSH both
    _clean_game(st, "9803", t0=datetime(2026, 8, 30, 12, 45, tzinfo=timezone.utc),
                olvc=184.0, clv=184.0)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        from blm_v4.trends import market_performance, time_of_day
        mp = market_performance(conn)
        assert mp["clv"]["n"] == 3
        assert mp["clv"]["over"] == 1 and mp["clv"]["under"] == 1
        assert mp["clv"]["push"] == 1
        assert mp["clv"]["over_pct"] == 33.3
        assert mp["olvc"]["n"] == 3
        assert mp["olvc"]["over"] == 1 and mp["olvc"]["under"] == 1
        assert mp["clv"]["avg_edge"] == round((-11 + 6 + 0) / 3, 2)
        assert mp["olvc"]["avg_edge"] == round((-6 + 9 + 0) / 3, 2)
        tod = time_of_day(conn)
        h1 = next(b for b in tod["hourly"] if b["hour"] == "01-02")
        assert h1["games"] == 1 and h1["clv_n"] == 1
        h12 = next(b for b in tod["hourly"] if b["hour"] == "12-13")
        assert h12["games"] == 2 and h12["clv_n"] == 2
        assert h12["over_clv"] == 1 and h12["push_clv"] == 1
        g01 = next(b for b in tod["grouped"] if b["period"] == "01-05")
        assert g01["games"] == 1
        g10 = next(b for b in tod["grouped"] if b["period"] == "10-13")
        assert g10["games"] == 2 and g10["over_clv"] == 1
        assert g10["mae_clv"] == round((abs(6) + abs(0)) / 2, 2)  # 3.0
    finally:
        conn.close()


def test_trends_model_vs_market(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_ANALYTICS_TZ", "UTC")
    st = _make_store(tmp_path)
    _clean_game(st, "9901", olvc=190.0, clv=195.0)  # final 184 -> UNDER
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        from blm_v4.trends import model_vs_market
        mm = model_vs_market(conn)
        by = mm["by_version"]
        assert "v4-pace-1" in by
        v = by["v4-pace-1"]
        assert v["n"] > 0
        assert v["dir_valid"] == v["n"]            # every pred has a market line
        assert v["dir_hit_rate"] is not None
        assert v["beat_market_rate"] is not None
        assert v["by_checkpoint"]                  # checkpoint breakdown present
    finally:
        conn.close()


# ═══════════ Market data first-class (live PokerBet lines) ═════════

def test_opening_snapshot_returns_first_line():
    """opening_snapshot = FIRST verified line (never moves with the market);
    market_snapshot = LATEST.  Empty history → None (honest, no fabrication)."""
    from blm_v4.projection import market_snapshot, opening_snapshot
    rows = [
        {"total_line": 190.0, "captured_at": "2026-08-30T20:00:00Z"},
        {"total_line": None,  "captured_at": "2026-08-30T20:00:20Z"},
        {"total_line": 195.0, "captured_at": "2026-08-30T20:00:40Z"},
        {"total_line": 193.0, "captured_at": "2026-08-30T20:01:00Z"},
    ]
    op = opening_snapshot(rows)
    assert op is not None and op["total_line"] == 190.0
    assert op["captured_at"] == "2026-08-30T20:00:00Z"
    latest = market_snapshot(rows)
    assert latest is not None and latest["total_line"] == 193.0   # latest, not opening
    assert opening_snapshot([]) is None
    assert opening_snapshot([{"total_line": None}]) is None  # stubs ≠ market


def test_closing_snapshot_only_when_ended():
    """closing_snapshot = None while live (latest live line is NOT closing);
    once ended = LAST verified line, immutable.  No lines → None."""
    from blm_v4.projection import closing_snapshot
    rows = [
        {"total_line": 190.0, "captured_at": "2026-08-30T20:00:00Z"},
        {"total_line": None,  "captured_at": "2026-08-30T20:00:20Z"},
        {"total_line": 195.0, "captured_at": "2026-08-30T20:00:40Z"},
        {"total_line": 193.0, "captured_at": "2026-08-30T20:01:00Z"},
    ]
    # still live → latest line is NOT closing
    assert closing_snapshot(rows, ended=False) is None
    # ended → last verified line is closing (immutable)
    cl = closing_snapshot(rows, ended=True)
    assert cl is not None and cl["total_line"] == 193.0
    assert cl["captured_at"] == "2026-08-30T20:01:00Z"
    # no verified lines → honest None even when ended
    assert closing_snapshot([{"total_line": None}], ended=True) is None
    assert closing_snapshot([], ended=True) is None


def test_prediction_freezes_market_at_checkpoint(tmp_path):
    """A prediction's market_total must be the last observed PokerBet line
    AT that checkpoint — a later line movement must NEVER rewrite it.
    Also: market_total is the observed line, never the model's projection."""
    st = _make_store(tmp_path)
    gid = "9501"
    t0 = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)
    gid_db = _add_game(st, gid, "BETUAL_NBA", "Home Virtual", "Away Virtual", status="ended")
    # Q1 with line 190.0, Q2 with line 195.0, ... final 96-88 (184)
    snaps = [
        (0, 0, 1, "09:00", 190.0), (8, 6, 1, "06:00", None), (16, 12, 1, "03:00", None),
        (24, 20, 1, "00:00", None), (30, 26, 2, "09:00", 195.0), (40, 34, 2, "06:00", None),
        (52, 42, 2, "03:00", None), (60, 50, 2, "00:00", None), (66, 58, 3, "09:00", None),
        (76, 66, 3, "06:00", None), (82, 72, 3, "03:00", None), (86, 78, 3, "00:00", None),
        (88, 80, 4, "09:00", None), (92, 84, 4, "06:00", None), (96, 88, 4, "00:00", None),
    ]
    for i, (hs, as_, q, clock, line) in enumerate(snaps):
        _snap(st, gid_db, gid, "BETUAL_NBA", t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, line)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        q1 = conn.execute(
            "SELECT market_total FROM predictions WHERE source_game_id=? AND checkpoint='q1'",
            (gid,)).fetchone()
        q2 = conn.execute(
            "SELECT market_total FROM predictions WHERE source_game_id=? AND checkpoint='q2'",
            (gid,)).fetchone()
        assert q1["market_total"] == 190.0   # frozen at Q1 (line 190.0)
        assert q2["market_total"] == 195.0   # frozen at Q2 (line moved to 195.0)
        # later line (195.0) must NOT appear in the Q1 prediction
        assert q1["market_total"] != 195.0
        # re-run: freeze is stable (idempotent)
    finally:
        conn.close()
    sc.run()
    conn = sc._connect()
    try:
        q1 = conn.execute(
            "SELECT market_total FROM predictions WHERE source_game_id=? AND checkpoint='q1'",
            (gid,)).fetchone()
        assert q1["market_total"] == 190.0
    finally:
        conn.close()


def test_fixed_checkpoint_freezes_market(tmp_path):
    """10-90% fixed checkpoints also freeze the market line available at
    that moment."""
    st = _make_store(tmp_path)
    gid = "9502"
    t0 = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)
    gid_db = _add_game(st, gid, "BETUAL_NBA", "Home Virtual", "Away Virtual", status="ended")
    snaps = [
        (0, 0, 1, "09:00", 190.0), (8, 6, 1, "06:00", None), (16, 12, 1, "03:00", None),
        (24, 20, 1, "00:00", None), (30, 26, 2, "09:00", 195.0), (40, 34, 2, "06:00", None),
        (52, 42, 2, "03:00", None), (60, 50, 2, "00:00", None), (66, 58, 3, "09:00", None),
        (76, 66, 3, "06:00", None), (82, 72, 3, "03:00", None), (86, 78, 3, "00:00", None),
        (88, 80, 4, "09:00", None), (92, 84, 4, "06:00", None), (96, 88, 4, "00:00", None),
    ]
    for i, (hs, as_, q, clock, line) in enumerate(snaps):
        _snap(st, gid_db, gid, "BETUAL_NBA", t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, line)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        # pct10 should be at Q1 (line 190), pct20-40 at Q2 (line 195)
        p10 = conn.execute(
            "SELECT market_total FROM predictions WHERE source_game_id=? AND checkpoint='pct10'",
            (gid,)).fetchone()
        p40 = conn.execute(
            "SELECT market_total FROM predictions WHERE source_game_id=? AND checkpoint='pct40'",
            (gid,)).fetchone()
        assert p10["market_total"] == 190.0
        assert p40["market_total"] == 195.0
    finally:
        conn.close()


# ═══════════ Fragment classification (short histories never headline) ═

def test_fragments_excluded_from_headline(tmp_path, monkeypatch):
    """Short-history games (FRAGMENT) are scored for diagnostics but never
    included in headline model-accuracy metrics."""
    st = _make_store(tmp_path)
    monkeypatch.setenv("BLM_POKERBET_DB", str(tmp_path / "blm.db"))
    _ended_game_snapshots(st, gid="9601")          # FULL: 16 snaps from Q1
    # FRAGMENT: 6 snaps, all 4th Quarter (the live 96-second-fragment shape)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=3)
    gf = _add_game(st, "9602", "BETUAL_NBA", "G1", "G2", status="ended")
    for i, ck in enumerate(["02:00", "01:45", "01:15", "01:00", "00:45", "00:15"]):
        _snap(st, gf, "9602", "BETUAL_NBA", t0 + timedelta(seconds=10 + i * 15),
              90 + i, 88 + i, 4, ck)

    sc = Scorecard(tmp_path / "blm.db")
    out = sc.run()
    assert out["scored"]["scored"] > 0
    conn = sc._connect()
    try:
        frag_rows = [dict(r) for r in conn.execute(
            "SELECT source_game_id, fragment FROM prediction_scores")]
        frag = {r["source_game_id"]: r["fragment"] for r in frag_rows}
        assert frag.get("9601") == 0 and frag.get("9602") == 1
        n_full = sum(1 for r in frag_rows if r["fragment"] == 0)
        n_frag = sum(1 for r in frag_rows if r["fragment"] == 1)
        assert n_full > 0 and n_frag > 0
    finally:
        conn.close()
    summary = sc.summary()
    ver = summary["versions"]["v4-pace-1"]
    assert ver["predictions"] == n_full            # headline = FULL only
    assert ver["games"] == 1
    frags = summary["versions"]["_fragments"]
    assert frags["excluded_from_headline"] is True
    assert frags["games"] == 1
    assert frags["predictions"] == n_frag


def test_event_view_verify_guard(tmp_path, monkeypatch):
    """Lobby/foreign event-view content must never be stored — the parsed
    page is only accepted when its scoreboard teams match the tracked game."""
    st = _make_store(tmp_path)
    monkeypatch.setenv("BLM_POKERBET_DB", str(tmp_path / "blm.db"))
    c = PokerBetCollector(store=st)
    game = PokerBetGame(
        source="PokerBet", source_game_id="9701", competition_id="comp",
        competition_slug="b", competition="Betual NBA", region="R",
        game_family="betual", classification="BETUAL_NBA", sport="basketball",
        home_team="Maccabi Tel Aviv Virtual",
        away_team="KK Partizan Belgrade Virtual",
        game_slug="m-p", source_url="https://x/9701", status="live")
    assert c._verified_event_view(
        game, {"home_team": "Maccabi Tel Aviv Virtual",
               "away_team": "KK Partizan Belgrade Virtual"}) is True
    # lobby text: no scoreboard teams within the parse window
    assert c._verified_event_view(game, {"home_team": "", "away_team": ""}) is False
    # a different game's event view (the WNBA state seen on every fixture)
    assert c._verified_event_view(
        game, {"home_team": "Minnesota Lynx", "away_team": "Atlanta Dream"}) is False
    # case/whitespace insensitive
    assert c._verified_event_view(
        game, {"home_team": "  maccabi tel aviv virtual ",
               "away_team": "kk partizan belgrade virtual"}) is True


# ═══════════ Helpers ══════════════════════════════════════════

def test_checkpoint_for_and_progress():
    assert _checkpoint_for(1, "08:00") == "q1"
    assert _checkpoint_for(2, "05:00") == "q2"
    assert _checkpoint_for(4, "01:00") == "final"
    assert _checkpoint_for(4, "03:00") == "q4"
    row = {"quarter": 2, "clock": "05:00"}
    assert abs(_progress_of(row) - 0.375) < 0.01  # 15/40 min
    row2 = {"quarter": None, "period_label": "3rd Quarter", "clock": "05:00"}
    assert abs(_progress_of(row2) - 0.625) < 0.01  # fallback via label
