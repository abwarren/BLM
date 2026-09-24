"""DOM ROW MATCHING — the results page is a LIST, not a single game.

Measured live 2026-09-24:

  * the page renders `div.results-block-bc` per game and echoes NO game id
    anywhere (the only occurrences of the id in the DOM are analytics
    trackers echoing the URL — verified by dumping 408 KB of HTML);
  * requesting a basketball virtual game id renders ~260 UNRELATED FOOTBALL
    rows; requesting other ids renders 4 empty skeleton blocks;
  * therefore the old inner-text parse ("the first scoreboard on the page")
    attached a DIFFERENT game's result whenever the requested game was not
    listed — which is the normal case.

So the reconciler must take a result only from a ROW THAT MATCHES the
fixture, and a page with no matching row is an observation of ABSENCE.
"""
from __future__ import annotations

from blm_v4.result_reconciler import select_matching_row
from blm_v4.results_fetcher import parse_row


def _row(home, away, hs, as_, quarters="(22:25, 24:17, 40:34, 26:17)",
         date="24.09.2026", time="18:20", comp="Betual NBA (Virtual Matches)"):
    return {"competition": comp,
            "teams": [{"name": home, "score": str(hs)},
                      {"name": away, "score": str(as_)}],
            "details": f"{hs}:{as_} {quarters}",
            "date": date, "time": time}


HOME = "Trabzonspor"
AWAY = "Anadolu Efes Sk"


def _game(**kw) -> dict:
    g = {"source_game_id": "31013207", "classification": "BETUAL_NBA",
         "home_team": HOME, "away_team": AWAY,
         "first_seen_at": "2026-09-24T18:05:00.000000Z",
         "last_seen_at": "2026-09-24T18:44:34.000000Z"}
    g.update(kw)
    return g


def test_parse_row_reads_the_details_line_and_footer():
    parsed = parse_row(_row("A Team Virtual", "B Team Virtual", 101, 111))
    assert parsed["home_team"] == "A Team Virtual"
    assert parsed["away_team"] == "B Team Virtual"
    assert (parsed["home_score"], parsed["away_score"]) == (101, 111)
    assert parsed["final_total"] == 212
    assert len(parsed["quarter_scores"]) == 4
    assert parsed["parse_quality"] == "full"
    # footer DD.MM.YYYY + HH:MM -> ISO-ish 'YYYY-MM-DD HH:MM'
    assert parsed["start_time"] == "2026-09-24 18:20"


def test_parse_row_partial_render_is_partial_quality():
    parsed = parse_row(_row("A Team", "B Team", 66, 59,
                            quarters="(20:18, 21:19, 25:22)"))
    assert len(parsed["quarter_scores"]) == 3
    assert parsed["parse_quality"] == "partial"


def test_a_matching_row_with_consistent_time_is_selected():
    rows = [_row("Other Home", "Other Away", 0, 2, quarters="(0:0)",
                 time="15:00"),
            _row(f"{HOME} Virtual", f"{AWAY} Virtual", 101, 111,
                 time="18:05")]
    match = select_matching_row(rows, _game())
    assert match["matched"] is True
    assert match["total_rows"] == 2
    assert match["parsed"]["home_score"] == 101


def test_no_matching_row_is_absence_not_a_result():
    """The live case: the page renders a list that does not contain the
    game.  Nothing may be attached."""
    rows = [_row("NK Koprivnica", "NK Uskok", 0, 2, quarters="(0:0)",
                 time="15:00"),
            _row("FK Teteks 1953", "FK Rabotnicki Skopje", 0, 1,
                 quarters="(0:1)", time="13:30")]
    match = select_matching_row(rows, _game())
    assert match["matched"] is False
    assert match["parsed"] is None
    assert match["total_rows"] == 2 and match["candidate_rows"] == 0
    assert "not among the 2 result row" in match["reason"]


def test_empty_row_list_is_absence():
    match = select_matching_row([], _game())
    assert match["matched"] is False and match["parsed"] is None
    assert match["total_rows"] == 0


def test_same_teams_from_another_instance_is_rejected():
    """A replay league renders the SAME two teams repeatedly: a name match
    with an inconsistent fixture time is a different instance."""
    rows = [_row(f"{HOME} Virtual", f"{AWAY} Virtual", 101, 111,
                 time="19:14"),                    # 30 min after last_seen
            _row(f"{HOME} Virtual", f"{AWAY} Virtual", 88, 92, time="17:00")]
    match = select_matching_row(rows, _game())
    assert match["matched"] is False
    assert match["parsed"] is None
    assert match["candidate_rows"] == 2
    assert "another instance" in match["reason"]


def test_several_untimed_name_matches_are_ambiguous_and_rejected():
    rows = [_row(f"{HOME} Virtual", f"{AWAY} Virtual", 101, 111,
                 date=None, time=None),
            _row(f"{HOME} Virtual", f"{AWAY} Virtual", 88, 92,
                 date=None, time=None)]
    match = select_matching_row(rows, _game())
    assert match["matched"] is False
    assert "another instance" in match["reason"]


def test_a_single_untimed_name_match_is_accepted_by_name_only():
    rows = [_row(f"{HOME} Virtual", f"{AWAY} Virtual", 101, 111,
                 date=None, time=None)]
    match = select_matching_row(rows, _game())
    assert match["matched"] is True
    assert match["start_time_consistent"] is None
    assert match["parsed"]["home_score"] == 101


def test_swapped_rendering_is_selected_and_re_oriented():
    rows = [_row(f"{AWAY} Virtual", f"{HOME} Virtual", 111, 101, time="18:05")]
    match = select_matching_row(rows, _game())
    assert match["matched"] is True
    parsed = match["parsed"]
    assert parsed["orientation"] == "re-oriented"
    # the RECORD's home is the away-rendered team, so the scores swap back
    assert parsed["home_team"] == HOME
    assert (parsed["home_score"], parsed["away_score"]) == (101, 111)
    assert parsed["quarter_scores"] == [(25, 22), (17, 24), (34, 40),
                                        (17, 26)]


def test_a_row_with_only_one_team_is_skipped():
    rows = [{"competition": "X", "teams": [{"name": HOME, "score": "10"}],
             "details": "10:0 (0:0)", "date": "24.09.2026", "time": "18:05"}]
    match = select_matching_row(rows, _game())
    assert match["matched"] is False and match["candidate_rows"] == 0
