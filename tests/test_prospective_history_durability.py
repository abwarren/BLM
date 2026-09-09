"""DURABILITY of the prospective health history (accumulation phase).

The daily snapshot history must live at the DURABLE repository-local
path (survives reboot), stay APPEND-ONLY (prior snapshots byte-identical,
never rewritten), tolerate failures without touching the history, and
the scheduler's cron installation must be idempotent.

No research semantics are involved anywhere in this file.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blm_v4.live_analytics import prospective_health as ph

REPO = Path("/home/ubuntu/BLM")
SCRIPT = REPO / "scripts" / "prospective_health_daily.sh"
DURABLE_DEFAULT = "/home/ubuntu/BLM/prospective_health_history.jsonl"


# ── default path is durable ──────────────────────────────────────────────

def test_default_history_path_is_durable():
    """The operational default is the repository-local durable path."""
    assert DURABLE_DEFAULT.startswith(str(REPO))
    assert "/tmp/" not in DURABLE_DEFAULT


def test_scheduler_default_is_durable_path():
    """The script's default HISTORY must be the durable path (and keep
    honouring an explicit PROSPECTIVE_HISTORY override)."""
    text = SCRIPT.read_text()
    assert ('HISTORY="${PROSPECTIVE_HISTORY:-%s}"' % DURABLE_DEFAULT) \
        in text
    assert "${PROSPECTIVE_HISTORY:-" in text      # override mechanism kept


def test_explicit_override_still_works(tmp_path, monkeypatch):
    """PROSPECTIVE_HISTORY=... redirects the snapshot (mechanism kept)."""
    custom = tmp_path / "custom" / "history.jsonl"
    monkeypatch.setenv("PROSPECTIVE_HISTORY", str(custom))
    env_default = subprocess.run(
        ["bash", "-c",
         'source %s >/dev/null 2>&1; echo "$HISTORY"' % SCRIPT],
        capture_output=True, text=True, check=True).stdout.strip()
    assert env_default == str(custom)


# ── append-only behaviour at the report layer ────────────────────────────

@pytest.fixture
def health_dbs(tmp_path):
    main, clean = tmp_path / "main.db", tmp_path / "clean.db"
    mc = sqlite3.connect(main)
    mc.executescript(
        "CREATE TABLE games (source_game_id TEXT PRIMARY KEY,"
        " classification TEXT, competition TEXT, competition_slug TEXT,"
        " competition_id TEXT);")
    mc.execute("INSERT INTO games VALUES"
               " ('G1','BETUAL_NBA','Betual NBA','betual-nba','1')")
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
    cc.execute(
        "INSERT INTO clean_projections (source_game_id, classification,"
        " captured_at, period_label, progress_pct, current_total_points,"
        " live_total_line, market_captured_at, market_status,"
        " actual_pts_per_min, required_pts_per_min, pace_gap,"
        " elapsed_game_minutes, terminal, status) VALUES"
        " ('G1','BETUAL_NBA','2026-09-09T14:00:00.000Z','2nd Quarter',"
        "  25.0, 40, 180.5, '2026-09-09T14:00:00.000Z','LIVE',"
        "  4.0, 3.5, -0.5, 10.0, 0, 'VALID')")
    cc.commit()
    cc.close()
    return main, clean


def _append_snapshot(db_paths, history: Path, now: datetime) -> dict:
    rep = ph.build_report(str(db_paths[0]), str(db_paths[1]), now,
                          window_hours=24, history_file=str(history))
    with open(history, "a") as fh:
        fh.write(json.dumps(rep, default=str) + "\n")
    return rep


def test_two_runs_append_two_records_prior_bytes_identical(health_dbs):
    """Requirements 1/2/4/5: two successful runs append two records;
    the first record's bytes never change."""
    hist = Path("/tmp/ph_durability_test.jsonl")
    if hist.exists():
        hist.unlink()
    r1 = _append_snapshot(health_dbs, hist,
                          datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc))
    first_bytes = hist.read_bytes()
    r2 = _append_snapshot(health_dbs, hist,
                          datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc))
    after = hist.read_bytes()
    # two records, appended
    lines = after.decode().splitlines()
    assert len(lines) == 2
    # prior record byte-identical (append-only, never rewritten)
    assert after == first_bytes + json.dumps(r2, default=str).encode() \
        + b"\n"
    assert json.loads(lines[0])["generated_utc"] == r1["generated_utc"]
    hist.unlink()


def test_failed_report_leaves_history_untouched(health_dbs):
    """Requirement 3: a failed report must not modify the history — the
    CLI writes the snapshot only after the report (and its descriptive-
    only guard) succeed."""
    hist = Path("/tmp/ph_durability_fail.jsonl")
    hist.write_text('{"existing": true}\n')
    before = hist.read_bytes()
    orig = ph.build_report

    def boom(*a, **k):
        raise RuntimeError("report failed")

    ph.build_report = boom
    argv = sys.argv
    try:
        sys.argv = ["prospective_health", "--main-db", str(health_dbs[0]),
                    "--clean-db", str(health_dbs[1]),
                    "--jsonl-append", str(hist)]
        with pytest.raises(RuntimeError):
            ph.main()
    finally:
        sys.argv = argv
        ph.build_report = orig
    assert hist.read_bytes() == before
    hist.unlink()


