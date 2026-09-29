import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.collect_pokerbet_quarter_dom import (  # noqa: E402
    _market_type, _market_values, _period, _provider_event_id_from_url,
    _rows_for_dom, _verify_event_identity,
)
import pytest
from blm_v4.models import PokerBetGame  # noqa: E402
from blm_v4.storage import PokerBetStore  # noqa: E402


def test_canonical_quarters_and_overtime():
    assert [_period(x) for x in (
        "1st Quarter Total", "2nd Quarter", "3rd Quarter",
        "4th Quarter", "Overtime Total",
    )] == ["Q1", "Q2", "Q3", "Q4", "OT"]


def test_only_explicit_game_total_market_and_line_are_normalized():
    for value in (160.5, 161, 162.5, 163):
        text = f"3rd Quarter Total Points {value} Over 1.91 Under 1.89"
        assert _market_type(text) == "total"
        assert _market_values(text) == (float(value), 1.91, 1.89)
    assert _market_type("3rd Quarter Handicap 2.5") == "handicap"
    assert _market_type("3rd Quarter Team Total 80.5") is None
    assert _market_values("3rd Quarter Over 80.5") == (None, None, None)


def test_dom_observation_keeps_quarter_score_market_and_raw_text():
    game = {"source_game_id": "30840226", "provider_event_id": "30840226", "id": 7,
            "competition": "League", "home_team": "Home", "away_team": "Away"}
    dom = {"capturedAt": "2026-09-26T12:00:00Z",
           "url": "https://book/en/sports/live/event-view/Basketball/World/123456/league/30840226/home-away",
           "verified_event_id": "30840226",
           "selected_period": "Q3", "score": "61 : 55, (18:14) (21:19) (22:22)",
           "markets": [{"text": "3rd Quarter Total Points 162.5 Over 1.91 Under 1.89",
                        "tag": "DIV", "className": "market-group", "marketId": "m1"}]}
    (row,) = _rows_for_dom(game, dom)
    assert (row["source_game_id"], row["period"], row["market_type"]) == (
        "30840226", "Q3", "total")
    assert row["line_value"] == 162.5
    assert row["over_price"] == 1.91 and row["under_price"] == 1.89
    assert row["cumulative_home_score"] == 61 and row["cumulative_away_score"] == 55
    assert row["quarter_home_score"] == 22 and row["quarter_away_score"] == 22
    assert row["raw_text"].endswith("Over 1.91 Under 1.89")


def test_provider_id_comes_from_canonical_event_url_segment():
    url = ("https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/"
           "Virtual%20Matches/18296756/betual-nba/30840226/home-away")
    assert _provider_event_id_from_url(url) == "30840226"
    # The competition ID and arbitrary query digits are not event identity.
    assert _provider_event_id_from_url(url + "?campaign=99999999") == "30840226"
    assert _provider_event_id_from_url("https://book/results?game=30840226") is None


def test_repeated_team_names_do_not_override_canonical_event_identity():
    base = ("https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/"
            "Virtual%20Matches/18296756/betual-nba")
    older = {"source_game_id": "30840225", "source_url": base + "/30840225/same-teams"}
    requested = {"source_game_id": "30840226", "source_url": base + "/30840226/same-teams"}
    assert _verify_event_identity(requested, requested["source_url"]) == "30840226"
    with pytest.raises(ValueError, match="event id mismatch"):
        _verify_event_identity(requested, older["source_url"])


def test_event_id_mismatch_is_rejected():
    with pytest.raises(ValueError, match="event id mismatch"):
        _verify_event_identity(
            {"source_game_id": "30840226", "source_url":
             "https://x/en/sports/live/event-view/Basketball/World/123456/league/30840226/a-b"},
            "https://x/en/sports/live/event-view/Basketball/World/123456/league/30840225/a-b")


