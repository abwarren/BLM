"""AUTOBET STRENGTH × FINGERPRINT QUALIFICATION (directive 2026-11-15 §5.5).

One test per required case, plus the wiring contract: the gate runs in the
executor immediately before the claim, so the fast lane and the poll worker are
both subject to it, and a refusal never claims a slot.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.betting import market_heat, qualify
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.fast_lane import FastLaneWorker
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED

AVG = 4.0
SLUG = "betual-euroleague"
GOOD = dict(progress=82.5, total=161.0, req=5.0714, act=4.8788,
            line=196.5, price=1.90)


def call(*, req=4.8, act=None, pct=82.5, avg=AVG, avg_q3=4.0, q3=3.9,
         slug=SLUG, shortfall=None, total=None, minutes=None,
         q3_ppm_override=None):
    """Call the gate with a q3 pace expressed as total/minutes by default.

    ``act`` defaults to 0.2 below ``req`` so the shortfall leg (code default
    ON) never masks the condition a test is actually about.
    """
    if act is None:
        act = req - 0.2
    if total is None:
        total, minutes = ((q3 * 10.0, 10.0) if q3 is not None
                          else (None, None))
    kwargs = {} if q3_ppm_override is None else {"q3_ppm": q3_ppm_override}
    return qualify.qualifies_for_autobet(
        slug, pct, req, act, avg, avg_q3, total, minutes,
        shortfall=shortfall, **kwargs)


# ── §5.5 — the required cases ─────────────────────────────────────────
def test_tier_a_qualifies_without_fingerprint():
    """1.20×+ needs no Q3: 4.8/4.0 = exactly 1.20 is Tier A."""
    ok, tier = call(req=4.8, q3=None, avg_q3=None, total=None, minutes=None)
    assert (ok, tier) == (True, "A")


def test_tier_a_qualifies_when_a_fingerprint_fires():
    ok, tier = call(req=5.2, q3=3.5)
    assert (ok, tier) == (True, "A")


def test_tier_b_qualifies_when_c5_fires():
    """q3_ratio 0.975 < 1.00 and 1.10 < req_ratio < 1.20 → C5."""
    ok, tier = call(req=4.6, q3=3.9)
    assert (ok, tier) == (True, "B")


def test_tier_b_qualifies_when_r2_fires():
    """req_ratio exactly 1.10 keeps C5's strict >1.10 leg FALSE; the material
    Q3 slump (0.85) still fires C3 and R2, which is enough."""
    ok, tier = call(req=4.4, q3=3.4)
    assert (ok, tier) == (True, "B")


def test_tier_b_qualifies_without_a_fingerprint_route_a():
    """POLICY 2026-11-17 (route A): fingerprints are recorded context, not an
    admission gate — a Tier-B game with Q3 present and nothing firing trades."""
    ok, tier = call(req=4.6, q3=4.2)            # q3_ratio 1.05 → nothing fires
    assert (ok, tier) == (True, "B")


def test_tier_c_qualifies_even_when_q3_is_unavailable_route_a():
    """POLICY 2026-11-17 (route A): Tier C no longer needs confirmation."""
    ok, tier = call(req=4.3, q3=None, avg_q3=None, total=None, minutes=None)
    assert (ok, tier) == (True, "C")


def test_tier_c_qualifies_when_a_fingerprint_fires():
    ok, tier = call(req=4.3, q3=3.95)           # 0.9875 < 1.00 → C3
    assert (ok, tier) == (True, "C")


def test_cyber_2k26_is_always_blocked():
    for req in (4.2, 4.8, 6.0):
        ok, reason = call(slug="cyber-basketball-2k26-matches", req=req)
        assert (ok, reason) == (False, qualify.COMPETITION_EXCLUDED)


def test_missing_avg_pace_is_fail_closed():
    ok, reason = call(avg=None)
    assert (ok, reason) == (False, qualify.MISSING_INPUT)
    ok, reason = call(avg=0)
    assert (ok, reason) == (False, qualify.MISSING_INPUT)
    ok, reason = call(pct=None)
    assert (ok, reason) == (False, qualify.MISSING_INPUT)
    ok, reason = call(req=float("nan"))
    assert (ok, reason) == (False, qualify.MISSING_INPUT)
    ok, reason = call(act=float("inf"))
    assert (ok, reason) == (False, qualify.MISSING_INPUT)
    ok, reason = call(slug=None)
    assert (ok, reason) == (False, qualify.MISSING_INPUT)


def test_req_ratio_exactly_120_is_tier_a():
    assert qualify.tier_for(1.20) == "A"
    assert qualify.tier_for(1.1999) == "B"
    assert call(req=4.8)[1] == "A"


def test_req_ratio_exactly_110_is_tier_b():
    assert qualify.tier_for(1.10) == "B"
    assert call(req=4.4)[1] == "B"


# ── the rest of §3: band, minimum strength, tier C gate, shortfall ────
def test_progress_band_is_the_alerts_execution_window():
    assert call(pct=75.0)[0] is True
    assert call(pct=92.0)[0] is True
    assert call(pct=74.9) == (False, qualify.PROGRESS_OUT_OF_BAND)
    assert call(pct=92.1) == (False, qualify.PROGRESS_OUT_OF_BAND)


def test_strength_at_or_below_the_production_margin_never_qualifies():
    assert qualify.tier_for(1.04) is None
    assert qualify.tier_for(1.0) is None
    assert call(req=4.16) == (False, qualify.BELOW_MIN_STRENGTH)


def test_shortfall_gate_follows_the_existing_toggle():
    from blm_v4 import trade_window as TW
    # required == actual → no shortfall
    assert call(req=4.8, act=4.8, shortfall=True) == (
        False, qualify.SHORTFALL_ABSENT)
    # explicit False (analysis override) lets it through
    assert call(req=4.8, act=4.8, shortfall=False)[0] is True


def test_shortfall_toggle_default_is_the_live_configuration(monkeypatch):
    from blm_v4 import trade_window as TW
    monkeypatch.setattr(TW, "TRADE_SHORTFALL_ENABLED", False, raising=False)
    assert call(req=4.8, act=4.8)[0] is True
    monkeypatch.setattr(TW, "TRADE_SHORTFALL_ENABLED", True, raising=False)
    assert call(req=4.8, act=4.8) == (False, qualify.SHORTFALL_ABSENT)


def test_c1_and_c2_are_recorded_context_not_gates():
    """Route A (2026-11-17): no fingerprint gates admission any more; C1/C2
    stay recorded context exactly as C3/C5/R2 do."""
    ok, tier = call(req=4.6, q3=4.3)     # q3_ratio 1.075, req in C1's band
    assert (ok, tier) == (True, "B")


# ── §5.4 — the audit record ───────────────────────────────────────────
def test_audit_block_carries_every_deciding_value():
    game = {"game_id": "31380565", "competition_slug": SLUG,
            "projector": {"progress_pct": 82.5, "required_pts_per_min": 4.6,
                          "actual_pts_per_min": 4.5},
            "competition_pace": {"avg_pace": AVG},
            "under_alert_fingerprint": {"league_average_pace": AVG,
                                        "q3_ppm": 3.9, "league_q3_avg": 4.0,
                                        "q3_ratio": 0.975,
                                        "fingerprint_c5_triggered": True}}
    block = qualify.check_game(game)
    assert block["gid"] == "31380565"
    assert block["slug"] == SLUG
    assert block["req_ratio"] == pytest.approx(1.15)
    assert block["tier"] == "B" and block["qualified"] is True
    assert block["reason"] == "TIER_B"
    assert block["fingerprints_fired"] == ["C5"]


def test_audit_block_of_a_refusal_names_the_reason():
    game = {"game_id": "1", "competition_slug": SLUG,
            "projector": {"progress_pct": 60.0, "required_pts_per_min": 4.3,
                          "actual_pts_per_min": 4.1},
            "competition_pace": {"avg_pace": AVG}}
    block = qualify.check_game(game)
    assert block["qualified"] is False
    assert block["reason"] == qualify.PROGRESS_OUT_OF_BAND


# ── the wiring: the fast lane's candidates pass through the gate ──────
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


def _row(req, act):
    now = datetime.now(timezone.utc)
    return ("31380565", _iso(now - timedelta(seconds=5)), GOOD["total"],
            GOOD["progress"], req, act, GOOD["line"], GOOD["price"],
            _iso(now - timedelta(seconds=5)), "VALID", 0)


def _worker(tmp_path, rows, monkeypatch):
    monkeypatch.setattr(market_heat, "refusal", lambda *a, **k: None)
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
    w._pace_reference = lambda: {SLUG: {"avg_pace": AVG, "games": 500}}
    return w


def test_lane_places_tier_c_without_q3_evidence_route_a(tmp_path, monkeypatch):
    """POLICY 2026-11-17 (route A): 4.3/4.0 = 1.075 → Tier C, the lane carries
    no Q3 evidence, and it still trades — the fingerprint leg is gone."""
    from blm_v4 import trade_window as TW
    monkeypatch.setattr(TW, "TRADE_MIN_PACE_RATIO", 1.0, raising=False)
    w = _worker(tmp_path, [_row(4.3, 4.1)], monkeypatch)
    out = w.pass_once()
    assert out["armed"] == 1 and out["would_bet"] == 1


def test_lane_still_places_a_tier_b_candidate(tmp_path, monkeypatch):
    """1.15 → Tier B, Q3 unavailable: the 77.6 % base rate is acceptable."""
    from blm_v4 import trade_window as TW
    monkeypatch.setattr(TW, "TRADE_MIN_PACE_RATIO", 1.0, raising=False)
    w = _worker(tmp_path, [_row(4.6, 4.5)], monkeypatch)
    out = w.pass_once()
    assert out["armed"] == 1 and out["would_bet"] == 1


# ── the wiring contract: refuse BEFORE the execution chain ──────────
class _Store:
    def __init__(self):
        self.reasons = None

    def is_enabled(self):
        return True

    def get_unit_price(self):
        return 10.0

    def today_stats(self):
        return {}

    def is_game_enabled(self, game_id):
        return True


class _Provider:
    def account_balance(self):
        return 100.0


def _armed_game(**over):
    from datetime import datetime, timezone
    cap = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    g = {"game_id": "31380565", "live": True, "live_reason": None,
         "competition_slug": SLUG,
         "market": {"total_line": 193.5, "market_status": "LIVE"},
         "projector": {"progress_pct": 82.5,
                       "required_pts_per_min": 5.0714,
                       "actual_pts_per_min": 4.8788,
                       "captured_at": cap},
         "under_alert_eligibility": {"eligible": True, "reason": None},
         "under_alert": {"active": True, "checkpoint": 75,
                         "trigger_line": 193.5, "required_pace": 5.0714,
                         "league_average_pace": AVG},
         "under_alert_exec_armed": True,
         "under_alert_fingerprint": {"league_average_pace": AVG,
                                     "fingerprint_count": 0,
                                     "fingerprints_fired": []}}
    g.update(over)
    return g


def _worker_stub(games):
    import blm_v4.betting.worker as worker_mod
    w = worker_mod.BettingWorker.__new__(worker_mod.BettingWorker)
    w.cfg = None
    w.store = _Store()
    w._live_payload_fn = lambda: list(games)
    w.poll_interval_s = 5.0
    w.trigger_feed = None
    w.provider = _Provider()
    w._stop = None
    w._thread = None
    w.last_error = None
    w._last_pass_log = 0.0
    from collections import Counter
    w._no_bet_reasons = Counter()
    return w


def test_worker_gate_runs_before_the_executor(monkeypatch):
    """§5.1/§7.3: a refused alert never reaches evaluate(claim=True)."""
    import blm_v4.betting.worker as worker_mod
    calls = []
    monkeypatch.setattr(worker_mod, "evaluate",
                        lambda *a, **k: calls.append(1) or {})
    w = _worker_stub([_armed_game(
        competition_slug=qualify.CYBER_2K26_SLUG)])
    out = w.poll_once()
    assert calls == [], "the executor must not be reached"
    assert out["no_bet"] == 1 and out["candidates"] == 0
    assert dict(w._no_bet_reasons) == {
        "autobet_gate:COMPETITION_EXCLUDED": 1}


def test_worker_lets_a_qualified_alert_reach_the_executor(monkeypatch):
    import blm_v4.betting.worker as worker_mod
    calls = []
    monkeypatch.setattr(
        worker_mod, "evaluate",
        lambda *a, **k: calls.append(a[0]) or {
            "decision": "NO_BET", "reason": "stubbed", "candidate": None})
    w = _worker_stub([_armed_game()])          # 1.184 → Tier B, no Q3
    w.poll_once()
    assert len(calls) == 1


def test_lane_places_a_tier_a_candidate(tmp_path, monkeypatch):
    from blm_v4 import trade_window as TW
    monkeypatch.setattr(TW, "TRADE_MIN_PACE_RATIO", 1.0, raising=False)
    w = _worker(tmp_path, [_row(4.8, 4.5)], monkeypatch)
    out = w.pass_once()
    assert out["armed"] == 1 and out["would_bet"] == 1
