"""A render is refused when our own collector scored HIGHER than the "final".

MEASURED 2026-10-29: 1,562 of 38,824 stored OK results were contradicted by the
feed — the stored "final" was a mid-game score (one was stamped 200 while the
feed ran to 267).  A too-low final always wins an UNDER, so all affected TC-W
bets were recorded as wins.

The collector is the second witness: it can REFUSE a render it disproves and can
never invent one, so every unprovable case fails open.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blm_v4.result_reconciler import ResultReconciler


def _feed(tmp_path: Path, rows: dict[str, int]) -> Path:
    db = tmp_path / "blm_metrics_clean.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE clean_observations(source_game_id TEXT, total_points INT)")
    conn.executemany("INSERT INTO clean_observations VALUES (?,?)", list(rows.items()))
    conn.commit()
    conn.close()
    return db


def _recon(tmp_path: Path, feed: Path | None) -> ResultReconciler:
    return ResultReconciler(tmp_path / "prod.db", feed_db_path=feed)


def test_a_render_below_the_feed_peak_is_refused(tmp_path):
    feed = _feed(tmp_path, {"g1": 267})
    r = _recon(tmp_path, feed)
    conn = sqlite3.connect(tmp_path / "prod.db")

    ok, checks = r._feed_contradiction(conn, "g1", 200)

    assert ok is False, "a 200 'final' was accepted for a game the feed took to 267"
    assert checks["feed_contradiction"] == "render_below_feed"
    assert checks["feed_peak"] == 267
    conn.close()


def test_a_render_at_or_above_the_peak_passes(tmp_path):
    feed = _feed(tmp_path, {"g1": 221})
    r = _recon(tmp_path, feed)
    conn = sqlite3.connect(tmp_path / "prod.db")

    assert r._feed_contradiction(conn, "g1", 221)[0] is True
    assert r._feed_contradiction(conn, "g1", 230)[0] is True
    # the 2-point slack absorbs a rounding/period artefact, not a live game
    assert r._feed_contradiction(conn, "g1", 219)[0] is True
    conn.close()


def test_a_never_observed_game_is_unaffected(tmp_path):
    feed = _feed(tmp_path, {"g1": 100})
    r = _recon(tmp_path, feed)
    conn = sqlite3.connect(tmp_path / "prod.db")

    ok, checks = r._feed_contradiction(conn, "never_seen", 180)
    assert ok is True
    assert checks["feed_contradiction"] == "no_observations"
    conn.close()


def test_the_guard_fails_open_when_the_feed_is_unavailable(tmp_path):
    r = _recon(tmp_path, None)          # no feed configured
    conn = sqlite3.connect(tmp_path / "prod.db")

    ok, checks = r._feed_contradiction(conn, "g1", 100)
    assert ok is True and checks["feed_contradiction"] == "disabled"

    missing = _recon(tmp_path, tmp_path / "does_not_exist.db")
    ok, checks = missing._feed_contradiction(conn, "g1", 100)
    assert ok is True and checks["feed_contradiction"] == "unavailable"
    conn.close()


def test_persist_result_refuses_and_writes_nothing(tmp_path, monkeypatch):
    """End to end on the write path: a contradicted render never lands."""
    feed = _feed(tmp_path, {"g1": 267})
    r = _recon(tmp_path, feed)
    conn = sqlite3.connect(tmp_path / "prod.db")
    conn.execute("CREATE TABLE IF NOT EXISTS game_results(source_game_id TEXT PRIMARY KEY,"
                 " classification TEXT, final_home INT, final_away INT, final_total INT,"
                 " result_at TEXT, final_result_status TEXT, result_source TEXT)")
    conn.commit()
    conn.close()

    game = {"source_game_id": "g1", "classification": "BETUAL_NBA", "status": "ended"}
    parsed = {"home_score": 100, "away_score": 100, "final_total": 200,
              "quarter_scores": []}
    rec = {"outcome": "VERIFIED", "checks": {}, "fetched_at": "2026-10-29T17:00:00Z"}

    assert r._persist_result(game, parsed, rec) is False

    conn = sqlite3.connect(tmp_path / "prod.db")
    n = conn.execute("SELECT COUNT(*) FROM game_results").fetchone()[0]
    conn.close()
    assert n == 0, "a contradicted render was written as the result"
