"""M009-M2 (REFINED) — MARKET VS FAIR scorecard aggregation.

The PRIMARY scorecard metric: where was the bookmaker's line, where was
BLM's fair value, how far apart were they, did the result validate the
position.  Aggregates the immutable checkpoint_market rows (clean
completed games only, by table construction) per checkpoint 10..100%.

Per checkpoint:
  n            games with BOTH market and fair (M-F available)
  avg_market   mean(live_market_line)
  avg_fair     mean(blm_fair_value)
  avg_mf       mean(market_vs_fair) — SIGNED, the primary metric
  median_mf    median signed M-F
  abs_mf       mean(abs(market_vs_fair))
  over_value_n/pct, under_value_n/pct, push_n/pct   (signal)
  over_win, over_loss, under_win, under_loss, push_outcome  (outcome)
  position_win_rate = (over_win + under_win) /
                      (over_win + over_loss + under_win + under_loss)
                      (pushes excluded from the denominator, reported separately)
  avg_olv_to_clv  mean(olv_to_clv)
  move_toward / move_away / move_unchanged

Game-level scorecard (games[]): per game — id, teams, OLV, CLV, final
total, outcome vs OLV, outcome vs CLV, and the progressive table rows[]
(checkpoint_pct, market, fair, mf, signal, actual, outcome).

Honest N: a checkpoint row with a NULL market (or result) is excluded
from market-linked stats but included in fair stats.  Never fabricated.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.scorecard import Scorecard
from tests.test_m009_checkpoint_market import _build, _LINES

HERE = Path(__file__).resolve().parent


@pytest.fixture
def sc(tmp_path):
    dbfile = tmp_path / "blm_pokerbet.db"
    # G-MIX: market on every snapshot, both disparity directions.
    _build(dbfile, "G-MIX", lines=_LINES)
    # G-PUSH: market == actual (143) -> every outcome PUSH; market < fair
    # (fair ~148) -> OVER_VALUE signal.
    _build(dbfile, "G-PUSH", lines=[143] * 20)
    # G-NOMKT: no market anywhere -> excluded from market-linked stats.
    _build(dbfile, "G-NOMKT")
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    return s


def _agg(sc: Scorecard) -> dict:
    return sc.market_vs_fair()


def _cp(agg: dict, pct: int) -> dict:
    return next(c for c in agg["checkpoints"] if c["checkpoint_pct"] == pct)


def _raw_pct(sc: Scorecard, pct: int) -> list[dict]:
    """Raw checkpoint_market rows at a checkpoint (source truth the
    aggregation must match)."""
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE checkpoint_pct=?", (pct,))]
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════

def test_mvf_checkpoints_shape(sc):
    """One entry per checkpoint 10..100, all required keys."""
    agg = _agg(sc)
    cps = agg["checkpoints"]
    assert [c["checkpoint_pct"] for c in cps] == list(range(10, 101, 10))
    for c in cps:
        for key in ("checkpoint_pct", "n", "avg_market", "avg_fair", "avg_mf",
                    "median_mf", "abs_mf", "over_value_n", "under_value_n",
                    "push_n", "over_win", "over_loss", "under_win",
                    "under_loss", "push_outcome", "position_win_rate",
                    "avg_olv_to_clv", "move_toward", "move_away",
                    "move_unchanged"):
            assert key in c, f"pct{c['checkpoint_pct']} missing {key}"


def test_mvf_pct50_exact_stats(sc):
    """The aggregation matches the raw checkpoint_market rows exactly
    (mean/median/abs of the signed M-F), and the directional invariants
    hold regardless of model internals: at pct50 BOTH market-bearing
    games are UNDER_VALUE (market > fair); G-MIX wins UNDER, G-PUSH
    PUSHES (actual == market); position win rate 1.0 (pushes excluded);
    moves: G-MIX AWAY, G-PUSH UNCHANGED."""
    c = _cp(_agg(sc), 50)
    mrows = [r for r in _raw_pct(sc, 50) if r["market_vs_fair"] is not None]
    mfs = [r["market_vs_fair"] for r in mrows]
    assert len(mrows) == 2                    # G-MIX + G-PUSH (G-NOMKT has no market)
    assert c["n"] == 2
    assert c["avg_mf"] == pytest.approx(sum(mfs) / 2)      # signed
    assert c["median_mf"] == pytest.approx(
        (sorted(mfs)[0] + sorted(mfs)[1]) / 2)             # n=2 -> mid-mean
    assert c["abs_mf"] == pytest.approx(sum(abs(m) for m in mfs) / 2)
    assert all(m > 0 for m in mfs)            # both UNDER_VALUE
    assert c["under_value_n"] == 2 and c["over_value_n"] == 0
    assert c["push_n"] == 0
    assert c["under_win"] == 1 and c["under_loss"] == 0
    assert c["over_win"] == 0 and c["over_loss"] == 0
    assert c["push_outcome"] == 1             # G-PUSH actual == market
    assert c["position_win_rate"] == pytest.approx(1.0)    # 1/1, pushes out
    assert c["move_away"] == 1 and c["move_unchanged"] == 1
    assert c["move_toward"] == 0


def test_mvf_sign_retained_negative(sc):
    """At pct10 BOTH market-bearing games are OVER_VALUE (market < fair,
    negative M-F); the aggregation retains the NEGATIVE sign — never
    abs'd for the primary metric."""
    c = _cp(_agg(sc), 10)
    mfs = [r["market_vs_fair"] for r in _raw_pct(sc, 10)
           if r["market_vs_fair"] is not None]
    assert len(mfs) == 2 and all(m < 0 for m in mfs)
    assert c["avg_mf"] == pytest.approx(sum(mfs) / 2)
    assert c["avg_mf"] < 0
    assert c["median_mf"] < 0
    assert c["over_value_n"] == 2 and c["under_value_n"] == 0


