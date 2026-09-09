"""FROZEN ARCHITECTURE — prospective-phase regression tests.

Covers the freeze directive (2026-09-09):
  §8  canonical identity (provider / competition_slug) on the game
      detail payload and in the modal GAME STATE panel;
  §10 the prospective health report — descriptive only, with a
      structural guard forbidding edge/win-rate/probability/signal/
      staking/EV concepts from ever appearing in the payload.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blm_v4.api import _analyze_game
from blm_v4.live_analytics.league import PROVIDERS
from blm_v4.live_analytics.prospective_health import (assert_descriptive_only,
                                                      build_report)

HERE = Path(__file__).resolve().parent
DASH_JS = HERE.parent / "blm_v4" / "dashboard" / "static" / "dashboard.js"

NOW = datetime(2026, 9, 9, 15, 0, 0, tzinfo=timezone.utc)


# ── §8 canonical identity ────────────────────────────────────────────────

def test_detail_payload_carries_canonical_identity():
    game = {"source_game_id": "30845868", "id": 1, "source": "PokerBet",
            "classification": "BETUAL_NBA", "competition": "Betual NBA",
            "competition_slug": "betual-nba", "competition_id": "18296756",
            "home_team": "H", "away_team": "A", "status": "live"}
    d = _analyze_game(game, [], NOW)
    # provider comes from the FROZEN family map, never inferred
    assert d["provider"] == PROVIDERS["BETUAL_NBA"] == "BETUAL"
    # competition identity is the authoritative slug + numeric id verbatim
    assert d["competition_slug"] == "betual-nba"
    assert d["competition_id"] == "18296756"
    # the display name stays available as provenance only
    assert d["competition"] == "Betual NBA"


def test_detail_identity_absent_when_metadata_missing():
    game = {"source_game_id": "X", "id": 2, "source": "s",
            "classification": "BETUAL_NBA", "home_team": "H",
            "away_team": "A", "status": "live"}
    d = _analyze_game(game, [], NOW)
    assert d["provider"] == "BETUAL"
    assert d["competition_slug"] is None      # honest absence — no default
    assert d["competition_id"] is None


def test_frontend_game_state_shows_canonical_identity():
    js = DASH_JS.read_text()
    # GAME STATE panel renders canonical provider + competition slug
    assert re.search(r">Provider<.*?g\.provider", js, re.S)
    assert re.search(r">Competition<.*?g\.competition_slug", js, re.S)
    # no BLM terminology returns to the LIVE modal: scope the check to the
    # modal region (the archived HISTORICAL/RESEARCH surface legitimately
    # retains market_vs_fair under the research firewall)
    i = js.find('modalPanel("Game State"')
    assert i > 0
    modal_region = js[max(0, i - 6000): i + 9000]
    assert "BLM fair" not in modal_region
    assert "market_vs_fair" not in modal_region


# ── §10 prospective health report ────────────────────────────────────────

@pytest.fixture
def health_dbs(tmp_path):
    """Fixture DBs shaped like production (main: games; clean:
    clean_projections) with known integrity conditions planted."""
    main = tmp_path / "main.db"
    clean = tmp_path / "clean.db"
    mc = sqlite3.connect(main)
    mc.executescript("""
        CREATE TABLE games (source_game_id TEXT PRIMARY KEY,
            classification TEXT, competition TEXT, competition_slug TEXT,
            competition_id TEXT);
    """)
    mc.executemany(
        "INSERT INTO games VALUES (?,?,?,?,?)",
        [("GN1", "BETUAL_NBA", "Betual NBA", "betual-nba", "18296756"),
         ("GT1", "BETUAL_NBA", "Betual NBA", "betual-tbsl", "18296900")])
    mc.commit()
    mc.close()

    cc = sqlite3.connect(clean)
    cc.executescript("""
        CREATE TABLE clean_projections (
            id INTEGER PRIMARY KEY, source_game_id TEXT,
            classification TEXT, captured_at TEXT, period_label TEXT,
            progress_pct REAL, current_total_points REAL,
            live_total_line REAL, market_captured_at TEXT,
            market_status TEXT, actual_pts_per_min REAL,
            required_pts_per_min REAL, pace_gap REAL,
            elapsed_game_minutes REAL, terminal INT, status TEXT);
    """)
    rows = [
        # NBA game: 4 clean rows (t4 carries an elapsed-clock drop: the
        # known feed-jitter artifact), then a 5th row sharing t4's
        # captured_at with a DIFFERENT line at the SAME market timestamp
        # (alternative-lines group + duplicate-observation group).
        ("GN1", "BETUAL_NBA", "2026-09-09T14:00:00.000Z", "2nd Quarter",
         25.0, 40, 180.5, "2026-09-09T13:00:00.000Z", "STALE", 4.0, 3.5,
         -0.5, 10.0, 0, "VALID"),
        ("GN1", "BETUAL_NBA", "2026-09-09T14:05:00.000Z", "2nd Quarter",
         37.5, 52, 180.5, "2026-09-09T14:05:00.000Z", "LIVE", 4.0, 3.5,
         -0.5, 15.0, 0, "VALID"),
        ("GN1", "BETUAL_NBA", "2026-09-09T14:10:00.000Z", "2nd Quarter",
         50.0, 60, 180.5, "2026-09-09T14:10:00.000Z", "LIVE", 3.0, 3.5,
         0.5, 20.0, 0, "VALID"),
        ("GN1", "BETUAL_NBA", "2026-09-09T14:15:00.000Z", "2nd Quarter",
         47.5, 61, 180.5, "2026-09-09T14:15:00.000Z", "LIVE", 3.0, 3.5,
         0.5, 19.5, 0, "VALID"),  # elapsed drop 20.0 -> 19.5
        ("GN1", "BETUAL_NBA", "2026-09-09T14:15:00.000Z", "2nd Quarter",
         47.5, 61, 178.5, "2026-09-09T14:15:00.000Z", "LIVE", 3.0, 3.6,
         0.6, 19.5, 0, "VALID"),  # duplicate captured_at + alt line
        # TSBL game: honest missing-line row
        ("GT1", "BETUAL_NBA", "2026-09-09T14:10:00.000Z", "2nd Quarter",
         40.0, 90, None, None, "MISSING", 4.5, None, None, 20.0, 0,
         "VALID"),
    ]
    cc.executemany(
        "INSERT INTO clean_projections (source_game_id, classification,"
        " captured_at, period_label, progress_pct, current_total_points,"
        " live_total_line, market_captured_at, market_status,"
        " actual_pts_per_min, required_pts_per_min, pace_gap,"
        " elapsed_game_minutes, terminal, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    cc.commit()
    cc.close()
    return main, clean


def test_health_report_counts_and_integrity(health_dbs):
    main, clean = health_dbs
    rep = build_report(str(main), str(clean), NOW, window_hours=24)
    assert rep["games_observed"] == 2
    assert rep["observations_collected"] == 6
    assert rep["provider_counts"] == {"BETUAL": 2}
    assert rep["competition_counts"] == {"betual-nba": 1, "betual-tbsl": 1}
    assert rep["unknown_count"] == 0
    # §5 isolation: the two competitions produce separate population keys
    # and the report's structural isolation audit is clean
    keys = {k["key"] for k in rep["benchmark_populations"]["largest_keys"]}
    assert any(k.startswith("BETUAL|betual-nba|") for k in keys)
    assert any(k.startswith("BETUAL|betual-tbsl|") for k in keys)
    assert rep["integrity"]["benchmark_isolation_violations"] == []
    # §7 integrity controls see exactly the planted conditions
    assert rep["integrity"]["duplicate_observation_groups"] == 1
    assert rep["integrity"]["alternative_line_groups"] == 1
    assert rep["integrity"]["elapsed_monotonicity_violations"] == 1
    assert rep["integrity"]["score_monotonicity_violations"] == 0
    assert rep["integrity"]["market_ts_after_capture_rows"] == 0
    assert rep["integrity"]["future_timestamp_rows"] == 0
    # honesty rates: 1 missing line of 6 rows; 1 stale of 5 line rows
    assert rep["missing_line_rate"] == pytest.approx(1 / 6, abs=1e-3)
    assert rep["stale_line_rate"] == pytest.approx(0.2, abs=1e-3)
    # every benchmark key is the frozen 4-part partition
    assert all(len(k.split("|")) == 4 for k in keys)
    # z availability: all 6 window rows resolve a benchmark key
    assert rep["z_availability"]["rows_with_resolvable_benchmark_key"] == 6
    assert rep["z_availability"]["availability_rate"] == 1.0


def test_health_report_is_structurally_descriptive_only(health_dbs):
    main, clean = health_dbs
    rep = build_report(str(main), str(clean), NOW, window_hours=24)
    # the guard passes on the real report...
    assert_descriptive_only(rep)
    # ...and the guard itself actually bites
    for bad in ({"edge": 1}, {"win_rate": {}}, {"a": [{"probability": 0.5}]},
                {"staking": None}, {"ev": 0.0}):
        with pytest.raises(ValueError):
            assert_descriptive_only(bad)


def test_report_cli_writes_json(tmp_path, health_dbs, capsys):
    main, clean = health_dbs
    out = tmp_path / "health.json"
    import sys
    from blm_v4.live_analytics import prospective_health as ph
    argv = sys.argv
    try:
        sys.argv = ["prospective_health", "--main-db", str(main),
                    "--clean-db", str(clean), "--window-hours", "24",
                    "--api-url", "", "--json-out", str(out)]
        assert ph.main() == 0
    finally:
        sys.argv = argv
    data = json.loads(out.read_text())
    assert data["games_observed"] == 2


# ── accumulation-phase sections (data maturation, descriptive only) ─────

def test_maturity_counts_are_descriptive(health_dbs):
    main, clean = health_dbs
    rep = build_report(str(main), str(clean), NOW, window_hours=24)
    bm = rep["benchmark_maturity"]
    # 5 window population keys exist (GN1: P025/P035/P045/P050, GT1: P040),
    # every one currently sparse (n=1) — reported honestly, never altered
    assert bm["populations_total"] == 5
    assert bm["n_at_least"] == {"30": 0, "100": 0, "500": 0, "1000": 0}
    assert "not statistical" in bm["note"]


def test_competition_maturation_reports_each_competition(health_dbs):
    main, clean = health_dbs
    rep = build_report(str(main), str(clean), NOW, window_hours=24)
    rows = {r["competition"]: r for r in rep["competition_maturation"]}
    assert set(rows) == {"betual-nba", "betual-tbsl"}
    nba = rows["betual-nba"]
    assert nba["games"] == 1 and nba["observations"] == 5
    # P045 holds both 14:15 rows (the planted duplicate pair) → n=2;
    # all other GN1 keys n=1
    assert nba["benchmark_keys"] == 4 and nba["max_benchmark_n"] == 2
    assert nba["min_benchmark_n"] == 1
    assert nba["missing_line_rate"] == 0.0
    tsbl = rows["betual-tbsl"]
    assert tsbl["observations"] == 1
    assert tsbl["missing_line_rate"] == 1.0   # honest sparse/missing report
    assert tsbl["stale_line_rate"] is None    # no line rows → no rate claim


def test_clock_jitter_recorded_never_corrected(health_dbs):
    main, clean = health_dbs
    rep = build_report(str(main), str(clean), NOW, window_hours=24)
    cj = rep["clock_jitter"]
    # exactly the planted micro-drop (20.0 → 19.5 min, same quarter,
    # score stayed monotonic); the report DESCRIBES it, never fixes it
    assert cj["micro_drops"] == 1
    assert cj["min_drop_minutes"] == cj["max_drop_minutes"] == 0.5
    assert cj["same_quarter"] == 1 and cj["period_transition"] == 0
    assert cj["score_remained_monotonic"] == 1
    assert cj["affected_games"] == 1
    assert cj["affected_competitions"] == ["betual-nba"]
    assert "never altered" in cj["note"]


def test_accumulation_growth_vs_previous_snapshot(health_dbs, tmp_path):
    main, clean = health_dbs
    # no history → growth is honestly None
    rep0 = build_report(str(main), str(clean), NOW, window_hours=24)
    assert rep0["accumulation"]["growth_vs_previous_snapshot"] is None
    assert rep0["accumulation"]["lifetime_observations"] > 0
    # a previous snapshot (append-only history) → growth is computed
    hist = tmp_path / "history.jsonl"
    life = rep0["accumulation"]["lifetime_observations"]
    prev = {"generated_utc": "2026-09-08T15:00:00.000Z",
            "accumulation": {"lifetime_observations": life - 120,
                             "lifetime_games":
                                 rep0["accumulation"]["lifetime_games"] - 2}}
    hist.write_text(json.dumps(prev) + "\n")
    rep1 = build_report(str(main), str(clean), NOW, window_hours=24,
                        history_file=str(hist))
    g = rep1["accumulation"]["growth_vs_previous_snapshot"]
    assert g["observations_added"] == 120
    assert g["games_added"] == 2
    # the LAST history line wins (append-only, newest = most recent)
    hist.open("a").write(json.dumps({"generated_utc": "2026-09-09T09:00:00Z",
                                     "accumulation": {
                                         "lifetime_observations": life - 3,
                                         "lifetime_games": 0}}) + "\n")
    rep2 = build_report(str(main), str(clean), NOW, window_hours=24,
                        history_file=str(hist))
    assert rep2["accumulation"]["growth_vs_previous_snapshot"][
        "observations_added"] == 3


def test_history_is_append_only(tmp_path, health_dbs):
    main, clean = health_dbs
    out = tmp_path / "history.jsonl"
    argv = sys.argv
    from blm_v4.live_analytics import prospective_health as ph
    try:
        for _ in range(2):
            sys.argv = ["prospective_health", "--main-db", str(main),
                        "--clean-db", str(clean), "--window-hours", "24",
                        "--api-url", "", "--jsonl-append", str(out)]
            assert ph.main() == 0
    finally:
        sys.argv = argv
    lines = out.read_text().splitlines()
    assert len(lines) == 2                     # appended, not rewritten
    assert lines[0] < lines[1] or lines[0] != lines[1]
    first = json.loads(lines[0])
    assert first["generated_utc"]             # prior snapshot intact
    assert "generated_utc" in json.loads(lines[1])
