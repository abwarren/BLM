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
    monkeypatch.setattr(slate_mod.market_heat, "history",
                        lambda *a, **k: [])
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


# ── 6. the history strip: the banner's number, day by day ─────────────
def test_daily_aggregates_the_same_definition_per_day():
    pairs = ([("2026-11-13", 210.0, 200.0)] * 3
             + [("2026-11-13", 200.0, 205.0)] * 2)
    rows = market_heat.daily(pairs, days=2, today="2026-11-14",
                             threshold=0.45, min_games_n=5)
    assert [r["day"] for r in rows] == ["2026-11-13", "2026-11-14"]
    assert [r["label"] for r in rows] == ["11-13", "11-14"]
    assert rows[0]["over"] == 3 and rows[0]["n"] == 5
    assert rows[0]["share"] == pytest.approx(0.6)
    assert rows[0]["tripped"] is True          # 60 % >= 45 % with 5 games
    assert rows[0]["today"] is False and rows[1]["today"] is True


def test_a_day_with_no_settled_game_keeps_its_slot():
    rows = market_heat.daily([("2026-11-14", 210.0, 200.0)],
                             days=3, today="2026-11-14")
    assert [r["share"] for r in rows] == [None, None, 1.0]
    assert rows[1]["n"] == 0 and rows[1]["tripped"] is False
    assert [r["today"] for r in rows] == [False, False, True]


def test_pushes_are_not_over_and_a_thin_day_never_reads_as_hot():
    rows = market_heat.daily([("2026-11-14", 205.0, 205.0)] * 3,
                             days=1, today="2026-11-14",
                             threshold=0.45, min_games_n=20)
    assert rows[0]["n"] == 3 and rows[0]["over"] == 0
    assert rows[0]["tripped"] is False
    # same sample, but it clears the minimum: a push-only day is 0 %, not hot
    assert market_heat.daily([("2026-11-14", 205.0, 205.0)] * 20,
                             days=1, today="2026-11-14",
                             min_games_n=20)[0]["tripped"] is False


def test_history_is_cached_and_reads_fail_open(monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return [("2026-11-15", 210.0, 200.0)]

    monkeypatch.setattr(market_heat, "samples_by_day", fake)
    first = market_heat.history(days=1, now=now, refresh=True)
    second = market_heat.history(days=1, now=now + 1)
    assert calls["n"] == 1                     # the second read hit the cache
    assert first == second
    assert first[-1]["over"] == 1 and first[-1]["today"] is True

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(market_heat, "samples_by_day", boom)
    assert market_heat.history(days=1, now=now, refresh=True) == []


def test_the_slate_ships_the_history_without_polluting_the_cached_state(
        monkeypatch):
    from blm_v4.betting import slate as slate_mod
    monkeypatch.setattr(slate_mod, "_live_rows", lambda *a, **k: [_live_row()])
    monkeypatch.setattr(slate_mod, "_slugs", lambda *a, **k: {"31380565": SLUG})
    monkeypatch.setattr(slate_mod, "_pace_reference",
                        lambda *a, **k: {SLUG: {"avg_pace": AVG_PACE}})
    cached = {"enabled": True, "tripped": False, "reason": None,
              "threshold": 0.45, "min_games": 20, "overshare": 0.30,
              "n": 30, "over": 9, "text": "9/30"}
    rows = [{"day": "2026-11-14", "label": "11-14", "over": 40, "n": 82,
             "share": 0.49, "today": False, "tripped": True}]
    monkeypatch.setattr(slate_mod.market_heat, "state", lambda *a, **k: cached)
    monkeypatch.setattr(slate_mod.market_heat, "history", lambda *a, **k: rows)
    d = slate_mod.build_slate("x", "y")
    assert d["gate"]["market_heat"]["history"] == rows
    assert "history" not in cached             # the module cache stays clean


def test_the_dashboard_draws_the_strip_next_to_the_banner():
    html = (Path(__file__).resolve().parents[1]
            / "blm_v4/dashboard/static/slate.html").read_text()
    assert 'id="heatStrip"' in html
    assert "OVER SHARE" in html
    assert "market_heat" in html and "history" in html
    # the strip must be built from the payload, never recomputed in the page
    assert "heat.history" in html and "hs-fill" in html
