"""MATURATION AUDIT — read-only regression tests.

Proves the audit is observational only (databases and snapshot history
are byte-identical after a run), keeps competitions isolated, keeps
UNKNOWN independently visible, never discards integrity violations,
never rewrites history, and stays under the descriptive-only firewall.

No research semantics are touched anywhere in this file.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blm_v4.live_analytics import maturation_audit as ma
from blm_v4.live_analytics import prospective_health as ph

NOW = datetime(2026, 9, 9, 16, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    """Two isolated competitions + one planted integrity violation +
    a two-snapshot append-only history."""
    main, clean = tmp_path / "main.db", tmp_path / "clean.db"
    mc = sqlite3.connect(main)
    mc.executescript(
        "CREATE TABLE games (source_game_id TEXT PRIMARY KEY,"
        " classification TEXT, competition TEXT, competition_slug TEXT,"
        " competition_id TEXT);")
    mc.executemany("INSERT INTO games VALUES (?,?,?,?,?)",
                   [("GN1", "BETUAL_NBA", "Betual NBA", "betual-nba", "1"),
                    ("GT1", "BETUAL_NBA", "Betual NBA", "betual-tbsl", "2")])
    mc.commit()
    mc.close()
    cc = sqlite3.connect(clean)
    cc.executescript(
        "CREATE TABLE clean_projections ("
        " id INTEGER PRIMARY KEY, source_game_id TEXT, classification TEXT,"
        " captured_at TEXT, period_label TEXT, progress_pct REAL,"
        " current_total_points REAL, live_total_line REAL,"
        " market_captured_at TEXT, market_status TEXT,"
        " actual_pts_per_min REAL, required_pts_per_min REAL, pace_gap REAL,"
        " elapsed_game_minutes REAL, terminal INT, status TEXT);")
    rows = [
        # NBA: clean rows + a planted score-monotonicity violation
        ("GN1", "BETUAL_NBA", "2026-09-09T14:00:00.000Z", "2nd Quarter",
         25.0, 40, 180.5, "2026-09-09T14:00:00.000Z", "LIVE",
         4.0, 3.5, -0.5, 10.0, 0, "VALID"),
        ("GN1", "BETUAL_NBA", "2026-09-09T14:05:00.000Z", "2nd Quarter",
         37.5, 55, 180.5, "2026-09-09T14:05:00.000Z", "LIVE",
         4.0, 3.5, -0.5, 15.0, 0, "VALID"),
        ("GN1", "BETUAL_NBA", "2026-09-09T14:10:00.000Z", "2nd Quarter",
         50.0, 54, 180.5, "2026-09-09T14:10:00.000Z", "LIVE",
         4.0, 3.5, -0.5, 20.0, 0, "VALID"),   # score drop 55→54 (planted)
        # TSBL: separate competition, honest missing line
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
    hist = tmp_path / "history.jsonl"
    snaps = []
    for ts, life in (("2026-09-09T14:00:00.000Z", 100),
                     ("2026-09-09T15:00:00.000Z", 220)):
        snaps.append({
            "generated_utc": ts, "window_hours": 24.0,
            "games_observed": 2, "observations_collected": 4,
            "provider_counts": {"BETUAL": 2}, "competition_counts": {},
            "unknown_count": 0, "benchmark_populations": {},
            "benchmark_maturity": {"populations_total": 2,
                                   "n_at_least": {"30": 2, "100": 2,
                                                  "500": 0, "1000": 0}},
            "z_availability": {"availability_rate": 0.99},
            "missing_line_rate": 0.25, "stale_line_rate": 0.0,
            "score_line_gap_distribution": {"n": 3},
            "actual_pace_distribution": {"n": 4},
            "required_pace_distribution": {"n": 3},
            "pace_gap_distribution": {"n": 3},
            "integrity": {"duplicate_observation_groups": 0,
                          "future_timestamp_rows": 0,
                          "market_ts_after_capture_rows": 0,
                          "score_monotonicity_violations": 0,
                          "elapsed_monotonicity_violations": 0,
                          "benchmark_isolation_violations": [],
                          "alternative_line_groups": 0},
            "competition_maturation": [], "clock_jitter": {"micro_drops": 0},
            "accumulation": {"lifetime_observations": life,
                             "lifetime_games": 2},
        })
    hist.write_text("".join(json.dumps(s) + "\n" for s in snaps))
    return main, clean, hist


def test_audit_is_read_only(world):
    """Databases AND the snapshot history are byte-identical after a run."""
    main, clean, hist = world
    before = (main.read_bytes(), clean.read_bytes(), hist.read_bytes())
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    after = (main.read_bytes(), clean.read_bytes(), hist.read_bytes())
    assert before == after
    assert audit["verdict"] in ("ACCUMULATE",
                                "MATURE ENOUGH FOR NEXT DESCRIPTIVE "
                                "RESEARCH STAGE")


def test_competition_populations_remain_isolated(world):
    """§2: competitions are reported separately and isolation violations
    remain empty; NBA and TSBL never merge into one row."""
    main, clean, hist = world
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    comps = [c["competition"] for c in audit["competition_maturity"]]
    assert comps == sorted(set(comps))          # one row per competition
    assert "betual-nba" in comps and "betual-tbsl" in comps
    assert audit["classification_integrity"][
        "benchmark_isolation_violations"] == []


def test_unknown_remains_independently_reported(world):
    """§4: UNKNOWN is its own field, never folded into a competition."""
    main, clean, hist = world
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    ci = audit["classification_integrity"]
    assert "unknown" in ci and ci["unknown"] == 0
    for key in ("missing_provider", "missing_canonical_competition",
                "ambiguous_competition"):
        assert key in ci


def test_integrity_violations_are_not_discarded(world):
    """§5: the planted score-monotonicity violation (55→54) must appear
    in the audit — and it must FAIL the data-integrity gate."""
    main, clean, hist = world
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    assert audit["data_integrity"]["score_monotonicity_violations"] == 1
    gates = {g["gate"]: g["pass"] for g in audit["maturation_gates"]}
    assert gates["data_integrity"] is False
    assert audit["verdict"] == "ACCUMULATE"


def test_audit_never_rewrites_snapshot_history(world):
    """§8: an audit run appends nothing and rewrites nothing."""
    main, clean, hist = world
    before = hist.read_bytes()
    ma.build_audit(str(main), str(clean), NOW, history_path=str(hist),
                   api_url=None)
    assert hist.read_bytes() == before
    assert len(hist.read_text().splitlines()) == 2


def test_growth_and_continuity_derived_from_history(world):
    """§1/§8: growth intervals come from the append-only history; the
    legacy-schema tolerance never hides genuine ordering failures."""
    main, clean, hist = world
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    g = audit["accumulation"]["per_interval"]
    assert g[-1]["observations_added"] == 120 and g[-1]["games_added"] == 0
    assert audit["snapshot_continuity"]["monotone_increasing"] is True
    assert audit["snapshot_continuity"]["duplicate_timestamps"] == 0
    # a duplicated timestamp is a REAL continuity failure (gate fails)
    lines = hist.read_text().splitlines()
    hist.write_text(lines[0] + "\n" + lines[0])
    audit2 = ma.build_audit(str(main), str(clean), NOW,
                            history_path=str(hist), api_url=None)
    sc = audit2["snapshot_continuity"]
    assert sc["duplicate_timestamps"] == 1
    assert sc["monotone_increasing"] is False
    gates = {x["gate"]: x["pass"] for x in audit2["maturation_gates"]}
    assert gates["snapshot_continuity"] is False


def test_maturation_trajectory_tracks_time_to_maturity(world):
    """Longitudinal tracking: a watch-list population's N is traced
    across snapshots; WAIT vs MATURE (with crossing timestamp) is
    derived from the history — never from inference or backfill."""
    main, clean, hist = world
    lines = hist.read_text().splitlines()
    s1, s2 = json.loads(lines[0]), json.loads(lines[1])
    s1["maturation_watch"] = {"watch_threshold_n": 100, "populations": [
        {"key": "BETUAL|betual-nba|Q4|P100", "n": 7},
        {"key": "TEST|crossed|Q1|P010", "n": 45}]}
    s2["maturation_watch"] = {"watch_threshold_n": 100, "populations": [
        {"key": "BETUAL|betual-nba|Q4|P100", "n": 9}]}
    hist.write_text(json.dumps(s1) + "\n" + json.dumps(s2) + "\n")
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    traj = {p["key"]: p for p in
            audit["maturation_trajectory"]["populations"]}
    # sparse population: observed 7 → 9 across snapshots, still WAIT for
    # N30 (its fresh N is never invented)
    sparse = traj["BETUAL|betual-nba|Q4|P100"]
    assert [o["n"] for o in sparse["observations"][:2]] == [7, 9]
    assert sparse["thresholds"]["30"]["state"] == "WAIT"
    assert sparse["thresholds"]["30"]["crossed_at"] is None
    # a population already at N>=30 in its first sighting is MATURE with
    # the crossing timestamp recorded
    crossed = traj["TEST|crossed|Q1|P010"]
    assert crossed["thresholds"]["30"]["state"] == "MATURE"
    assert crossed["thresholds"]["30"]["crossed_at"] == \
        s1["generated_utc"]
    assert crossed["thresholds"]["100"]["state"] == "WAIT"


def test_descriptive_only_firewall_active_in_audit(world, tmp_path):
    """The audit payload cannot carry banned concepts — and the guard
    still bites when one is planted."""
    main, clean, hist = world
    audit = ma.build_audit(str(main), str(clean), NOW,
                           history_path=str(hist), api_url=None)
    ph.assert_descriptive_only(audit)          # real audit passes
    audit["edge"] = 0.01
    with pytest.raises(ValueError):
        ph.assert_descriptive_only(audit)


def test_cli_writes_both_outputs(world, tmp_path):
    """§10: the CLI writes machine-readable JSON + the sectioned TXT."""
    main, clean, hist = world
    j = tmp_path / "a.json"
    t = tmp_path / "a.txt"
    rc = ma.main(["--main-db", str(main), "--clean-db", str(clean),
                  "--history", str(hist), "--api-url", "",
                  "--json-out", str(j), "--txt-out", str(t)])
    assert rc == 0
    data = json.loads(j.read_text())
    for section in ("CURRENT STATUS", "ACCUMULATION", "COMPETITION MATURITY",
                    "BENCHMARK MATURITY", "CLASSIFICATION INTEGRITY",
                    "DATA INTEGRITY", "MARKET COMPLETENESS",
                    "SOURCE/CLOCK QUALITY", "SNAPSHOT CONTINUITY",
                    "MATURATION GATES", "LIMITATIONS", "VERDICT"):
        assert section in t.read_text(), section
    assert data["verdict"] in ("ACCUMULATE",
                               "MATURE ENOUGH FOR NEXT DESCRIPTIVE "
                               "RESEARCH STAGE")
