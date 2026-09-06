"""CALIBRATION SLICE — invariant tests (directive section 14).

Proves:
  1. no final outcome enters calibration before settlement (a game's
     own outcome can never change its own walk-forward predictions,
     while later games' predictions DO move — proving the state is
     actually fed and the barrier is the only thing holding it back);
  2. no future game enters the calibration state;
  3. no terminal 100% rows enter primary calibration;
  4. OVER and UNDER calibration remain separate;
  5. chronological walk-forward ordering is enforced;
  6. game-level weighting is correct (a 9-checkpoint game never gets
     9x the influence of a 1-checkpoint game in the game-level metric).

Also covers: tautology-guard exclusion, LIVE/STALE separation,
baseline accounting, end-to-end report smoke, and calibrator numerics.

The calibration layer is READ-ONLY research: these tests pin the
information barriers, not any predictive claim.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4 import calibration as cal


# ── synthetic store builder ────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE games (id INTEGER PRIMARY KEY, source_game_id TEXT UNIQUE,
  classification TEXT, home_team TEXT, away_team TEXT, status TEXT,
  first_seen_at TEXT, last_seen_at TEXT);
CREATE TABLE snapshots (id INTEGER PRIMARY KEY, game_id INTEGER,
  source_game_id TEXT, captured_at TEXT, home_score INTEGER,
  away_score INTEGER, total_line REAL, quarter INTEGER, clock TEXT,
  period_label TEXT);
CREATE TABLE game_results (source_game_id TEXT PRIMARY KEY,
  classification TEXT, final_home INTEGER, final_away INTEGER,
  final_total INTEGER, result_at TEXT, final_result_status TEXT);
CREATE TABLE game_quality (source_game_id TEXT PRIMARY KEY,
  classification TEXT, status TEXT, reason TEXT, checked_at TEXT);
CREATE TABLE checkpoint_market (id INTEGER PRIMARY KEY,
  source_game_id TEXT, classification TEXT, checkpoint_pct INTEGER,
  checkpoint_timestamp TEXT, quarter INTEGER, progress REAL,
  elapsed_minutes REAL, opening_line REAL, live_market_line REAL,
  market_timestamp TEXT, blm_fair_value REAL, closing_line REAL,
  actual_final_total INTEGER, market_vs_fair REAL, signal TEXT,
  outcome TEXT, model_version TEXT, recorded_at TEXT);
"""