def test_mvf_honest_n_missing_market(sc):
    """G-NOMKT has fair values but no market: it never enters the
    market-linked stats (n stays 2) but DOES enter avg_fair (3 games)."""
    c = _cp(_agg(sc), 50)
    all_rows = _raw_pct(sc, 50)
    fair_vals = [r["blm_fair_value"] for r in all_rows
                 if r["blm_fair_value"] is not None]
    mkt_vals = [r["live_market_line"] for r in all_rows
                if r["live_market_line"] is not None]
    assert len(all_rows) == 3 and len(fair_vals) == 3
    assert len(mkt_vals) == 2
    assert c["n"] == 2
    assert c["avg_fair"] == pytest.approx(sum(fair_vals) / 3)
    assert c["avg_market"] == pytest.approx(sum(mkt_vals) / 2)
    assert c["avg_mf"] is not None            # computed over the 2, not NULL


def test_mvf_games_level_scorecard(sc):
    """games[]: per-game anchors + progressive table.  G-MIX OLV 170 /
    CLV 189 / final 143 -> outcome UNDER vs both; G-PUSH OLV=CLV=143==
    actual -> PUSH vs both."""
    agg = _agg(sc)
    games = {g["source_game_id"]: g for g in agg["games"]}
    assert set(games) == {"G-MIX", "G-PUSH", "G-NOMKT"}
    gm = games["G-MIX"]
    assert gm["olv"] == 170.0 and gm["clv"] == 189.0
    assert gm["final_total"] == 143
    assert gm["outcome_olv"] == "UNDER" and gm["outcome_clv"] == "UNDER"
    assert len(gm["rows"]) == 10
    assert gm["rows"][0]["checkpoint_pct"] == 10
    r50 = next(r for r in gm["rows"] if r["checkpoint_pct"] == 50)
    assert r50["market"] == 180.0
    assert r50["signal"] == "UNDER_VALUE"
    assert r50["outcome"] == "UNDER_WIN"
    gp = games["G-PUSH"]
    assert gp["olv"] == 143.0 and gp["clv"] == 143.0
    assert gp["outcome_olv"] == "PUSH" and gp["outcome_clv"] == "PUSH"


def test_mvf_immutable_source_still_holds(sc):
    """The aggregation reads checkpoint_market only; re-running the
    recorder does not change the aggregates (immutability preserved)."""
    a1 = _agg(sc)
    sc.record_checkpoint_market()
    a2 = _agg(sc)
    assert a1["checkpoints"] == a2["checkpoints"]
    assert a1["games"] == a2["games"]
