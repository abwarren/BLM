"""LIVE ROUTE — SINGLE-FLIGHT + SHORT-TTL CACHE (directive 2026-10-01).

The 2026-10-01 incident: /api/v4/live is a SYNC route, so FastAPI runs its
body on the AnyIO worker pool (default 40 tokens).  Its body is a per-game
deep analysis costing tens of seconds on the production DB, and the
dashboard polled it every 5 s with no overlap guard — so every pool token
was consumed by /live builds, and the framework work that shares that pool
(FileResponse for /login, StaticFiles for the JS/CSS) starved.  The page
could not load its assets and the /status poll timed out.

THE CONTRACT these tests lock in (directive FIX 1 / FIX 4 / FIX 5):

  1. SINGLE-FLIGHT — concurrent callers trigger exactly ONE analysis.
  2. TTL — a second call inside the TTL reuses the completed payload; a
     call after the TTL rebuilds.
  3. NO POISONING — a failed build is never cached as a value, never
     overwrites a good payload, and never leaves an in-flight marker.
  4. FAIL-FAST — after a failed build, a burst of callers is refused
     (503) instead of each re-running the expensive analysis.
  5. STALE-WHILE-REVALIDATE — a caller arriving during a slow build is
     served the last completed payload immediately (no blocked token).
  6. THE LOCK IS NOT HELD DURING A BUILD — nothing else in the process
     (notably /api/v4/status) can be blocked by a running build.
  7. /api/v4/status NEVER runs the /live analysis.

These are behavioural tests against the real ``_live_payload`` seam, with
the expensive build replaced by a counting/slow fake.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from blm_v4 import api as v4api


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def live_cache(monkeypatch):
    """Fresh cache state + explicit thresholds for every test.

    The module reads its thresholds from the environment PER CALL, so
    monkeypatch is enough — no reimport.  The three module dicts are
    replaced so no state can leak between tests."""
    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "5")
    monkeypatch.setenv("BLM_LIVE_MAX_STALE_S", "300")
    monkeypatch.setenv("BLM_LIVE_SINGLEFLIGHT_WAIT_S", "8")
    monkeypatch.setenv("BLM_LIVE_ERROR_COOLDOWN_S", "5")
    monkeypatch.setattr(v4api, "_LIVE_CACHE", {})
    monkeypatch.setattr(v4api, "_LIVE_INFLIGHT", {})
    monkeypatch.setattr(v4api, "_LIVE_FAILED_AT", {})
    monkeypatch.setattr(v4api, "_LIVE_LOCK", threading.Lock())
    yield


def _no_worker(monkeypatch):
    monkeypatch.setattr(v4api, "_PACE_REF_WORKER", None)
    monkeypatch.setattr(v4api, "_PACE_REF_LATEST",
                        {"pace": None, "q3": None})


# ══════════════════════════════════════════════════════════════════════
# 1. SINGLE-FLIGHT
# ══════════════════════════════════════════════════════════════════════

def test_concurrent_callers_trigger_exactly_one_analysis(monkeypatch):
    """THE amplification fix: N simultaneous callers ⇒ 1 analysis."""
    builds: list = []
    release = threading.Barrier(9)          # 8 workers + main

    def fake_build(classification):
        builds.append(time.monotonic())
        time.sleep(0.3)                     # a build that takes real time
        return {"generated_at": "x", "games": [], "build_no": len(builds)}

    monkeypatch.setattr(v4api, "_v4_live_uncached", fake_build)

    results: list = []
    errors: list = []

    def worker():
        try:
            release.wait(10)                # release all callers together
            results.append(v4api._live_payload(None))
        except BaseException as exc:        # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    release.wait(10)
    for t in threads:
        t.join(20)

    assert not errors, f"callers raised: {errors}"
    assert len(results) == 8
    assert len(builds) == 1, (
        f"expected exactly ONE analysis for 8 concurrent callers, "
        f"got {len(builds)}")
    # every caller is served the outcome of that single build
    assert all(r["build_no"] == 1 for r in results)


def test_repeated_concurrent_rounds_still_one_build_per_round(monkeypatch):
    """A second burst AFTER the TTL expires builds once, not once per caller."""
    builds: list = []
    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "0.05")

    def fake_build(classification):
        builds.append(time.monotonic())
        time.sleep(0.2)
        return {"build_no": len(builds)}

    monkeypatch.setattr(v4api, "_v4_live_uncached", fake_build)
    for _round in range(2):
        _burst(v4api._live_payload, 6)
        time.sleep(0.1)                     # let the TTL lapse
    assert len(builds) == 2, f"expected 1 build per round, got {len(builds)}"


def _burst(fn, n):
    release = threading.Barrier(n + 1)
    out: list = []

    def worker():
        release.wait(10)
        out.append(fn(None))

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    release.wait(10)
    for t in ts:
        t.join(20)
    return out


# ══════════════════════════════════════════════════════════════════════
# 2. TTL
# ══════════════════════════════════════════════════════════════════════

def test_call_within_ttl_reuses_the_completed_build(monkeypatch):
    calls: list = []
    monkeypatch.setattr(v4api, "_v4_live_uncached",
                        lambda cls: (calls.append(1), {"n": len(calls)})[1])
    first = v4api._live_payload(None)
    second = v4api._live_payload(None)
    assert first == second == {"n": 1}
    assert len(calls) == 1, "a call inside the TTL must not rebuild"


def test_call_after_ttl_rebuilds(monkeypatch):
    calls: list = []
    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "0.05")
    monkeypatch.setattr(v4api, "_v4_live_uncached",
                        lambda cls: (calls.append(1), {"n": len(calls)})[1])
    assert v4api._live_payload(None) == {"n": 1}
    time.sleep(0.1)
    assert v4api._live_payload(None) == {"n": 2}
    assert len(calls) == 2


def test_ttl_zero_disables_caching_entirely(monkeypatch):
    """TTL 0 is the documented "build every call" mode the rest of the suite
    relies on (tests/conftest.py sets it by default)."""
    calls: list = []
    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "0")
    monkeypatch.setattr(v4api, "_v4_live_uncached",
                        lambda cls: (calls.append(1), {"n": len(calls)})[1])
    v4api._live_payload(None)
    v4api._live_payload(None)
    assert len(calls) == 2


# ══════════════════════════════════════════════════════════════════════
# 3. NO CACHE POISONING
# ══════════════════════════════════════════════════════════════════════

def test_failed_build_is_never_cached_and_leaves_no_in_flight_marker(monkeypatch):
    monkeypatch.setenv("BLM_LIVE_ERROR_COOLDOWN_S", "0")   # retry immediately
    n = {"calls": 0}

    def flaky(cls):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setattr(v4api, "_v4_live_uncached", flaky)
    with pytest.raises(RuntimeError):
        v4api._live_payload(None)
    assert v4api._LIVE_CACHE == {}, "a failure must never be stored as a value"
    assert v4api._LIVE_INFLIGHT == {}, "in-flight marker leaked after failure"
    assert v4api._live_payload(None) == {"ok": True}


def test_a_failed_rebuild_degrades_to_the_last_good_payload(monkeypatch):
    """A failed REFRESH must not take the dashboard down: the last good
    payload is served, and the failure is never cached over it."""
    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "0.01")
    monkeypatch.setenv("BLM_LIVE_ERROR_COOLDOWN_S", "0")
    state = {"fail": False}

    def build(cls):
        if state["fail"]:
            raise RuntimeError("boom")
        return {"ok": True, "v": 1}

    monkeypatch.setattr(v4api, "_v4_live_uncached", build)
    assert v4api._live_payload(None)["v"] == 1
    time.sleep(0.02)                       # TTL lapsed → next call rebuilds
    state["fail"] = True
    got = v4api._live_payload(None)        # the rebuild fails...
    assert got == {"ok": True, "v": 1}, (
        "a failed refresh must degrade to the last good payload, not error")
    # ...and it cached nothing and left no marker
    assert v4api._LIVE_CACHE[""]["value"] == {"ok": True, "v": 1}
    assert v4api._LIVE_INFLIGHT == {}


# ══════════════════════════════════════════════════════════════════════
# 4. FAIL-FAST COOLDOWN
# ══════════════════════════════════════════════════════════════════════

def test_callers_fail_fast_after_a_failed_build(monkeypatch):
    monkeypatch.setenv("BLM_LIVE_ERROR_COOLDOWN_S", "30")
    builds: list = []

    def boom(cls):
        builds.append(1)
        raise RuntimeError("database is broken")

    monkeypatch.setattr(v4api, "_v4_live_uncached", boom)
    with pytest.raises(RuntimeError):
        v4api._live_payload(None)          # the first caller does the work
    assert len(builds) == 1
    # during the cooldown further callers are refused with 503 rather than
    # each re-running the analysis
    for _ in range(5):
        with pytest.raises(HTTPException) as ei:
            v4api._live_payload(None)
        assert ei.value.status_code == 503
    assert len(builds) == 1, "callers re-ran the analysis during the cooldown"


def test_a_good_payload_is_served_instead_of_a_503_during_the_cooldown(monkeypatch):
    """The fail-fast cooldown protects the pool only when there is NOTHING
    to serve.  With a usable payload the caller is served it — a transient
    failure never converts a working dashboard into a 503."""
    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "0.01")
    monkeypatch.setenv("BLM_LIVE_ERROR_COOLDOWN_S", "30")
    state = {"fail": False}

    def build(cls):
        if state["fail"]:
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setattr(v4api, "_v4_live_uncached", build)
    assert v4api._live_payload(None) == {"ok": True}
    time.sleep(0.02)
    state["fail"] = True
    for _ in range(3):
        assert v4api._live_payload(None) == {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# 5. STALE-WHILE-REVALIDATE + 6. LOCK NOT HELD DURING A BUILD
# ══════════════════════════════════════════════════════════════════════

def test_caller_is_served_the_last_payload_while_a_build_runs(monkeypatch):
    """A concurrent caller must NOT park a pool token waiting on a build."""
    monkeypatch.setattr(v4api, "_v4_live_uncached", lambda cls: {"v": 1})
    assert v4api._live_payload(None)["v"] == 1

    monkeypatch.setenv("BLM_LIVE_CACHE_TTL_S", "0.01")
    time.sleep(0.02)                        # TTL lapsed → next call rebuilds
    started, release = threading.Event(), threading.Event()

    def slow_build(cls):
        started.set()
        release.wait(10)
        return {"v": 2}

    monkeypatch.setattr(v4api, "_v4_live_uncached", slow_build)
    leader = threading.Thread(target=lambda: v4api._live_payload(None))
    leader.start()
    try:
        assert started.wait(10), "leader never started building"
        t0 = time.monotonic()
        got = v4api._live_payload(None)
        elapsed = time.monotonic() - t0
        assert got["v"] == 1, "concurrent caller did not get the last payload"
        assert elapsed < 1.0, (
            f"concurrent caller blocked {elapsed:.2f}s on a running build")
    finally:
        release.set()
        leader.join(10)


def test_the_cache_lock_is_not_held_while_a_build_runs(monkeypatch):
    """Nothing else in the process may be blocked by a running build —
    this is what keeps /api/v4/status (and the static/FileResponse paths)
    responsive while /live is building."""
    started, release = threading.Event(), threading.Event()

    def slow_build(cls):
        started.set()
        release.wait(10)
        return {"v": 1}

    monkeypatch.setattr(v4api, "_v4_live_uncached", slow_build)
    leader = threading.Thread(target=lambda: v4api._live_payload(None))
    leader.start()
    try:
        assert started.wait(10)
        acquired = v4api._LIVE_LOCK.acquire(timeout=1.0)
        assert acquired, "the cache lock is held across the build"
        v4api._LIVE_LOCK.release()
    finally:
        release.set()
        leader.join(10)


# ══════════════════════════════════════════════════════════════════════
# 7. /status IS INDEPENDENT OF THE /live ANALYSIS  (FIX 4)
# ══════════════════════════════════════════════════════════════════════

def _empty_pipeline_db(tmp_path: Path) -> Path:
    """A minimal but schema-complete pipeline DB for the /status route."""
    db = tmp_path / "blm_pokerbet.db"
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
        CREATE TABLE market_observations (
            id INTEGER PRIMARY KEY, source_game_id TEXT, captured_at TEXT,
            market_type TEXT, period_label TEXT, clock TEXT,
            home_score INTEGER, away_score INTEGER, line_value REAL);
        CREATE TABLE reconciliation (
            id INTEGER PRIMARY KEY, source TEXT, source_game_id TEXT,
            result TEXT, checked_at TEXT);
        CREATE INDEX idx_snapshots_class_captured
            ON snapshots(classification, captured_at);
        CREATE INDEX idx_snapshots_game_captured
            ON snapshots(game_id, captured_at);
    """)
    conn.commit()
    conn.close()
    return db


