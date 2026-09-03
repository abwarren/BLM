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
    # fair 170 == market 170 but actual 180 != market -> NO_EDGE (position
    # no-bet), NOT PUSH.  A genuine PUSH requires actual == market.
    assert _checkpoint_outcome(170.0, 170.0, 180) == "NO_EDGE"


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


# ═══════════ Diagnostic label fix: BLM selection vs actual outcome ═══════════

class TestDiagnosticSelectionVsOutcome:
    """The O/U diagnostic block labelled 'BLM Over / BLM Under' must count
    ou_prediction (the side BLM selected vs the market line), NOT ou_result
    (the actual market-side outcome).  The two distributions answer different
    questions and must stay separate.  Synthetic data: ou_prediction OVER=1 /
    UNDER=1 while ou_result OVER=2 / UNDER=0 — a regression implementation
    counting ou_result returns ou_over=2 and this test fails."""

    def test_blm_counts_use_ou_prediction_not_ou_result(self, tmp_path):
        import sqlite3
        from blm_v4.scorecard import _market_compare_sql
        db = tmp_path / "sel.db"
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE prediction_scores (
                prediction_id INTEGER PRIMARY KEY,
                model_total REAL, market_total REAL, actual_total REAL,
                total_error REAL, market_error REAL, model_beat_market INTEGER,
                ou_prediction INTEGER, ou_result INTEGER, ou_correct INTEGER,
                fragment INTEGER);
            -- (1) BLM OVER pick (190>170), actual OVER (180>170)
            -- (2) BLM UNDER pick (150<170), actual OVER (180>170)
            INSERT INTO prediction_scores VALUES
              (1, 190, 170, 180,  10, -10, 1,  1,  1, 1, 0),
              (2, 150, 170, 180, -30, -10, 0, -1,  1, 0, 0);
        """)
        try:
            out = _market_compare_sql(con)
            assert out["n"] == 2
            # BLM selections (ou_prediction): 1 OVER + 1 UNDER
            assert out["ou_over"] == 1, \
                f"BLM Over must count ou_prediction==1, got {out['ou_over']}"
            assert out["ou_under"] == 1
            assert out["ou_push"] == 0
            # Actual outcomes (ou_result): 2 OVER + 0 UNDER
            assert out["actual_over"] == 2
            assert out["actual_under"] == 0
            assert out["actual_push"] == 0
            # partition: every market-bearing row has exactly one selection
            assert out["ou_over"] + out["ou_under"] + out["ou_push"] \
                == out["ou_predictions"] == 2
        finally:
            con.close()


# ═══════════ NO_EDGE vs PUSH accounting (settlement-forensic fix) ═══════════

class TestNoEdgeVsPush:
    """MODEL POSITION == market is NO_EDGE/NO_BET, never PUSH.  A genuine
    settlement PUSH exists ONLY when actual == market.  With x.5 markets
    and integer actuals the outcome-push branch is unreachable, so the
    expected true PUSH count is 0 for the live population."""

    def test_fair_eq_market_actual_below_is_no_edge(self):
        # fair 175.5 == market 175.5, actual 172 -> NO_EDGE (NOT PUSH)
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(175.5, 175.5, 172) == "NO_EDGE"

    def test_over_win(self):
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(190, 170.5, 180) == "OVER_WIN"

    def test_over_loss(self):
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(190, 170.5, 160) == "OVER_LOSS"

    def test_under_win(self):
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(160, 175.5, 170) == "UNDER_WIN"

    def test_under_loss(self):
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(160, 175.5, 180) == "UNDER_LOSS"

    def test_actual_eq_market_is_true_push(self):
        # fair != market, actual == market -> genuine PUSH
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(190, 170.5, 170.5) == "PUSH"

    def test_fair_and_actual_eq_market_is_no_edge(self):
        # fair == market AND actual == market -> NO_EDGE for the position
        # (the market outcome itself is a PUSH, but BLM had no bet)
        from blm_v4.scorecard import _checkpoint_outcome
        assert _checkpoint_outcome(170.5, 170.5, 170.5) == "NO_EDGE"

    def test_no_edge_and_push_never_conflated(self):
        from blm_v4.scorecard import _checkpoint_outcome
        # the two branches must be mutually exclusive
        r1 = _checkpoint_outcome(175.5, 175.5, 172)   # position no-bet
        r2 = _checkpoint_outcome(190, 170.5, 170.5)   # genuine push
        assert r1 == "NO_EDGE" and r2 == "PUSH" and r1 != r2

    def test_integrity_scan_expects_no_edge_split(self):
        """The integrity scan must expect NO_EDGE for fair==market and
        PUSH only for actual==market — and must report 0 mismatches on
        the reclassified rows."""
        import sqlite3
        from blm_v4.scorecard import (SCORECARD_SCHEMA,
                                      _checkpoint_outcome,
                                      settlement_integrity_violations)
        db = tmp_path = __import__("pathlib").Path("/tmp") / "noedge_scan.db"
        try:
            db.unlink()
        except FileNotFoundError:
            pass
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        con.executescript(SCORECARD_SCHEMA)
        # craft a minimal checkpoint_market via direct insert (mirror the
        # writer: outcome = _checkpoint_outcome(fair, live, actual))
        con.execute("""
            INSERT INTO checkpoint_market (source_game_id, classification,
                checkpoint_pct, checkpoint_timestamp, model_version,
                opening_line, live_market_line, market_timestamp,
                blm_fair_value, closing_line, actual_final_total,
                market_vs_fair, signal, outcome, frozen, recorded_at)
            VALUES ('9001','BETUAL_NBA',100,'2026-01-01T00:00:00Z','v4-pace-1',
                167.5, 175.5, '2026-01-01T00:00:00Z', 175.5, 175.5, 172,
                0.0, 'PUSH', ?, 1, '2026-01-01T00:00:00Z')""",
            (_checkpoint_outcome(175.5, 175.5, 172),))
        con.commit()
        try:
            v = settlement_integrity_violations(con)
            assert v["cm_outcome_mismatch"] == [], \
                f"integrity scan must accept NO_EDGE for fair==market: {v['cm_outcome_mismatch']}"
        finally:
            con.close()


# ═══════════ Model-output invariant scan (post-merge review, L3) ═══════════

class TestModelOutputInvariantScan:
    """The integrity scan's half-grid invariant must (a) enforce only
    rows written AT/AFTER the code-deploy cutover, (b) bucket the
    predictions table by predicted_at (row-write time) — never
    source_snapshot_at, which would let a post-cutover settlement hide
    behind a pre-cutover game — and (c) survive naive (tz-less) legacy
    timestamps without a TypeError."""

    CUT = "2026-09-03T17:46:59Z"
    POST, PRE = "2026-09-04T00:00:00Z", "2026-09-01T00:00:00Z"

    def _db(self, tmp):
        import sqlite3
        from pathlib import Path
        from blm_v4.scorecard import SCORECARD_SCHEMA
        db = Path(tmp) / "invariant.db"
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        con.executescript(SCORECARD_SCHEMA)
        return con

    def _seed(self, con):
        # checkpoint_market: (post, non-half), (pre, non-half), (post, half)
        con.execute("""INSERT INTO checkpoint_market (source_game_id, classification,
            checkpoint_pct, checkpoint_timestamp, model_version, blm_fair_value,
            recorded_at) VALUES ('G1','BETUAL_NBA',50,'2026-09-04T00:00:00Z',
            'v4-pace-1', 174.3, ?)""", (self.POST,))
        con.execute("""INSERT INTO checkpoint_market (source_game_id, classification,
            checkpoint_pct, checkpoint_timestamp, model_version, blm_fair_value,
            recorded_at) VALUES ('G2','BETUAL_NBA',50,'2026-09-01T00:00:00Z',
            'v4-pace-1', 174.3, ?)""", (self.PRE,))
        con.execute("""INSERT INTO checkpoint_market (source_game_id, classification,
            checkpoint_pct, checkpoint_timestamp, model_version, blm_fair_value,
            recorded_at) VALUES ('G3','BETUAL_NBA',50,'2026-09-04T00:00:00Z',
            'v4-pace-1', 174.5, ?)""", (self.POST,))
        # predictions: post-cutover WRITE from a pre-cutover game -> must be
        # NEW (enforced); pre-cutover write -> historical; half value ignored
        for gid, p_at, snap_at, v in [
            ("P1", self.POST, "2026-08-31T00:00:00Z", 174.3),   # game pre, write post
            ("P2", self.PRE, "2026-08-31T00:00:00Z", 174.3),    # write pre
            ("P3", self.POST, "2026-09-04T00:00:00Z", 174.5),   # half -> ignored
        ]:
            con.execute("""INSERT INTO predictions (source_game_id, classification,
                model_version, checkpoint, predicted_at, source_snapshot_at,
                projected_total) VALUES (?, 'BETUAL_NBA', 'v4-pace-1', 'pct50',
                ?, ?, ?)""", (gid, p_at, snap_at, v))
        # prediction_scores: non-half model_total scored post-cutover
        pid = con.execute("SELECT id FROM predictions WHERE source_game_id='P1'").fetchone()["id"]
        con.execute("""INSERT INTO prediction_scores (prediction_id, source_game_id,
            classification, model_version, scored_at, model_total) VALUES (?, 'P1',
            'BETUAL_NBA', 'v4-pace-1', ?, 174.3)""", (pid, self.POST))
        # naive (tz-less) timestamp post-cutover must not crash the compare
        con.execute("""INSERT INTO checkpoint_market (source_game_id, classification,
            checkpoint_pct, checkpoint_timestamp, model_version, blm_fair_value,
            recorded_at) VALUES ('G4','BETUAL_NBA',50,'2026-09-04T00:00:00Z',
            'v4-pace-1', 173.3, '2026-09-04T00:00:00')""")
        con.commit()

    def test_cutover_buckets_and_predicted_at(self, tmp_path):
        from blm_v4.scorecard import settlement_integrity_violations
        con = self._db(tmp_path)
        try:
            self._seed(con)
            v = settlement_integrity_violations(con)["model_output_invariant"]
            assert v["cutoff"] == self.CUT
            # checkpoint_market: post non-half G1 + naive-ts G4 -> new; G2 -> historical
            assert v["cm_new_non_half"] == 2, v["cm_new"]
            assert v["cm_historical_non_half"] == 1, v["cm_historical_non_half"]
            # predictions: P1 (pre-game, POST write) is NEW; P2 historical
            assert v["predictions_new_non_half"] == 1, v["predictions_new"]
            assert [g[0] for g in v["predictions_new"]] == ["P1"]
            assert v["predictions_historical_non_half"] == 1
            # prediction_scores scored post-cutover -> new
            assert v["ps_new_non_half"] == 1, v["ps_new"]
            assert v["ps_historical_non_half"] == 0
        finally:
            con.close()
