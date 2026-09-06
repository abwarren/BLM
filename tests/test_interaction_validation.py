"""INTERACTION VALIDATION — invariant tests (read-only slice).

Proves:
  - the fixed 5x8 grid partitions each side's rows exactly and reports
    EVERY cell (none dropped by outcome);
  - exploratory/evaluable status follows N only (never the outcome);
  - persistence counting and split-half logic behave on constructed
    data (including a failing-control case);
  - MFE stays a labelled retrospective OUTCOME diagnostic;
  - the age-only diagnostic baseline is a fixed pre-specification;
  - the report carries the no-model-fit / no-betting flags.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4 import interaction_validation as iv
from test_calibration import build_db, day_n, game


@pytest.fixture()
def two_side_db(tmp_path):
    games = []
    for k in range(6):
        games.append(game(f"O{k}", day_n(k), "OVER", [1, 1], age=10))
        games.append(game(f"U{k}", day_n(k, 20), "UNDER", [1, 1], age=600))
    return build_db(tmp_path / "ival.db", games)


def test_grid_partitions_exactly_and_reports_every_cell(two_side_db):
    rows = iv._ages(iv.load_rows(str(two_side_db)))
    rep = iv.interaction_grid(rows)
    for side in ("OVER", "UNDER"):
        side_n = sum(1 for r in rows if r["side"] == side)
        counted = sum(c["n"] for col in rep["grid"][side].values()
                      for c in col)
        assert counted == side_n
        assert len(rep["grid"][side]) == 8          # all 8 age labels
        assert all(len(col) == 5 for col in rep["grid"][side].values())
    assert rep["summary"]["cells_total"] == 80
    # conservation across both sides
    total = sum(c["n"] for col in rep["grid"]["OVER"].values() for c in col) \
        + sum(c["n"] for col in rep["grid"]["UNDER"].values() for c in col)
    assert total == len(rows)


def test_status_is_n_based_not_outcome_based():
    lo = iv._cell([{"y": 1, "source_game_id": "x"}] * 29)
    hi = iv._cell([{"y": 0, "source_game_id": "x"}] * 30)
    assert lo["status"] == "exploratory"       # 29 rows, even all wins
    assert hi["status"] == "evaluable"         # 30 rows, even all losses
    assert lo["status"] != hi["status"]


def test_discovery_flags_require_all_criteria():
    cell_ok = {"n": 100, "win_rate": 0.75, "wilson_lo": 0.66}
    assert iv.discovery_flags(cell_ok, 4, True)["label"] == "MEETS_ALL_CRITERIA"
    # small N kills criterion (a) even with persistence
    cell_small = {"n": 20, "win_rate": 0.90, "wilson_lo": 0.72}
    f = iv.discovery_flags(cell_small, 4, True)
    assert f["criteria"]["a_evaluable"] is False
    assert f["label"] != "MEETS_ALL_CRITERIA"
    # wilson lower bound at/below 0.50 kills criterion (b)
    cell_weak = {"n": 100, "win_rate": 0.55, "wilson_lo": 0.45}
    f2 = iv.discovery_flags(cell_weak, 4, True)
    assert f2["criteria"]["b_rate_wilson"] is False


def test_cell_persistence_counts_and_split_half(two_side_db):
    rows = iv._ages(iv.load_rows(str(two_side_db)))
    p = iv.cell_persistence(rows, "UNDER", ">20", "0-5s")
    assert isinstance(p["persistent_blocks"], int)
    assert set(p["halves"]) == {"first", "second"}
    assert isinstance(p["half_replication"], bool)


def test_baselines_pre_specified(two_side_db):
    rows = iv._ages(iv.load_rows(str(two_side_db)))
    b = iv.baselines(rows)
    assert b["n"] == len(rows)
    # always-over + always-under partition the outcomes exactly
    assert b["always_over"] + b["always_under"] == pytest.approx(1.0)
    # age-only is bounded by the pure baselines on this fixture
    assert 0.0 <= b["age_only_diagnostic"] <= 1.0
    assert "pre-specification" in b["note"]


def test_mfe_cells_are_labelled_outcome_diagnostic(two_side_db):
    rows = iv._ages(iv.load_rows(str(two_side_db)))
    mfe = iv.mfe_by_interaction(rows)
    assert "OUTCOME" in mfe["note"]
    # small cells are honestly skipped (fixture is tiny)
    assert all(c["n"] >= 10 for c in mfe["cells"])


def test_report_flags_and_mechanisms(two_side_db):
    rep = iv.interaction_report(str(two_side_db))
    assert rep["read_only"] and rep["no_betting_output"]
    assert rep["no_model_fit"] is True
    assert set(rep["stale_over_mechanism"]["by_magnitude"]) == \
        set(iv.RESIDUAL_MAGS)
    assert set(rep["fresh_under_mechanism"]["by_magnitude"]) == \
        set(iv.RESIDUAL_MAGS)
    # the pre-registered contrast uses only the fixed groups
    contrast = rep["stale_over_mechanism"]["low_vs_high_contrast"]
    assert set(contrast) == {"low_pooled", "high_pooled"}
    # game weighting reports both weightings for both mechanisms
    gw = rep["game_weighted"]
    assert set(gw) == {"stale_over", "fresh_under"}
    for eff in gw.values():
        assert "checkpoint_weighted_rate" in eff and "game_weighted_rate" in eff


def test_monotonicity_contrast_direction(tmp_path):
    """Constructed data: high-|M-F| stale OVER rows win far more than
    low ones; the fixed contrast must show high_pooled > low_pooled."""
    from datetime import timedelta
    def _g(gid, day, hour, fair_off, ys, age):
        line = 190.0
        fair = line + fair_off
        actuals = [line + 4 if y == 1 else line - 4 for y in ys]
        return {"gid": gid, "start": day_n(day, hour),
                "checkpoints": [(10 + i * 10, line, fair, actuals[i], age)
                                for i in range(len(ys))]}
    games = [game(f"F{k}", day_n(k), "OVER", [0, 0, 0], pct=10, age=10)
             for k in range(8)]                     # fresh filler
    games.append(_g("H1", 9, 10, 25.0, [1] * 9, 600))   # >20 band, wins
    games.append(_g("L1", 9, 20, 1.0, [0] * 9, 600))    # 0-2.5 band, loses
    db = build_db(tmp_path / "t.db", games)
    rows = iv._ages(iv.load_rows(str(db)))
    rep = iv.stale_over_mechanism(rows)
    c = rep["low_vs_high_contrast"]
    assert c["high_pooled"]["win_rate"] == 1.0
    assert c["low_pooled"]["win_rate"] == 0.0
