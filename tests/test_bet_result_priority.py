"""PLACED-BET RESULT PRIORITY (directive 2026-11-01) — a bet's game settles first.

Requirement: a game carrying a PLACED bet must be reconciled immediately, ahead of
the general backlog, so the win ladder can decide the NEXT rung instead of
re-deriving rung 1 while the result is still missing.

These tests drive the SHIPPED modules (`settle_worker.settle_once`,
`result_reconciler.ResultReconciler.candidate_games`, `SettleWorker.run_once`) —
no copies of the ordering logic — and pin:

  1. `pending_bet_game_ids()` returns the games of OPEN placed bets only
  2. it is FAIL-SOFT: no ledger / unreadable ledger -> ()
  3. it is bounded by the lookback window
  4. settle_once CHECKS the bet's game even when the backlog fills the batch, and
     does NOT without the priority (same fixture, both directions)
  5. with no priority ids the ordering is exactly the previous oldest-first rule
  6. candidate_games puts the bet's game ahead of a full newest-first backlog
  7. SettleWorker.run_once wires the ledger in — the game is checked end to end
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4 import settle_worker as sw
from blm_v4.bet_result_priority import (
    pending_bet_game_ids,
    priority_order_sql,
)
from blm_v4.models import PokerBetGame
from blm_v4.settle_worker import SettleWorker, settle_once

NOW = datetime.now(timezone.utc)
ISO = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")     # noqa: E731


# ── fixtures ──────────────────────────────────────────────────────────
def _pipeline(tmp_path: Path, n_games: int = 30) -> tuple[Path, dict]:
    """A real PokerBetStore DB with ``n_games`` ENDED, unresolved games.

    ``g00`` is the OLDEST (last_seen furthest back) and ``gNN`` the newest, so an
    oldest-first batch of 25 reaches g00..g24 and never the newest ones.
    """
    from blm_v4.storage import PokerBetStore
    db = tmp_path / "blm_pokerbet.db"
    store = PokerBetStore(db)
    ids = {}
    for i in range(n_games):
        gid = "g%02d" % i
        ended = NOW - timedelta(minutes=10 * (n_games - i))
        store.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="c", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team="Home Five",
            away_team="Away Six", game_slug="%s-game" % gid,
            source_url="https://x/%s" % gid, status="ended",
            first_seen_at=ISO(ended - timedelta(minutes=30)),
            last_seen_at=ISO(ended)))
        ids[gid] = gid
    return db, ids


def _ledger(tmp_path: Path, rows) -> Path:
    """A real BettingStore ledger with the given (game_id, status) rows."""
    from blm_v4.betting.store import BettingStore
    db = tmp_path / "blm_betting.db"
    store = BettingStore(str(db))
    for i, (gid, status) in enumerate(rows):
        ok, _ = store.claim({
            "execution_id": "e%03d" % i, "idempotency_key": "k%03d" % i,
            "game_id": gid, "alert_id": "a1", "checkpoint": "Q3",
            "triggered_line": 187.5, "price": 1.8, "unit_price": 150.0,
            "stake_units": 1.0, "stake_amount": 150.0, "status": status})
        assert ok, "fixture claim refused for %s" % gid
    return db


def _age_bet(db: Path, game_id: str, hours: float) -> None:
    when = (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE bet_executions SET requested_at_utc=? WHERE game_id=?",
                     (when, game_id))
        conn.commit()
    finally:
        conn.close()


def _checked(monkeypatch) -> list[str]:
    """Record which games a settle pass actually VISITS (verdict not needed)."""
    seen: list[str] = []

    def fake(conn, g):
        seen.append(g["source_game_id"])
        return None

    monkeypatch.setattr(sw, "_settle_game", fake)
    return seen


# ── 1/2/3 · the ledger read ──────────────────────────────────────────
def test_pending_bet_game_ids_returns_only_open_placed_bets(tmp_path):
    ledger = _ledger(tmp_path, [("g00", "SUBMITTED"), ("g01", "ACCEPTED"),
                                ("g02", "FAILED"), ("g03", "UNKNOWN")])
    assert pending_bet_game_ids(ledger) == ("g01", "g00")   # newest bet first
    assert "g02" not in pending_bet_game_ids(ledger)        # never placed
    assert "g03" not in pending_bet_game_ids(ledger)        # ambiguous


def test_pending_bet_game_ids_is_fail_soft_without_a_ledger(tmp_path):
    assert pending_bet_game_ids(None) == ()
    assert pending_bet_game_ids(tmp_path / "does-not-exist.db") == ()
    a_file = tmp_path / "not-a-db.txt"
    a_file.write_text("not sqlite")
    assert pending_bet_game_ids(a_file) == ()


def test_pending_bet_game_ids_respects_the_lookback_window(tmp_path):
    ledger = _ledger(tmp_path, [("g00", "SUBMITTED"), ("g01", "SUBMITTED")])
    _age_bet(ledger, "g00", 30.0)          # yesterday: backlog, not a live rung
    assert pending_bet_game_ids(ledger, lookback_h=24.0) == ("g01",)
    assert set(pending_bet_game_ids(ledger, lookback_h=48.0)) == {"g00", "g01"}


# ── 4/5 · settle_once ordering ───────────────────────────────────────
def test_settle_once_checks_the_bet_game_even_when_the_backlog_fills_the_batch(
        tmp_path, monkeypatch):
    "The bet sits on the NEWEST game, which an oldest-first batch of 25 skips."
    db, _ = _pipeline(tmp_path, 30)
    bet_db = _ledger(tmp_path, [("g29", "SUBMITTED")])

    baseline = _checked(monkeypatch)
    settle_once(db, batch_limit=25)
    assert "g29" not in baseline, "fixture must start without the bet's game"
    assert len(baseline) == 25

    with_priority = _checked(monkeypatch)
    settle_once(db, batch_limit=25,
                priority_ids=pending_bet_game_ids(bet_db))
    assert with_priority[0] == "g29", "the bet's game must be settled FIRST"
    assert len(with_priority) == 25, "priority must not widen the batch"


def test_settle_once_keeps_the_previous_order_without_priority_ids(
        tmp_path, monkeypatch):
    db, _ = _pipeline(tmp_path, 30)
    seen = _checked(monkeypatch)
    settle_once(db, batch_limit=25, priority_ids=())
    assert seen == ["g%02d" % i for i in range(25)], "oldest-first, unchanged"


def test_priority_order_sql_is_a_no_op_when_empty():
    assert priority_order_sql("g.source_game_id", "g.last_seen_at ASC", ()) == (
        "g.last_seen_at ASC", [])
    sql, params = priority_order_sql("g.source_game_id", "g.last_seen_at DESC",
                                     ["a", "b", "a"])
    assert params == ["a", "b"], "ids are deduped and ordered as given"
    assert sql.startswith("CASE WHEN g.source_game_id IN"), \
        "the CASE must test the GAME ID, never the ordering column"
    assert sql.endswith("g.last_seen_at DESC")


# ── 6 · reconciler candidates ────────────────────────────────────────
def _candidates(db: Path, priority=None):
    from blm_v4.result_reconciler import ResultReconciler
    try:
        # this branch predates the feed-contradiction guard (feed_db_path);
        # the check under test is candidate ORDERING, so either shape is fine.
        rec = ResultReconciler(db, feed_db_path=None, batch_limit=25)
    except TypeError:
        rec = ResultReconciler(db, batch_limit=25)
    conn = rec._connect()
    try:
        return [c["source_game_id"] for c in
                rec.candidate_games(conn, priority)]
    finally:
        conn.close()


def test_candidate_games_prioritises_the_bet_game_over_the_newest_backlog(tmp_path):
    "Newest-first means the bet's game (the OLDEST here) is normally cut off."
    db, _ = _pipeline(tmp_path, 30)
    bet_db = _ledger(tmp_path, [("g00", "SUBMITTED")])

    baseline = _candidates(db)
    assert "g00" not in baseline and len(baseline) == 25

    priority = pending_bet_game_ids(bet_db)
    with_priority = _candidates(db, priority)
    assert with_priority[0] == "g00"
    assert len(with_priority) == 25, "priority must not widen the batch"


# ── 7 · end-to-end wiring ────────────────────────────────────────────
def test_settle_worker_reads_the_ledger_and_prioritises_the_bet_game(
        tmp_path, monkeypatch):
    db, _ = _pipeline(tmp_path, 30)
    bet_db = _ledger(tmp_path, [("g29", "SUBMITTED")])
    seen = _checked(monkeypatch)

    SettleWorker(db, betting_db_path=bet_db).run_once()
    assert seen[0] == "g29"

    # no ledger wired = today's behaviour, unchanged
    seen.clear()
    SettleWorker(db).run_once()
    assert seen[0] == "g00"
