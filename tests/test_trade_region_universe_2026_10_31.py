"""The TRADE UNIVERSE leg: the engine may only see the region it trades.

Operator directive 2026-10-31 — keep auto-bet on the Betual VIRTUAL feed and
stop it trading real-league fixtures that share the section name "Betual NBA".

Measured on the prod DB before shipping:
  region = Virtual%20Matches   43,061 games   (simulated feed, the traded set)
  South%20Korea 95 · North%20America 74 · China 16 · Europe 2 · World 5
                               192 games      (real fixtures, must never trade)
  the team-name "... Virtual" marker covers 9,921/43,061 virtual games and 0
  October ones, so it cannot serve as the test — the region can.

One named test per requirement.
"""
from __future__ import annotations

from blm_v4.trade_window import (
    REGION_NOT_TRADED,
    filter_tradeable_games,
    trade_region_reason,
)


# ── the traded set ───────────────────────────────────────────────────────

def test_the_virtual_matches_region_is_traded():
    assert trade_region_reason("Virtual Matches") is None
    assert trade_region_reason("Virtual%20Matches") is None     # as stored
    assert trade_region_reason("virtual   matches") is None      # case + spacing


# ── real-league regions are refused ──────────────────────────────────────

def test_a_real_league_region_is_refused():
    for region in ("South%20Korea", "South Korea", "North%20America",
                   "China", "Europe", "World"):
        assert trade_region_reason(region) == REGION_NOT_TRADED, region


def test_the_2026_10_31_kbl_fixture_is_refused_by_region():
    """The game that took R125 (source_game_id 31256628)."""
    assert trade_region_reason("South%20Korea") == REGION_NOT_TRADED


# ── absence fails closed (a positive membership requirement) ─────────────

def test_an_absent_region_is_refused():
    assert trade_region_reason(None) == REGION_NOT_TRADED
    assert trade_region_reason("") == REGION_NOT_TRADED
    assert trade_region_reason("   ") == REGION_NOT_TRADED


# ── the worker's payload filter ──────────────────────────────────────────

def test_the_filter_keeps_only_the_traded_region():
    games = [
        {"game_id": "a", "region": "Virtual%20Matches"},
        {"game_id": "b", "region": "South%20Korea"},       # 31256628's class
        {"game_id": "c", "region": "North%20America"},
        {"game_id": "d", "region": "Virtual Matches"},
    ]
    kept = filter_tradeable_games(games)
    assert [g["game_id"] for g in kept] == ["a", "d"]


def test_the_filter_tolerates_an_empty_or_odd_payload():
    assert filter_tradeable_games([]) == []
    assert filter_tradeable_games(None) == []
    assert filter_tradeable_games([None, {}, {"game_id": "x"}]) == []


# ── no-regression guards for the untouched legs ─────────────────────────

def test_the_competition_exclusion_leg_still_fails_open_on_an_absent_slug():
    """The exclusion leg's fail direction must NOT be flipped by this change."""
    from blm_v4.trade_window import trade_competition_reason

    assert trade_competition_reason(None) is None
    assert trade_competition_reason("kbl") is None       # slug alone says nothing
    assert trade_competition_reason("cyber-basketball-2k26-matches") is not None


def test_the_score_leg_is_unchanged_by_this_change():
    from blm_v4.trade_window import SCORE_FLOOR, score_reason

    assert score_reason(134) is None
    assert score_reason(69) == SCORE_FLOOR
    assert score_reason(None) is None                     # documented absent-input contract
