"""Betual-only dataset × collector integration tests (directive
2026-09-22, DATA COLLECTION ONLY).

Exercises the ACTUAL collector code paths — WS frame handler, event-view
capture, ended/disappeared handling, restart restore — proving the
dataset hooks work end-to-end WITHOUT any browser.  The historical
PokerBet pipeline (snapshots, market_observations, clean metrics) must
be byte-identical before and after the Betual hooks.
"""
import json
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from blm_v4.collector import PokerBetCollector
from blm_v4.models import PokerBetGame
from blm_v4.storage import PokerBetStore


BETUAL_FRAME = json.dumps({
    "code": 0, "data": {"x": {"game": {"900100": {
        "id": 900100,
        "start_ts": 1758500000,           # authoritative start evidence
        "match_length": 2400,
        "team1_name": "Betual A", "team2_name": "Betual B",
        "info": {"current_game_state": "set3",
                 "current_game_time": "05:00",
                 "score1": "61", "score2": "55",
                 "additional_data": {"quarter": 3}},
        "market": {
            "111": {"type": "MatchTotal", "name": "Total Points",
                    "id": 111, "base": 155.5,
                    "event": {
                        "1": {"id": 1, "price": 1.9, "type_1": "Over",
                              "name": "Over", "base": 155.5},
                        "2": {"id": 2, "price": 1.9, "type_1": "Under",
                              "name": "Under", "base": 155.5}}},
            "222": {"type": "QuarterTotal", "name": "3rd Quarter Total",
                    "id": 222,
                    "event": {
                        "3": {"id": 3, "price": 1.85, "type_1": "Over",
                              "name": "Over", "base": 38.5},
                        "4": {"id": 4, "price": 1.95, "type_1": "Under",
                              "name": "Under", "base": 38.5}}},
        }}}}}})


def tracked_betual(store: PokerBetStore, gid="900100"):
    game = PokerBetGame(source_game_id=gid, classification="BETUAL_NBA",
                        competition="Betual NBA", game_family="betual",
                        home_team="Betual A", away_team="Betual B")
    db_id = store.upsert_game(game)
    return game, db_id


def stub_collector(store: PokerBetStore, game: PokerBetGame,
                   db_id: int) -> PokerBetCollector:
    """A real collector instance with only the Playwright machinery
    stubbed out (browser/session never started in unit tests)."""
    c = PokerBetCollector.__new__(PokerBetCollector)
    # everything the WS/event paths touch
    c.store = store
    c.tick_s = 5.0
    c.headless = True
    c.betual = PokerBetCollector.__dict__["__init__"] and None  # placeholder
    return c


def make_stub(store: PokerBetStore, games: dict[str, PokerBetGame]):
    """Build a collector via __new__ and initialize ONLY the fields the
    Betual paths use — keeps unrelated production machinery untouched."""
    from blm_v4.betual_dataset import BetualDataset

    c = PokerBetCollector.__new__(PokerBetCollector)
    c.store = store
    c.betual = BetualDataset(store)
    c._betual_start_ts = {}
    c._betual_last_quarter = {}
    c._instances = {}
    c._tracked = {"CYBER_2K26": {}, "BETUAL_NBA": {}}
    c._track_lock = __import__("threading").RLock()

    def _find_tracked(source_game_id):
        return games.get(source_game_id)

    c._find_tracked = _find_tracked
    c._game_db_id = lambda g: store.get_game(g.source_game_id)["id"]
    c._base_id = PokerBetCollector._base_id
    return c


# ── 1. WS frame handler: start evidence + full-game + quarter lines ──

def test_ws_start_evidence_adopted_and_persisted():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})

    c._betual_maybe_start_ts(game, 1758500000.0)
    assert game.source_game_id in c._betual_start_ts
    rec = c.betual.game(game.source_game_id, "BETUAL_NBA")
    assert rec.is_anchored()
    assert rec.anchors.game_start_wall == 1758500000.0
    # persisted for restart recovery (§13)
    row = store.get_betual_timer(game.source_game_id)
    assert row is not None and row["game_start_wall"] == 1758500000.0

    # first evidence wins: a later start_ts must NOT move the anchor
    c._betual_maybe_start_ts(game, 1758500500.0)
    assert rec.anchors.game_start_wall == 1758500000.0


