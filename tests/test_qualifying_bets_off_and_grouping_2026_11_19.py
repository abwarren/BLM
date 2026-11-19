"""QUALIFYING BETS — listed while Auto-Bet is OFF, one line per fixture (2026-11-19).

Operator directive: "dont have a line item for each entry -- one tab per unique
game id ... the other entries can be under a dropdown of that fixture" and
"also if autobetting was off -- the games that qualified shoulda also be listed
here".

Two contracts are pinned here:

  * the fast lane EVALUATES and RECORDS qualifications while the persisted
    Auto-Bet switch is OFF (reason ``autobet_off``) and never reaches the
    execution path, and
  * the dashboard panel renders ONE line per unique game id, with the remaining
    ledger entries under that fixture's dropdown.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.fast_lane import FastLaneWorker
from blm_v4.betting.store import KEY_AUTO_ENABLED, BettingStore

SLUG = "betual-euroleague"
AVG_PACE = 4.2842
GOOD = dict(progress=82.5, total=161.0, req=5.0714, act=4.8788,
            line=196.5, price=1.90)
DASHBOARD_JS = (Path(__file__).resolve().parents[1]
                / "blm_v4/dashboard/static/dashboard.js")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _clean_db(tmp_path, rows) -> str:
    path = str(tmp_path / "blm_metrics_clean.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE clean_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_game_id TEXT, captured_at TEXT, total_points REAL,
        progress_pct REAL, required_pts_per_min REAL, actual_pts_per_min REAL,
        live_total_line REAL, under_price REAL, market_captured_at TEXT,
        status TEXT, terminal INTEGER)""")
    for r in rows:
        con.execute(
            "INSERT INTO clean_observations (source_game_id, captured_at, "
            "total_points, progress_pct, required_pts_per_min, "
            "actual_pts_per_min, live_total_line, under_price, "
            "market_captured_at, status, terminal) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()
    return path


def _row(game="31380565", **over):
    now = datetime.now(timezone.utc)
    v = dict(GOOD)
    v.update(over)
    return (game, _iso(now - timedelta(seconds=5)), v["total"],
            v["progress"], v["req"], v["act"], v["line"], v["price"],
            _iso(now - timedelta(seconds=5)), "VALID", 0)


def _worker(tmp_path, rows, *, enabled):
    clean = _clean_db(tmp_path, rows)
    store = BettingStore(str(tmp_path / "blm_betting.db"))
    store.set_config(KEY_AUTO_ENABLED, "true" if enabled else "false")
    store.set_unit_price(10.0)
    cfg = BettingConfig(dry_run=True, max_stake_per_bet=100.0,
                        max_bets_per_day=5, max_daily_exposure=None,
                        stake_units=1.0, min_unit_price=1.0,
                        max_unit_price=1000.0, alert_max_age_s=90.0,
                        db_path=str(tmp_path / "blm_betting.db"))
    w = FastLaneWorker(prod_db_path=tmp_path / "blm_pokerbet.db",
                       clean_db_path=Path(clean), cfg=cfg,
                       store=store, provider=None, interval_s=3.0,
                       lookback_s=120.0, log=lambda *a, **k: None)
    w._slug_for = lambda source_game_id: SLUG
    w._pace_reference = lambda: {SLUG: {"avg_pace": AVG_PACE, "games": 500}}
    return w, store


# ── 1. OFF still LISTS the qualification (and never executes) ─────────
def test_autobet_off_records_the_qualified_game_as_not_placed(tmp_path):
    w, store = _worker(tmp_path, [_row()], enabled=False)
    out = w.pass_once()
    assert out["autobet_off"] is True
    assert out["armed"] == 1
    assert out["would_bet"] == 0 and out["executed"] == 0
    rows = store.candidates_recent(limit=10)
    assert len(rows) == 1, "the qualified game must be LISTED while OFF"
    assert rows[0]["game_id"] == "31380565"
    assert rows[0]["outcome"] == "NOT_PLACED"
    assert rows[0]["reason"] == "autobet_off"
    assert store.today_stats()["bets"] == 0, "OFF must never claim a bet"


def test_autobet_off_records_nothing_for_a_game_that_fails_a_leg(tmp_path):
    w, store = _worker(tmp_path, [_row(progress=67.5)], enabled=False)
    out = w.pass_once()
    assert out["autobet_off"] is True
    assert out["armed"] == 0
    assert store.candidates_recent(limit=10) == []


def test_autobet_off_keeps_the_truthful_already_placed_reason(tmp_path):
    w, store = _worker(tmp_path, [_row()], enabled=False)
    store.claim({"execution_id": "bet-off-1",
                 "idempotency_key": "31380565|75|31380565|75#r0",
                 "game_id": "31380565", "alert_id": "31380565|75#r0",
                 "checkpoint": "75", "status": "SUBMITTED",
                 "triggered_line": 196.5, "price": 1.9})
    w.pass_once()
    rows = store.candidates_recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["reason"] == "already_placed"


def test_autobet_on_still_executes_the_same_row(tmp_path):
    """Scope guard: the OFF listing must not change the ON path."""
    w, store = _worker(tmp_path, [_row()], enabled=True)
    out = w.pass_once()
    assert out["autobet_off"] is False
    assert out["armed"] == 1 and out["would_bet"] == 1
    assert store.today_stats()["bets"] == 1
    reasons = {r["reason"] for r in store.candidates_recent(limit=10)}
    assert "autobet_off" not in reasons


# ── 2. the dashboard groups one line per unique game id ───────────────
def test_panel_groups_candidates_by_unique_game_id():
    src = DASHBOARD_JS.read_text(encoding="utf-8")
    start = src.index("async function loadCandidates()")
    end = src.index("const _abCandRefresh", start)
    block = src[start:end]
    # grouping happens before rendering, keyed on the game id
    assert "const byGame = new Map();" in block
    assert 'const gid = String(r.game_id || "");' in block
    assert "byGame.set(gid, [])" in block
    # one <li>/<details> per group, and the other entries under a dropdown
    assert "groups.map(" in block
    assert "<details>" in block and "<summary" in block
    assert '<ul class="ab-cand-entries">' in block
    # the header counts GAMES, not ledger rows
    assert "todayGames.size" in block
    # PLACED wins the fixture's headline row, then the staked attempt
    assert "placed || staked || list[0]" in block