_DAY0 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_db(path: Path, games: list[dict]) -> Path:
    """games: [{gid, start (datetime), checkpoints: [(pct, line, fair,
    actual, market_age_s)]}].  Checkpoint captured 60 min after start;
    market observed market_age_s before its checkpoint."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for i, g in enumerate(games, start=1):
        gid, start = g["gid"], g["start"]
        cp_ts = start + timedelta(minutes=60)
        with conn:
            conn.execute(
                """INSERT INTO games (id, source_game_id, classification,
                                      home_team, away_team, status,
                                      first_seen_at, last_seen_at)
                   VALUES (?,?, 'CYBER_2K26','H','A','ended',?,?)""",
                (i, gid, _iso(start), _iso(cp_ts)))
            # two Q1 snapshots set game_start (MIN captured_at)
            for j in range(2):
                conn.execute(
                    """INSERT INTO snapshots (id, game_id, source_game_id,
                           captured_at, home_score, away_score, total_line,
                           quarter, clock, period_label)
                       VALUES (?,?,?,?,0,0,1,1,'10:00','1st Quarter')""",
                    (i * 10 + j, i, gid, _iso(start + timedelta(seconds=j))))
            conn.execute(
                """INSERT INTO game_results (source_game_id, classification,
                       final_home, final_away, final_total, result_at,
                       final_result_status)
                   VALUES (?, 'CYBER_2K26',100,90,190,?, 'OK')""",
                (gid, _iso(cp_ts)))
            for (pct, line, fair, actual, age) in g["checkpoints"]:
                mts = cp_ts - timedelta(seconds=age)
                conn.execute(
                    """INSERT INTO checkpoint_market (
                         source_game_id, classification, checkpoint_pct,
                         checkpoint_timestamp, live_market_line,
                         market_timestamp, blm_fair_value,
                         actual_final_total, market_vs_fair,
                         model_version, recorded_at)
                       VALUES (?, 'CYBER_2K26', ?, ?, ?, ?, ?, ?, ?,
                               'v4-pace-1', ?)""",
                    (gid, pct, _iso(cp_ts), line, _iso(mts), fair, actual,
                     round(fair - line, 2), _iso(start)))
    return path


def game(gid: str, start: datetime, side: str, ys: list[int],
         pct: int = 40, line: float = 190.0, age: int = 10) -> dict:
    """A game whose checkpoints are all `side`; ys[i]=1 means the side
    WINS checkpoint i (actual lands on the winning side of the line)."""
    fair = line + 6.0 if side == "OVER" else line - 6.0
    actuals = [line + 4.0 if (side == "OVER") == (y == 1) else line - 4.0
               for y in ys]
    return {
        "gid": gid, "start": start,
        # each checkpoint on a DISTINCT valid pct (10..90 step 10)
        "checkpoints": [(pct + i * 10, line, fair, actuals[i], age)
                        for i in range(len(ys))],
    }


def day_n(n: int, hour: int = 10) -> datetime:
    return _DAY0 + timedelta(days=n, hours=hour - 10)


@pytest.fixture()
def two_side_db(tmp_path):
    """6 OVER games + 6 UNDER games, 2 checkpoints each, one day apart."""
    games = []
    for k in range(6):
        games.append(game(f"O{k}", day_n(k), "OVER", [1, 1]))
        games.append(game(f"U{k}", day_n(k, 20), "UNDER", [1, 1]))
    return build_db(tmp_path / "cal.db", games)


# ── 3: terminal rows never enter primary calibration ───────────────────
def test_no_terminal_rows_in_primary(tmp_path):
    g = game("T1", day_n(0), "OVER", [1], pct=10)
    g["checkpoints"].append((100, 190.0, 200.0, 200.0, 10))  # terminal
    db = build_db(tmp_path / "t.db", [g])
    rows = cal.load_rows(str(db))
    assert len(rows) == 1 and rows[0]["checkpoint_pct"] == 10


def test_tautology_rows_excluded(tmp_path):
    # checkpoint frozen at the game's first observed instant -> excluded
    g = game("TAU", day_n(0), "OVER", [1])
    db = build_db(tmp_path / "t.db", [g])
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute("UPDATE checkpoint_market SET checkpoint_timestamp = "
                     "(SELECT MIN(captured_at) FROM snapshots)")
    conn.close()
    assert cal.load_rows(str(db)) == []


# ── 1 + 2: information barriers ────────────────────────────────────────
def _preds_by_game(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["game"], []).append(r)
    return out


def test_no_final_outcome_enters_own_calibration(tmp_path):
    """Flip ONLY game G3's final outcomes: G0..G3 predictions (including
    G3's own) are unchanged — its outcome never calibrated itself —
    while G4..G8 predictions DO move (the state is genuinely fed)."""
    games = [game(f"G{k}", day_n(k), "OVER", [1, 1]) for k in range(9)]
    db_a = build_db(tmp_path / "a.db", games)
    flipped = [dict(g) for g in games]
    flipped[3] = game("G3", day_n(3), "OVER", [0, 0])
    db_b = build_db(tmp_path / "b.db", flipped)
    pa = _preds_by_game(cal.walk_forward(cal.load_rows(str(db_a)),
                                         min_history=4)["rows"])
    pb = _preds_by_game(cal.walk_forward(cal.load_rows(str(db_b)),
                                         min_history=4)["rows"])
    for k in range(4):  # barrier: up to and including the flipped game
        a = [(r["p_logistic"], r["p_isotonic"]) for r in pa[f"G{k}"]]
        b = [(r["p_logistic"], r["p_isotonic"]) for r in pb[f"G{k}"]]
        assert a == b, f"game G{k}'s own/later outcome leaked backward"
    moved = [k for k in range(4, 9)
             if [(r["p_logistic"], r["p_isotonic"]) for r in pa[f"G{k}"]]
             != [(r["p_logistic"], r["p_isotonic"]) for r in pb[f"G{k}"]]]
    assert moved, "state never consumed outcomes (test insensitive)"


def test_no_future_game_enters_calibration_state(tmp_path):
    """Flip outcomes of LATER games only: earlier games' predictions
    must be unchanged (no future observation enters the state)."""
    games = [game(f"G{k}", day_n(k), "OVER", [1, 1]) for k in range(8)]
    db_a = build_db(tmp_path / "a.db", games)
    flipped = [dict(g) for g in games]
    flipped[5] = game("G5", day_n(5), "OVER", [0, 0])
    flipped[6] = game("G6", day_n(6), "OVER", [0, 0])
    db_b = build_db(tmp_path / "b.db", flipped)
    pa = _preds_by_game(cal.walk_forward(cal.load_rows(str(db_a)),
                                         min_history=4)["rows"])
    pb = _preds_by_game(cal.walk_forward(cal.load_rows(str(db_b)),
                                         min_history=4)["rows"])
    for k in range(5):  # strictly before the first flipped game
        a = [(r["p_logistic"], r["p_isotonic"]) for r in pa[f"G{k}"]]
        b = [(r["p_logistic"], r["p_isotonic"]) for r in pb[f"G{k}"]]
        assert a == b, f"future game leaked into G{k}"


# ── 4: sides stay separate ─────────────────────────────────────────────
def test_over_under_calibrated_separately(tmp_path):
    """Flipping every UNDER outcome must not move a single OVER
    prediction, and vice versa."""
    games = [game(f"O{k}", day_n(k), "OVER", [1, 1]) for k in range(6)]
    games += [game(f"U{k}", day_n(k, 20), "UNDER", [1, 1]) for k in range(6)]
    db_a = build_db(tmp_path / "a.db", games)
    flipped = []
    for g in games:
        if g["gid"].startswith("U"):
            k = int(g["gid"][1:])
            flipped.append(game(f"U{k}", day_n(k, 20), "UNDER", [0, 0]))
        else:
            flipped.append(dict(g))
    db_b = build_db(tmp_path / "b.db", flipped)
    pa = _preds_by_game(cal.walk_forward(cal.load_rows(str(db_a)),
                                         min_history=4)["rows"])
    pb = _preds_by_game(cal.walk_forward(cal.load_rows(str(db_b)),
                                         min_history=4)["rows"])
    for k in range(6):
        a = [(r["p_logistic"], r["p_isotonic"]) for r in pa[f"O{k}"]]
        b = [(r["p_logistic"], r["p_isotonic"]) for r in pb[f"O{k}"]]
        assert a == b, "UNDER outcomes leaked into OVER calibration"


# ── 5: chronological ordering enforced ─────────────────────────────────
def test_walk_forward_ordering_enforced(tmp_path):
    """Insertion order in the DB is irrelevant: load_rows sorts by
    (game_start, game, checkpoint) before any calibration."""
    games = [game(f"G{k}", day_n(k), "OVER", [1, 1]) for k in range(8)]
    db = build_db(tmp_path / "ordered.db", games)
    shuffled = build_db(tmp_path / "shuffled.db", list(reversed(games)))
    ra = cal.walk_forward(cal.load_rows(str(db)), min_history=4)
    rb = cal.walk_forward(cal.load_rows(str(shuffled)), min_history=4)
    assert ra["rows"] == rb["rows"]
    assert ra["skipped_no_history"] == rb["skipped_no_history"] > 0


def test_walk_forward_skips_until_history(tmp_path):
    # 2 rows per game -> prior counts: 0,2,4,6,8
    games = [game(f"G{k}", day_n(k), "OVER", [1, 1]) for k in range(5)]
    db = build_db(tmp_path / "t.db", games)
    wf = cal.walk_forward(cal.load_rows(str(db)), min_history=6)
    assert wf["evaluated"] == 4 and wf["skipped_no_history"] == 6
    wf2 = cal.walk_forward(cal.load_rows(str(db)), min_history=11)
    assert wf2["evaluated"] == 0 and wf2["skipped_no_history"] == 10


# ── 6: game-level weighting ────────────────────────────────────────────
def test_game_weighting_is_per_game(tmp_path):
    """Filler + one 9-checkpoint game + one 1-checkpoint game (same
    side): game-weighted Brier must equal the MEAN of the two
    per-game Briers, never the checkpoint-weighted pooling."""
    games = [game("FILL", day_n(0), "OVER", [1, 1, 1])]
    games.append(game("BIG", day_n(1), "OVER", [1] * 9, pct=10))
    games.append(game("SMALL", day_n(2), "OVER", [0], pct=10))
    db = build_db(tmp_path / "t.db", games)
    wf = cal.walk_forward(cal.load_rows(str(db)), min_history=1)
    ev = [r for r in wf["rows"] if r["p_logistic"] is not None]
    rep = cal.side_report(ev, "logistic")
    big = [r for r in ev if r["game"] == "BIG"]
    small = [r for r in ev if r["game"] == "SMALL"]
    assert len(big) == 9 and len(small) == 1
    b_big = cal.brier([r["p_logistic"] for r in big], [r["y"] for r in big])
    b_small = cal.brier([r["p_logistic"] for r in small],
                        [r["y"] for r in small])
    gw = rep["game_weighted"]
    assert gw["games"] == 2
    assert gw["brier_mean_over_games"] == pytest.approx(
        (b_big + b_small) / 2, abs=1e-6)
    ck = rep["brier"]
    assert ck == pytest.approx(
        (b_big * len(big) + b_small * len(small)) / (len(big) + len(small)),
        abs=1e-6)
    # the two weightings genuinely differ when checkpoint counts differ
    assert gw["brier_mean_over_games"] != pytest.approx(ck, abs=1e-9)


# ── LIVE / STALE separation ────────────────────────────────────────────
def test_freshness_split_recorded(tmp_path):
    games = [game(f"L{k}", day_n(k), "OVER", [1, 1], age=10)
             for k in range(4)]
    games += [game(f"S{k}", day_n(k, 20), "UNDER", [1, 1], age=600)
              for k in range(4)]
    db = build_db(tmp_path / "t.db", games)
    rows = cal.load_rows(str(db))
    live = [r for r in rows if r["market_status"] == "LIVE"]
    stale = [r for r in rows if r["market_status"] == "STALE"]
    assert len(live) == 8 and len(stale) == 8
    assert all(r["side"] == "OVER" for r in live)
    assert all(r["side"] == "UNDER" for r in stale)


# ── baselines + verdict plumbing ───────────────────────────────────────
def test_baselines_account_exactly(tmp_path):
    db = build_db(
        tmp_path / "b.db",
        [game(f"O{k}", day_n(k), "OVER", [1, 0]) for k in range(6)]
        + [game(f"U{k}", day_n(k, 20), "UNDER", [0, 1]) for k in range(6)])
    rows = cal.load_rows(str(db))
    wf = cal.walk_forward(rows, min_history=2)
    ev = [r for r in wf["rows"] if r["p_logistic"] is not None]
    base = cal.baselines(ev)
    assert base["n"] == len(ev) > 0
    # always-OVER + always-UNDER outcomes partition the rows exactly
    assert base["always_over_hit_rate"] + base["always_under_hit_rate"] \
        == pytest.approx(1.0)
    assert base["fade_model_hit_rate"] == pytest.approx(
        1.0 - base["raw_model_direction_hit_rate"])


def test_status_fail_on_insufficient_data(two_side_db):
    report = cal.calibration_report(str(two_side_db))
    status = cal.calibration_status(report)
    assert status["CALIBRATION_STATUS"] == "FAIL"


# ── end-to-end smoke on the synthetic store ────────────────────────────
def test_calibration_report_end_to_end(two_side_db):
    report = cal.calibration_report(str(two_side_db))
    assert report["population"]["primary_rows"] == 24
    assert report["read_only"] is True
    assert report["no_betting_output"] is True
    desc = report["descriptive"]
    assert set(desc["by_abs_residual_band"]) == set(cal.RESIDUAL_BANDS)
    assert set(desc["by_checkpoint"]) == set(cal.PRIMARY_PCTS)
    # walk-forward warming is HONEST: 24 rows < the 30-row history gate
    # means every row is skipped (never silently pooled in-sample)
    assert report["population"]["evaluated_walk_forward"] == 0
    assert report["population"]["skipped_no_history"] == 24
    # both methods always reported, both sides, no selection between them
    for side in ("over", "under"):
        assert set(report[side]) == {"logistic", "isotonic"}
        for method in ("logistic", "isotonic"):
            rep = report[side][method]
            assert rep["n"] == 0
            assert "brier" not in rep  # no metrics invented from zero rows
    status = cal.calibration_status(report)
    for key in ("CALIBRATION_STATUS", "EMPIRICAL_RELATIONSHIP",
                "OVER_CALIBRATION", "UNDER_CALIBRATION", "LIVE_CALIBRATION",
                "STALE_CALIBRATION", "LOGISTIC", "ISOTONIC", "WALK-FORWARD",
                "GAME-LEVEL", "TERMINAL EXCLUSION", "VERDICT"):
        assert key in status
    assert status["CALIBRATION_STATUS"] == "FAIL"
    assert "INSUFFICIENT" in status["OVER_CALIBRATION"]
    assert "INSUFFICIENT" in status["UNDER_CALIBRATION"]


def test_report_evaluates_with_sufficient_history(tmp_path):
    """With enough prior history the walk-forward gate opens and both
    sides carry real metrics (18 games/side = 36 rows/side; the 30-row
    gate opens at game 16: prior rows 30, 32, 34)."""
    games = []
    for k in range(18):
        games.append(game(f"O{k}", day_n(k), "OVER", [1, 1]))
        games.append(game(f"U{k}", day_n(k, 20), "UNDER", [0, 1]))
    db = build_db(tmp_path / "t.db", games)
    report = cal.calibration_report(str(db))
    pop = report["population"]
    assert pop["primary_rows"] == 72
    assert pop["evaluated_walk_forward"] == 12  # 3 late games x 2 rows x 2 sides
    assert pop["over_evaluated"] == 6 and pop["under_evaluated"] == 6
    for side in ("over", "under"):
        for method in ("logistic", "isotonic"):
            rep = report[side][method]
            assert rep["n"] == 6
            assert "brier" in rep and "reliability_bins" in rep
            assert rep["game_weighted"]["games"] == 3


# ── y-semantics: UNDER y=1 means the UNDER side won ────────────────────
def test_under_y_semantics(tmp_path):
    db = build_db(tmp_path / "t.db", [game("U0", day_n(0), "UNDER", [1, 0])])
    rows = cal.load_rows(str(db))
    wins = [r["y"] for r in rows]
    assert wins == [1, 0]  # y=1 <=> actual landed UNDER the line


# ── slice-1: Wilson CI, buckets, merge rule, terminal sanity ───────────
def test_wilson_ci_known_values():
    # 6/10 -> [0.313, 0.832] at z=1.96 (standard reference value)
    lo, hi = cal.wilson_ci(6, 10)
    assert lo == 0.313 and hi == 0.832
    assert cal.wilson_ci(0, 0) == (None, None)
    lo, hi = cal.wilson_ci(0, 5)
    assert lo == 0.0 and 0.0 <= hi < 0.6
    lo, hi = cal.wilson_ci(5, 5)
    assert 0.4 < lo <= 1.0 and hi == 1.0


def test_residual_bucket_edges():
    assert cal.residual_bucket(-20.0) == "-20 to -15"   # lo inclusive
    assert cal.residual_bucket(-20.1) == "< -20"
    assert cal.residual_bucket(-2.5) == "-2.5 to 0"
    assert cal.residual_bucket(0.0) == "0 to +2.5"
    assert cal.residual_bucket(19.9) == "+15 to +20"
    assert cal.residual_bucket(20.0) == "> +20"
    assert cal.residual_bucket(-7.5) == "-7.5 to -5"


def test_merge_rule_is_size_only_and_documented():
    # 3 adjacent buckets; only the tail is below the threshold and the
    # merge must pull it TOWARD THE CENTRE, never outward
    buckets = [{"bucket": "a", "n": 50, "wins": 25, "losses": 25},
               {"bucket": "b", "n": 40, "wins": 20, "losses": 20},
               {"bucket": "c", "n": 10, "wins": 9, "losses": 1}]
    merged, rule = cal.merge_small_buckets(buckets, min_n=30)
    assert "n < 30" in rule and "size-only" in rule
    assert len(merged) == 2
    assert merged[-1]["bucket"] == "b+c"       # inner neighbour wins
    assert merged[-1]["n"] == 50 and merged[-1]["wins"] == 29
    # outcome-blind: flipping c's wins/losses must not change the merge
    buckets2 = [{"bucket": "a", "n": 50, "wins": 25, "losses": 25},
                {"bucket": "b", "n": 40, "wins": 20, "losses": 20},
                {"bucket": "c", "n": 10, "wins": 1, "losses": 9}]
    merged2, _ = cal.merge_small_buckets(buckets2, min_n=30)
    assert [m["bucket"] for m in merged] == [m["bucket"] for m in merged2]


def test_empirical_table_buckets_and_conservation(two_side_db):
    rows = cal.load_rows(str(two_side_db))
    t = cal.empirical_bucket_table(rows)
    assert sum(b["n"] for b in t["buckets"]) == len(rows) == 24
    for b in t["buckets"]:
        assert b["wins"] + b["losses"] == b["n"]
        if b["n"]:
            assert b["wilson_lo"] <= b["win_rate"] <= b["wilson_hi"]
    assert t["merge_rule"]  # documented rule always carried


def test_checkpoint_dependence_covers_all_and_partitions(two_side_db):
    rows = cal.load_rows(str(two_side_db))
    dep = cal.empirical_checkpoint_dependence(rows)
    assert set(dep) == set(cal.PRIMARY_PCTS)
    assert sum(v["n"] for v in dep.values()) == 24
    # every per-checkpoint bucket set is the fixed grid, unmerged
    assert all(len(v["buckets"]) == len(cal.RESIDUAL_BUCKET_EDGES)
               for v in dep.values())


def test_direction_freshness_disjoint_and_exhaustive(two_side_db):
    rows = cal.load_rows(str(two_side_db))
    df = cal.empirical_direction_freshness(rows)
    o = df["by_direction"]["OVER"]
    u = df["by_direction"]["UNDER"]
    assert o["n"] + u["n"] == 24 and o["n"] == 12 and u["n"] == 12
    # LIVE/STALE cells partition within each direction
    assert o["live"]["n"] + o["stale"]["n"] == o["n"]
    assert u["live"]["n"] + u["stale"]["n"] == u["n"]
    # freshness axis partitions the whole population exactly once
    lv = df["by_freshness"]["LIVE"]
    stl = df["by_freshness"]["STALE"]
    assert lv["n"] + stl["n"] == 24
    assert lv["over"]["n"] + lv["under"]["n"] == lv["n"]


def test_block_summaries_per_block(two_side_db):
    rows = cal.load_rows(str(two_side_db))
    blocks = cal.block_summaries(rows)
    assert len(blocks) == 6                       # one per day
    assert sum(b["raw_direction"]["n"] for b in blocks) == 24
    assert all(b["games"] == 2 for b in blocks)   # 1 OVER + 1 UNDER per day
    for b in blocks:
        assert b["over"]["n"] + b["under"]["n"] == b["raw_direction"]["n"]
        assert b["live"]["n"] + b["stale"]["n"] == b["raw_direction"]["n"]


def test_terminal_sanity_counts_and_separation(tmp_path):
    g = game("P", day_n(0), "OVER", [1], pct=10)
    # terminal: fair above line, actual lands OVER -> OVER_WIN (mechanical)
    g2 = game("Q", day_n(1), "OVER", [1], pct=10)
    g2["checkpoints"].append((100, 190.0, 200.0, 195.0, 10))
    # terminal loss case: fair above line, actual lands UNDER
    g3 = game("R", day_n(2), "OVER", [1], pct=10)
    g3["checkpoints"].append((100, 190.0, 200.0, 185.0, 10))
    db = build_db(tmp_path / "t.db", [g, g2, g3])
    ts = cal.terminal_sanity(str(db))
    term = ts["terminal"]
    assert term["n_decided"] == 2 and term["wins"] == 1 and term["losses"] == 1
    # model side was OVER both times: one OVER_WIN, one OVER_LOSS
    assert term["over_win"] == 1 and term["over_loss"] == 1
    assert term["under_win"] == 0 and term["under_loss"] == 0
    assert 0.0 < term["wilson_lo"] <= term["win_rate"] <= term["wilson_hi"] < 1.0
    assert "mechanically" in term["mechanical_note"]
    # and the terminal rows NEVER entered the primary population
    assert all(r["checkpoint_pct"] != 100 for r in cal.load_rows(str(db)))


def test_verdict_format_slice1(two_side_db):
    report = cal.calibration_report(str(two_side_db))
    st = cal.calibration_status(report)
    expected = ["CALIBRATION_STATUS", "EMPIRICAL_RELATIONSHIP",
                "OVER_CALIBRATION", "UNDER_CALIBRATION", "LIVE_CALIBRATION",
                "STALE_CALIBRATION", "LOGISTIC", "ISOTONIC", "WALK-FORWARD",
                "GAME-LEVEL", "TERMINAL EXCLUSION", "VERDICT"]
    assert list(st.keys()) == expected
    assert st["VERDICT"].startswith("Read-only calibration research")


def test_report_contains_empirical_and_terminal(two_side_db):
    report = cal.calibration_report(str(two_side_db))
    assert "empirical" in report and "terminal_sanity" in report
    assert report["population"]["games"] == 12
    assert set(report["empirical"]) == {
        "residual_buckets", "by_checkpoint", "direction_freshness", "blocks"}


# ── calibrator numerics ────────────────────────────────────────────────
def test_logistic_recovers_signal_direction():
    xs = [-6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6] * 3
    ys = [0 if x < 0 else 1 for x in xs]
    m = cal.fit_logistic([float(x) for x in xs], ys)
    assert m["intercept_only"] == 0.0
    assert m["b1"] > 0  # larger raw -> higher P(win)
    assert cal.predict_logistic(m, 6.0) > 0.9
    assert cal.predict_logistic(m, -6.0) < 0.1


def test_logistic_intercept_only_fallback():
    m = cal.fit_logistic([1.0, 2.0, 3.0], [1, 0, 1])  # tiny n
    assert m["intercept_only"] == 1.0
    assert m["b1"] == 0.0


def test_isotonic_is_monotone_and_tie_safe():
    xs = [1.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [1, 0, 0, 0, 1, 1]
    knots = cal.fit_isotonic(xs, ys)
    means = [m for _, m in knots]
    assert means == sorted(means)          # monotone non-decreasing
    assert all(knots[i][0] < knots[i + 1][0]
               for i in range(len(knots) - 1))  # function of x (ties merged)
    for x, _ in zip(xs, ys):
        p = cal.predict_isotonic(knots, x)
        assert 0.0 <= p <= 1.0
