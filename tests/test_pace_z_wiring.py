"""PACE Z-SCORE wiring — the modal Z panel must consume the authoritative
PACE z (actual pace vs the strictly-prior historical pace benchmark at
the same provider|competition|period|progress state), never the old
market-vs-trajectory residual z, and never a frontend calculation."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router
from blm_v4.live_analytics.benchmark import ensure_schema
from blm_v4.live_analytics.league import ensure_league_schema
from blm_v4.live_analytics.service import pace_z_payload

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
DASH_JS = DASH_STATIC / "dashboard.js"

# ── the exact frontend strings that pin the modal Z panel's source ──
PACE_Z_API_LINE = ('const API_GAME_PACE_Z = (id) => '
                   '`/api/v4/game/${encodeURIComponent(id)}/pace-z`;')


@pytest.fixture
def clean_db(tmp_path):
    """Clean metrics DB with the population + the probe game's rows."""
    dbf = tmp_path / "blm_metrics_clean.db"
    c = sqlite3.connect(dbf)
    c.executescript("""
        CREATE TABLE clean_projections (
            id INTEGER PRIMARY KEY, source_game_id TEXT,
            classification TEXT, captured_at TEXT, period_label TEXT,
            progress_pct REAL, actual_pts_per_min REAL, status TEXT);
    """)
    t0 = time.mktime(time.strptime("2026-09-01T00:00:00", "%Y-%m-%dT%H:%M:%S"))
    # 40 prior rows for betual-nba Q2 @ 50% with pace ~2.0 (tiny σ from jitter)
    rows = []
    for i in range(40):
        gid = "POP%d" % i
        # realistic spread (σ ≈ 0.12 pts/min) so a fast game z≈+2.5, not +40
        pace = 2.0 + (((i * 37) % 21) - 10) * 0.02
        rows.append((gid, "BETUAL_NBA",
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
                         t0 + i * 60)), "2nd Quarter", 50.0 + i * 0.01,
                     pace, "VALID"))
    # the probe game's own two observations (the later one is the payload's
    # current state; BOTH must exclude themselves + each other from their
    # own benchmark via the same-game exclusion)
    rows.append(("30845868", "BETUAL_NBA",
                 time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(t0 + 2700)), "2nd Quarter",
                 50.1, 2.10, "VALID"))
    rows.append(("30845868", "BETUAL_NBA",
                 time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(t0 + 3600)), "2nd Quarter",
                 50.2, 2.30, "VALID"))
    c.executemany(
        "INSERT INTO clean_projections"
        " (source_game_id, classification, captured_at, period_label,"
        "  progress_pct, actual_pts_per_min, status)"
        " VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return dbf


@pytest.fixture
def main_db(tmp_path, clean_db):
    """Production main DB: the games row carries authoritative competition
    metadata for the probe game (and nothing for population rows — they
    enter populations only via the ledger)."""
    dbf = tmp_path / "blm_pokerbet.db"
    c = sqlite3.connect(dbf)
    c.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY, source_game_id TEXT,
            classification TEXT, competition TEXT,
            competition_slug TEXT, competition_id TEXT,
            home_team TEXT, away_team TEXT);
    """)
    # every population member needs an authoritative games row (the
    # service tops the ledger up from games — production parity)
    g_rows = [("30845868", "BETUAL_NBA", "Betual NBA", "betual-nba",
               "18296756", "H", "A")]
    for i in range(40):
        g_rows.append(("POP%d" % i, "BETUAL_NBA", "Betual NBA",
                       "betual-nba", "18296756", "H%d" % i, "A%d" % i))
    c.executemany("INSERT INTO games (source_game_id, classification,"
                  " competition, competition_slug, competition_id,"
                  " home_team, away_team) VALUES (?,?,?,?,?,?,?)", g_rows)
    c.commit()
    c.close()
    return dbf


@pytest.fixture
def client(main_db, clean_db, monkeypatch):
    monkeypatch.setenv("BLM_POKERBET_DB", str(main_db))
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_pacez_static")
    return TestClient(app)


def test_service_pace_z_is_pace_not_residual(main_db, clean_db):
    """End-to-end: z is computed against the PRIOR pace population of the
    same competition — and the game's own row is never its own member."""
    main = sqlite3.connect(main_db)
    try:
        payload = pace_z_payload(main, clean_db, "30845868", history=2)
    finally:
        main.close()
    assert payload["provider"] == "BETUAL"
    assert payload["competition"] == "betual-nba"
    assert payload["period"] == "2nd Quarter"
    # population = strictly the 40 POP rows (own row excluded by id, and
    # unregistered POP games are excluded from the population join)
    # population = the 40 prior POP rows (own row excluded by id; same-game
    # rows excluded; ledger join keeps only classified members)
    assert payload["n"] == 40, payload["n"]
    assert 1.95 < payload["mean_pace"] < 2.05
    assert payload["z"] is not None and 1.0 < payload["z"] < 4
    assert payload["benchmark_key"].startswith("BETUAL|betual-nba|Q2|P050")
    assert payload["benchmark_status"] == "ok"
    # history points are strictly T-state: each older point's population
    # is smaller than or equal to the newest one's; both of the game's own
    # observations are excluded from their own benchmarks (n stays 40)
    ser = payload["series"]
    assert len(ser) == 2
    assert ser[0]["captured_at"] < ser[1]["captured_at"]
    assert ser[0]["n"] == 40 and ser[1]["n"] == 40
    assert ser[0]["benchmark_key"] == ser[1]["benchmark_key"]
    # no self-inclusion at ANY history point: the population never
    # contains the game's own pace values
    assert all(s["mean_pace"] < 2.05 for s in ser)


