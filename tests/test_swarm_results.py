"""SWARM results source — the feed that backs the PokerBet results page.

Discovered 2026-09-24 by hooking the WebSocket the results page itself opens
(see blm_v4/swarm_results.py).  Why this replaces DOM scraping:

  * the DOM carries NO game id (408 KB of page, id appears only in analytics
    trackers echoing the URL), so DOM identity can only be inferred from
    team names + footer time;
  * measured: 0/10 sampled page-verified games were present in the page's
    default Sport=Football list at all;
  * the API matches game_id EXACTLY and returns full team identities, the
    fixture start timestamp and a structured 4-quarter score line.

The parsing layer here is pure — it is what the reconciler trusts.
"""
from __future__ import annotations

from blm_v4.swarm_results import (
    normalize_result,
    parse_scores,
)

# A real frame captured from the feed (game 31013207).
REAL = {
    "competition_name": "Betual TBSL",
    "competition_id": 18296900,
    "game_name": "Trabzonspor Virtual - Anadolu Efes SK Virtual",
    "scores": "101:111(29:23, 25:27, 26:31, 21:30)",
    "region_id": 1661,
    "region_name": "Virtual Matches",
    "date": 1790273100,
    "game_id": "31013207",
    "sport_id": "3",
    "sport_alias": "Basketball",
    "sport_name": "Basketball",
    "team1_id": 1423916,
    "team1_name": "Trabzonspor Virtual",
    "team2_id": 1423878,
    "team2_name": "Anadolu Efes SK Virtual",
}


def test_parse_scores_reads_the_swarm_score_line():
    p = parse_scores("101:111(29:23, 25:27, 26:31, 21:30)")
    assert p["home"] == 101 and p["away"] == 111
    assert p["quarters"] == [(29, 23), (25, 27), (26, 31), (21, 30)]
    assert sum(q[0] for q in p["quarters"]) == 101
    assert sum(q[1] for q in p["quarters"]) == 111


def test_parse_scores_reads_a_football_score_line():
    p = parse_scores("2:3 (2:3)")
    assert (p["home"], p["away"]) == (2, 3)
    assert p["quarters"] == [(2, 3)]


def test_parse_scores_without_a_split_is_still_a_final():
    p = parse_scores("88:94")
    assert (p["home"], p["away"]) == (88, 94)
    assert p["quarters"] == []


def test_parse_scores_rejects_non_scores():
    for bad in ("", None, "-", "Postponed", "x:y", "12:", ":9", "1:2:3"):
        assert parse_scores(bad) is None


def test_normalize_result_yields_a_complete_four_quarter_final():
    r = normalize_result(REAL)
    assert r["game_id"] == "31013207"
    assert r["home_team"] == "Trabzonspor Virtual"
    assert r["away_team"] == "Anadolu Efes SK Virtual"
    assert (r["home_score"], r["away_score"]) == (101, 111)
    assert r["final_total"] == 212
    assert len(r["quarter_scores"]) == 4
    assert r["render_complete"] is True
    assert r["parse_quality"] == "full"
    assert r["team1_id"] == 1423916 and r["team2_id"] == 1423878
    assert r["sport"] == "Basketball" and r["sport_id"] == "3"
    assert r["competition"] == "Betual TBSL"
    # 1790273100 == 2026-09-24T18:05:00Z
    assert r["start_time"] == "2026-09-24T18:05:00Z"
    assert r["start_ts"] == 1790273100


def test_normalize_result_marks_a_missing_split_partial():
    r = normalize_result(dict(REAL, scores="88:94"))
    assert (r["home_score"], r["away_score"]) == (88, 94)
    assert r["render_complete"] is False
    assert r["parse_quality"] == "no_split"


def test_normalize_result_rejects_a_gameless_node():
    assert normalize_result({}) is None
    assert normalize_result({"scores": "not a score"}) is None
    assert normalize_result(None) is None


def test_a_real_bulk_frame_normalizes_every_game():
    """The bulk call returned 1170 basketball results for one day — every
    node must normalize or be skipped, never crash."""
    games = [
        REAL,
        dict(REAL, game_id="31013283",
             scores="72:74(26:18, 17:19, 14:23, 15:14)",
             team1_name="Nanjing Tongxi Monkey King Virtual",
             team2_name="Ningbo Rockets Virtual"),
        dict(REAL, game_id="31013637", scores="",
             team1_name="Goyang Sono Skygunners Virtual",
             team2_name="Seoul Samsung Thunders Virtual"),
    ]
    out = [normalize_result(g) for g in games]
    assert [r for r in out if r] != []
    assert len([r for r in out if r]) == 2          # the empty score is skipped
    ok = {r["game_id"]: r for r in out if r}
    assert ok["31013283"]["final_total"] == 146
    assert ok["31013283"]["quarter_scores"][0] == (26, 18)
