"""SCORE_LINE_GAP — live score-vs-bookmaker-line measurement (directive
2026-09-09: "SIMPLIFY LIVE LINE TRACKING").

The live comparison is ONLY:
    score_line_gap = current_combined_score − observed_live_line
at each checkpoint T, from two OBSERVED values.  It must never use BLM
fair / trajectory / pace projection / market-trajectory residual /
z-score / calibration / probability / edge / staking.  The retrospective
settlement question (did the FINAL score beat the checkpoint line?)
remains in checkpoint_market / prediction_scores and is untouched.

Covers:
  1. API `_analyze_game` emits market.score_line_gap = combined − line
     exactly (positive, negative, zero) — the arithmetic proof that no
     model value enters the gap.
  2. Missing line or missing score → gap None (never fabricated).
  3. The gap is computed from the freshest observed line (WS preferred
     over snapshot) — same line the card displays.
  4. Served dashboard.js renders SCORE / LINE / SCORE − LINE, keeps the
     LIVE/STALE 300s semantics, and labels the Z series as research
     (never the gap).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blm_v4.api import _analyze_game

HERE = Path(__file__).resolve().parent
DASH_JS = HERE.parent / "blm_v4" / "dashboard" / "static" / "dashboard.js"


def _db(tmp_path, line, home=44, away=53):
    """Minimal games/snapshots/market_observations fixture."""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT 'PokerBet',
            source_game_id TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT 'BETUAL_NBA',
            home_team TEXT, away_team TEXT, status TEXT DEFAULT 'live',
            last_seen_at TEXT, source_url TEXT,
            competition TEXT, region TEXT, sport TEXT);
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL REFERENCES games(id),
            source TEXT NOT NULL DEFAULT 'PokerBet',
            source_game_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            period_label TEXT, clock TEXT, quarter INTEGER,
            home_score INTEGER, away_score INTEGER,
            total_line REAL, total_over_odds REAL, total_under_odds REAL,
            spread REAL, spread_indicator TEXT,
            home_total_line REAL, away_total_line REAL,
            w1_odds REAL, w2_odds REAL,
            source_url TEXT, markets_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER REFERENCES games(id),
            source_game_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            market_type TEXT NOT NULL,
            market_name TEXT NOT NULL,
            line_value REAL, over_price REAL, under_price REAL,
            home_score INTEGER, away_score INTEGER,
            period_label TEXT, clock TEXT,
            raw_json TEXT NOT NULL DEFAULT '{}');
    """)
    conn.execute("""INSERT INTO games (source_game_id, home_team, away_team, status)
        VALUES ('30734614', 'Dorados', 'Mineros', 'live')""")
    gid = conn.execute("SELECT id FROM games").fetchone()[0]
    # snapshot WITHOUT a line (score only) + a WS market observation —
    # the freshest observed line must come from the WS row (API rule)
    conn.execute("""INSERT INTO snapshots (game_id, source_game_id, classification,
        captured_at, home_score, away_score, period_label, clock, quarter)
        VALUES (?, '30734614', 'BETUAL_NBA', '2026-09-05T06:00:30Z', ?, ?,
                '3rd Quarter', '06:48', 3)""", (gid, home, away))
    if line is not None:
        conn.execute("""INSERT INTO market_observations
            (game_id, source_game_id, captured_at, market_type, market_name,
             line_value, over_price, under_price, home_score, away_score)
            VALUES (?, '30734614', '2026-09-05T06:00:10Z', 'MatchTotal',
                    'Total Points', ?, 1.94, 1.87, ?, ?)""", (gid, line, home, away))
    conn.commit()
    game = dict(conn.execute("SELECT * FROM games").fetchone())
    rows = [dict(r) for r in conn.execute("SELECT * FROM snapshots").fetchall()]
    return conn, game, rows


@pytest.mark.parametrize("line,expected", [
    (204.5, -107.5),   # score below the live line
    (80.0, 17.0),      # score above the live line
    (97.0, 0.0),       # exactly on the line
])
def test_score_line_gap_is_pure_score_minus_line(tmp_path, line, expected):
    """score_line_gap == (home+away) − observed line, EXACTLY — the same
    numbers a human would subtract; no model value can hide in it."""
    conn, game, rows = _db(tmp_path, line)
    d = _analyze_game(game, rows, datetime.now(timezone.utc), conn)
    assert d["market"]["total_line"] == line
    assert d["market"]["score_line_gap"] == pytest.approx(expected)
    # and it is the plain difference of the two emitted observed values
    assert d["market"]["score_line_gap"] == pytest.approx(
        d["home_score"] + d["away_score"] - d["market"]["total_line"])
    conn.close()


def test_score_line_gap_none_when_line_missing(tmp_path):
    """No observed line → gap None (never fabricated, never zero)."""
    conn, game, rows = _db(tmp_path, None)
    d = _analyze_game(game, rows, datetime.now(timezone.utc), conn)
    assert d["market"]["total_line"] is None
    assert d["market"]["score_line_gap"] is None
    conn.close()


