"""THE TRADE BAND IS CONFIGURABLE — route A (2026-11-17).

Two pins:
  1. the fast lane's ROW QUERY honours TRADE_MIN_PROGRESS_PCT / _MAX_ (it used to
     read the module constant 75 and a hard-coded 92, so an exported 60 never
     reached the lane);
  2. the qualification gate's progress band reads the same two settings, so the
     gate and the lane cannot disagree about which rows are eligible.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blm_v4 import trade_window as TW
from blm_v4.betting import market_heat, qualify
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.fast_lane import FastLaneWorker
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED

AVG = 4.0
SLUG = "betual-euroleague"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _clean_db(tmp_path, progress: float) -> str:
    path = str(tmp_path / "blm_metrics_clean.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE clean_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_game_id TEXT, captured_at TEXT, total_points REAL,
        progress_pct REAL, required_pts_per_min REAL, actual_pts_per_min REAL,
        live_total_line REAL, under_price REAL, market_captured_at TEXT,
        status TEXT, terminal INTEGER)""")
    now = datetime.now(timezone.utc)
    con.execute(
        "INSERT INTO clean_observations (source_game_id, captured_at, "
        "total_points, progress_pct, required_pts_per_min, actual_pts_per_min, "
        "live_total_line, under_price, market_captured_at, status, terminal) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("31380565", _iso(now - timedelta(seconds=5)), 161.0, progress,
         4.6, 4.5, 196.5, 1.90, _iso(now - timedelta(seconds=5)), "VALID", 0))
    con.commit()
    con.close()
    return path


def _worker(tmp_path, progress: float) -> FastLaneWorker:
    clean = _clean_db(tmp_path, progress)
    store = BettingStore(str(tmp_path / "blm_betting.db"))
    store.set_config(KEY_AUTO_ENABLED, "true")
    store.set_unit_price(10.0)
    w = FastLaneWorker(
        prod_db_path=tmp_path / "blm_pokerbet.db",
        clean_db_path=Path(clean),
        cfg=BettingConfig(dry_run=True, max_stake_per_bet=100.0,
                          max_bets_per_day=5, max_daily_exposure=None,
                          stake_units=1.0, min_unit_price=1.0,
                          max_unit_price=1000.0, alert_max_age_s=90.0,
                          db_path=str(tmp_path / "blm_betting.db")),
        store=store, provider=None, interval_s=3.0, lookback_s=120.0,
        log=lambda *a, **k: None)
    return w


def test_lane_row_query_reads_the_configured_band(tmp_path, monkeypatch):
    """A 62 % row is invisible at the old hard-coded 75 and visible at 60."""
    monkeypatch.setattr(market_heat, "refusal", lambda *a, **k: None)
    monkeypatch.setattr(TW, "TRADE_MIN_PROGRESS_PCT", 75.0, raising=False)
    monkeypatch.setattr(TW, "TRADE_MAX_PROGRESS_PCT", 92.0, raising=False)
    w = _worker(tmp_path, 62.0)
    assert w.fresh_rows() == []

    monkeypatch.setattr(TW, "TRADE_MIN_PROGRESS_PCT", 60.0, raising=False)
    assert len(w.fresh_rows()) == 1


def test_lane_row_query_honours_the_ceiling(tmp_path, monkeypatch):
    """93 % stays out even when the floor is 60 — the 92 cap is independent."""
    monkeypatch.setattr(market_heat, "refusal", lambda *a, **k: None)
    monkeypatch.setattr(TW, "TRADE_MIN_PROGRESS_PCT", 60.0, raising=False)
    monkeypatch.setattr(TW, "TRADE_MAX_PROGRESS_PCT", 92.0, raising=False)
    w = _worker(tmp_path, 93.0)
    assert w.fresh_rows() == []


def test_the_gate_band_reads_the_same_settings(monkeypatch):
    """qualify() must accept 62 % at a 60 floor and refuse it at 75."""
    monkeypatch.setattr(TW, "TRADE_MIN_PROGRESS_PCT", 60.0, raising=False)
    monkeypatch.setattr(TW, "TRADE_MAX_PROGRESS_PCT", 92.0, raising=False)
    ok, tier = qualify.qualifies_for_autobet(
        SLUG, 62.0, 4.6, 4.5, AVG, None, None, None, q3_ppm=None, shortfall=False)
    assert (ok, tier) == (True, "B")

    monkeypatch.setattr(TW, "TRADE_MIN_PROGRESS_PCT", 75.0, raising=False)
    ok, reason = qualify.qualifies_for_autobet(
        SLUG, 62.0, 4.6, 4.5, AVG, None, None, None, q3_ppm=None, shortfall=False)
    assert (ok, reason) == (False, qualify.PROGRESS_OUT_OF_BAND)
