"""Settlement-semantics regression tests (forensic settlement directive).

The invariant under test — BLM prediction vs MARKET LINE chooses the side;
ACTUAL TOTAL vs MARKET LINE settles WIN/LOSS/PUSH; model prediction error
is a model metric and NEVER a betting result:

    BLM=190 Market=170 Actual=180  ->  OVER, WIN   (error -10, still a win)
    BLM=150 Market=170 Actual=160  ->  UNDER, WIN  (error +10, still a win)
    BLM=190 Market=170 Actual=160  ->  OVER, LOSS
    BLM=150 Market=170 Actual=180  ->  UNDER, LOSS
    BLM=190 Market=170 Actual=170  ->  PUSH (actual == market)
    BLM=170 Market=170 Actual=180  ->  PUSH (position == market, no side)

Plus: the settlement-integrity scan over a real synthetic pipeline run,
the 12:00/Half-End position-sentinel fix (checkpoint pct labels), and the
market_history WS-line fallback (snapshots carry no lines in production).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from blm_v4.collector import PokerBetCollector  # noqa: F401  (schema side-effects)
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.projection import clock_minutes
from blm_v4.scorecard import (
    Scorecard,
    _checkpoint_outcome,
    _progress_of,
    settlement_integrity_violations,
)
from blm_v4.storage import PokerBetStore

# ═══════════════ helpers (mirror test_scorecard.py conventions) ═════

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_store(tmp_path) -> PokerBetStore:
    return PokerBetStore(tmp_path / "blm.db")


def _add_game(st: PokerBetStore, gid: str, cls: str, home: str, away: str,
              status: str = "ended") -> int:
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
          total: float | None = None) -> None:
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid, classification=cls,
        captured_at=_iso(t), home_team="H", away_team="A",
        home_score=hs, away_score=as_,
        period_label=f"{q}th Quarter" if q else "", quarter=q, clock=clock,
        game_status="live", total_line=total,
        markets_json=json.dumps({"total": {"first_line": total}}) if total else "{}",
    )
    st.insert_snapshot(gid_db, obs, force=True)


def _full_game(st: PokerBetStore, gid: str = "9001",
               cls: str = "BETUAL_NBA", final_total: int = 184,
               with_snap_lines: bool = True) -> int:
    """Full monotonic Q1..Q4 game ending at final_total (default 96-88)."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=25)
    gid_db = _add_game(st, gid, cls, "Home Virtual", "Away Virtual")
    line = 190.0 if with_snap_lines else None
    snaps = [
        (0, 0, 1, "09:00"), (8, 6, 1, "06:00"), (16, 12, 1, "03:00"),
        (24, 20, 1, "00:00"), (30, 26, 2, "09:00"), (40, 34, 2, "06:00"),
        (52, 42, 2, "03:00"), (60, 50, 2, "00:00"), (66, 58, 3, "09:00"),
        (76, 66, 3, "06:00"), (82, 72, 3, "03:00"), (86, 78, 3, "00:00"),
        (88, 80, 4, "09:00"), (92, 84, 4, "06:00"), (96, 88, 4, "00:00"),
    ]
    for i, (hs, as_, q, clock) in enumerate(snaps):
        _snap(st, gid_db, gid, cls, t0 + timedelta(minutes=i * 1.2),
              hs, as_, q, clock, line)
    return gid_db


# ═══════════ 1. the six settlement cases (pure semantics) ═══════════

def test_over_win_blm_error_does_not_decide():
    """BLM=190 Market=170 Actual=180: OVER WIN despite error -10."""
    out = _checkpoint_outcome(190.0, 170.0, 180)
    assert out == "OVER_WIN"
    assert 180 != 190  # the model was wrong...
    assert out == "OVER_WIN"  # ...but the bet still won


def test_under_win():
    out = _checkpoint_outcome(150.0, 170.0, 160)
    assert out == "UNDER_WIN"


def test_over_loss():
    out = _checkpoint_outcome(190.0, 170.0, 160)
    assert out == "OVER_LOSS"


def test_under_loss():
    out = _checkpoint_outcome(150.0, 170.0, 180)
    assert out == "UNDER_LOSS"


def test_push_actual_on_line():
    assert _checkpoint_outcome(190.0, 170.0, 170) == "PUSH"


def test_push_position_on_line():
    assert _checkpoint_outcome(170.0, 170.0, 180) == "PUSH"


# ═══════════ 2. same cases through the authoritative scorer ═════════

def _scored(model_total, market_total, actual_total):
    """Run Scorecard._score_row with the directive's primitive values."""
    tup = Scorecard._score_row({
        "pid": 1, "source_game_id": "g", "classification": "BETUAL_NBA",
        "model_version": "v4-pace-1",
        "projected_home": model_total / 2.0, "projected_away": model_total / 2.0,
        "projected_total": model_total, "market_total": market_total,
        "final_home": actual_total // 2, "final_away": actual_total - actual_total // 2,
        "final_total": actual_total, "result_at": _iso(
            datetime.now(timezone.utc) + timedelta(hours=1)),
    })
    return {"ou_prediction": tup[16], "ou_result": tup[17],
            "ou_correct": tup[18], "total_error": tup[6]}


def test_scorer_over_win():
    s = _scored(190, 170, 180)
    assert s["ou_prediction"] == 1 and s["ou_result"] == 1 and s["ou_correct"] == 1
    assert s["total_error"] == 10  # model-actual sign convention; irrelevant to result


