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

import pytest

from blm_v4.collector import PokerBetCollector
from blm_v4.discovery import RowGame
from blm_v4.models import MarketObservation, PokerBetGame, utcnow_iso
from blm_v4.projection import MODEL_VERSION
from blm_v4.scorecard import (
    MAX_DISTANCE_PCT,
    FIXED_CHECKPOINT_PCTS,
    Scorecard,
    _checkpoint_for,
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
    assert mc["over"] + mc["under"] + mc["push"] == mc["ou_predictions"]


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
                                    _row(31, 24, 2, "20:00")) is True
    assert c._detect_instance_reset(c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"],
                                    _row(100, 92, 4, "01:00")) is False  # continuation


def test_collector_detects_event_view_reset(tmp_path):
    """The event-view capture path must detect the same replay reset."""
    st = _make_store(tmp_path)
    _ended_game_snapshots(st, gid="5001")  # last snapshot 96-88
    c = _collector(st)
    game = c._tracked["BETUAL_NBA"]["Home Virtual|Away Virtual"]
    assert c._detect_event_reset(game, 25, 32) is True    # new replay, big drop
    assert c._detect_event_reset(game, 97, 89) is False   # continuation


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
    assert c._detect_event_reset(game, 28, 28, "1st Quarter", "01:30") is True
    # same-phase continuation must NOT trigger
    assert c._detect_event_reset(game, 44, 54, "4th Quarter", "00:30") is False
    # clock regression alone (even with a rising score) triggers
    assert c._detect_event_reset(game, 50, 55, "2nd Quarter", "05:00") is True
    # score explosion within a short window (Q1 19-14 -> Q4 62-71 in 11s)
    # is a different replay — triggers even with a rising score and phase
    _snap(st, gid_db, "5002", "BETUAL_NBA", datetime.now(timezone.utc), 19, 14, 1, "03:45")
    assert c._detect_event_reset(game, 62, 71, "4th Quarter", "21:00") is True


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
        lambda text: {"home_score": 28, "away_score": 28,
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
        lambda text: {"home_score": 103, "away_score": 68,
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