def test_missing_event_id_never_falls_back_to_team_names():
    with pytest.raises(ValueError, match="opened page has no canonical"):
        _verify_event_identity(
            {"source_game_id": "30840226", "source_url":
             "https://x/en/sports/live/event-view/Basketball/World/123456/league/30840226/a-b"},
            "https://x/en/sports/results?game=30840226")


def test_dom_rows_reject_missing_mismatched_or_aliased_game_identity():
    game = {"source_game_id": "30840226", "provider_event_id": "30840226", "id": 7}
    dom = {"url": "https://book/en/sports/live/event-view/Basketball/World/123456/league/30840226/home-away",
           "selected_period": "Q3",
           "verified_event_id": "30840226",
           "markets": [{"text": "3rd Quarter Total Points 162.5"}]}
    assert _rows_for_dom(game, dom)[0]["source_game_id"] == "30840226"
    assert _rows_for_dom(game, {**dom, "verified_event_id": "30840227"}) == []
    assert _rows_for_dom(game, {**dom, "url": "https://book/results?game=30840226"}) == []
    assert _rows_for_dom(game, {**dom, "url":
                                "https://book/en/sports/live/event-view/Basketball/World/123456/league/30840227/home-away"}) == []
    assert _rows_for_dom(game, {k: v for k, v in dom.items()
                                if k != "verified_event_id"}) == []
    alias = {**game, "source_game_id": "30840226#i1"}
    assert _rows_for_dom(alias, dom) == []


def test_quarter_row_keeps_internal_game_and_quarter_identity():
    game = {"source_game_id": "30840226", "provider_event_id": "30840226", "id": 701}
    dom = {"url": "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/"
                 "World/123456/league/30840226/home-away",
           "verified_event_id": "30840226", "selected_period": "Q3",
           "markets": [{"text": "3rd Quarter Total Points 162.5"}]}
    (row,) = _rows_for_dom(game, dom)
    assert (row["game_id"], row["source_game_id"], row["period"]) == (
        701, "30840226", "Q3")


def test_storage_rejects_cross_game_primary_key_linkage(tmp_path):
    """Test DB only: the foreign key and source ID must identify one game."""
    store = PokerBetStore(tmp_path / "quarter-dom-test.db")
    ids = []
    for gid in ("30840226", "30840227"):
        ids.append(store.upsert_game(PokerBetGame(
            source_game_id=gid, competition="League", game_family="betual",
            classification="BETUAL_NBA", home_team="Same Home",
            away_team="Same Away", source_url="https://example.invalid/" + gid)))
    obs = {"game_id": ids[0], "source_game_id": "30840226",
           "observed_at": "2026-09-26T12:00:00Z", "period": "Q3",
           "raw_text": "3rd Quarter Total Points 162.5"}
    store.insert_pokerbet_dom_market_observation(obs)
    with pytest.raises(ValueError, match="game_id/source_game_id mismatch"):
        store.insert_pokerbet_dom_market_observation(
            {**obs, "game_id": ids[1], "source_game_id": "30840226"})
    import sqlite3
    con = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT game_id,source_game_id,period FROM pokerbet_dom_market_observations"
        ).fetchall()
    finally:
        con.close()
    assert rows == [(ids[0], "30840226", "Q3")]


def test_unrecognized_market_does_not_become_total():
    game = {"source_game_id": "12345678", "provider_event_id": "12345678", "id": 1}
    dom = {"url": "https://book/en/sports/live/event-view/Basketball/World/123456/league/12345678/home-away",
           "verified_event_id": "12345678", "selected_period": "Q4", "markets": [
        {"text": "Spread -3.5", "tag": "DIV"},
        {"text": "Home Team Total 82.5", "tag": "DIV"},
    ]}
    rows = _rows_for_dom(game, dom)
    assert rows[0]["market_type"] == "handicap"
    assert rows[0]["line_value"] is None
    assert rows[1]["market_type"] is None
    assert rows[1]["line_value"] is None