def test_status_never_runs_the_live_analysis(tmp_path, monkeypatch):
    """The collector-health route must not touch the expensive path — even
    if the /live builder would blow up, /status must still answer."""
    db = _empty_pipeline_db(tmp_path)
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    _no_worker(monkeypatch)
    v4api._FRESH_STATE_CACHE["key"] = None
    v4api._FRESH_STATE_CACHE["value"] = None

    def _boom(cls=None):
        raise AssertionError("/status ran the /live analysis")

    monkeypatch.setattr(v4api, "_v4_live_uncached", _boom)

    app = FastAPI()
    app.include_router(v4api.router)
    with TestClient(app) as client:
        resp = client.get("/api/v4/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "collector" in body and "db" in body


def test_status_reports_collector_state_without_a_database(tmp_path, monkeypatch):
    """Fix 2's server-side half: collector state comes from its own file, so
    a DB problem is reported, not conflated, and the route degrades to an
    explicit state rather than an exception."""
    monkeypatch.setenv("BLM_POKERBET_DB", str(tmp_path / "missing.db"))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    (tmp_path / "collector_state.json").write_text(
        '{"status": "running", "last_tick_at": "2026-10-01T00:00:00+00:00"}')
    app = FastAPI()
    app.include_router(v4api.router)
    with TestClient(app) as client:
        resp = client.get("/api/v4/status")
    assert resp.status_code == 200
    body = resp.json()
    # the collector block is present even though the DB is unreachable
    assert body["collector"]["status"] == "running"


# ══════════════════════════════════════════════════════════════════════
# 8/9. THE SAME CONTRACT AT THE HTTP LAYER  (FIX 1 / FIX 5)
# ══════════════════════════════════════════════════════════════════════

def _live_app(tmp_path, monkeypatch):
    db = _empty_pipeline_db(tmp_path)
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    _no_worker(monkeypatch)
    v4api._FRESH_STATE_CACHE["key"] = None
    v4api._FRESH_STATE_CACHE["value"] = None
    app = FastAPI()
    app.include_router(v4api.router)
    return app


def test_http_concurrent_live_requests_share_one_build(tmp_path, monkeypatch):
    """FIX 1 at the HTTP boundary: 8 simultaneous GET /api/v4/live produce
    ONE analysis — the amplification the incident was made of."""
    builds: list = []

    def fake_build(classification):
        builds.append(time.monotonic())
        time.sleep(0.4)
        return {"generated_at": "2026-10-01T00:00:00+00:00", "games": [],
                "totals": {"live": 0, "total": 0}}

    monkeypatch.setattr(v4api, "_v4_live_uncached", fake_build)
    app = _live_app(tmp_path, monkeypatch)
    release = threading.Barrier(9)
    codes: list = []
    with TestClient(app) as client:
        def hit():
            release.wait(15)
            codes.append(client.get("/api/v4/live").status_code)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        release.wait(15)
        for t in threads:
            t.join(30)

    assert codes and all(c == 200 for c in codes), f"statuses: {codes}"
    assert len(builds) == 1, (
        f"the HTTP layer ran {len(builds)} analyses for 8 concurrent "
        f"requests — the single-flight contract is broken")


def test_status_stays_responsive_while_live_is_building(tmp_path, monkeypatch):
    """FIX 5: /api/v4/status must answer while a /live build is in flight —
    the two must not share a serialising lock."""
    started, release = threading.Event(), threading.Event()

    def slow_build(cls):
        started.set()
        release.wait(20)
        return {"generated_at": "2026-10-01T00:00:00+00:00", "games": []}

    monkeypatch.setattr(v4api, "_v4_live_uncached", slow_build)
    app = _live_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        live_thread = threading.Thread(target=lambda: client.get("/api/v4/live"))
        live_thread.start()
        try:
            assert started.wait(15), "the /live build never started"
            t0 = time.monotonic()
            resp = client.get("/api/v4/status")
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            live_thread.join(30)

    assert resp.status_code == 200
    assert elapsed < 5.0, (
        f"/api/v4/status took {elapsed:.1f}s while /live was building — "
        f"the pool is being held by the live analysis")