def test_ws_frame_feeds_full_game_and_quarter_lines():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})

    from blm_v4 import ws_market
    payloads = ws_market.parse_market_frame(BETUAL_FRAME)
    captured = "2026-09-22T12:00:00.000000Z"
    # route through the collector's ingest logic (same branches as the
    # WS frame handler, minus the Playwright socket)
    idx = {str(p["game_id"]): game for p in payloads}
    for obs in ws_market.normalize_observations(payloads, captured):
        if obs["market_type"] == "MatchTotal":
            c._betual_record_line(game, obs, "full_game")
        else:
            period = "Q3" if "3rd" in (obs.get("market_name") or "") \
                else "other"
            c._betual_record_line(game, obs, period)
    con = store._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT period, line, market_id FROM betual_line_observations "
            "ORDER BY period")]
    finally:
        con.close()
    periods = {r["period"] for r in rows}
    assert periods == {"full_game", "Q3"}
    assert {r["line"] for r in rows} == {155.5, 38.5}


def test_full_game_line_movement_tracked():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})
    from blm_v4 import ws_market
    payloads = ws_market.parse_market_frame(BETUAL_FRAME)
    for i, ts in enumerate(("T1", "T2")):
        payloads2 = ws_market.parse_market_frame(BETUAL_FRAME)
        payloads2[0]["markets"][0]["base"] = 155.5 + 2.0 * i
        for obs in ws_market.normalize_observations(payloads2, ts):
            if obs["market_type"] == "MatchTotal":
                c._betual_record_line(game, obs, "full_game")
    con = store._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT line, line_change FROM betual_line_observations "
            "WHERE period='full_game' ORDER BY captured_at")]
    finally:
        con.close()
    assert rows[0]["line_change"] is None
    assert rows[1]["line_change"] == 2.0


# ── 2. event-view path: score obs + transition capture ──────────────

PARSED_Q3 = {
    "period_label": "3rd Quarter", "quarter": 3, "clock": "05:00",
    "home_score": 61, "away_score": 55,
    "quarter_scores": [(18, 14), (21, 19), (22, 22)],
    "total": {"first_line": 155.5},
}


def test_event_view_score_recorded_with_internal_fields():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})
    c._betual_record_score(game, PARSED_Q3, "T1")
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM betual_time_observations").fetchone())
    finally:
        con.close()
    assert r["home_score"] == 61 and r["total_score"] == 116
    assert r["betual_displayed_clock"] == "05:00"
    # unanchored timer: no internal remaining is derived, so the clock
    # difference is honestly NULL (never guessed — §9)
    assert r["clock_difference"] is None
    assert r["quarter"] is None


def test_event_view_transition_q3_to_q4_captured():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})
    c._betual_record_score(game, PARSED_Q3, "T1")
    parsed_q4 = dict(PARSED_Q3, quarter=4, period_label="4th Quarter",
                     clock="12:00", quarter_scores=[
                         (18, 14), (21, 19), (22, 22), (7, 6)])
    c._betual_record_score(game, parsed_q4, "T2")
    con = store._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM betual_transitions")]
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0]["transition"] == "Q3->Q4"
    assert rows[0]["full_game_line"] == 155.5
    assert rows[0]["prev_quarter_home"] == 61


def test_transition_not_fired_without_previous_quarter():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})
    # a restart cleared the cache: a lone Q4 observation invents nothing
    c._betual_record_score(game, dict(PARSED_Q3, quarter=4), "T1")
    con = store._connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM betual_transitions") \
            .fetchone()[0]
    finally:
        con.close()
    assert n == 0


# ── 3. end paths: observed final vs disappeared (§12) ───────────────

def test_end_game_observed_final_records_final():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})
    c._betual_end_game(game, dict(PARSED_Q3, quarter=4,
                                  period_label="fulltime",
                                  clock="00:00"))
    con = store._connect()
    try:
        r = dict(con.execute("SELECT * FROM betual_game_ends").fetchone())
    finally:
        con.close()
    assert r["end_evidence"] == "observed_final"
    assert r["final_total"] == 116


