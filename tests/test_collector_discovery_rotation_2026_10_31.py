"""Discovery rotation on a PARTIAL board, not only an empty one.

2026-10-31: the collector sat at ``tracked=1`` for hours.  `board_diag` showed
``comps=1 [CYBER_2K26:Cyber Basketball. 2K26 Match:games=1]`` — the Betual
section absent from the parsed board — but `_ensure_discovery_page()` ran only
from the ``if not comps:`` branch, and the board was never empty.  Delegating
to it was not enough either: it caches the landing that carried ANY relevant
competition, so it re-picked the Cyber URL.  The fix lands on the MISSING
class's own competition URL, on a per-class cooldown.

One named test per requirement.
"""
from __future__ import annotations

import logging

from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector, competition_url


class Comp:
    def __init__(self, cls, games=1, display_name="x"):
        self.classification = cls
        self.games = [object()] * games
        self.display_name = display_name


def _collector():
    c = object.__new__(PokerBetCollector)
    c.comp_ids = {}
    c.gotos = []
    c.waited = 0
    c.expanded = 0

    def _goto(page, url):
        c.gotos.append(url)
        return True

    c._goto = _goto
    c._wait_panel = lambda page: setattr(c, "waited", c.waited + 1)
    c._expand_target_sections = lambda page: setattr(
        c, "expanded", c.expanded + 1)
    return c


def test_a_board_with_only_cyber_reports_betual_as_missing():
    c = _collector()
    missing = c._missing_discovery_classes([Comp(Classification.CYBER_2K26)])
    assert missing == [Classification.BETUAL_NBA]


def test_a_board_with_both_classes_reports_nothing_missing():
    c = _collector()
    comps = [Comp(Classification.CYBER_2K26), Comp(Classification.BETUAL_NBA)]
    assert c._missing_discovery_classes(comps) == []


def test_rotation_lands_on_the_MISSING_class_lobby(caplog):
    c = _collector()
    with caplog.at_level(logging.WARNING, logger="blm_v4.collector"):
        missing = c._maybe_rotate_for_missing_classes(
            None, [Comp(Classification.CYBER_2K26)])
    assert missing == [Classification.BETUAL_NBA]
    assert c.gotos == [competition_url(Classification.BETUAL_NBA, {})]
    assert "betual-nba" in c.gotos[0]
    assert c.waited == 1 and c.expanded == 1
    assert "board lacks BETUAL_NBA" in " ".join(
        r.getMessage() for r in caplog.records)


def test_rotation_does_not_navigate_when_the_board_is_complete():
    c = _collector()
    c._maybe_rotate_for_missing_classes(
        None, [Comp(Classification.CYBER_2K26), Comp(Classification.BETUAL_NBA)])
    assert c.gotos == []


def test_an_empty_board_is_left_to_the_existing_empty_tick_branch():
    """Empty → the caller's `if not comps:` path owns the navigation."""
    c = _collector()
    missing = c._maybe_rotate_for_missing_classes(None, [])
    assert missing == [Classification.CYBER_2K26, Classification.BETUAL_NBA]
    assert c.gotos == []


def test_the_rotation_is_cooled_down_per_class():
    c = _collector()
    comps = [Comp(Classification.CYBER_2K26)]
    c._maybe_rotate_for_missing_classes(None, comps)
    c._maybe_rotate_for_missing_classes(None, comps)
    c._maybe_rotate_for_missing_classes(None, comps)
    assert len(c.gotos) == 1          # 60 s cooldown, not one per tick


def test_a_navigation_failure_does_not_escape_the_tick():
    c = _collector()

    def _boom(page, url):
        raise RuntimeError("page gone")

    c._goto = _boom
    c._maybe_rotate_for_missing_classes(None, [Comp(Classification.CYBER_2K26)])
    # reaching here is the assertion


def test_the_guard_never_raises_on_a_malformed_competition():
    class Bogus:
        pass

    c = _collector()
    missing = c._missing_discovery_classes([Bogus()])
    assert missing == [Classification.CYBER_2K26, Classification.BETUAL_NBA]
