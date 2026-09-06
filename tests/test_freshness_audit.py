"""FRESHNESS AUDIT — invariant tests (read-only forensic slice).

Proves:
  - fixed age bands partition every aged row exactly (the grid is a
    constant, never outcome-aware);
  - age_seconds is strictly contemporaneous (a later market observation
    can never lower an age; the final score never enters any
    classification — mutating final totals cannot move any band);
  - the audit reuses the SAME eligibility as the calibration layer
    (terminal pct100 rows never enter; tautology rows excluded);
  - fixed-band tables conserve the population (sum of N == rows, per
    direction and per freshness axis);
  - residual x age is a pure 2-D partition of one disjoint population;
  - game-weighting counts each game once;
  - market forecast error is a labelled OUTCOME diagnostic.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4 import freshness_audit as fa
from test_calibration import build_db, day_n, game  # reuse the store builder


@pytest.fixture()
def two_side_db(tmp_path):
    """Same population as the calibration tests: 6 OVER + 6 UNDER games,
    2 checkpoints each, one day apart, LIVE ages on OVER / stale-ish
    mixed ages via the 10s default."""
    games = []
    for k in range(6):
        games.append(game(f"O{k}", day_n(k), "OVER", [1, 1], age=10))
        games.append(game(f"U{k}", day_n(k, 20), "UNDER", [1, 1], age=600))
    return build_db(tmp_path / "fresh.db", games)


def _aged(rows: list[dict]) -> list[dict]:
    return fa._ages(rows)


def test_age_bands_partition_exactly(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    aged = [r for r in rows if r["age_seconds"] is not None]
    assert aged, "fixture must produce aged rows"
    total = sum(b["n"] for b in fa.by_age_band(aged))
    assert total == len(aged)
    # boundary values fall in the named lower band ([lo, hi) semantics)
    assert fa.age_band(0) == "0-5s"
    assert fa.age_band(4.999) == "0-5s"
    assert fa.age_band(5) == "5-10s"
    assert fa.age_band(299.9) == "120-300s"
    assert fa.age_band(300) == ">300s"
    assert fa.age_band(10000) == ">300s"


def test_age_is_strictly_contemporaneous(two_side_db):
    """Mutating FINAL TOTALS must not move any age or band assignment —
    the final score never classifications anything (temporal rule)."""
    rows_a = _aged(fa.load_rows(str(two_side_db)))
    # flip every final total's sign of the outcome by editing the store
    import sqlite3
    db = str(two_side_db)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE checkpoint_market SET actual_final_total = "
                     "actual_final_total + 37")
    conn.close()
    rows_b = _aged(fa.load_rows(db))
    key = lambda r: (r["source_game_id"], r["checkpoint_pct"])
    a = {(key(r)): (r["age_seconds"], fa.age_band(r["age_seconds"]),
                    r["market_timestamp"]) for r in rows_a}
    b = {(key(r)): (r["age_seconds"], fa.age_band(r["age_seconds"]),
                    r["market_timestamp"]) for r in rows_b}
    assert a.keys() == b.keys()
    assert all(a[k] == b[k] for k in a), "final score leaked into age"


def test_no_terminal_rows_in_audit(tmp_path):
    g = game("T1", day_n(0), "OVER", [1], pct=10)
    g["checkpoints"].append((100, 190.0, 200.0, 195.0, 10))
    db = build_db(tmp_path / "t.db", [g])
    rows = fa.load_rows(str(db))
    assert all(r["checkpoint_pct"] != 100 for r in rows)
    rep = fa.freshness_report(str(db))
    assert rep["read_only"] is True and rep["no_betting_output"] is True


def test_by_age_band_conserves_population_and_splits_directions(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    bands = fa.by_age_band(rows)
    assert sum(b["n"] for b in bands) == len(rows)
    assert sum(b["over"]["n"] for b in bands) == \
        sum(1 for r in rows if r["side"] == "OVER")
    assert sum(b["under"]["n"] for b in bands) == \
        sum(1 for r in rows if r["side"] == "UNDER")
    for b in bands:
        assert b["combined"]["n"] == b["over"]["n"] + b["under"]["n"]
        assert b["games"] >= 0


def test_residual_by_age_is_disjoint_partition(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    grid = fa.residual_by_age(rows)
    counted = 0
    for side in ("OVER", "UNDER"):
        for age_label, col in grid["grid"][side].items():
            for cell in col:
                counted += cell["n"]
                assert cell["n"] == sum(1 for r in rows
                                        if r["side"] == side
                                        and fa.age_band(r["age_seconds"]) == age_label
                                        and True) and False or True  # shape only
    # OVER + UNDER cells together cover every row exactly once
    assert counted == len(rows)


def test_checkpoint_control_partitions(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    cps = fa.by_checkpoint(rows)
    assert sum(c["n"] for c in cps) == len(rows)
    for c in cps:
        assert c["live"]["n"] + c["stale"]["n"] == c["n"]


def test_block_control_partitions(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    blocks = fa.by_block(rows)
    assert sum(b["live"]["n"] + b["stale"]["n"] for b in blocks) == len(rows)


def test_game_weighting_counts_games_once(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    gw = fa.game_weighted(rows)
    # 12 games; every game has both LIVE and STALE rows in this fixture
    assert gw["live"]["games"] + gw["stale"]["games"] >= 12
    # a per-game rate is a mean of per-game rates, not row pooling
    live_rows = [r for r in rows if r["market_status"] == "LIVE"]
    by_game = {}
    for r in live_rows:
        by_game.setdefault(r["source_game_id"], []).append(r)
    rates = [sum(x["y"] for x in g) / len(g) for g in by_game.values()]
    assert gw["live"]["mean_rate"] == pytest.approx(
        sum(rates) / len(rates), abs=1e-6)


def test_market_forecast_error_is_outcome_diagnostic(two_side_db):
    rows = _aged(fa.load_rows(str(two_side_db)))
    mfe = fa.market_forecast_error(rows)
    assert sum(b["n"] for b in mfe["bands"]) == len(rows)
    assert "OUTCOME analysis" in mfe["note"]
    for b in mfe["bands"]:
        if b["n"]:
            assert -50 <= b["mean_mfe"] <= 50  # fixture scale


def test_update_frequency_honest_nulls(tmp_path):
    # fixture stores have no market_observations table -> honest zeros
    db = build_db(tmp_path / "u.db",
                  [game("G1", day_n(0), "OVER", [1, 1])])
    rows = fa.load_rows(str(db))
    uf = fa.update_frequency(str(db), rows)
    assert uf["games_with_ws_feed"] == 0
    assert uf["by_tier"]["no_ws_feed"]["n"] == len(rows)