def test_score_line_gap_uses_freshest_observed_line(tmp_path):
    """With BOTH a snapshot line and a fresher WS line, the gap uses the
    WS line (the one the live card displays as current)."""
    conn, game, rows = _db(tmp_path, 199.5)
    # snapshot carrying the OLDER line (06:00:20Z)
    conn.execute("""INSERT INTO snapshots (game_id, source_game_id, classification,
        captured_at, home_score, away_score, period_label, clock, quarter,
        total_line)
        VALUES (?, '30734614', 'BETUAL_NBA', '2026-09-05T06:00:20Z', 44, 53,
                '3rd Quarter', '06:48', 3, 199.5)""", (game["id"],))
    # fresher WS observation (06:01:10Z): the API's observed-freshest rule
    conn.execute("""UPDATE market_observations SET captured_at='2026-09-05T06:01:10Z'
        WHERE source_game_id='30734614'""")
    conn.commit()
    rows = [dict(r) for r in conn.execute("SELECT * FROM snapshots").fetchall()]
    d = _analyze_game(game, rows, datetime.now(timezone.utc), conn)
    assert d["market"]["market_source"] == "ws"
    assert d["market"]["total_line"] == 199.5
    assert d["market"]["score_line_gap"] == pytest.approx(97 - 199.5)
    conn.close()


def test_market_block_carries_no_model_fields(tmp_path):
    """The live-state object stays model-free: no fair/expected/prob/edge
    fields anywhere (the gap must not become a covert edge)."""
    conn, game, rows = _db(tmp_path, 204.5)
    d = _analyze_game(game, rows, datetime.now(timezone.utc), conn)
    assert "model" not in d
    assert "signals" not in d
    market_keys = " ".join(d["market"].keys())
    for banned in ("fair", "expected", "prob", "edge", "z", "signal", "stake"):
        assert banned not in market_keys.lower()
    conn.close()


def test_dashboard_renders_score_line_gap_readout():
    """Served dashboard.js: the modal shows SCORE − LINE arithmetic, the
    gap readout element, and the Z series is explicitly research."""
    js = DASH_JS.read_text()
    # live gap row in the Live Market panel
    assert "Score − line" in js
    assert "SCORE ${liveScore != null ? liveScore : \"–\"} − LINE" in js
    # header readout on the primary chart (cached across re-renders)
    assert "id=\"gapReadout\"" in js
    assert "SCORE − LINE" in js
    assert "state.modalGapText" in js
    # gap arithmetic in the chart code: current score minus last observed line
    assert "curScore - curLine" in js


def test_dashboard_z_panel_is_separate_visible_chart():
    """Z is its OWN visible panel chart (never a hidden dataset on the
    score/line chart), with its own readout element, drawn from the
    authoritative stored deviation z_score only."""
    js = DASH_JS.read_text()
    assert "PACE Z-SCORE</h4>" in js       # separate panel title in markup
    assert 'id="mcZ"' in js               # its own canvas
    assert 'id="zPanelReadout"' in js     # its own visible readout
    assert "state.modalCharts.z" in js    # its own Chart instance
    # zero reference on the Z panel's own y scale
    assert "chart.scales.y; // Z panel uses its own default y scale" in js
    # explicit null breaks: non-adjacent stored z observations are gaps,
    # never carried forward or interpolated
    assert "zData.push({ x: zPrevT, y: null })" in js
    assert "zData.push({ x, y: s.z })" in js
    assert "spanGaps: false" in js  # missing values render as gaps, not bridges
    # Z is NOT a dataset on the primary score/line chart anymore
    assert "Z-score (research)" not in js
    assert "y3" not in js


def test_dashboard_primary_chart_is_score_vs_line_only():
    """Primary modal chart datasets: actual combined score + observed live
    O/U line ONLY.  (Trajectory/residual stay available in the separate
    research/deviation view, but never enter the modal chart code.)"""
    js = DASH_JS.read_text()
    m = re.search(r"async function renderModalCharts\(g\) \{.*?\n\}", js, re.S)
    assert m, "renderModalCharts not found"
    modal_js = m.group(0)
    assert 'label: "Actual score (combined)"' in modal_js
    assert 'label: "Live O/U line"' in modal_js
    assert "projected_final_total" not in modal_js
    assert "market_trajectory_residual" not in modal_js
    assert "BLM trajectory" not in modal_js
    assert "residData" not in modal_js
    assert 'yAxisID' not in modal_js          # no hidden second-scale datasets
    # The ONLY datasets built by the modal chart code are: actual score,
    # observed live line, and the separate Z panel series.  (The Z panel
    # has two construction sites — initial build + in-place rebuild.)
    labels = set(re.findall(r'label: "([^"]+)"', modal_js))
    assert labels == {
        "Actual score (combined)",
        "Live O/U line",
        "PACE Z (stored benchmark)",
    }


def test_dashboard_freshness_semantics_unchanged():
    """Research firewall: the 300s LIVE/STALE word and the STALE presentation
    ('LAST OBSERVED … · STALE') are exactly the existing semantics."""
    js = DASH_JS.read_text()
    assert 'return age <= 300 ? "LIVE" : "STALE";' in js
    assert "LAST OBSERVED" in js
    assert "st-stale" in js
