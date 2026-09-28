"""LIVE-ROUTE LATENCY REGRESSION GUARD (empty-live-data diagnostic
2026-09-25).

The empty-dashboard incident: /api/v4/live called
``competition_pace_reference`` + ``q3_pace_reference`` INLINE per request.
The Q3 reference is a whole-table GROUP BY over ``snapshots``; on the
production database (9.4 GB, ≈2M rows) one scan takes minutes, every
cache-miss request hung the sync worker pool, and the dashboard froze on
its placeholders forever.

THE CONTRACT these tests lock in:

  1. LATENCY — /api/v4/live and /api/v4/status must answer in well under
     a second on a PRODUCTION-SHAPED database (a snapshots table with
     hundreds of thousands of rows spread over many games).  The guard
     threshold is deliberately generous (5 s) so a slow CI box cannot
     flake, while still failing 60× earlier than the minutes-long hang
     that shipped.
  2. SOURCE — with a worker configured, the route must never compute a
     reference inline: the module-level wrappers must return the
     published payload without touching their engine functions.
  3. WARMUP — before the first scan lands the route serves empty
     references (fail closed, alert layer renders UNAVAILABLE) and still
     answers fast.
  4. WORKER — the PaceReferenceWorker change-detector skips scans when
     nothing changed, rescans on data change / max age, publishes each
     completed scan to its consumer, and never raises out of its loop.
  5. STUB CONTRACT — the pre-existing test seam is preserved:
     monkeypatching ``v4api._pace_reference`` / ``v4api._q3_pace_reference``
     still controls what /api/v4/live serves when no worker is wired.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4 import api as v4api
from blm_v4.api import router as v4_router
from blm_v4.live_analytics.pace_reference_worker import PaceReferenceWorker

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"

#: Latency guard.  The production incident was a minutes-long hang; even
#: on a slow CI box a healthy /live build stays far below this.
LIVE_MAX_SECONDS = 5.0
STATUS_MAX_SECONDS = 5.0

#: Production-shape scale: the incident DB held ≈2M rows.  CI cannot
#: afford 2M, but the scan cost that matters is SUPERLINEAR vs the
#: per-game row count — 200k rows × 2k games overweights the GROUP BY
#: exactly the way production does, and inserts in ~seconds.
PERF_ROWS = 200_000
PERF_GAMES = 2_000


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════

def _make_app():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")

    @app.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))

    return app


@pytest.fixture
def no_worker(monkeypatch):
    """The bare-router shape: no pace-reference worker wired."""
    monkeypatch.setattr(v4api, "_PACE_REF_WORKER", None)
    monkeypatch.setattr(v4api, "_PACE_REF_LATEST", {"pace": None, "q3": None})


@pytest.fixture
def client(no_worker):
    return TestClient(_make_app())


@pytest.fixture
def populated_db(tmp_path, monkeypatch):
    """A production-SHAPED pipeline DB: many games, many snapshots per
    game, enough rows that an inline whole-table GROUP BY would blow the
    latency budget several times over."""
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY, source TEXT, source_game_id TEXT,
            classification TEXT, competition_slug TEXT,
            home_team TEXT, away_team TEXT, status TEXT,
            first_seen_at TEXT, last_seen_at TEXT);
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY, game_id INTEGER, source TEXT,
            source_game_id TEXT, classification TEXT, captured_at TEXT,
            home_team TEXT, away_team TEXT, home_score INTEGER,
            away_score INTEGER, period_label TEXT, quarter INTEGER,
            clock TEXT, game_status TEXT, total_line REAL,
            w1_odds REAL, w2_odds REAL, spread REAL,
            home_total_line REAL, away_total_line REAL,
            total_over_odds REAL, total_under_odds REAL,
            spread_indicator TEXT, spread_home_odds REAL,
            spread_away_odds REAL);
        CREATE TABLE game_results (
            source_game_id TEXT PRIMARY KEY, final_total REAL,
            final_result_status TEXT, result_at TEXT);
        -- The /live route reads the freshest validated WS game state from
        -- market_observations (api._ws_game_state) — a table the production
        -- DB always carries, so the prod-shaped fixture must too.
        CREATE TABLE market_observations (
            id INTEGER PRIMARY KEY, source_game_id TEXT, captured_at TEXT,
            market_type TEXT, period_label TEXT, clock TEXT,
            home_score INTEGER, away_score INTEGER, line_value REAL);
        CREATE TABLE reconciliation (
            id INTEGER PRIMARY KEY, source TEXT, source_game_id TEXT,
            result TEXT, checked_at TEXT);
        CREATE INDEX idx_snapshots_source_ts
            ON snapshots(source_game_id, captured_at);
        -- Production's UNIQUE(game_id, captured_at) autoindex is what makes
        -- the /status live-games EXISTS (s.game_id = g.id AND captured_at>=?)
        -- an index seek; without it the correlated subquery degenerates into
        -- games × full-table scans (the exact regression this guard exists
        -- to catch, indistinguishable from a real one).
        CREATE UNIQUE INDEX idx_snapshots_game_captured
            ON snapshots(game_id, captured_at);
    """)
    rows = []
    per_game = PERF_ROWS // PERF_GAMES
    for g in range(PERF_GAMES):
        gid = f"G{g:06d}"
        conn.execute(
            "INSERT INTO games (source, source_game_id, classification,"
            " competition_slug, home_team, away_team, status, first_seen_at,"
            " last_seen_at) VALUES ('PokerBet', ?, 'BETUAL_NBA',"
            " 'betual-nba', 'Alpha', 'Beta', 'live',"
            " '2026-09-25T10:00:00Z', '2026-09-25T11:00:00Z')", (gid,))
        rows.extend(
            (g + 1, gid, "2026-09-25T10:%02d:%02d.000000Z" % (s // 60 % 60,
                                                              s % 60),
             50 + s % 30, 40 + s % 25, "1st Quarter", 1, "12:00", "live",
             210.5 + (s % 10))
            for s in range(per_game))
        conn.execute(
            "INSERT INTO game_results (source_game_id, final_total,"
            " final_result_status, result_at) VALUES (?, ?, 'OK', ?)",
            (gid, 380, "2026-09-25T11:00:00Z"))
    conn.executemany(
        "INSERT INTO snapshots (game_id, source_game_id, captured_at,"
        " home_score, away_score, period_label, quarter, clock,"
        " game_status, total_line) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


# ══════════════════════════════════════════════════════════════════════
# 1. LATENCY — the guard itself (marked slow: prod-shaped DB)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_live_responds_fast_on_production_shaped_db(populated_db, monkeypatch):
    """THE regression guard: /api/v4/live answers in seconds, not minutes.

    Pre-fix, this shape hung until the whole-table Q3 GROUP BY finished —
    minutes on production, and minutes here too.  Post-fix the references
    come from the worker's published payload and the route answers from
    indexed per-game queries only.
    """
    published = {
        "pace": {"betual-nba": {"avg_pace": 4.2, "games": 1000}},
        "q3": {"betual-nba": {"avg_q3_pace": 4.0, "games": 1000}},
    }
    monkeypatch.setattr(v4api, "_PACE_REF_LATEST", published)
    worker = PaceReferenceWorker(populated_db, interval_s=3600)
    monkeypatch.setattr(v4api, "_PACE_REF_WORKER", worker)
    app = _make_app()
    with TestClient(app) as client:
        t0 = time.monotonic()
        resp = client.get("/api/v4/live")
        elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    body = resp.json()
    assert body["pace_reference"] == published["pace"]
    # /live serves the freshest 100 games by contract (_load_games limit);
    # the 2,000-game fixture exists to make the DB prod-shaped for latency.
    assert len(body["games"]) == min(PERF_GAMES, 100)
    assert elapsed < LIVE_MAX_SECONDS, (
        f"/api/v4/live took {elapsed:.1f}s on a production-shaped DB "
        f"({PERF_ROWS} snapshot rows) — the inline-scan hang has "
        f"regressed")


@pytest.mark.slow
def test_status_responds_fast_on_production_shaped_db(populated_db,
                                                      monkeypatch):
    """/api/v4/status must not run whole-table scans per poll (it serves
    snapshot volume as MAX(rowid) estimates and freshest-state within a
    bounded recent window)."""
    worker = PaceReferenceWorker(populated_db, interval_s=3600)
    monkeypatch.setattr(v4api, "_PACE_REF_WORKER", worker)
    v4api._FRESH_STATE_CACHE["key"] = None
    v4api._FRESH_STATE_CACHE["value"] = None
    app = _make_app()
    with TestClient(app) as client:
        t0 = time.monotonic()
        resp = client.get("/api/v4/status")
        elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    db = resp.json()["db"]
    assert db["total_snapshots"] == PERF_ROWS   # MAX(rowid) estimate
    assert elapsed < STATUS_MAX_SECONDS, (
        f"/api/v4/status took {elapsed:.1f}s on a production-shaped DB "
        f"— a whole-table scan has regressed onto the request path")


# ══════════════════════════════════════════════════════════════════════
# 2/3. SOURCE + WARMUP — worker-configured route behavior
# ══════════════════════════════════════════════════════════════════════

def _wired(monkeypatch, published):
    monkeypatch.setattr(v4api, "_PACE_REF_LATEST", published)
    monkeypatch.setattr(v4api, "_PACE_REF_WORKER",
                        PaceReferenceWorker(":memory:", interval_s=3600))


def test_wrappers_serve_published_payload_without_inline_scan(monkeypatch):
    """With a worker wired, the wrappers must NOT touch the engines —
    the whole point is that no request path ever runs the scan."""
    published = {"pace": {"betual-nba": {"avg_pace": 4.2, "games": 5}},
                 "q3": {"betual-nba": {"avg_q3_pace": 4.0, "games": 5}}}
    _wired(monkeypatch, published)

    def _boom(_conn=None):
        raise AssertionError("inline reference scan ran on the "
                             "worker-configured path")

    monkeypatch.setattr(
        "blm_v4.live_analytics.competition_pace.competition_pace_reference",
        _boom)
    monkeypatch.setattr(
        "blm_v4.live_analytics.fingerprint_c5.q3_pace_reference", _boom)
    assert v4api._pace_reference(None) == published["pace"]
    assert v4api._q3_pace_reference(None) == published["q3"]


def test_warmup_serves_empty_references_fail_closed(monkeypatch):
    """Before the first scan lands: {} on both references (the alert
    layer's documented fail-closed state) and status says warming."""
    _wired(monkeypatch, {"pace": None, "q3": None})
    assert v4api._pace_reference(None) == {}
    assert v4api._q3_pace_reference(None) == {}
    st = v4api.pace_reference_status()
    assert st["worker_configured"] is True
    assert st["pace_available"] is False
    assert st["q3_available"] is False
    assert st["worker"]["runs"] == 0


def test_configure_publishes_and_reports(monkeypatch):
    """The composition-root wiring: configure → publish → readable."""
    monkeypatch.setattr(v4api, "_PACE_REF_WORKER", None)
    monkeypatch.setattr(v4api, "_PACE_REF_LATEST", {"pace": None, "q3": None})
    worker = PaceReferenceWorker(":memory:", interval_s=3600)
    v4api.configure_pace_reference_worker(worker)
    try:
        v4api._publish_pace_references({"x": {"avg_pace": 1.0, "games": 1}},
                                       {"x": {"avg_q3_pace": 2.0,
                                              "games": 1}})
        assert v4api._pace_reference(None) == {
            "x": {"avg_pace": 1.0, "games": 1}}
        st = v4api.pace_reference_status()
        assert st["pace_available"] and st["q3_available"]
    finally:
        v4api._PACE_REF_WORKER = None


# ══════════════════════════════════════════════════════════════════════
# 4. WORKER unit behavior
# ══════════════════════════════════════════════════════════════════════

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: production-scale latency guards (seconds to run)")


def _seed(db: Path, q3_pairs=200, q2_pairs=190):
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY, source_game_id TEXT,
            competition_slug TEXT, classification TEXT);
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY, source_game_id TEXT,
            period_label TEXT, home_score INTEGER, away_score INTEGER);
        CREATE TABLE game_results (
            source_game_id TEXT PRIMARY KEY, final_total REAL,
            final_result_status TEXT);
    """)
    conn.execute("INSERT INTO games VALUES (1,'g1','betual-nba','BETUAL_NBA')")
    # away_score must be an integer: the reference reads home_score+away_score
    # (NULL would make the whole expression NULL and the row fail closed).
    conn.execute("INSERT INTO snapshots VALUES "
                 "(1,'g1','3rd Quarter',?,0)", (q3_pairs,))
    conn.execute("INSERT INTO snapshots VALUES "
                 "(2,'g1','2nd Quarter',?,0)", (q2_pairs,))
    conn.execute("INSERT INTO game_results VALUES ('g1', 380, 'OK')")
    conn.commit()
    conn.close()


def test_worker_scans_publishes_and_skips_unchanged(tmp_path, monkeypatch):
    from blm_v4.live_analytics import fingerprint_c5
    from blm_v4.live_analytics import competition_pace
    monkeypatch.setattr(fingerprint_c5, "CACHE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(competition_pace, "CACHE_TTL_SECONDS", 0.0)
    db = tmp_path / "p.db"
    _seed(db)
    got: list = []
    w = PaceReferenceWorker(db, interval_s=3600, consumer=lambda p, q:
                            got.append((p, q)))
    assert w._run_once(force=True) is True
    assert w._run_once(force=True) is True
    assert len(got) == 2
    pace, q3 = got[-1]
    assert pace["betual-nba"]["avg_pace"] == pytest.approx(380.0 / 40.0)
    # Q3 pace = (q3_end − q2_end) / quarter_minutes(BETUAL_NBA = 10)
    assert q3["betual-nba"]["avg_q3_pace"] == pytest.approx(
        (200 - 190) / 10.0)   # _seed's q3_pairs=200, q2_pairs=190, BETUAL=10min
    # unchanged data → change-detector skips the scan
    assert w.should_scan() == (False, "unchanged")
    assert w._run_once() is False
    assert len(got) == 2
    snap = w.snapshot()
    assert snap["runs"] == 2 and snap["skips"] == 1
    assert snap["available"] is True


def test_worker_rescans_when_data_changes(tmp_path, monkeypatch):
    from blm_v4.live_analytics import fingerprint_c5
    from blm_v4.live_analytics import competition_pace
    monkeypatch.setattr(fingerprint_c5, "CACHE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(competition_pace, "CACHE_TTL_SECONDS", 0.0)
    db = tmp_path / "p.db"
    _seed(db)
    w = PaceReferenceWorker(db, interval_s=3600)
    assert w._run_once(force=True) is True
    scan, why = w.should_scan()
    assert (scan, why) == (False, "unchanged")
    # a new game settles → the key moves → rescan
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO games VALUES (2,'g2','betual-nba','BETUAL_NBA')")
    conn.execute("INSERT INTO game_results VALUES ('g2', 400, 'OK')")
    conn.commit()
    conn.close()
    scan, why = w.should_scan()
    assert (scan, why) == (True, "data_changed")


def test_worker_scan_failure_never_publishes_and_reports(tmp_path):
    db = tmp_path / "p.db"
    db.write_text("this is not a database")
    got: list = []
    w = PaceReferenceWorker(db, interval_s=3600,
                            consumer=lambda p, q: got.append((p, q)))
    assert w._run_once(force=True) is False
    assert got == []
    assert w.snapshot()["available"] is False
    assert w.snapshot()["last_error"]


def test_worker_start_stop_runs_at_least_once(tmp_path):
    from blm_v4.live_analytics import fingerprint_c5
    from blm_v4.live_analytics import competition_pace
    monkeypatch_ok = True
    db = tmp_path / "p.db"
    _seed(db)
    w = PaceReferenceWorker(db, interval_s=0.05)
    try:
        w.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and w.snapshot()["runs"] == 0:
            time.sleep(0.05)
        assert w.snapshot()["runs"] >= 1, "worker loop never completed a scan"
    finally:
        w.stop(timeout=2)
    assert w.snapshot()["runs"] >= 1


# ══════════════════════════════════════════════════════════════════════
# 5. STUB CONTRACT — the seam existing tests monkeypatch
# ══════════════════════════════════════════════════════════════════════

def test_route_still_honors_v4api_pace_reference_stub(client, monkeypatch):
    """No worker + stubbed v4api._pace_reference → /live serves the stub.
    This is the exact seam test_active_alert_triggered_line and friends
    rely on; the worker path must not have broken it."""
    monkeypatch.setattr(v4api, "_pace_reference",
                        lambda conn: {"betual-nba": {"avg_pace": 9.0,
                                                     "games": 100}})
    resp = client.get("/api/v4/live")
    assert resp.status_code == 200
    assert resp.json()["pace_reference"] == {
        "betual-nba": {"avg_pace": 9.0, "games": 100}}


def test_route_serves_real_inline_reference_when_no_worker(client,
                                                           populated_db):
    """No worker, no stub → the wrapper falls back to the inline engine
    computation (pre-fix contract preserved for tiny test DBs)."""
    resp = client.get("/api/v4/live")
    assert resp.status_code == 200
    ref = resp.json()["pace_reference"]
    assert ref.get("betual-nba", {}).get("games", 0) >= 1