def test_scorer_under_win():
    s = _scored(150, 170, 160)
    assert s["ou_prediction"] == -1 and s["ou_result"] == -1 and s["ou_correct"] == 1


def test_scorer_over_loss():
    s = _scored(190, 170, 160)
    assert s["ou_prediction"] == 1 and s["ou_result"] == -1 and s["ou_correct"] == 0


def test_scorer_under_loss():
    s = _scored(150, 170, 180)
    assert s["ou_prediction"] == -1 and s["ou_result"] == 1 and s["ou_correct"] == 0


def test_scorer_push():
    s = _scored(190, 170, 170)
    assert s["ou_result"] == 0 and s["ou_correct"] == 0  # push, not loss


# ═══════════ 3. integrity scan over a real pipeline run ════════════

def test_pipeline_zero_settlement_violations(tmp_path):
    st = _make_store(tmp_path)
    _full_game(st, "9001")
    _full_game(st, "9002", final_total=196)
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        v = settlement_integrity_violations(conn)
        assert v["prediction_scores_total"] > 0
        assert v["ps_side_mismatch"] == []
        assert v["ps_result_mismatch"] == []
        assert v["ps_correct_mismatch"] == []
        assert v["ps_stored_ou_without_market"] == []
        assert v["cm_outcome_mismatch"] == []
        assert v["mh_outcome_mismatch"] == []
        assert v["dup_checkpoint_market"] == [] and v["dup_predictions"] == []
        assert v["scored_after_result"] == []
        assert v["contaminated_cm"] == [] and v["contaminated_ps"] == []
    finally:
        conn.close()


def test_integrity_scan_detects_corruption(tmp_path):
    """A WIN stored where sides disagree must be caught by the scan."""
    st = _make_store(tmp_path)
    _full_game(st, "9001")
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        # corrupt one row: sides disagree (pred OVER, actual UNDER) yet WIN
        conn.execute("""
            UPDATE prediction_scores
            SET ou_correct = 1
            WHERE ou_prediction = 1 AND ou_result = -1
            LIMIT 1""")
        conn.commit()
        v = settlement_integrity_violations(conn)
        assert len(v["ps_correct_mismatch"]) >= 1
        # and corrupt a checkpoint outcome
        conn.execute("""
            UPDATE checkpoint_market SET outcome = 'OVER_WIN'
            WHERE outcome = 'UNDER_WIN' LIMIT 1""")
        conn.commit()
        v = settlement_integrity_violations(conn)
        assert len(v["cm_outcome_mismatch"]) >= 1
    finally:
        conn.close()


# ═══════════ 4. 12:00 sentinel / Half-End position fix ═════════════

def test_clock_minutes_1200_sentinel_is_period_start():
    # Q2 at the 12:00 sentinel = 10 elapsed (was 8 -> pct20 mislabel)
    assert clock_minutes(2, "12:00") == 10.0
    assert clock_minutes(4, "12:00") == 30.0
    # pre-tick displays clamp to period start instead of going negative
    assert clock_minutes(2, "11:30") == 10.0
    assert clock_minutes(1, "12:00") == 0.0


def test_progress_of_1200_sentinel():
    assert _progress_of({"quarter": 2, "clock": "12:00"}) == 0.25
    assert _progress_of({"quarter": 4, "clock": "12:00"}) == 0.75
    assert _progress_of({"quarter": 1, "clock": "12:00"}) == 0.0


def test_progress_of_half_end():
    assert _progress_of({"quarter": 2, "period_label": "Half End",
                         "clock": "12:00"}) == 0.5
    assert _progress_of({"period_label": "Half End", "quarter": None,
                         "clock": "12:00"}) == 0.5


# ═══════════ 5. market_history WS-line fallback (D1) ═══════════════

def test_market_history_uses_ws_lines_when_snapshots_carry_none(tmp_path):
    """Snapshots in production carry no total_line; OLV/CLV must come from
    market_observations (same authoritative fallback as checkpoint_market)
    so market_history outcomes are recorded instead of all-NULL."""
    st = _make_store(tmp_path)
    _full_game(st, "9001", with_snap_lines=False)  # final total 184
    t0 = datetime.now(timezone.utc) - timedelta(minutes=25)
    for i, lv in enumerate([185.5, 187.5, 184.5]):
        st.upsert_market_observation({
            "source_game_id": "9001",
            "captured_at": _iso(t0 + timedelta(minutes=i * 4)),
            "market_type": "MatchTotal", "market_name": "Total Points",
            "line_value": lv, "raw": {},
        })
    sc = Scorecard(tmp_path / "blm.db")
    sc.run()
    conn = sc._connect()
    try:
        row = conn.execute(
            "SELECT opening_total, closing_total, final_total, "
            "outcome_olvc, outcome_clv, opening_total_edge, closing_total_edge "
            "FROM market_history WHERE source_game_id='9001'"
        ).fetchone()
        assert row is not None
        # OLV = 185.5 (earliest obs), CLV = 184.5 (latest obs)
        assert row["opening_total"] == 185.5
        assert row["closing_total"] == 184.5
        assert row["final_total"] == 184
        assert row["outcome_olvc"] == "UNDER"    # 184 < 185.5
        assert row["outcome_clv"] == "UNDER"     # 184 < 184.5
        assert row["opening_total_edge"] == -1.5
        assert row["closing_total_edge"] == -0.5
    finally:
        conn.close()
