"""Live-panel discovery coverage (2026-10-03).

INCIDENT: only ~6 games were ever tracked while the live panel rendered ~46
relevant games across ~6 competitions, so resolution, snapshots and the
downstream quarter-score capture were starved.  Games visible in the WS feed
(e.g. 31103377 Minnesota Timberwolves Virtual vs Dallas Mavericks Virtual)
never became tracked games.

ROOT CAUSE: commit 0561ea2 (2026-09-27) re-indented the per-row discovery
block OUT of the ``for row in comp.games:`` loop up to the ``for comp``
level.  Every row's canonicalisation / seen-key / resolve-queue / snapshot
work then ran ONCE PER COMPETITION on the leaked last ``row``, capping
discovery at one game per competition (observed: ``games_tracked=6``,
``pending_resolve=1`` against a 46-row panel).

These tests pin the fix: EVERY row of EVERY competition is processed.
"""
from __future__ import annotations

import threading
import types

import pytest

from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector
from blm_v4.discovery import DiscoveredCompetition, RowGame
from blm_v4.models import PokerBetGame


def _rows(*pairs: tuple[str, str]) -> list[RowGame]:
    return [RowGame(home_team=h, away_team=a) for h, a in pairs]


def _comps() -> list[DiscoveredCompetition]:
    """A panel shaped like production: several competitions, several games
    each (production: 12/8/7/5/5 Betual + 3 Cyber)."""
    return [
        DiscoveredCompetition(
            display_name="Betual NBA", classification=Classification.BETUAL_NBA,
            games=_rows(
                ("Houston Rockets Virtual", "Charlotte Hornets Virtual"),
                ("Golden State Warriors Virtual", "San Antonio Spurs Virtual"),
                ("Minnesota Timberwolves Virtual", "Dallas Mavericks Virtual"),
            ),
        ),
        DiscoveredCompetition(
            display_name="Betual TBSL", classification=Classification.BETUAL_NBA,
            games=_rows(
                ("Aliaga Petkim Spor Virtual", "Anadolu Efes SK Virtual"),
                ("Trabzonspor Virtual", "Fenerbahce Virtual"),
                ("Besiktas JK Virtual", "Buyukcekmece SK Virtual"),
            ),
        ),
        DiscoveredCompetition(
            display_name="Cyber Basketball. 2K26 Matches",
            classification=Classification.CYBER_2K26,
            games=_rows(
                ("Phoenix Suns Cyber", "San Antonio Spurs Cyber"),
                ("Brooklyn Nets Cyber", "New Orleans Pelicans Cyber"),
                ("Dallas Mavericks Cyber", "Boston Celtics Cyber"),
            ),
        ),
    ]


class _Stub:
    """Minimal self for ``_discover_panel_rows`` — no browser, no store."""

    def __init__(self, tracked=None):
        self._track_lock = threading.RLock()
        self._tracked = tracked or {
            Classification.BETUAL_NBA.value: {},
            Classification.CYBER_2K26.value: {},
        }
        self.queued: list[tuple[str, str, str]] = []
        self._canonical_teams = PokerBetCollector._canonical_teams

    def _queue_resolve(self, cls, row):
        self.queued.append((cls.value, row.home_team, row.away_team))


def test_every_row_of_every_competition_is_processed():
    """9 rows across 3 competitions ⇒ 9 seen keys and 9 resolve requests.
    The 2026-09-27 regression produced 3 (one per competition)."""
    stub = _Stub()
    seen, to_snap = PokerBetCollector._discover_panel_rows(stub, _comps())

    total_rows = sum(len(c.games) for c in _comps())
    assert total_rows == 9
    assert sum(len(v) for v in seen.values()) == total_rows
    assert len(stub.queued) == total_rows, (
        "each NEW row must queue its own resolve request — got "
        f"{len(stub.queued)}, expected {total_rows}")
    assert to_snap == []          # nothing tracked yet


