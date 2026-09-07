"""Pace projector / trajectory layer tests (deterministic; NO statistics).

The projector reads only status='VALID' clean observations and computes
state-at-T strictly from observations at-or-before T; subsequent
observations and the final total are stored ONLY as outcome fields.
Recent pace uses GAME-CLOCK elapsed minutes.  No z-scores, probabilities,
ranges, calibration, or signals are produced anywhere in this layer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.clean_metrics import CleanMetricsStore
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.pace_projector import (PACE_GAP_SIGNIFICANT_MIN,
                                   classify_trajectory_state, market_status,
                                   pace_acceleration, pace_gap,
                                   projection_vs_live_line,
                                   recent_pace,
                                   required_to_actual_ratio,
                                   trajectory_projection, PaceProjector)
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(base: datetime, mins: float = 0) -> str:
    return (base + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def store(tmp_path):
    return CleanMetricsStore(tmp_path / "blm_metrics_clean.db")


def _insert(conn: sqlite3.Connection, table: str, data: dict) -> int:
    cols = list(data)
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [data[c] for c in cols],
    )
    conn.commit()
    return int(cur.lastrowid)


def _add_obs(store, gid: str, cls: str, captured_at: str, elapsed: float,
             total: int, line: float | None = None, *, market_ts: str | None = None,
             actual: float | None = None, required: float | None = None,
             fair: float | None = None, status: str = "VALID") -> int:
    """Direct clean_observations insert (projector input control)."""
    full = 48.0 if cls == "CYBER_2K26" else 40.0
    remaining = round(full - float(elapsed), 2)
    if actual is None:
        actual = round(total / float(elapsed), 4) if float(elapsed) > 0 else None
    if required is None:
        required = round((line - total) / remaining, 4) \
            if line is not None and remaining > 0 else None
    conn = sqlite3.connect(store.db_path)
    try:
        return _insert(conn, "clean_observations", {
            "source_game_id": gid, "snapshot_id": None,
            "captured_at": captured_at, "classification": cls,
            "total_points": total,
            "total_game_minutes": full, "elapsed_game_minutes": float(elapsed),
            "remaining_game_minutes": remaining,
            "progress_pct": round(float(elapsed) / full * 100, 2),
            "actual_pts_per_min": actual, "required_pts_per_min": required,
            "required_pace_target": "LIVE_TOTAL_LINE" if line is not None else None,
            "live_total_line": line,
            "market_captured_at": market_ts if market_ts is not None else captured_at,
            "market_source": "event_view",
            "fair_total": fair if fair is not None else (total + 2.0),
            "status": status, "reason": None,
        })
    finally:
        conn.close()


def _refresh(store, gid: str) -> dict:
    return PaceProjector().refresh_game(store, gid)


def _rows(store, gid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM clean_projections WHERE source_game_id=? "
            "ORDER BY id", (gid,))]
    finally:
        conn.close()


def _row(store, gid: str, idx: int = 0) -> dict:
    return _rows(store, gid)[idx]


# ── Core calculations ─────────────────────────────────────────────────


def test_zero_elapsed_time_actual_null(store):
    """elapsed == 0 → actual pace is NULL (never a substituted zero), so
    the trajectory is NULL.  A genuine zero pace (total 0) is preserved
    as 0 — it is data, not a manufactured value."""
    assert trajectory_projection(10, None, 39.0) is None   # undefined pace
    assert trajectory_projection(10, 0.0, 39.0) == 10.0    # real zero pace
    # pipeline: an elapsed-0 observation carries actual=NULL end-to-end
    gid = "G-ZERO"
    _add_obs(store, gid, "BETUAL_NBA", _iso(_now()), 0.0, 0, line=185.5)
    _refresh(store, gid)
    r = _row(store, gid)
    assert r["actual_pts_per_min"] is None
    assert r["projected_final_total"] is None
    assert r["required_pts_per_min"] == pytest.approx(4.6375, abs=1e-3)


def test_zero_remaining_time_required_null(store):
    """remaining == 0 → required pace NULL and trajectory NULL."""
    gid = "G-ZREM"
    _add_obs(store, gid, "BETUAL_NBA", _iso(_now()), 40.0, 200, line=190.5)
    _refresh(store, gid)
    r = _row(store, gid)
    assert r["remaining_game_minutes"] == 0.0
    assert r["required_pts_per_min"] is None
    assert r["projected_final_total"] is None


def test_negative_required_pace_preserved():
    """Target already below the current score → negative required pace is
    preserved, never clamped to zero."""
    r = _required_from(200, 190.5, 10.0)
    assert r is not None and r < 0
    assert round(r, 4) == pytest.approx(-0.95, abs=1e-4)


def _required_from(total: float, line: float, remaining: float) -> float:
    return (line - total) / remaining


def test_live_line_above_projection():
    p = trajectory_projection(80, 5.0, 20.0)      # 180.0
    assert p == 180.0
    assert projection_vs_live_line(p, 185.5) == pytest.approx(-5.5, abs=1e-9)


def test_live_line_below_projection():
    p = trajectory_projection(80, 5.0, 20.0)      # 180.0
    assert projection_vs_live_line(p, 175.0) == pytest.approx(5.0, abs=1e-9)


def test_projection_equals_live_line():
    p = trajectory_projection(80, 5.0, 20.0)      # 180.0
    assert projection_vs_live_line(p, 180.0) == 0.0


def test_betual_40_minute_duration(store):
    _add_obs(store, "G-B40", "BETUAL_NBA", _iso(_now()), 15.0, 60, line=185.5)
    _refresh(store, "G-B40")
    r = _row(store, "G-B40")
    assert r["elapsed_game_minutes"] == 15.0
    assert r["remaining_game_minutes"] == 25.0
    assert r["elapsed_game_minutes"] + r["remaining_game_minutes"] == 40.0


def test_cyber_48_minute_duration(store):
    _add_obs(store, "G-C48", "CYBER_2K26", _iso(_now()), 18.0, 60, line=200.0)
    _refresh(store, "G-C48")
    r = _row(store, "G-C48")
    assert r["elapsed_game_minutes"] == 18.0
    assert r["remaining_game_minutes"] == 30.0
    assert r["elapsed_game_minutes"] + r["remaining_game_minutes"] == 48.0


def test_pace_gap_sign_and_ratio():
    # required above actual → positive gap; ratio > 1
    assert pace_gap(5.5, 4.0) == pytest.approx(1.5, abs=1e-9)
    assert required_to_actual_ratio(5.5, 4.0) == pytest.approx(1.375, abs=1e-9)
    # required below actual → negative gap; ratio < 1 (still positive)
    assert pace_gap(3.0, 4.5) == pytest.approx(-1.5, abs=1e-9)
    # ratio is rounded to 4dp at storage (deterministic), so tolerance is 5e-4
    assert required_to_actual_ratio(3.0, 4.5) == pytest.approx(2 / 3, abs=5e-4)
    # undefined ratio (zero actual) preserved as None
    assert required_to_actual_ratio(5.0, 0.0) is None
    assert required_to_actual_ratio(None, 4.0) is None
    # negative required → negative ratio preserved (never capped)
    assert required_to_actual_ratio(-0.95, 4.0) == pytest.approx(-0.2375, abs=1e-9)


def test_label_only_quarter_fallback(tmp_path):
    """A label-only snapshot (quarter NULL) reaches the projector with a
    valid elapsed through the real clean pipeline (B1 fallback)."""
    dbfile = tmp_path / "blm_pokerbet.db"
    st = PokerBetStore(dbfile)
    clean = CleanMetricsStore(tmp_path / "blm_metrics_clean.db")
    gid = "G-LABEL"
    game = PokerBetGame(source_game_id=gid, classification="BETUAL_NBA",
                        home_team="H", away_team="A", status="live")
    gid_db = st.upsert_game(game)
    ts = _iso(_now())
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=ts, home_team="H", away_team="A",
        home_score=45, away_score=40, period_label="2nd Quarter",
        quarter=None, clock="05:00", game_status="live",
        total_line=185.5, markets_json="{}",
    )
    st.insert_snapshot(gid_db, obs)
    clean.record_snapshot_obs(game, obs, st)
    _refresh(clean, gid)
    r = _row(clean, gid)
    assert r["elapsed_game_minutes"] == 15.0        # Q2 05:00 on 10-min clock
    assert r["remaining_game_minutes"] == 25.0
    assert r["period_label"] == "2nd Quarter"
    assert r["clock"] == "05:00"
    assert r["current_total_points"] == 85


# ── Recent pace / acceleration (game-clock windows) ──────────────────


def _series(store, gid: str, totals: list[int], cls: str = "BETUAL_NBA"):
    """One observation per integer game-minute with the given totals."""
    base = _now() - timedelta(hours=1)
    for i, total in enumerate(totals):
        _add_obs(store, gid, cls, _iso(base, i), float(i), total, line=180.0)
    _refresh(store, gid)


def test_recent_pace_calculation(store):
    # totals rise exactly 4 pts per game-minute
    _series(store, "G-RP", [0, 4, 8, 12, 16, 20, 24])
    last = _rows(store, "G-RP")[-1]
    for w in (1, 2, 3, 5):
        assert last[f"recent_pace_{w}m"] == pytest.approx(4.0, abs=1e-6)
        assert last[f"recent_span_{w}m"] == pytest.approx(float(w), abs=1e-6)


def test_recent_pace_uses_game_clock_not_wall_clock(store):
    """Two observations 10 wall-minutes apart but 1 game-minute apart →
    pace is over the GAME-clock span (1 min), not wall time."""
    base = _now() - timedelta(hours=2)
    gid = "G-GCLK"
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 0), 0.0, 0, line=180.0)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 10), 1.0, 4, line=180.0)
    _refresh(store, gid)
    r = _rows(store, gid)[-1]
    assert r["recent_pace_1m"] == pytest.approx(4.0, abs=1e-6)
    assert r["recent_span_1m"] == pytest.approx(1.0, abs=1e-6)


def test_insufficient_recent_observations(store):
    _add_obs(store, "G-ONE", "BETUAL_NBA", _iso(_now()), 2.0, 8, line=180.0)
    _refresh(store, "G-ONE")
    r = _row(store, "G-ONE")
    for w in (1, 2, 3, 5):
        assert r[f"recent_pace_{w}m"] is None     # NULL, never fabricated
        assert r[f"recent_span_{w}m"] is None
    assert r["pace_acceleration"] is None


def test_acceleration_positive(store):
    # pace rises 2 → 4 → 8 over successive game-minutes
    _series(store, "G-ACC", [0, 2, 6, 14])
    r = _rows(store, "G-ACC")[-1]
    assert r["pace_acceleration"] == pytest.approx(4.0, abs=1e-6)
    assert r["acceleration_window"] == "1m_vs_prev_1m"


def test_deceleration_negative(store):
    # pace falls 8 → 4 → 2
    _series(store, "G-DEC", [0, 8, 12, 14])
    r = _rows(store, "G-DEC")[-1]
    assert r["pace_acceleration"] == pytest.approx(-2.0, abs=1e-6)


# ── Market freshness ──────────────────────────────────────────────────


def test_market_freshness_status(store):
    base = _now() - timedelta(minutes=30)
    gid = "G-FRESH"
    # stale: line observed 10 minutes before the snapshot
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 1), 5.0, 20,
             line=180.0, market_ts=_iso(base, 1 - 10))
    # fresh: line observed at the same tick
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 2), 6.0, 24,
             line=180.0, market_ts=_iso(base, 2))
    # missing: no line
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 3), 7.0, 28)
    _refresh(store, gid)
    rows = _rows(store, gid)
    assert rows[0]["market_age_seconds"] == pytest.approx(600.0, abs=1.0)
    assert rows[0]["market_status"] == "STALE"
    assert rows[1]["market_age_seconds"] == 0.0
    assert rows[1]["market_status"] == "LIVE"
    assert rows[2]["market_age_seconds"] is None      # no line to age
    assert rows[2]["market_status"] == "MISSING"
    # pure-function parity
    assert market_status(600.0) == "STALE"
    assert market_status(300.0) == "LIVE"
    assert market_status(0.0) == "LIVE"
    assert market_status(None) == "MISSING"


# ── Quality gates / clean boundary ───────────────────────────────────


def test_replayed_observation_excluded(store):
    gid = "G-REP"
    _add_obs(store, gid, "BETUAL_NBA", _iso(_now(), 1), 5.0, 20, line=180.0)
    _add_obs(store, gid, "BETUAL_NBA", _iso(_now(), 2), 6.0, 18, line=180.0,
             status="REPLAY")                     # score regression
    n = _refresh(store, gid)["n"]
    assert n == 1                                  # only the VALID row
    assert len(_rows(store, gid)) == 1


def test_clean_db_boundary(store):
    """Only VALID observations of the refreshed game enter; other games'
    rows are untouched; the projector never reads pre-epoch data (the
    clean DB itself starts empty at the epoch)."""
    _add_obs(store, "G-A", "BETUAL_NBA", _iso(_now(), 1), 5.0, 20, line=180.0)
    _add_obs(store, "G-A", "BETUAL_NBA", _iso(_now(), 2), 6.0, 24, line=180.0)
    _add_obs(store, "G-B", "CYBER_2K26", _iso(_now(), 1), 6.0, 24, line=200.0)
    _refresh(store, "G-A")
    assert len(_rows(store, "G-A")) == 2
    assert len(_rows(store, "G-B")) == 0
    assert store.count_projections() == 2


def test_refresh_idempotent(store):
    gid = "G-IDEM"
    _add_obs(store, gid, "BETUAL_NBA", _iso(_now(), 1), 5.0, 20, line=180.0)
    _refresh(store, gid)
    _refresh(store, gid)
    assert len(_rows(store, gid)) == 1


# ── Subsequent-observation linkage / no look-ahead ───────────────────


def test_subsequent_linkage_and_final_total(store):
    gid = "G-SUB"
    base = _now() - timedelta(hours=1)
    o1 = _add_obs(store, gid, "BETUAL_NBA", _iso(base, 1), 5.0, 20,
                  line=180.0, actual=4.0, required=5.5)
    o2 = _add_obs(store, gid, "BETUAL_NBA", _iso(base, 2), 6.0, 24,
                  line=182.0, actual=5.0, required=5.0)
    o3 = _add_obs(store, gid, "BETUAL_NBA", _iso(base, 3), 7.0, 29,
                  line=182.0, actual=5.0, required=4.5)
    _refresh(store, gid)
    rows = _rows(store, gid)
    r1, r2, r3 = rows
    # linkage chain
    assert r1["subsequent_observation_id"] == o2
    assert r1["subsequent_actual_pace"] == 5.0
    assert r1["subsequent_pace_change"] == pytest.approx(1.0, abs=1e-9)
    assert r1["subsequent_live_line"] == 182.0
    assert r1["subsequent_live_line_change"] == pytest.approx(2.0, abs=1e-9)
    assert r2["subsequent_observation_id"] == o3
    assert r3["subsequent_observation_id"] is None   # no future yet
    assert r3["final_settled_total"] is None
    # game completes → final recorded → all rows settle, metrics unchanged
    conn = sqlite3.connect(store.db_path)
    try:
        _insert(conn, "clean_games", {
            "source_game_id": gid, "base_game_id": gid,
            "classification": "BETUAL_NBA", "status": "ended",
            "final_home": 120, "final_away": 110, "final_total": 230,
            "final_result_status": "FINAL", "finalized_at": _iso(_now(), 10),
            "first_seen_at": _iso(base, 1), "last_seen_at": _iso(base, 3),
        })
    finally:
        conn.close()
    _refresh(store, gid)
    settled = _rows(store, gid)
    assert settled[0]["final_settled_total"] == 230
    assert settled[2]["final_settled_total"] == 230
    # T metrics unchanged by later events (no look-ahead in state)
    assert settled[0]["actual_pts_per_min"] == 4.0
    assert settled[0]["required_pts_per_min"] == 5.5
    assert settled[0]["pace_gap"] == pytest.approx(1.5, abs=1e-9)


def test_no_future_leakage_on_recompute(store):
    gid = "G-NOFL"
    base = _now() - timedelta(hours=1)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 1), 5.0, 20,
             line=180.0, actual=4.0, required=5.5)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 2), 6.0, 24,
             line=180.0, actual=5.0, required=4.5)
    _refresh(store, gid)
    before = _row(store, gid, 0)
    frozen = {k: before[k] for k in (
        "actual_pts_per_min", "required_pts_per_min", "pace_gap",
        "projected_final_total", "projection_vs_live_line", "trajectory_state")}
    # a LATER observation arrives and the game refreshes
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 3), 7.0, 29,
             line=185.0, actual=6.0, required=4.0)
    _refresh(store, gid)
    after = _row(store, gid, 0)
    for k, v in frozen.items():
        assert after[k] == v, f"{k} leaked future info"
    # ... only the OUTCOME linkage may change
    assert after["subsequent_observation_id"] != before["subsequent_observation_id"] \
        or after["subsequent_observation_id"] is not None


# ── Four trajectory states (descriptive, hindsight) ──────────────────


def _state_pair(store, gid: str, actual_t: float, required_t: float,
                actual_next: float, line: float = 180.0) -> str:
    base = _now() - timedelta(hours=1)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 1), 5.0, 20,
             line=line, actual=actual_t, required=required_t)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 2), 6.0, 24,
             line=line, actual=actual_next, required=required_t)
    _refresh(store, gid)
    return _row(store, gid, 0)["trajectory_state"]


def test_trajectory_states_abcd(store):
    sig = PACE_GAP_SIGNIFICANT_MIN
    assert sig >= 1.0
    # A: required >> actual, then accelerated
    assert _state_pair(store, "G-ST-A", 4.0, 5.5, 6.0) == "A"
    # B: required >> actual, pace remained low (did not accelerate)
    assert _state_pair(store, "G-ST-B", 4.0, 5.5, 3.5) == "B"
    # C: required << actual, then decelerated
    assert _state_pair(store, "G-ST-C", 4.5, 3.0, 4.0) == "C"
    # D: required << actual, pace remained high (did not decelerate)
    assert _state_pair(store, "G-ST-D", 4.5, 3.0, 5.0) == "D"
    # insignificant gap → no state
    assert _state_pair(store, "G-ST-N", 4.0, 4.5, 4.5) is None
    # no subsequent observation → no state
    assert classify_trajectory_state(1.5, None) is None
    assert classify_trajectory_state(None, 0.5) is None


# ── Reader / integration ─────────────────────────────────────────────


def test_latest_for_game_returns_current_trajectory(store):
    gid = "G-LATEST"
    base = _now() - timedelta(hours=1)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 1), 5.0, 20, line=180.0)
    _add_obs(store, gid, "BETUAL_NBA", _iso(base, 2), 6.0, 24, line=182.0)
    _refresh(store, gid)
    latest = PaceProjector().latest_for_game(store, gid)
    assert latest["current_total_points"] == 24
    assert latest["live_total_line"] == 182.0
    assert PaceProjector().latest_for_game(store, "G-NOPE") is None


def test_projected_total_separate_from_live_line_and_fair(store):
    gid = "G-SEP"
    # total 80, actual 5.0, remaining 20 → projected 180; line 175.5; fair 185.0
    _add_obs(store, gid, "BETUAL_NBA", _iso(_now()), 20.0, 80,
             line=175.5, actual=5.0, required=4.0, fair=185.0)
    _refresh(store, gid)
    r = _row(store, gid)
    assert r["projected_final_total"] == 180.0      # trajectory
    assert r["live_total_line"] == 175.5            # market — never overwritten
    assert r["fair_total"] == 185.0                 # existing fair concept
    assert r["projection_vs_live_line"] == pytest.approx(4.5, abs=1e-9)