"""The STATS cohort is the LIVE TRADEABLE set — the engine's own legs.

Operator directive 2026-11-02: "all the data on the stats to relate to my live
tradeable cohort".  ``tradeable_refusal`` must refuse with the same leg the
engine would, and must refuse a game that only passes the old trigger condition
(pace + window) but fails the shortfall or the flat execution price.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blm_v4.live_analytics.fingerprint_stats import tradeable_refusal
from blm_v4 import trade_window as tw


GOOD = dict(slug="betual-nba", required=5.0, avg=4.0, actual=4.0,
            prog=80.0, points=150.0, price=1.80)
# required/avg = 1.25 -> clears the 1.04 pace bar; required-actual = 1.0 ->
# clears the +0.50 shortfall; 80% is inside 75-92; 150 pts clears 70; 1.80
# clears the 1.70 execution floor.


def test_a_game_that_clears_every_leg_is_tradeable():
    assert tradeable_refusal(**GOOD) is None


def test_it_refuses_when_only_the_old_trigger_condition_held(tmp_path):
    # passes pace + window + score + price, but the total is running AHEAD of
    # the required rate, so the SHORTFALL leg binds and the game is not
    # tradeable even though the alert fired.
    bad = dict(GOOD, actual=6.0)          # required - actual = -1.0
    assert tradeable_refusal(**bad).startswith("shortfall:")


def test_the_flat_execution_price_floor_is_the_engines_not_the_banded_table():
    # 1.55 passes the old banded floor (1.48 in the 75-85 band) but is below
    # the engine's flat 1.70 execution floor.
    assert tradeable_refusal(**dict(GOOD, price=1.55)).startswith("price:")


def test_competition_exclusion_and_window_are_the_engines_own():
    import os
    excluded = (os.environ.get("TRADE_EXCLUDED_COMPETITIONS") or "").split(",")
    slug = next((s.strip() for s in excluded if s.strip()), None)
    if slug is None:
        # no exclusion configured in this environment: the leg cannot fire,
        # so assert the window leg instead rather than skipping silently.
        assert tradeable_refusal(**dict(GOOD, prog=95.0)).startswith("window:")
    else:
        assert tradeable_refusal(**dict(GOOD, slug=slug)).startswith("competition:")
