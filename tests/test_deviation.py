"""Deviation / benchmark / z-score measurement layer tests (Phase 2).

Proofs required by the directive:
  1. Z-score calculation is deterministic.
  2. Benchmark accumulation is deterministic (idempotent refresh; same
     input sequence -> identical rows).
  3. Small-sample maturity is explicit (EXPLORATORY / PROVISIONAL /
     ESTABLISHED = operational bucket-size labels, never significance).
  4. Zero / invalid standard deviation is handled safely (z -> NULL,
     never an infinity).
  5. Future observations cannot alter a historical T-state z-score
     (rows are written once and never rewritten).
  6. Replay / fake-pace observations cannot enter the benchmark (only
     VALID clean observations feed the trajectory rows that feed this
     layer).
  7. Both positive and negative residuals are represented (signed).
  8. The three variables stay distinct: pace_gap != residual != z.
  9. Live API / live game payload does not expose predictive O/U / edge /
     probability fields; deviation research endpoints carry descriptive
     fields + explicit maturity + a no-O/U note only.
  10. The existing frozen test suite stays green (run separately).

Benchmark semantics under test: N is the count of PRIOR residuals in the
league+period bucket (never total observations); the current observation
is standardized against the prior benchmark, then enters the benchmark.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router
from blm_v4.clean_metrics import CleanMetricsStore
from blm_v4.deviation import (DeviationEngine, DeviationStore,
                               benchmark_key, benchmark_status,
                               market_trajectory_residual, normalize_period,
                               z_score)
from blm_v4.pace_projector import PaceProjector


def _iso(base: datetime, mins: float = 0) -> str:
    return (base + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def clean(tmp_path) -> CleanMetricsStore:
    return CleanMetricsStore(tmp_path / "blm_metrics_clean.db")


def _full(cls: str) -> float:
    return 48.0 if cls == "CYBER_2K26" else 40.0


def _add_obs(clean: CleanMetricsStore, gid: str, cls: str, base: datetime,
             off_min: float, elapsed: float, total: int, line: float,
             period: int) -> int:
    """Insert one clean VALID observation + its snapshot (period_label/quarter
    set so the trajectory row carries a period for the benchmark key)."""
    cap = _iso(base, off_min)
    full = _full(cls)
    remaining = round(full - elapsed, 2)
    actual = round(total / elapsed, 4) if elapsed > 0 else None
    required = round((line - total) / remaining, 4) \
        if line is not None and remaining > 0 else None
    conn = sqlite3.connect(f"file:{clean.db_path}?mode=rwc", uri=True)
    try:
        cur = conn.execute(
            "INSERT INTO clean_snapshots (source_game_id, captured_at,"
            " period_label, quarter, clock, home_score, away_score,"
            " total_points, game_status, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, cap, f"Q{period}", period, "06:00", None, None,
             total, "live", "test"),
        )
        snap_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO clean_observations (source_game_id, snapshot_id,"
            " captured_at, classification, total_points, total_game_minutes,"
            " elapsed_game_minutes, remaining_game_minutes, progress_pct,"
            " actual_pts_per_min, required_pts_per_min,"
            " required_pace_target, live_total_line, market_captured_at,"
            " market_source, status, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, snap_id, cap, cls, total, full, round(elapsed, 2),
             remaining, round(elapsed / full * 100, 4), actual, required,
             "LIVE_TOTAL_LINE" if line is not None else None,
             line, cap, "event_view", "VALID", None),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _add_replay_obs(clean: CleanMetricsStore, gid: str, cls: str,
                    base: datetime, off_min: float, elapsed: float,
                    total: int, line: float, period: int) -> int:
    """Insert a REPLAY observation directly (a fake/regressed frame that the
    real pipeline would have gated).  It must never reach the benchmark."""
    cap = _iso(base, off_min)
    full = _full(cls)
    conn = sqlite3.connect(f"file:{clean.db_path}?mode=rwc", uri=True)
    try:
        cur = conn.execute(
            "INSERT INTO clean_observations (source_game_id, captured_at,"
            " classification, total_points, total_game_minutes,"
            " elapsed_game_minutes, remaining_game_minutes, progress_pct,"
            " live_total_line, market_captured_at, market_source, status, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, cap, cls, total, full, round(elapsed, 2),
             round(full - elapsed, 2), round(elapsed / full * 100, 4),
             line, cap, "event_view", "REPLAY",
             "score regression vs prior clean observation"),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _run(clean: CleanMetricsStore, gid: str) -> dict:
    """Production path: projector first, then the deviation layer."""
    PaceProjector().refresh_game(clean, gid)
    return DeviationEngine(clean.db_path).refresh_game(gid)


def _residual_rows(clean: CleanMetricsStore, gid: str) -> list[dict]:
    return DeviationStore(clean.db_path).residuals_for_game(gid)


def _row_sig(rows: list[dict]) -> list[tuple]:
    return [
        (r["observation_id"], r["benchmark_key"], r["market_trajectory_residual"],
         r["benchmark_n"], r["benchmark_mean"], r["benchmark_std"],
         r["benchmark_status"], r["z_score"])
        for r in rows
    ]


# ── Pure functions ────────────────────────────────────────────────────

def test_three_variables_are_distinct():
    """pace_gap is required − actual pace; the residual is live line −
    projected trajectory; z is the standardized residual.  They are
    separate quantities and must never be collapsed."""
    # pure residual: market above trajectory -> positive; below -> negative
    assert market_trajectory_residual(190.0, 180.5) == 9.5
    assert market_trajectory_residual(180.5, 190.0) == -9.5
    assert market_trajectory_residual(None, 180.5) is None
    assert market_trajectory_residual(190.0, None) is None
    # a residual of 0 is a real value (market exactly on trajectory)
    assert market_trajectory_residual(190.0, 190.0) == 0.0


def test_maturity_labels_are_operational_thresholds():
    assert benchmark_status(0) == "EXPLORATORY"
    assert benchmark_status(99) == "EXPLORATORY"
    assert benchmark_status(100) == "PROVISIONAL"
    assert benchmark_status(999) == "PROVISIONAL"
    assert benchmark_status(1000) == "ESTABLISHED"


def test_normalize_period_and_key():
    assert normalize_period("Q3") == 3
    assert normalize_period("3rd Quarter") == 3
    assert normalize_period(None) is None
    assert normalize_period("Final") is None
    assert benchmark_key("BETUAL_NBA", 2) == "BETUAL_NBA|Q2"
    assert benchmark_key("CYBER_2K26", None) == "CYBER_2K26|UNK"
    # classification is ALWAYS part of the key: no unconditional global bucket
    assert benchmark_key("BETUAL_NBA", 1) != benchmark_key("CYBER_2K26", 1)


def test_z_safety_for_zero_invalid_std():
    assert z_score(2.0, 0.0, 0.0) is None       # zero std -> NULL, not inf
    assert z_score(2.0, 0.0, None) is None      # missing std
    assert z_score(None, 0.0, 1.0) is None      # missing residual
    assert z_score(2.0, None, 1.0) is None
    # valid: (3.0 - 1.0) / 2.0
    assert z_score(3.0, 1.0, 2.0) == 1.0


# ── End-to-end determinism + immutability ────────────────────────────

def _build_basic_game(clean: CleanMetricsStore, gid: str, cls: str,
                      base: datetime, n: int = 5, line: float = 160.0,
                      period: int = 1) -> None:
    """n observations at 4-minute intervals (elapsed 8..(8+4n)).  Total
    grows so the trajectory projection is stable around ``line``."""
    for i in range(n):
        elapsed = 8.0 + 4.0 * i
        total = int(30 + (elapsed / _full(cls)) * 170)
        _add_obs(clean, gid, cls, base, 2.0 + 4.0 * i, elapsed, total,
                 line, period)


def test_deterministic_and_idempotent(clean, tmp_path):
    base = datetime.now(timezone.utc)
    _build_basic_game(clean, "G-DET", "BETUAL_NBA", base, n=6)
    st1 = _run(clean, "G-DET")
    assert st1["added"] == 6
    rows1 = _residual_rows(clean, "G-DET")
    assert len(rows1) == 6

    # idempotent: a second refresh adds nothing and changes nothing
    st2 = DeviationEngine(clean.db_path).refresh_game("G-DET")
    assert st2["added"] == 0
    assert _row_sig(_residual_rows(clean, "G-DET")) == _row_sig(rows1)

    # determinism: an identical DB built from the same inputs yields the
    # same residual rows (core fields)
    clean2 = CleanMetricsStore(tmp_path / "blm_metrics_clean2.db")
    _build_basic_game(clean2, "G-DET", "BETUAL_NBA", base, n=6)
    _run(clean2, "G-DET")
    assert _row_sig(_residual_rows(clean2, "G-DET")) == _row_sig(rows1)


def test_no_self_contamination_benchmark_n(clean):
    """Row i uses the bucket benchmark built from rows 0..i-1 (prior
    residuals only): benchmark_n == i, and the row only enters the
    benchmark AFTER its own z is computed."""
    base = datetime.now(timezone.utc)
    _build_basic_game(clean, "G-N", "CYBER_2K26", base, n=5)
    _run(clean, "G-N")
    rows = _residual_rows(clean, "G-N")
    assert [r["benchmark_n"] for r in rows] == [0, 1, 2, 3, 4]
    # first two rows have no std (n<2) -> z NULL; from n>=2 a z exists
    assert rows[0]["z_score"] is None
    assert rows[1]["z_score"] is None
    assert rows[2]["z_score"] is not None
    # stored maturity snapshot matches the prior-N label
    assert rows[0]["benchmark_status"] == "EXPLORATORY"


def test_future_observations_cannot_alter_historical_z(clean):
    base = datetime.now(timezone.utc)
    _build_basic_game(clean, "G-HIST", "BETUAL_NBA", base, n=4)
    _run(clean, "G-HIST")
    before = _row_sig(_residual_rows(clean, "G-HIST"))

    # add LATER observations (and another game) and refresh
    _build_basic_game(clean, "G-HIST", "BETUAL_NBA",
                      base + timedelta(minutes=40), n=3)
    _build_basic_game(clean, "G-LATER", "BETUAL_NBA",
                      base + timedelta(minutes=90), n=3)
    _run(clean, "G-HIST")
    _run(clean, "G-LATER")

    after_all = _row_sig(_residual_rows(clean, "G-HIST"))
    assert len(after_all) == 7  # 4 original + 3 later rows for this game
    # the ORIGINAL rows are byte-identical — history is never rewritten
    before_ids = {s[0] for s in before}
    after_hist = [s for s in after_all if s[0] in before_ids]
    assert after_hist == before
    # the later game's rows saw the grown benchmark (their N counts it)
    later_rows = _residual_rows(clean, "G-LATER")
    assert later_rows[0]["benchmark_n"] == 7  # 4 earlier + 3 later G-HIST rows
    assert later_rows[0]["benchmark_status"] == "EXPLORATORY"


def test_zero_std_handled_end_to_end(clean):
    """A bucket whose residuals are all identical has std 0 -> every z is
    NULL (never an infinity), while rows still accumulate normally."""
    base = datetime.now(timezone.utc)
    gid = "G-ZSTD"
    # constant scoring rate of 8 pts/min: projection is always exactly 320
    # (total = 8*elapsed, remaining = 40-elapsed), line pinned 7 above it,
    # so every residual is exactly 7.0 and the bucket std is 0.
    for i in range(6):
        elapsed = 10.0 + 4.0 * i
        total = int(8 * elapsed)
        line = 327.0  # 320 projected + 7.0
        _add_obs(clean, gid, "BETUAL_NBA", base, 2.0 + 4.0 * i, elapsed,
                 total, line, 1)
    _run(clean, gid)
    rows = _residual_rows(clean, gid)
    assert len(rows) == 6
    residuals = {r["market_trajectory_residual"] for r in rows}
    assert residuals == {7.0}  # all identical -> prior std is 0
    assert all(r["z_score"] is None for r in rows)
    assert all(r["benchmark_std"] == 0.0 or r["benchmark_std"] is None
               for r in rows)


def test_replay_observations_cannot_enter_benchmark(clean):
    base = datetime.now(timezone.utc)
    gid = "G-REPLAY"
    _build_basic_game(clean, gid, "BETUAL_NBA", base, n=4)
    _run(clean, gid)
    assert len(_residual_rows(clean, gid)) == 4

    # a fake / replayed frame (score regression) is recorded with status
    # REPLAY — the trajectory feed (VALID only) never sees it
    _add_replay_obs(clean, gid, "BETUAL_NBA",
                    base + timedelta(minutes=20), 3.0, 30.0, 200, 155.0, 2)
    _add_replay_obs(clean, gid, "BETUAL_NBA",
                    base + timedelta(minutes=22), 5.0, 32.0, 205, 155.0, 2)
    stats = _run(clean, gid)
    assert stats["added"] == 0
    rows = _residual_rows(clean, gid)
    assert len(rows) == 4          # replays never create residual rows
    assert rows[-1]["benchmark_n"] == 3  # bucket state untouched by replay


def test_both_signs_represented_and_distinct_from_pace_gap(clean):
    """Market above the trajectory (residual > 0) and below it (residual < 0)
    are both stored signed; pace_gap (required−actual) stays a separate
    column/quantity on the same observation."""
    base = datetime.now(timezone.utc)
    gid = "G-SIGN"
    for i in range(6):
        elapsed = 10.0 + 4.0 * i
        total = int(40 + (elapsed / 40.0) * 150)
        proj = round(total + (total / elapsed) * (40.0 - elapsed), 2)
        line = proj + 8.0 if i % 2 == 0 else proj - 6.0  # +8 / −6 residuals
        _add_obs(clean, gid, "BETUAL_NBA", base, 2.0 + 4.0 * i, elapsed,
                 total, line, 1)
    _run(clean, gid)
    rows = _residual_rows(clean, gid)
    resid = [r["market_trajectory_residual"] for r in rows]
    assert any(v > 0 for v in resid) and any(v < 0 for v in resid)
    assert rows[0]["market_trajectory_residual"] > 0
    assert rows[1]["market_trajectory_residual"] < 0

    # distinctness: pace_gap lives on the trajectory row and differs from
    # the market-trajectory residual at the same observation
    conn = sqlite3.connect(f"file:{clean.db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        proj_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM clean_projections WHERE source_game_id=? "
            "ORDER BY captured_at", (gid,))]
    finally:
        conn.close()
    assert len(proj_rows) == len(rows)
    for p, r in zip(proj_rows, rows):
        assert p["observation_id"] == r["observation_id"]
        # three distinct quantities on the same observation:
        # pace_gap (required − actual), residual (line − projection), z
        assert p["pace_gap"] is not None
        assert r["market_trajectory_residual"] is not None
        assert (r["market_trajectory_residual"] - p["pace_gap"]) != pytest.approx(0, abs=0.01)


def test_maturity_provisional_once_bucket_reaches_100(clean):
    """A later observation in a bucket whose prior size reached 100 is
    PROVISIONAL — the operational label tracks the bucket's prior N."""
    base = datetime.now(timezone.utc)
    # spread across three games so every row is its own observation
    for g in range(3):
        gid = f"G-MAT-{g}"
        for i in range(34):
            elapsed = 10.0 + 0.7 * i
            total = int(40 + (elapsed / 40.0) * 150)
            proj = round(total + (total / elapsed) * (40.0 - elapsed), 2)
            line = proj + (8.0 if i % 2 == 0 else -6.0)
            _add_obs(clean, gid, "BETUAL_NBA",
                     base + timedelta(minutes=0.0 if g == 0 else 40.0 * g),
                     3.0 + 0.7 * i, elapsed, total, line, 1)
        _run(clean, gid)
    # 3 games x 34 = 102 rows; the final row saw a prior bucket of 101
    rows = _residual_rows(clean, "G-MAT-2")
    assert len(rows) == 34
    assert rows[-1]["benchmark_n"] == 101
    assert rows[-1]["benchmark_status"] == "PROVISIONAL"
    assert rows[-1]["z_score"] is not None