def test_non_last_row_still_queues_its_resolve_request():
    """The exact 31103377 shape: a game that is NOT the last row of its
    competition must still be discovered."""
    stub = _Stub()
    PokerBetCollector._discover_panel_rows(stub, _comps())
    names = [(h, a) for _, h, a in stub.queued]
    assert ("Minnesota Timberwolves", "Dallas Mavericks") in names, (
        f"Minnesota/Dallas (mid-list, not last) was skipped: {names}")


def test_seen_keys_cover_every_row_so_mark_ended_keeps_them():
    """``seen_keys`` is the ``_mark_ended`` denominator: a row missing from
    it is treated as vanished and its tracked game is closed."""
    stub = _Stub()
    seen, _ = PokerBetCollector._discover_panel_rows(stub, _comps())
    assert seen[Classification.BETUAL_NBA.value] == {
        "Houston Rockets|Charlotte Hornets",
        "Golden State Warriors|San Antonio Spurs",
        "Minnesota Timberwolves|Dallas Mavericks",
        "Aliaga Petkim Spor|Anadolu Efes SK",
        "Trabzonspor|Fenerbahce",
        "Besiktas JK|Buyukcekmece SK",
    }
    assert seen[Classification.CYBER_2K26.value] == {
        "Phoenix Suns Cyber|San Antonio Spurs Cyber",
        "Brooklyn Nets Cyber|New Orleans Pelicans Cyber",
        "Dallas Mavericks Cyber|Boston Celtics Cyber",
    }


def test_tracked_rows_are_snapshotted_not_re_resolved():
    game = PokerBetGame(source_game_id="31103468",
                        classification=Classification.BETUAL_NBA.value,
                        home_team="Zhejiang Golden Bulls",
                        away_team="Ningbo Rockets")
    stub = _Stub(tracked={
        Classification.BETUAL_NBA.value: {
            "Zhejiang Golden Bulls|Ningbo Rockets": game},
        Classification.CYBER_2K26.value: {},
    })
    comps = [DiscoveredCompetition(
        display_name="Betual CBA", classification=Classification.BETUAL_NBA,
        games=_rows(("Zhejiang Golden Bulls Virtual", "Ningbo Rockets Virtual"),
                    ("Shanxi Loongs Virtual", "Shanghai Sharks Virtual")))]
    seen, to_snap = PokerBetCollector._discover_panel_rows(stub, comps)

    # the tracked row is snapshotted (canonicalised key matches)...
    assert len(to_snap) == 1
    assert to_snap[0][0] is game
    # ...and the untracked one is queued for resolution
    assert len(stub.queued) == 1
    assert stub.queued[0][1:] == ("Shanxi Loongs", "Shanghai Sharks")
    assert len(seen[Classification.BETUAL_NBA.value]) == 2


def test_canonicalisation_is_applied_per_row():
    """Betual's "Virtual" presentation marker is stripped for EVERY row, not
    just the last one — the tracked key and the panel key must agree."""
    stub = _Stub()
    seen, _ = PokerBetCollector._discover_panel_rows(stub, _comps())
    for keys in seen.values():
        for k in keys:
            assert "Virtual" not in k, f"uncanonicalised key: {k}"


def test_empty_competition_is_tolerated():
    stub = _Stub()
    seen, to_snap = PokerBetCollector._discover_panel_rows(
        stub, [DiscoveredCompetition(
            display_name="Empty", classification=Classification.BETUAL_NBA,
            games=[])])
    assert seen == {Classification.BETUAL_NBA.value: set()}
    assert to_snap == [] and stub.queued == []


def test_discovery_is_wired_into_the_tick():
    """The tick must consume the helper's return values (a regression that
    drops them would silently disable discovery again)."""
    import inspect
    src = inspect.getsource(PokerBetCollector._tick_body)
    assert "_discover_panel_rows" in src
    assert "seen_keys" in src and "to_snapshot" in src
