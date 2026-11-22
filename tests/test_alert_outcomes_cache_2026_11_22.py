"""OUTCOME BLOCK CACHE — /api/v4/alert-outcomes must not rescan a settled game.

Every id costs one ``_load_snapshot_tail`` (500 rows, JOIN + temp b-tree).
Measured on prod 2026-11-22: ~0.85 s per game, ~170 s for the panel's 200 ids;
the panel re-asked on every poll, so a few dashboard tabs filled the anyio
thread pool and /static/* starved (the dashboard could not load).

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_settled_block_is_scanned_once_and_served_identically
  * test_unsettled_block_is_re_used_inside_the_ttl_and_re_scanned_after_it
  * test_a_game_that_settles_is_recomputed_once_then_pinned
  * test_cache_is_keyed_by_the_database_path
  * test_unknown_ids_are_never_cached_or_invented
"""
from __future__ import annotations

import sqlite3

import pytest

import blm_v4.api as v4api


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE games (source_game_id TEXT, classification TEXT)")
    c.executemany("INSERT INTO games VALUES (?,?)",
                  [("g1", "BETUAL_NBA"), ("g2", "BETUAL_NBA")])
    return c


@pytest.fixture
def spy(monkeypatch):
    """The endpoint with its DB work instrumented; a cache cleared per test."""
    state = {"scans": 0, "computed": 0, "db": "/tmp/outcome-cache-test.db",
             "settled": {"g1": {"final_total": 180.0}}}

    monkeypatch.setattr(v4api, "_db_path", lambda: state["db"])
    monkeypatch.setattr(v4api, "_connect", lambda: _conn())
    monkeypatch.setattr(v4api, "_settled_result_map",
                        lambda ids: {i: state["settled"][i]
                                     for i in ids if i in state["settled"]})
    monkeypatch.setattr(v4api, "_projection_lines_map", lambda ids: {})

    def fake_tail(conn, gid, limit=25):
        state["scans"] += 1
        return [{"source_game_id": gid}]

    def fake_block(rows, cls, settled=None, projection_rows=None):
        state["computed"] += 1
        return {"gid": rows[0]["source_game_id"],
                "settled": settled is not None}

    monkeypatch.setattr(v4api, "_load_snapshot_tail", fake_tail)
    monkeypatch.setattr(v4api, "under_alert_outcome", fake_block)
    monkeypatch.setattr(v4api, "_OUTCOME_CACHE", {})
    monkeypatch.setenv("BLM_OUTCOME_CACHE_TTL_S", "30")
    return state


def test_settled_block_is_scanned_once_and_served_identically(spy):
    first = v4api.v4_alert_outcomes(ids="g1,g2")
    assert spy["scans"] == 2 and spy["computed"] == 2
    second = v4api.v4_alert_outcomes(ids="g1,g2")
    assert spy["scans"] == 2 and spy["computed"] == 2       # zero DB work
    assert second == first                                  # same blocks


def test_unsettled_block_is_re_used_inside_the_ttl_and_re_scanned_after_it(spy):
    v4api.v4_alert_outcomes(ids="g1,g2")
    v4api.v4_alert_outcomes(ids="g1,g2")                     # inside the TTL
    assert spy["scans"] == 2
    spy_ttl = "0"
    import os
    os.environ["BLM_OUTCOME_CACHE_TTL_S"] = spy_ttl          # force expiry
    try:
        v4api.v4_alert_outcomes(ids="g1,g2")
    finally:
        os.environ["BLM_OUTCOME_CACHE_TTL_S"] = "30"
    # only the UNSETTLED game (g2) is re-scanned; the settled one stays pinned
    assert spy["scans"] == 3 and spy["computed"] == 3


def test_a_game_that_settles_is_recomputed_once_then_pinned(spy):
    import os
    os.environ["BLM_OUTCOME_CACHE_TTL_S"] = "0"
    try:
        v4api.v4_alert_outcomes(ids="g2")                    # unsettled
        spy["settled"]["g2"] = {"final_total": 179.0}        # now settled
        got = v4api.v4_alert_outcomes(ids="g2")
        assert got["outcomes"]["g2"]["settled"] is True
        before = spy["scans"]
        v4api.v4_alert_outcomes(ids="g2")                    # pinned, no scan
        assert spy["scans"] == before
    finally:
        os.environ["BLM_OUTCOME_CACHE_TTL_S"] = "30"


def test_cache_is_keyed_by_the_database_path(spy):
    v4api.v4_alert_outcomes(ids="g1")
    assert spy["scans"] == 1
    spy["db"] = "/tmp/another-deployment.db"
    v4api.v4_alert_outcomes(ids="g1")
    assert spy["scans"] == 2                                 # never cross-served


def test_unknown_ids_are_never_cached_or_invented(spy):
    got = v4api.v4_alert_outcomes(ids="g1,ghost")
    assert set(got["outcomes"]) == {"g1"}
    v4api.v4_alert_outcomes(ids="ghost")
    assert spy["scans"] == 1                                 # ghost never scanned