# ── API: research endpoints only, no predictive mapping ──────────────

@pytest.fixture
def dev_api(tmp_path, monkeypatch):
    main = tmp_path / "blm_pokerbet.db"
    main.touch()
    monkeypatch.setenv("BLM_POKERBET_DB", str(main))
    clean = CleanMetricsStore(tmp_path / "blm_metrics_clean.db")
    base = datetime.now(timezone.utc)
    _build_basic_game(clean, "G-API", "BETUAL_NBA", base, n=5)
    _run(clean, "G-API")
    app = FastAPI()
    app.include_router(v4_router)
    return TestClient(app)


def test_deviation_api_payloads_descriptive_only(dev_api):
    d = dev_api.get("/api/v4/game/G-API/deviation")
    assert d.status_code == 200
    body = d.json()
    assert body["section"] == "deviation_research"
    assert body["classification"] == "BETUAL_NBA"
    assert body["total"] == 5
    assert body["series"]
    row = body["series"][0]
    # maturity exposure: every z row carries the benchmark snapshot
    for k in ("benchmark_n", "benchmark_mean", "benchmark_std",
              "benchmark_status", "z_score", "market_trajectory_residual"):
        assert k in row
    # no predictive mapping fields anywhere in the payload
    txt = json.dumps(body)
    for bad in ("ou_prediction", "over_selection", "under_selection",
                "edge", "recommendation", "fair_value_decision",
                "win_probability", "confidence_score", "trap"):
        assert bad not in txt.lower()
    assert "maturity" in body and "note" in body

    b = dev_api.get("/api/v4/deviation/benchmarks").json()
    assert b["section"] == "deviation_benchmarks"
    assert any(x["benchmark_key"].startswith("BETUAL_NBA|Q1")
               for x in b["benchmarks"])
    for x in b["benchmarks"]:
        assert set(x) >= {"benchmark_key", "n", "mean", "std", "status"}


def test_deviation_api_empty_when_no_clean_db(tmp_path, monkeypatch):
    main = tmp_path / "blm_pokerbet.db"
    main.touch()
    monkeypatch.setenv("BLM_POKERBET_DB", str(main))
    app = FastAPI()
    app.include_router(v4_router)
    c = TestClient(app)
    body = c.get("/api/v4/game/G-NOPE/deviation").json()
    assert body["total"] == 0 and body["series"] == []