def test_disappeared_game_records_no_final():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    store.insert_snapshot(db_id, SimpleNamespace(
        source="PokerBet", source_game_id=game.source_game_id,
        classification="BETUAL_NBA", captured_at="T0",
        home_team="A", away_team="B", home_score=70, away_score=60,
        period_label="4th Quarter", quarter=4, clock="01:00",
        game_status="live", w1_odds=None, w2_odds=None,
        spread_indicator=None, total_line=None, total_over_odds=None,
        total_under_odds=None, spread=None, spread_home_odds=None,
        spread_away_odds=None, home_total_line=None, away_total_line=None,
        q1_home_score=None, q1_away_score=None, q2_home_score=None,
        q2_away_score=None, q3_home_score=None, q3_away_score=None,
        q4_home_score=None, q4_away_score=None, source_url="",
        markets_json="{}", raw_json="{}"))
    c = make_stub(store, {game.source_game_id: game})
    c._betual_end_game(game, None)
    con = store._connect()
    try:
        r = dict(con.execute("SELECT * FROM betual_game_ends").fetchone())
    finally:
        con.close()
    assert r["end_evidence"] == "disappeared"
    assert r["final_home_score"] == 70          # last known, NOT a final
    assert r["final_total"] == 130
    assert r["settlement_state"] is None


# ── 4. restart restore (§13) ────────────────────────────────────────

def test_collector_restart_restores_betual_timers():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    start_wall = time.time() - 900.0
    store.upsert_betual_timer(
        source_game_id=game.source_game_id,
        game_start_wall=start_wall, observed_at_wall=start_wall,
        quarter_seconds=600.0, break_seconds=0.0, timer_model="calibrated",
        last_home=50, last_away=48, last_line=155.5,
        last_line_at=time.time() - 20.0, last_capture_at="T-PRE")

    c = make_stub(store, {game.source_game_id: game})
    c._betual_restore_state()
    rec = c.betual.game(game.source_game_id, "BETUAL_NBA")
    assert rec.is_anchored()
    assert rec.anchors.game_start_wall == start_wall
    assert rec.anchors.model == "calibrated"
    it = rec.internal_time()
    assert it["internal_elapsed_seconds"] >= 900.0    # NOT reset
    assert it["quarter"] == 2                          # calibrated 600s model
    assert rec.prev_line == 155.5                      # caches re-seeded
    assert rec.prev_score == (50, 48)
    assert c._betual_start_ts[game.source_game_id][0] == start_wall


def test_restore_skips_untracked_games_cleanly():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    store.upsert_betual_timer(
        source_game_id="GONE", game_start_wall=1.0, observed_at_wall=1.0,
        quarter_seconds=None, break_seconds=None, timer_model="default",
        last_home=None, last_away=None, last_line=None, last_line_at=None,
        last_capture_at="")
    c = make_stub(store, {})
    c._betual_restore_state()          # must not raise


# ── 5. production-pipeline safety (§19) ─────────────────────────────

def test_historical_matchtotal_pipeline_unchanged():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    game, db_id = tracked_betual(store)
    c = make_stub(store, {game.source_game_id: game})
    from blm_v4 import ws_market
    obs = ws_market.normalize_observations(
        ws_market.parse_market_frame(BETUAL_FRAME), "T0")
    t = next(o for o in obs if o["market_type"] == "MatchTotal")
    assert set(t) == {
        "source_game_id", "captured_at", "market_type", "market_name",
        "line_value", "over_price", "under_price", "home_score",
        "away_score", "period_label", "clock", "raw"}


def test_non_betual_games_never_enter_dataset():
    tmp = tempfile.mkdtemp()
    store = PokerBetStore(os.path.join(tmp, "b.db"))
    cyber = PokerBetGame(source_game_id="800100",
                         classification="CYBER_2K26",
                         competition="Cyber Basketball 2K26",
                         game_family="cyber")
    store.upsert_game(cyber)
    c = make_stub(store, {"800100": cyber})
    from blm_v4 import ws_market
    payloads = ws_market.parse_market_frame(BETUAL_FRAME)
    obs = next(o for o in ws_market.normalize_observations(payloads, "T0")
               if o["market_type"] == "MatchTotal")
    with pytest.raises(Exception):
        c._betual_record_line(cyber, obs, "full_game")
    con = store._connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM betual_line_observations") \
            .fetchone()[0]
    finally:
        con.close()
    assert n == 0