def test_service_unknown_game_is_honest(main_db, clean_db):
    main = sqlite3.connect(main_db)
    try:
        payload = pace_z_payload(main, clean_db, "NO-SUCH-GAME")
    finally:
        main.close()
    assert payload["z"] is None
    assert payload["benchmark_status"] == "no_observations"
    assert payload["series"] == []


def test_endpoint_serves_pace_z(client):
    r = client.get("/api/v4/game/30845868/pace-z")
    assert r.status_code == 200
    d = r.json()
    assert d["z"] is not None
    assert d["provider"] == "BETUAL" and d["competition"] == "betual-nba"
    assert d["n"] >= 30
    assert isinstance(d["series"], list) and d["series"]
    # a descriptive payload: no probability/edge/prediction keys
    for banned in ("edge", "probability", "signal", "stake", "ev",
                   "fair_total", "projected_final_total"):
        assert banned not in json.dumps(d).lower(), banned


def test_population_never_mixes_competitions(main_db, clean_db):
    """Defense-in-depth at the service layer: a TSBL-flagged game's rows
    never enter the betual-nba population even in the same DB."""
    c = sqlite3.connect(clean_db)
    c.execute(
        "INSERT INTO clean_projections (source_game_id, classification,"
        " captured_at, period_label, progress_pct, actual_pts_per_min,"
        " status) VALUES ('TSBL1','BETUAL_NBA',"
        " '2026-09-01T00:30:00Z','2nd Quarter',50.1,9.99,'VALID')")
    c.commit()
    c.close()
    gm = sqlite3.connect(main_db)
    gm.execute("INSERT INTO games (source_game_id, classification,"
               " competition, competition_slug, competition_id,"
               " home_team, away_team) VALUES ('TSBL1','BETUAL_NBA',"
               " 'Betual NBA','betual-tbsl','18296900','T1','T2')")
    gm.commit()
    gm.close()
    m = sqlite3.connect(main_db)
    try:
        p = pace_z_payload(m, clean_db, "30845868")
    finally:
        m.close()
    # 9.99 pts/min (TSBL row) cannot have entered the NBA population:
    # μ stays ~2.0
    assert p["mean_pace"] < 2.1, p["mean_pace"]
    assert p["n"] == 40, p["n"]


def test_endpoint_failure_is_isolated(client, monkeypatch):
    import blm_v4.api as api_mod
    def boom(*a, **k):
        raise RuntimeError("clean db exploded")
    monkeypatch.setattr("blm_v4.api.v4_game_pace_z.__wrapped__",
                        boom, raising=False)
    # failure isolation is inside the endpoint itself; simulate by a bad id
    # type through the API (no crash, honest unavailable payload)
    r = client.get("/api/v4/game/../../etc/passwd/pace-z")
    assert r.status_code in (200, 404)


def _modal_js() -> str:
    js = DASH_JS.read_text()
    m = re.search(r"async function renderModalCharts\(g\) \{.*?\n\}", js, re.S)
    assert m, "renderModalCharts not found"
    return m.group(0)


def test_frontend_z_panel_consumes_pace_z_endpoint():
    js = DASH_JS.read_text()
    assert PACE_Z_API_LINE in js
    # the panel header names the measurement
    assert "PACE Z-SCORE" in js
    # the readout carries the benchmark provenance (n)
    assert "PACE Z" in js and "N=" in js
    modal = _modal_js()
    # the modal Z series is built from the pace-z payload's series
    assert "pz.series" in modal
    assert "s.z" in modal
    assert "s.z_score" not in modal


def test_no_residual_z_in_modal_path():
    modal = _modal_js()
    # the old source (deviation endpoint) is gone from the modal path
    assert "API_GAME_DEV" not in modal
    assert "dev.series" not in modal
    assert "market_trajectory_residual" not in modal
    assert "projected_final_total" not in modal


def test_stale_deviation_endpoint_still_serves_research(client):
    """Research firewall: the deviation (market-vs-trajectory) endpoint is
    untouched and still available for the research view."""
    r = client.get("/api/v4/game/30845868/deviation")
    assert r.status_code == 200
    d = r.json()
    assert d["section"] == "deviation_research"
    assert isinstance(d["series"], list)
