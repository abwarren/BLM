"""PROSPECTIVE CONFIRMATION — invariant tests (frozen-spec slice).

Proves the ADDITIONAL AUDIT REQUIREMENT:
  - a chronological split within the EXISTING historical dataset can
    never count as prospective: rows already in the DB at the freeze
    instant stay HISTORICAL no matter how late their block sorts;
  - C (prospective) requires checkpoint_timestamp strictly AFTER the
    freeze AND a game-level gate (no checkpoint of that game at or
    before the freeze);
  - the spec is tamper-evident (sha256 pin); altering the frozen
    hypothesis refuses to evaluate;
  - with no post-freeze rows the status is AWAITING_PROSPECTIVE_DATA
    and nothing is manufactured;
  - B (historical walk-forward) is the SAME population as A and is
    labelled non-prospective; only C feeds the verdict.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4 import confirmation as conf
from blm_v4 import interaction_validation as iv
from blm_v4.calibration import load_rows
from blm_v4.freshness_audit import _ages
from test_calibration import build_db, day_n, game

FREEZE = datetime(2026, 9, 6, 21, 0, 0, tzinfo=timezone.utc)
POST = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def side_game(gid: str, start: datetime, side: str, off: float,
              ys: list[int], pct0: int = 10, age: int = 10) -> dict:
    """A game whose signed residual is ±off (fixing its mag cell) and
    whose checkpoints all land on the nominated age band; ys[i]=1 means
    the side wins checkpoint i."""
    line = 190.0
    fair = line + off if side == "OVER" else line - off
    if side == "OVER":
        actuals = [line + 4 if y == 1 else line - 4 for y in ys]
    else:
        actuals = [line - 4 if y == 1 else line + 4 for y in ys]
    return {"gid": gid, "start": start,
            "checkpoints": [(pct0 + i * 10, line, fair, actuals[i], age)
                            for i in range(len(ys))]}


def nominated_fixture(start: datetime, games_per_cell: int = 4) -> list[dict]:
    """Games filling all four nominated cells at `start` (all wins)."""
    ys9 = [1] * 9
    out: list[dict] = []
    for k in range(games_per_cell):
        out += [
            side_game(f"SO51{k}", start, "OVER", 7.0, ys9, age=600),
            side_game(f"SO101{k}", start, "OVER", 15.0, ys9, age=600),
            side_game(f"SO201{k}", start, "OVER", 25.0, ys9, age=600),
            side_game(f"FU101{k}", start, "UNDER", 15.0, ys9, age=0),
        ]
    return out


# ── 1: pre-freeze rows are NEVER prospective ────────────────────────────
def test_pre_freeze_rows_are_never_prospective(tmp_path):
    games = [game(f"P{k}", day_n(k), "OVER" if k % 2 else "UNDER", [1, 1])
             for k in range(6)]
    db = build_db(tmp_path / "c.db", games)
    rows = _ages(load_rows(str(db)))
    part = conf.partition(rows, FREEZE)
    assert len(part["A"]) == len(rows) and len(part["C"]) == 0
    rep = conf.confirmation_report(str(db))
    assert rep["status"] == "AWAITING_PROSPECTIVE_DATA"
    assert rep["C_prospective"]["n_rows"] == 0
    for st in rep["C_prospective"]["nominated_cells"]:
        assert st["n"] == 0 and st["win_rate"] is None
        assert st["pass_evaluation"]["passed"] is False


# ── 2: strictly-post-freeze whole games ARE prospective ─────────────────
def test_post_freeze_games_are_prospective(tmp_path):
    db = build_db(tmp_path / "c.db", nominated_fixture(POST, 1))
    rows = _ages(load_rows(str(db)))
    part = conf.partition(rows, FREEZE)
    assert len(part["C"]) == len(rows) and len(part["A"]) == 0


# ── 3: a game partly observed pre-freeze is NOT prospective ─────────────
def test_mixed_game_is_not_prospective(tmp_path):
    g = side_game("MIX", day_n(9, 12), "OVER", 7.0, [1, 1], pct0=40, age=600)
    db = build_db(tmp_path / "c.db", [g])   # both cps pre-freeze
    conn = sqlite3.connect(str(db))
    with conn:   # move the SECOND checkpoint strictly after the freeze
        conn.execute("UPDATE checkpoint_market "
                     "SET checkpoint_timestamp = ? "
                     "WHERE source_game_id='MIX' AND checkpoint_pct=50",
                     ("2026-09-07T00:00:00Z",))
    rows = _ages(load_rows(str(db)))
    part = conf.partition(rows, FREEZE)
    # the later-timestamp row still belongs to a partly-exposed game:
    # BOTH rows stay historical (game-level gate)
    assert len(part["C"]) == 0 and len(part["A"]) == len(rows)


# ── 4: boundary — exactly AT the freeze is historical (strictly after) ──
def test_row_exactly_at_freeze_is_historical(tmp_path):
    g = side_game("B1", day_n(9, 12), "OVER", 7.0, [1, 1], pct0=40, age=600)
    db = build_db(tmp_path / "c.db", [g])
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute("UPDATE checkpoint_market SET checkpoint_timestamp=?",
                     ("2026-09-06T21:00:00Z",))   # == freeze, not after
    rows = _ages(load_rows(str(db)))
    part = conf.partition(rows, FREEZE)
    assert len(part["C"]) == 0


# ── 5: spec is tamper-evident ───────────────────────────────────────────
def test_spec_hash_tamper_evidence(tmp_path):
    spec_copy = tmp_path / "spec.json"
    spec_copy.write_bytes(conf.SPEC_PATH.read_bytes())
    conf.load_spec(spec_copy)                     # intact copy loads
    spec_copy.write_bytes(spec_copy.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        conf.load_spec(spec_copy)                 # any edit refuses


# ── 6: CONFIRMED path — all four nominated cells pass on C alone ────────
def test_confirmed_requires_all_stale_over_and_one_fresh_under(tmp_path):
    db = build_db(tmp_path / "c.db", nominated_fixture(POST, 4))
    rep = conf.confirmation_report(str(db), "CYBER_2K26")
    assert rep["C_prospective"]["n_rows"] == 4 * 4 * 9
    for st in rep["C_prospective"]["nominated_cells"]:
        assert st["n"] == 36 and st["pass_evaluation"]["passed"] is True
    assert rep["status"] == "CONFIRMED"
    # C games must ALSO be absent from A (nothing was pre-freeze)
    assert rep["A_discovery"]["n_rows"] == 0


# ── 7: NOT_CONFIRMED — stale-OVER pass but fresh-UNDER fails ────────────
def test_not_confirmed_when_fresh_under_fails(tmp_path):
    db = build_db(tmp_path / "c.db", nominated_fixture(POST, 4))
    conn = sqlite3.connect(str(db))
    with conn:   # flip every fresh-UNDER settlement to a loss
        conn.execute("UPDATE checkpoint_market SET actual_final_total=196 "
                     "WHERE source_game_id LIKE 'FU10%'")
    rep = conf.confirmation_report(str(db), "CYBER_2K26")
    so = [st for st in rep["C_prospective"]["nominated_cells"]
          if st["side"] == "OVER"]
    fu = [st for st in rep["C_prospective"]["nominated_cells"]
          if st["side"] == "UNDER"]
    assert all(st["pass_evaluation"]["passed"] for st in so)
    assert not any(st["pass_evaluation"]["passed"] for st in fu)
    assert rep["status"] == "NOT_CONFIRMED"


# ── 8: INSUFFICIENT — post-freeze rows exist but every cell N < 30 ──────
def test_insufficient_prospective_n(tmp_path):
    db = build_db(tmp_path / "c.db", nominated_fixture(POST, 2))
    rep = conf.confirmation_report(str(db), "CYBER_2K26")
    assert rep["C_prospective"]["n_rows"] == 4 * 2 * 9
    assert all(st["n"] < 30
               for st in rep["C_prospective"]["nominated_cells"])
    assert rep["status"] == "INSUFFICIENT_PROSPECTIVE_N"


# ── 9: B is the SAME population as A and labelled non-prospective ───────
def test_b_equals_a_and_labelled_historical(tmp_path):
    games = nominated_fixture(POST, 1) + [
        game("OLD", day_n(0), "OVER", [1, 1])]
    db = build_db(tmp_path / "c.db", games)
    rep = conf.confirmation_report(str(db), "CYBER_2K26")
    B = rep["B_historical_validation"]
    A = rep["A_discovery"]
    assert B["n_rows"] == A["n_rows"] and B["is_prospective"] is False
    assert rep["C_prospective"]["is_prospective"] is True
    assert rep["terminology_audit"][
        "chronological_split_is_prospective"] is False


# ── 10: A-cell stats agree with the frozen interaction grid ─────────────
def test_a_cells_match_interaction_validation_grid(tmp_path):
    games = [game(f"O{k}", day_n(k), "OVER", [1, 1]) for k in range(6)]
    db = build_db(tmp_path / "c.db", games)
    rows = _ages(load_rows(str(db)))
    grid = iv.interaction_grid(rows)["grid"]["OVER"]["0-5s"]
    ref = next(c for c in grid if c["mag"] == "5-10")
    mine = conf._cell_stats(rows, "OVER", "5-10", "0-5s")
    assert mine["n"] == ref["n"]
    assert mine["win_rate"] == pytest.approx(ref["win_rate"])
    assert mine["wilson_lo"] == pytest.approx(ref["wilson_lo"])


# ── 11: report carries the read-only / no-output flags ──────────────────
def test_report_flags(tmp_path):
    db = build_db(tmp_path / "c.db", [game("X", day_n(0), "OVER", [1])])
    rep = conf.confirmation_report(str(db))
    assert rep["read_only"] and rep["no_betting_output"]
    assert rep["no_model_fit"] and rep["spec_frozen_while_waiting"]
    assert rep["spec_intact"] is True
