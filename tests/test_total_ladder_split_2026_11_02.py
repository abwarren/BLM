"""TOTAL-LADDER SPLIT GUARD (operator directive 2026-11-02).

The bookmaker sometimes leaves its OPENING line on the panel as an extra row
BENEATH the live line, and it stays constant for most of the game.  The
selector is price-aware and its tie-break is "the LOWER line is the main line",
so a stale row that happens to sit near 1.85 would win outright — the engine
would then alert and bet a market that no longer exists.

The operator's rule: a real ladder steps by at most 2 points between rows, so
when one reading shows two rows further apart than that, the reading must be
REFUSED — no line, no side, no price — and the bet aborted.

These tests pin, one requirement each:

  1. a single-line ladder is untouched
  2. a 2-point step is still a normal ladder (existing selection preserved)
  3. a 3-row ladder stepping 2.0 selects the middle line at 1.85 (unchanged)
  4. an opening line beneath the live line is REFUSED, even when its price is
     the more attractive one
  5. the refusal survives the PARSER — parse_event_view returns no line and
     names the rule, so nothing downstream can bet it
  6. the boundary is exact: exactly 2.0 points is allowed, 2.5 is refused
  7. a refused reading carries no side and no price (no fallback possible)
"""
from __future__ import annotations

from blm_v4.event_parser import (
    LADDER_MAX_ROW_GAP,
    ladder_split_gap,
    parse_event_view,
    select_total_market,
)


def L(line, over, under):
    return {"line": line, "over": over, "under": under}


# 1 ─────────────────────────────────────────────────────────────────
def test_single_line_ladder_is_untouched():
    sel = select_total_market([L(215.5, 1.70, 2.02)])
    assert (sel["line"], sel["side"], sel["price"]) == (215.5, "OVER", 1.70)
    assert sel["rule"] == "fallback_nearest_1.85"
    assert ladder_split_gap([L(215.5, 1.7, 2.02)]) is None


# 2/3 ───────────────────────────────────────────────────────────────
def test_two_point_steps_still_select_normally():
    sel = select_total_market([L(214.5, 1.62, 1.68), L(216.5, 1.88, 1.92)])
    assert (sel["line"], sel["side"], sel["price"]) == (216.5, "OVER", 1.88)
    assert sel["rule"] == "band_closest_to_1.85"
    assert ladder_split_gap([L(214.5, 0, 0), L(216.5, 0, 0)]) == 2.0

    three = [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)]
    sel3 = select_total_market(three)
    assert (sel3["line"], sel3["price"]) == (171.5, 1.85)


# 4 ─────────────────────────────────────────────────────────────────
def test_opening_line_below_the_live_line_is_refused_even_when_it_prices_better():
    """The stale row sits at 198.5 @ 1.85 (the target price); the live row is
    231.5 @ 1.90.  Before the guard the price rule picked the STALE line."""
    ladder = [L(198.5, 1.85, 1.85), L(231.5, 1.90, 1.80)]
    assert ladder_split_gap(ladder) == 33.0
    sel = select_total_market(ladder)
    assert sel["line"] is None, "the stale opening line must never be selected"
    assert sel["rule"] == "ladder_split_ambiguous"
    assert sel["ladder_gap"] == 33.0


# 5 ─────────────────────────────────────────────────────────────────
def _panel(total_rows: list[tuple[float, float, float]]) -> str:
    body = ["Cyber Basketball. 2K26 Matches", "4th Quarter", "09:46'",
            "Oklahoma City Thunder Cyber", "San Antonio Spurs Cyber",
            "100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46",
            "All Match Totals Handicaps Markets", "Total Points"]
    for line, over, under in total_rows:
        body += [f"{line}", f"{over}", f"{under}"]
    body += ["Points Handicap", "Oklahoma City Thunder Cyber"]
    return "\n".join(body)


def test_parser_refuses_the_split_ladder_and_names_the_rule():
    parsed = parse_event_view(_panel([(198.5, 1.85, 1.85), (231.5, 1.90, 1.80)]))
    total = parsed["total"]
    assert total is not None, "the section must still be PARSED — we refuse it, not skip it"
    assert total["first_line"] is None
    assert total["selection_rule"] == "ladder_split_ambiguous"
    assert total["ladder_gap"] == 33.0
    # the ladder itself is preserved for audit/research — untouched
    assert [r["line"] for r in total["ladder"]] == [198.5, 231.5]


def test_parser_still_resolves_a_normal_ladder():
    parsed = parse_event_view(_panel([(169.5, 1.50, 2.40), (171.5, 1.85, 1.85),
                                      (173.5, 2.40, 1.50)]))
    total = parsed["total"]
    assert total["first_line"] == 171.5
    assert total["selection_rule"] == "band_closest_to_1.85"
    assert total.get("ladder_gap") is None


# 6 ─────────────────────────────────────────────────────────────────
def test_gap_boundary_is_exact():
    assert LADDER_MAX_ROW_GAP == 2.0
    at_bound = select_total_market([L(210.5, 1.80, 1.95), L(212.5, 1.85, 1.85)])
    assert at_bound["line"] is not None, "exactly 2.0 points is a legal ladder"
    just_over = select_total_market([L(210.5, 1.80, 1.95), L(213.0, 1.85, 1.85)])
    assert just_over["line"] is None and just_over["ladder_gap"] == 2.5


# 7 ─────────────────────────────────────────────────────────────────
def test_refused_reading_carries_no_side_or_price():
    sel = select_total_market([L(170.5, 1.85, 1.85), L(200.5, 1.90, 1.80)])
    assert sel["line"] is None and sel["side"] is None and sel["price"] is None
    assert sel["over"] is None and sel["under"] is None