# ── scheduler layer (real bash, real append semantics) ───────────────────

def _run_script(monkeypatch, history: Path, expect_fail: bool = False):
    monkeypatch.setenv("PROSPECTIVE_HISTORY", str(history))
    monkeypatch.setenv("PROSPECTIVE_LOG",
                       str(history.parent / "sched.log"))
    monkeypatch.setenv("PROSPECTIVE_WINDOW_HOURS", "24")
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                       timeout=240)
    return r


def test_scheduler_creates_history_if_absent_and_appends(tmp_path,
                                                         monkeypatch):
    """Requirements 6/7: the scheduler uses the durable path and creates
    the history if absent; two successful runs append two records."""
    hist = tmp_path / "dur_sched.jsonl"
    assert not hist.exists()
    r1 = _run_script(monkeypatch, hist)
    assert r1.returncode == 0
    assert hist.exists() and len(hist.read_text().splitlines()) == 1
    snap = json.loads(hist.read_text().splitlines()[0])
    for key in ("generated_utc", "window_hours", "games_observed",
                "observations_collected", "provider_counts",
                "competition_counts", "unknown_count",
                "benchmark_populations", "benchmark_maturity",
                "z_availability", "missing_line_rate", "stale_line_rate",
                "score_line_gap_distribution", "actual_pace_distribution",
                "required_pace_distribution", "pace_gap_distribution",
                "integrity", "competition_maturation", "clock_jitter"):
        assert key in snap, key
    first_bytes = hist.read_bytes()
    r2 = _run_script(monkeypatch, hist)
    assert r2.returncode == 0
    lines = hist.read_text().splitlines()
    assert len(lines) == 2
    assert hist.read_bytes().startswith(first_bytes)  # append-only
    hist.unlink()


def test_scheduler_failed_report_leaves_history_untouched(tmp_path,
                                                          monkeypatch):
    """Requirement 3 at the scheduler layer: a broken report run leaves
    the existing history bytes exactly as they were."""
    hist = tmp_path / "dur_sched_fail.jsonl"
    hist.write_text('{"existing": true}\n')
    before = hist.read_bytes()
    monkeypatch.setenv("PROSPECTIVE_HISTORY", str(hist))
    monkeypatch.setenv("PROSPECTIVE_LOG", str(tmp_path / "sched.log"))
    # break the REPORT RUN itself: a non-numeric window makes argparse
    # exit(2) before any report/write happens ("-m" puts the repo cwd on
    # sys.path, so PYTHONPATH sabotage would NOT fail the import)
    r = subprocess.run(
        ["bash", "-c",
         'PROSPECTIVE_HISTORY=%s PROSPECTIVE_LOG=%s '
         'PROSPECTIVE_WINDOW_HOURS=not-a-number %s'
         % (json.dumps(str(hist)), json.dumps(str(tmp_path / "sched.log")),
            SCRIPT)],
        capture_output=True, text=True, timeout=240)
    assert r.returncode == 0          # the wrapper logs failures and
    # continues by design; the CONTRACT is history-untouched + FAILED log
    log = (tmp_path / "sched.log").read_text()
    assert "SNAPSHOT FAILED" in log
    assert hist.read_bytes() == before
    hist.unlink()


def test_scheduler_install_cron_is_idempotent(tmp_path, monkeypatch):
    """Requirement 9: repeated installation yields exactly one entry.
    The upsert is exercised as a pure function on synthetic crontabs."""
    text = SCRIPT.read_text()
    # extract the pure upsert function and test it directly
    for marker_line, existing in [
        ("5 15 * * * /home/ubuntu/BLM/scripts/prospective_health_daily.sh",
         "# other job\n0 0 * * * /some/other.sh\n"),
        ("5 15 * * * /home/ubuntu/BLM/scripts/prospective_health_daily.sh",
         "5 15 * * * /home/ubuntu/BLM/scripts/"
         "prospective_health_daily.sh\n"),
        ("5 15 * * * /home/ubuntu/BLM/scripts/"
         "prospective_health_daily.sh",
         "5 15 * * * /home/ubuntu/BLM/scripts/"
         "prospective_health_daily.sh\n"
         "5 15 * * * /home/ubuntu/BLM/scripts/"
         "prospective_health_daily.sh\n"),
    ]:
        out = subprocess.run(
            ["bash", "-c",
             'source %s >/dev/null 2>&1; printf %%s %s | upsert_cron_line'
             % (SCRIPT, json.dumps(existing))],
            capture_output=True, text=True, check=True).stdout
        lines = [l for l in out.splitlines() if l.strip()]
        matches = [l for l in lines if "prospective_health_daily.sh" in l]
        assert len(matches) == 1, (existing, lines)
        assert matches[0] == marker_line


def test_assert_descriptive_only_guard_remains_active():
    """The freeze guard is still present and still bites."""
    for bad in ({"edge": 1}, {"win_rate": 0.5}, {"probability": [1]},
                {"ev": 2}, {"threshold": 3}):
        with pytest.raises(ValueError):
            ph.assert_descriptive_only(bad)
    ph.assert_descriptive_only({"z": 1, "n": 30, "mean_pace": 4.5})
