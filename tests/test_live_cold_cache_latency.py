"""COLD-CACHE /live latency regression guard.

Budget: the LIVE dashboard polls /api/v4/live every 5 s, so a cold poll
(empty historical-context cache) must stay under that polling budget and
must not scale worse than ONE population scan per game that carries
historical context.

Why this test exists: the dominant cold cost is the historical-context
layer — `HistoricalContextEngine.refresh_stats` (one whole-archive pass
per process) plus one strictly-prior population scan per game
(`benchmark._stats`, ~6k-19k rows per cell on production).  A regression
that (a) drops or bypasses the per-process stats cache, (b) drops the
120 s `_HCTX_CACHE`, or (c) makes the population scan run per checkpoint /
per poll instead of once per game would show up here as extra scans.

The SCAN-COUNT assertions are the real guard: they are deterministic and
machine-speed independent.  The wall-clock budget is the production 5 s
polling budget (NOT a tuned fixture number) — the fixture is small, so it
passes with a wide margin and only trips on an order-of-magnitude
regression.

The production DBs are never touched: every fixture is a tmp DB.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4 import api as v4api
from blm_v4.live_analytics import benchmark as bench
from blm_v4.live_analytics.historical_context import HistoricalContextEngine

# the LIVE dashboard's polling budget
POLL_BUDGET_S = 5.0
# games seeded live (each carrying historical context)
LIVE_GAMES = 6
# archival population per benchmark cell (must exceed MIN_BENCHMARK_N)
POP = 400


def _ro(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _build_world(tmp_path: Path):
    """Minimal but faithful world: prod DB (games/snapshots/market),
    clean DB (clean_projections population) and the sidecar ledger."""
    prod = tmp_path / "blm_pokerbet.db"
    clean = tmp_path / "blm_metrics_clean.db"
    side = Path(str(clean) + ".live_analytics.db")

    p = sqlite3.connect(prod)
    p.executescript("""
      CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL DEFAULT 'PokerBet', source_game_id TEXT NOT NULL,
        competition_id TEXT, competition_slug TEXT, competition TEXT,
        region TEXT, game_family TEXT, classification TEXT,
        sport TEXT DEFAULT 'basketball', home_team TEXT, away_team TEXT,
        game_slug TEXT, source_url TEXT, status TEXT DEFAULT 'live',
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        UNIQUE(source, source_game_id));
      CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER, source TEXT DEFAULT 'PokerBet', source_game_id TEXT,
        classification TEXT, captured_at TEXT, home_team TEXT, away_team TEXT,
        home_score INTEGER, away_score INTEGER, period_label TEXT,
        quarter INTEGER, clock TEXT, game_status TEXT DEFAULT 'live',
        w1_odds REAL, w2_odds REAL, spread_indicator TEXT, total_line REAL,
        total_over_odds REAL, total_under_odds REAL, spread REAL,
        spread_home_odds REAL, spread_away_odds REAL, home_total_line REAL,
        away_total_line REAL, source_url TEXT);
      CREATE TABLE market_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER, source_game_id TEXT, captured_at TEXT,
        market_type TEXT, market_name TEXT, line_value REAL, over_price REAL,
        under_price REAL, home_score INTEGER, away_score INTEGER,
        period_label TEXT, clock TEXT, raw_json TEXT DEFAULT '{}',
        UNIQUE(source_game_id, market_type, line_value, captured_at));
      CREATE TABLE game_results (source_game_id TEXT, final_total REAL);
    """)
    p.commit()

    c = sqlite3.connect(clean)
    c.executescript("""
      CREATE TABLE clean_projections (id INTEGER PRIMARY KEY AUTOINCREMENT,
        observation_id INTEGER UNIQUE, model_version TEXT DEFAULT 'v1',
        source_game_id TEXT, classification TEXT, captured_at TEXT,
        period_label TEXT, clock TEXT, elapsed_game_minutes REAL,
        remaining_game_minutes REAL, progress_pct REAL,
        current_total_points INTEGER, live_total_line REAL,
        market_captured_at TEXT, market_age_seconds REAL, market_status TEXT,
        actual_pts_per_min REAL, required_pts_per_min REAL, pace_gap REAL,
        required_to_actual_ratio REAL, projected_final_total REAL,
        projection_vs_live_line REAL, fair_total REAL, recent_pace_1m REAL,
        recent_span_1m REAL, recent_pace_2m REAL, recent_span_2m REAL,
        recent_pace_3m REAL, recent_span_3m REAL, recent_pace_5m REAL,
        recent_span_5m REAL, pace_acceleration REAL, acceleration_window TEXT,
        trajectory_state TEXT, subsequent_observation_id INTEGER,
        subsequent_actual_pace REAL, subsequent_pace_change REAL,
        subsequent_live_line REAL, subsequent_live_line_change REAL,
        final_settled_total INTEGER, status TEXT, computed_at TEXT,
        terminal INTEGER DEFAULT 0, predictive_eligible INTEGER DEFAULT 1);
      CREATE INDEX idx_clean_proj_bench ON clean_projections(
        classification, period_label, progress_pct, captured_at);
    """)
    c.commit()

    s = sqlite3.connect(side)
    s.executescript("""
      CREATE TABLE IF NOT EXISTS competition_ledger (source_game_id TEXT
        PRIMARY KEY, provider TEXT, competition TEXT, competition_id TEXT,
        source_classification TEXT, status TEXT, reason TEXT,
        resolved_at TEXT);
      CREATE TABLE IF NOT EXISTS pace_benchmark_cache (benchmark_key TEXT,
        cutoff_captured_at TEXT, cutoff_observation_id INTEGER,
        n INTEGER, mean_pace REAL, std_pace REAL, computed_at TEXT,
        PRIMARY KEY (benchmark_key, cutoff_captured_at,
                     cutoff_observation_id));
      CREATE TABLE IF NOT EXISTS historical_context_cache (benchmark_key TEXT
        PRIMARY KEY, n INTEGER, under_n INTEGER,
        equal_game_under_pct REAL, under_pct REAL, over_pct REAL,
        computed_at TEXT);
    """)
    s.commit()
    s.close()
    p.row_factory = sqlite3.Row
    c.row_factory = sqlite3.Row
    return prod, clean, side, p, c


def _seed_population(c, side, p, comp="betual-nba", period="4th Quarter",
                     prog=93.75, n=POP):
    """n settled archival observations in ONE benchmark cell, ledger-
    classified, with game_results so refresh_stats can join."""
    fam = "BETUAL_NBA"
    prov = "BETUAL"
    rows, gids = [], []
    for i in range(n):
        gid = f"{comp}-{i // 2:04d}"          # 2 observations per game
        gids.append(gid)
        rows.append((i + 1, gid, fam, f"2026-01-01T10:{i // 60:02d}:{i % 60:02d}Z",
                     period, prog, 36.5, 3.5, 150, 200.5, 4.0, 5.0, 1.0,
                     "VALID", "2026-01-01T10:00:00Z"))
    c.executemany(
        "INSERT INTO clean_projections (observation_id, source_game_id,"
        " classification, captured_at, period_label, progress_pct,"
        " elapsed_game_minutes, remaining_game_minutes, current_total_points,"
        " live_total_line, actual_pts_per_min, required_pts_per_min, pace_gap,"
        " status, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    uniq = sorted(set(gids))
    side.executemany(
        "INSERT OR REPLACE INTO competition_ledger VALUES (?,?,?,?,?,?,?,?)",
        [(g, prov, comp, None, fam, "classified", None,
          "2026-01-01T00:00:00Z") for g in uniq])
    side.commit()
    p.executemany("INSERT OR REPLACE INTO game_results VALUES (?,?)",
                  [(g, 180.0) for g in uniq])
    p.commit()


def _seed_live_games(p, c, side, now_iso):
    """N live games at the SAME benchmark cell as the population, each with
    a fresh latest VALID observation (so age <= LIVE_AGE_S)."""
    for i in range(LIVE_GAMES):
        gid = f"LIVE-{i:03d}"
        p.execute(
            "INSERT OR REPLACE INTO games (source_game_id, competition_slug,"
            " classification, home_team, away_team, status, first_seen_at,"
            " last_seen_at) VALUES (?,?,?,?,?,'live',?,?)",
            (gid, "betual-nba", "BETUAL_NBA", f"H{i}", f"A{i}",
             "2026-01-01T10:00:00Z", now_iso))
        c.execute(
            "INSERT INTO clean_projections (observation_id, source_game_id,"
            " classification, captured_at, period_label, progress_pct,"
            " elapsed_game_minutes, remaining_game_minutes,"
            " current_total_points, live_total_line, actual_pts_per_min,"
            " required_pts_per_min, pace_gap, status, computed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (10_000 + i, gid, "BETUAL_NBA", now_iso, "4th Quarter", 93.75,
             36.5, 3.5, 150, 200.5, 4.0, 5.0, 1.0, "VALID", now_iso))
        side.execute(
            "INSERT OR REPLACE INTO competition_ledger VALUES"
            " (?,?,?,?,?,?,?,?)",
            (gid, "BETUAL", "betual-nba", None, "BETUAL_NBA", "classified",
             None, "2026-01-01T00:00:00Z"))
    p.commit()
    c.commit()
    side.commit()


@pytest.fixture
def world(tmp_path, monkeypatch):
    prod, clean, side, p, c = _build_world(tmp_path)
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _seed_population(c, sqlite3.connect(str(side)), p)
    _seed_live_games(p, c, sqlite3.connect(str(side)), now_iso)

    monkeypatch.setenv("BLM_POKERBET_DB", str(prod))
    # isolate the process-level caches the request path relies on
    monkeypatch.setattr(v4api, "_HCTX_CACHE", {})
    monkeypatch.setattr(v4api, "_HIST_ENGINE", None)
    monkeypatch.setattr(v4api, "_HIST_ENGINE_PATH", None)
    # the composition root's worker serves the league references; the
    # historical-context path under test is independent of them
    monkeypatch.setattr(v4api, "_pace_reference", lambda conn=None: {})
    monkeypatch.setattr(v4api, "_q3_pace_reference", lambda conn=None: {})

    # cheap, deterministic per-game seams (the population scan is REAL)
    games = [dict(r) for r in _ro(p, "SELECT * FROM games")]
    for g in games:
        g["first_seen_at"] = now_iso
        g["last_seen_at"] = now_iso
    monkeypatch.setattr(v4api, "_load_games", lambda conn, cls=None: games)
    monkeypatch.setattr(v4api, "_load_snapshot_tail",
                        lambda conn, gid, limit=500: [])
    monkeypatch.setattr(v4api, "_quality_map", lambda conn, ids: {})
    monkeypatch.setattr(v4api, "_settled_result_map", lambda ids: {})
    monkeypatch.setattr(v4api, "_projection_lines_map", lambda ids: {})
    monkeypatch.setattr(v4api, "_pace_projector_for", lambda gid: None)
    yield prod, clean, side, p, c
    p.close()
    c.close()


def _count_scans(monkeypatch):
    """Instrument the population scan + the whole-archive stats pass."""
    counts = {"stats": 0, "refresh": 0}
    real_stats = bench._stats
    real_refresh = HistoricalContextEngine.refresh_stats

    def stats(*a, **k):
        counts["stats"] += 1
        return real_stats(*a, **k)

    def refresh(self, *a, **k):
        counts["refresh"] += 1
        return real_refresh(self, *a, **k)

    monkeypatch.setattr(bench, "_stats", stats)
    monkeypatch.setattr(HistoricalContextEngine, "refresh_stats", refresh)
    return counts


def test_cold_poll_under_budget_and_one_scan_per_game(world, monkeypatch):
    counts = _count_scans(monkeypatch)
    t0 = time.perf_counter()
    payload = v4api.v4_live(classification=None)
    cold_s = time.perf_counter() - t0

    hctx = [g for g in payload["games"]
            if isinstance(g.get("historical_context"), dict)
            and g["historical_context"].get("status") == "matched"]
    # the historical-context path must actually have been exercised
    assert len(hctx) == LIVE_GAMES, [g.get("game_id") for g in payload["games"]]
    # exactly ONE population scan per context-carrying game; the whole-
    # archive refresh runs at most once per process
    assert counts["stats"] == LIVE_GAMES, counts
    assert counts["refresh"] == 1, counts
    # production polling budget (not a tuned fixture threshold)
    assert cold_s < POLL_BUDGET_S, f"cold /live {cold_s:.3f}s >= {POLL_BUDGET_S}s"


def test_warm_poll_reuses_cache_no_rescan(world, monkeypatch):
    counts = _count_scans(monkeypatch)
    v4api.v4_live(classification=None)          # cold (populates the cache)
    cold = dict(counts)
    t0 = time.perf_counter()
    v4api.v4_live(classification=None)          # warm
    warm_s = time.perf_counter() - t0

    # THE regression guard: a warm poll must not rescan the population nor
    # re-run the whole-archive pass — the 120 s cache serves.
    assert cold["stats"] == LIVE_GAMES and cold["refresh"] == 1, cold
    assert counts == cold, (
        "warm poll re-scanned the historical population "
        f"(cold={cold}, after={counts}) — the _HCTX_CACHE is not serving")
    assert warm_s < POLL_BUDGET_S, f"warm /live {warm_s:.3f}s >= {POLL_BUDGET_S}s"


def test_context_output_is_deterministic_across_polls(world, monkeypatch):
    """Same inputs → same outputs: the cached block must equal a freshly
    computed one for the same game state (semantic identity)."""
    payload1 = v4api.v4_live(classification=None)
    fresh = [g["historical_context"] for g in payload1["games"]
             if isinstance(g.get("historical_context"), dict)]
    v4api._HCTX_CACHE.clear()                   # force recomputation
    v4api._HIST_ENGINE = None
    payload2 = v4api.v4_live(classification=None)
    again = [g["historical_context"] for g in payload2["games"]
             if isinstance(g.get("historical_context"), dict)]
    assert json.dumps(fresh, sort_keys=True) == json.dumps(again, sort_keys=True)
