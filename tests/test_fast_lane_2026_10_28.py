"""FAST LANE — one named test per requirement (2026-10-28).

The lane exists because a TC-X moment lasts a median of one captured row while
the served payload the poll worker decides on is 90-190 s old.  These tests pin
the contract: the lane changes WHEN the decision is taken, never WHAT is
decided — every leg, the market-freshness bound, the kill switch, the stake
limits and the one-bet-per-game rule still come from the shared executor.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.fast_lane import FastLaneWorker, first_refusal, legs
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED

SLUG = "betual-euroleague"
AVG_PACE = 4.2842          # the served competition reference for the league
#  the shape of the row that entered TC-X on 2026-10-28 11:43 SAST
GOOD = dict(progress=82.5, total=161.0, req=5.0714, act=4.8788,
            line=196.5, price=1.90)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _clean_db(tmp_path, rows) -> str:
    """A clean_observations table carrying only the columns the lane reads."""
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


def _row(age_s=5.0, market_age_s=5.0, game="31380565", **over):
    now = datetime.now(timezone.utc)
    v = dict(GOOD)
    v.update(over)
    return (game, _iso(now - timedelta(seconds=age_s)), v["total"],
            v["progress"], v["req"], v["act"], v["line"], v["price"],
            _iso(now - timedelta(seconds=market_age_s)), "VALID", 0)


def _cfg(tmp_path) -> BettingConfig:
    return BettingConfig(dry_run=True, max_stake_per_bet=100.0,
                         max_bets_per_day=5, max_daily_exposure=None,
                         stake_units=1.0, min_unit_price=1.0,
                         max_unit_price=1000.0, alert_max_age_s=90.0,
                         db_path=str(tmp_path / "blm_betting.db"))


def _worker(tmp_path, rows, *, cfg=None, enabled=True):
    clean = _clean_db(tmp_path, rows)
    store = BettingStore(str(tmp_path / "blm_betting.db"))
    store.set_config(KEY_AUTO_ENABLED, "true" if enabled else "false")
    store.set_unit_price(10.0)
    w = FastLaneWorker(prod_db_path=tmp_path / "blm_pokerbet.db",
                       clean_db_path=Path(clean), cfg=cfg or _cfg(tmp_path),
                       store=store, provider=None, interval_s=3.0,
                       lookback_s=120.0, log=lambda *a, **k: None)
    w._slug_for = lambda source_game_id: SLUG   # no games table in the tmp DB
    w._pace_reference = lambda: {SLUG: {"avg_pace": AVG_PACE, "games": 500}}
    return w, store


# ── 1. the legs ───────────────────────────────────────────────────────
def test_legs_report_the_failing_leg_and_pass_when_all_hold(monkeypatch):
    # production runs the strict TC-X rung (unit file: TRADE_MIN_PACE_RATIO=1.16)
    import blm_v4.trade_window as tw
    monkeypatch.setattr(tw, "TRADE_MIN_PACE_RATIO", 1.16, raising=False)
    ok = legs(required_pace=GOOD["req"], avg_pace=AVG_PACE,
              actual_pace=GOOD["act"], progress_pct=GOOD["progress"],
              total_points=GOOD["total"], under_odds=GOOD["price"],
              competition_slug=SLUG)
    assert first_refusal(ok) is None

    cases = {
        # ratio 5.07 / 4.60 = 1.10 < the 1.16 trade rung
        "pace": dict(avg_pace=4.60),
        # required - actual = -1.2, below the -0.5 margin
        "shortfall": dict(actual_pace=GOOD["req"] + 1.2),
        "window": dict(progress_pct=67.5),
        "score": dict(total_points=64.0),
        "price": dict(under_odds=1.50),
        "competition": dict(competition_slug="cyber-basketball-2k26-matches"),
    }
    for name, over in cases.items():
        kw = dict(required_pace=GOOD["req"], avg_pace=AVG_PACE,
                  actual_pace=GOOD["act"], progress_pct=GOOD["progress"],
                  total_points=GOOD["total"], under_odds=GOOD["price"],
                  competition_slug=SLUG)
        kw.update(over)
        lmap = legs(**kw)
        assert lmap[name] is not None, \
            f"{name} leg must refuse, got {lmap}"
        assert first_refusal(lmap) is not None


# ── 2. only fresh, in-band rows are read ──────────────────────────────
def test_fresh_rows_drops_out_of_band_stale_and_invalid_rows(tmp_path):
    rows = [
        _row(),                                            # kept
        _row(game="1", progress=67.5),                     # below the band
        _row(game="2", progress=94.0),                     # above the band
        _row(game="3", age_s=600),                         # outside lookback
        _row(game="4", price=None),                        # no price
        _row(game="5", total=40.0),                        # score floor
        ("6", _iso(datetime.now(timezone.utc)), 160.0, 80.0,
         5.0, 4.8, 195.5, 1.9, _iso(datetime.now(timezone.utc)),
         "INVALID", 0),                                    # not VALID
    ]
    w, _ = _worker(tmp_path, rows)
    got = {r["source_game_id"] for r in w.fresh_rows()}
    assert got == {"31380565"}


# ── 3. a qualifying row reaches the SHARED executor ───────────────────
def test_qualifying_row_reaches_the_shared_executor_as_would_bet(tmp_path):
    w, store = _worker(tmp_path, [_row()])
    out = w.pass_once()
    assert out["armed"] == 1 and out["would_bet"] == 1
    rows = store.today_stats()
    assert rows["bets"] == 1, "the claim must be recorded on the ledger"


# ── 4. the kill switch still governs ──────────────────────────────────
def test_kill_switch_off_means_no_bet(tmp_path):
    w, store = _worker(tmp_path, [_row()], enabled=False)
    out = w.pass_once()
    assert out["would_bet"] == 0 and out["executed"] == 0
    assert store.today_stats()["bets"] == 0


# ── 5. a stale market is refused before the executor ──────────────────
def test_stale_market_is_refused(tmp_path):
    w, store = _worker(tmp_path, [_row(market_age_s=900)])
    out = w.pass_once()
    assert out["armed"] == 0
    assert out["refused"].get("eligibility") == 1
    assert store.today_stats()["bets"] == 0


# ── 6. one bet per game, even when the moment persists ────────────────
def test_one_bet_per_game_across_passes(tmp_path):
    w, store = _worker(tmp_path, [_row()])
    first = w.pass_once()
    second = w.pass_once()
    assert first["would_bet"] == 1
    assert second["would_bet"] == 0 and second["executed"] == 0
    assert store.today_stats()["bets"] == 1
