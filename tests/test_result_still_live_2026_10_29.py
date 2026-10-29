"""A game the games table still lists as LIVE is never terminal.

MEASURED 2026-10-29 (operator report): tc-all settled 31394934 (Orlando Magic v
Denver Nuggets) as 177 — a MID-Q4 render, quarters 30:33 23:24 32:23 5:7 — at
16:40:17 while the game ran on past 218 points.

Why the existing guard missed it: `game_is_terminal` decides on SILENCE
(90 s since the last snapshot).  The snapshot stream for that game had been quiet
14 minutes — a collector gap, not an end of game — so the guard said "terminal"
and the render was accepted.

The games table is a second, independent witness and it outranks stream silence:
if it still says `live`, the game has not finished.
"""
from __future__ import annotations

from blm_v4.result_reconciler import ResultReconciler

QUIET_14_MIN = "2026-10-29T16:26:20.094081Z"
NOW = "2026-10-29T16:40:17.268888Z"


def _guard(game: dict):
    r = ResultReconciler.__new__(ResultReconciler)   # no DB, no I/O
    return r._premature_settlement_guard(game, NOW)


def test_a_live_game_is_refused_even_when_its_snapshot_stream_is_quiet():
    allowed, checks = _guard({"source_game_id": "31394934",
                              "status": "live",
                              "last_snap_at": QUIET_14_MIN})
    assert allowed is False, "a live game was allowed to settle"
    assert checks["terminal_guard"] == "game_still_live"
    assert checks["last_snapshot_at"] == QUIET_14_MIN


def test_an_ended_game_still_uses_the_silence_rule():
    allowed, checks = _guard({"source_game_id": "31394934",
                              "status": "ended",
                              "last_snap_at": QUIET_14_MIN})
    assert allowed is True
    assert checks["terminal_guard"] == "terminal"


def test_a_fresh_ended_game_is_still_refused():
    """status alone must not be enough — a fresh stream means it is still on."""
    allowed, checks = _guard({"source_game_id": "x", "status": "ended",
                              "last_snap_at": NOW})
    assert allowed is False
    assert checks["terminal_guard"] == "game_not_terminal"
