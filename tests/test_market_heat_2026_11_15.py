"""MARKET-HEAT GUARDRAIL — one named test per requirement (2026-11-15).

The guardrail exists because the 70-92 cohort's win rate fell from 74.2 % to
59.6 % while every PER-GAME input stayed put: the market itself went hot (share
of games finishing OVER the last line 26 % → 40 %).  So the pause is GLOBAL:
it must refuse arming for every game, be visible in the served payload and the
dashboard, and still hold at the submission boundary.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.betting import market_heat
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.fast_lane import FastLaneWorker
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED

SLUG = "betual-euroleague"
AVG_PACE = 4.2842
GOOD = dict(progress=82.5, total=161.0, req=5.0714, act=4.8788,
            line=196.5, price=1.90)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


# ── 1. the measurement itself ─────────────────────────────────────────
def test_overshare_counts_over_games_and_ignores_pushes():
    agg = market_heat.overshare([(210.0, 205.0), (200.0, 205.0),
                                 (205.0, 205.0), (None, 205.0)])
    assert agg == {"n": 3, "over": 1, "share": pytest.approx(1 / 3)}


def test_overshare_of_nothing_is_unknown_not_zero():
    assert market_heat.overshare([]) == {"n": 0, "over": 0, "share": None}


# ── 2. trip / no-trip / insufficient sample / off ─────────────────────
def _fixed(pairs):
    return lambda *a, **k: pairs


def test_trips_when_the_over_share_reaches_the_threshold(monkeypatch):
    monkeypatch.setattr(market_heat, "samples",
                        _fixed([(210.0, 200.0)] * 10 + [(200.0, 210.0)] * 15))
    st = market_heat.state(refresh=True)
    assert st["n"] == 25 and st["overshare"] == pytest.approx(0.40)
    assert st["tripped"] is False          # 40 % < 45 %

    monkeypatch.setattr(market_heat, "samples",
                        _fixed([(210.0, 200.0)] * 15 + [(200.0, 210.0)] * 10))
    st = market_heat.state(refresh=True)
    assert st["tripped"] is True and st["overshare"] == pytest.approx(0.60)
    assert st["reason"].startswith("unders_beaten_60pct_of_25")
    assert "OVER" in st["text"]


def test_a_thin_sample_never_pauses_trading(monkeypatch):
    monkeypatch.setattr(market_heat, "samples", _fixed([(210.0, 200.0)] * 5))
    st = market_heat.state(refresh=True)
    assert st["n"] == 5 and st["tripped"] is False
    assert "insufficient sample" in st["text"]


def test_the_guardrail_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("TRADE_MARKET_HEAT_ENABLED", "0")
    monkeypatch.setattr(market_heat, "samples",
                        _fixed([(210.0, 200.0)] * 40))
    st = market_heat.state(refresh=True)
    assert st["enabled"] is False and st["tripped"] is False
    assert market_heat.refusal() is None


def test_a_broken_read_fails_open_with_the_error_visible(monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(market_heat, "samples", boom)
    st = market_heat.state(refresh=True)
    assert st["tripped"] is False and "locked" in st["error"]
    assert "unavailable" in st["text"]


def test_refusal_is_shaped_like_a_leg_reason(monkeypatch):
    monkeypatch.setattr(market_heat, "samples",
                        _fixed([(210.0, 200.0)] * 30))
    market_heat.state(refresh=True)       # other tests share the 120 s cache
    why = market_heat.refusal()
    assert why.startswith("market_heat:")
    assert why.split(":")[0] == "market_heat"


# ── 3. the submission boundary ────────────────────────────────────────
def test_revalidation_refuses_on_heat_even_with_revalidation_off(monkeypatch):
    from blm_v4.betting import revalidate
    monkeypatch.delenv("BET_REVALIDATE", raising=False)
    cand = {"game_id": "1", "price": 1.9}
    code, why = revalidate.revalidate(cand, heat=True)
    assert code == "REVALIDATION_MARKET_HEAT"
    assert "market-heat" in why
    assert revalidate.revalidate(cand, heat=False) is None


# ── 4. the lane arms nothing while the market is hot ──────────────────
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


def _games_row(tmp_path, game="31380565"):
    now = datetime.now(timezone.utc)
    return (game, _iso(now - timedelta(seconds=5)), GOOD["total"],
            GOOD["progress"], GOOD["req"], GOOD["act"], GOOD["line"],
            GOOD["price"], _iso(now - timedelta(seconds=5)), "VALID", 0)


def _worker(tmp_path, rows):
    clean = _clean_db(tmp_path, rows)
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
    w._slug_for = lambda source_game_id: SLUG
    w._pace_reference = lambda: {SLUG: {"avg_pace": AVG_PACE, "games": 500}}
    return w


def test_lane_pauses_every_game_while_the_market_is_hot(tmp_path, monkeypatch):
    monkeypatch.setattr(market_heat, "refusal",
                        lambda *a, **k: "market_heat:unders_beaten_60pct_of_25")
    w = _worker(tmp_path, [_games_row(tmp_path)])
    out = w.pass_once()
    assert out["market_heat"] is True
    assert out["armed"] == 0 and out["would_bet"] == 0
    assert out["refused"].get("market_heat") == 1


def test_lane_arms_normally_when_the_market_is_cool(tmp_path, monkeypatch):
    monkeypatch.setattr(market_heat, "refusal", lambda *a, **k: None)
    w = _worker(tmp_path, [_games_row(tmp_path)])
    out = w.pass_once()
    assert out["market_heat"] is False
    assert out["armed"] == 1 and out["would_bet"] == 1


# ── 5. the slate + dashboard say so ───────────────────────────────────
def _live_row():
    now = datetime.now(timezone.utc)
    return {"source_game_id": "31380565", "captured_at": _iso(now),
            "total_points": GOOD["total"], "progress_pct": GOOD["progress"],
            "required_pts_per_min": GOOD["req"],
            "actual_pts_per_min": GOOD["act"],
            "live_total_line": GOOD["line"], "under_price": GOOD["price"],
            "remaining_game_minutes": 7.0, "classification": "BETUAL_NBA",
            "status": "VALID", "market_captured_at": _iso(now),
            "terminal": 0, "home_team": "A", "away_team": "B",
            "game_status": "live"}


def test_slate_publishes_the_pause_and_refuses_every_row(monkeypatch):
    from blm_v4.betting import slate as slate_mod
    monkeypatch.setattr(slate_mod, "_live_rows", lambda *a, **k: [_live_row()])
    monkeypatch.setattr(slate_mod, "_slugs", lambda *a, **k: {"31380565": SLUG})
    monkeypatch.setattr(slate_mod, "_pace_reference",
                        lambda *a, **k: {SLUG: {"avg_pace": AVG_PACE}})
    monkeypatch.setattr(slate_mod.market_heat, "state", lambda *a, **k: {
        "enabled": True, "tripped": True,
        "reason": "unders_beaten_61pct_of_33", "threshold": 0.58,
        "min_games": 20, "overshare": 0.61, "n": 33, "over": 20,
        "text": "20/33 recent settled games finished OVER the line"})
    d = slate_mod.build_slate("x", "y")
    assert d["gate"]["market_heat"]["tripped"] is True
    g = d["games"][0]
    assert g["armed"] is False and g["on_track"] is False
    assert g["refusal"].startswith("market_heat:")
    assert d["counts"]["armed"] == 0


def test_the_dashboard_shows_a_banner_and_a_heat_chip():
    html = (Path(__file__).resolve().parents[1]
            / "blm_v4/dashboard/static/slate.html").read_text()
    assert 'id="heatBanner"' in html
    assert "AUTO-BET PAUSED" in html
    assert "market_heat" in html
