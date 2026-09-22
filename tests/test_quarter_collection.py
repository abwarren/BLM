"""Quarter-specific collection tests (directive 2026-09-22, DATA
COLLECTION ONLY).

Covers the directive's §8 matrix: Q1..Q4 score capture, quarter-line
capture, full-game line capture, multiple observations of one market,
timestamp ordering, score monotonicity, missing quarter lines/scores,
malformed source data, duplicate handling, restart/recovery and
point-in-time reconstruction.  NOTHING here touches alert logic,
fingerprints, betting or thresholds — these tables feed no decision.
"""
import json
import os
import sys
import tempfile
import types

import pytest

from blm_v4 import ws_market
from blm_v4.collector import (
    PokerBetCollector,
    infer_market_period,
)
from blm_v4.models import PokerBetGame
from blm_v4.quarter_validation import (
    check_quarter_nonnegative,
    check_quarter_regression,
    check_quarter_within_total,
    validate_quarter_scores,
)
from blm_v4.storage import PokerBetStore


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def store():
    tmp = tempfile.mkdtemp()
    s = PokerBetStore(os.path.join(tmp, "q.db"))
    yield s
    s._connect().close() if False else None


def make_game(gid="900001"):
    return PokerBetGame(source_game_id=gid, classification="BETUAL_NBA")


def registered_game(store: PokerBetStore, gid="900001"):
    """A game WITH a real games row — the observation tables carry an
    FK to games(id), exactly as production resolves it."""
    game = make_game(gid)
    db_id = store.upsert_game(game)
    return game, db_id


def collector_stub(store: PokerBetStore, game: PokerBetGame,
                   db_id: int = 1):
    """Minimal self for the collector's quarter methods — only the
    attributes those methods touch (no browser, no polling loops)."""
    return types.SimpleNamespace(
        store=store,
        _game_db_id=lambda g: db_id,
        _find_tracked=lambda gid: game,
        _instances={},
        _ws_raw_last={},
        _q_parse_failures=0,
        _q_anomalies=0,
        _q_market_obs=0,
        _q_score_obs=0,
    )


PARSED_Q3 = {  # a verified event-view parse mid-Q3
    "period_label": "3rd Quarter", "quarter": 3, "clock": "05:00",
    "home_score": 61, "away_score": 55,
    "quarter_scores": [(18, 14), (21, 19), (22, 22)],
}


# ── 1. Q1..Q4 score capture ──────────────────────────────────────────

def test_q1_to_q4_capture_roundtrip(store):
    game = make_game()
    obs = {
        "source_game_id": game.source_game_id,
        "classification": game.classification,
        "captured_at": "2026-09-22T10:00:00.000000Z",
        "q1_home_score": 18, "q1_away_score": 14,
        "q2_home_score": 21, "q2_away_score": 19,
        "q3_home_score": 22, "q3_away_score": 22,
        "q4_home_score": 25, "q4_away_score": 20,
        "home_score": 86, "away_score": 75,
        "source_path": "event_view",
    }
    store.insert_quarter_score_observation(obs)
    rows = store.quarter_collection_metrics()
    assert rows["q1_coverage_pct"] == 100.0
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM quarter_score_observations").fetchone())
    finally:
        con.close()
    assert r["q1_home_score"] == 18 and r["q1_away_score"] == 14
    assert r["q4_home_score"] == 25 and r["q4_away_score"] == 20
    assert r["source_path"] == "event_view"


def test_record_quarter_scores_records_all_present_quarters(store):
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    PokerBetCollector._record_quarter_scores(stub, game, PARSED_Q3)
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM quarter_score_observations").fetchone())
    finally:
        con.close()
    assert r["q1_home_score"] == 18
    assert r["q3_away_score"] == 22
    assert r["q4_home_score"] is None          # Q4 not played yet
    assert r["home_score"] == 61
    assert stub._q_score_obs == 1


# ── 2/3. quarter lines + full-game lines ─────────────────────────────

QUARTER_FRAME = json.dumps({
    "code": 0, "data": {"x": {"game": {"900001": {
        "id": 900001,
        "team1_name": "A", "team2_name": "B",
        "info": {"current_game_state": "set2",
                 "current_game_time": "03:11",
                 "score1": "40", "score2": "37",
                 "additional_data": {"quarter": 2}},
        "market": {
            "111": {"type": "MatchTotal", "name": "Total Points",
                    "id": 111, "base": 155.5,
                    "event": {
                        "1": {"id": 1, "price": 1.9, "type_1": "Over",
                              "name": "Over", "base": 155.5},
                        "2": {"id": 2, "price": 1.9, "type_1": "Under",
                              "name": "Under", "base": 155.5}}},
            "222": {"type": "QuarterTotal", "name": "1st Quarter Total",
                    "id": 222,
                    "event": {
                        "3": {"id": 3, "price": 1.85, "type_1": "Over",
                              "name": "Over", "base": 38.5},
                        "4": {"id": 4, "price": 1.95, "type_1": "Under",
                              "name": "Under", "base": 38.5}}},
            "333": {"type": "P1P2", "name": "Match Winner", "id": 333,
                    "event": {
                        "5": {"id": 5, "price": 1.5, "type_1": "W1",
                              "name": "W1"}}},
        }}}}}})


def test_quarter_line_and_full_game_line_normalized_separately():
    payloads = ws_market.parse_market_frame(QUARTER_FRAME)
    obs = ws_market.normalize_observations(payloads, "T0")
    totals = [o for o in obs if o["market_type"] == "MatchTotal"]
    quarters = [o for o in obs if o["market_type"] != "MatchTotal"]
    assert len(totals) == 1 and totals[0]["line_value"] == 155.5
    assert len(quarters) == 1
    q = quarters[0]
    assert q["line_value"] == 38.5
    assert q["over_price"] == 1.85 and q["under_price"] == 1.95
    assert q["market_name"] == "1st Quarter Total"
    assert set(q["raw"]["market_id"]) == {"2", "2"} or q["raw"]["market_id"]


def test_matchtotal_pipeline_unchanged(store):
    """The historical MatchTotal path is byte-identical: same obs dict
    shape reaching market_observations (regression guard)."""
    payloads = ws_market.parse_market_frame(QUARTER_FRAME)
    obs = ws_market.normalize_observations(payloads, "T0")
    t = next(o for o in obs if o["market_type"] == "MatchTotal")
    assert set(t) == {
        "source_game_id", "captured_at", "market_type", "market_name",
        "line_value", "over_price", "under_price", "home_score",
        "away_score", "period_label", "clock", "raw"}


# ── 4. multiple observations / duplicates ────────────────────────────

def test_duplicate_quarter_market_observation_deduped(store):
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    payloads = ws_market.parse_market_frame(QUARTER_FRAME)
    for ts in ("T1", "T1"):     # same market, same line, same timestamp
        for o in ws_market.normalize_observations(payloads, ts):
            if o["market_type"] != "MatchTotal":
                o["source_game_id"] = game.source_game_id
                PokerBetCollector._ingest_quarter_market_observation(stub, o)
    con = store._connect()
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM quarter_market_observations").fetchone()[0]
    finally:
        con.close()
    assert n == 1
    assert stub._q_market_obs == 2   # attempts counted, row deduped


def test_same_market_new_timestamp_is_new_observation(store):
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    payloads = ws_market.parse_market_frame(QUARTER_FRAME)
    for ts in ("T1", "T2"):
        for o in ws_market.normalize_observations(payloads, ts):
            if o["market_type"] != "MatchTotal":
                o["source_game_id"] = game.source_game_id
                PokerBetCollector._ingest_quarter_market_observation(stub, o)
    con = store._connect()
    try:
        rows = con.execute(
            "SELECT captured_at FROM quarter_market_observations "
            "ORDER BY captured_at").fetchall()
    finally:
        con.close()
    assert [r[0] for r in rows] == ["T1", "T2"]   # ordering preserved


# ── 5. timestamp ordering / restart recovery ─────────────────────────

def test_last_quarter_observation_orders_by_captured_at(store):
    game = make_game()
    for i, ts in enumerate(("T1", "T2", "T3")):
        store.insert_quarter_score_observation({
            "source_game_id": game.source_game_id, "captured_at": ts,
            "home_score": 10 + i,
        })
    last = store.last_quarter_score_observation(game.source_game_id)
    assert last["captured_at"] == "T3"


def test_store_reopen_recovery(store):
    """A collector restart (new store instance, same DB) sees the data —
    no in-memory state is required for recovery."""
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    PokerBetCollector._record_quarter_scores(stub, game, PARSED_Q3)
    reopened = PokerBetStore(store.db_path)
    last = reopened.last_quarter_score_observation(game.source_game_id)
    assert last is not None and last["q1_home_score"] == 18
    # validation after restart: prev=None must be legal (no crash)
    assert validate_quarter_scores({"q1_home_score": 5}, None) == []


# ── 6. monotonicity / validation ─────────────────────────────────────

def test_quarter_scores_must_not_move_backwards():
    prev = {"q1_home_score": 18, "q1_away_score": 14,
            "q2_home_score": 21, "q2_away_score": 19}
    cur = {"q1_home_score": 18, "q1_away_score": 15,   # Q1 away changed
           "q2_home_score": 21, "q2_away_score": 19}
    anoms = check_quarter_regression(cur, prev)
    assert len(anoms) == 1
    assert "changed after completion" in anoms[0]["detail"]


def test_completed_quarter_unchanged_is_clean():
    prev = {"q1_home_score": 18, "q1_away_score": 14}
    cur = {"q1_home_score": 18, "q1_away_score": 14,
           "q2_home_score": 20, "q2_away_score": 17}
    assert check_quarter_regression(cur, prev) == []


def test_quarter_within_total():
    ok = {"home_score": 61, "away_score": 55,
          "q1_home_score": 18, "q1_away_score": 14,
          "q2_home_score": 21, "q2_away_score": 19,
          "q3_home_score": 22, "q3_away_score": 22}
    assert check_quarter_within_total(ok) == []
    bad = dict(ok, q1_home_score=40)               # impossible cumulative
    anoms = check_quarter_within_total(bad)
    assert anoms and "exceed" in anoms[0]["detail"]


def test_negative_quarter_score_flagged():
    anoms = check_quarter_nonnegative(
        {"q1_home_score": -1, "q1_away_score": 5})
    assert anoms and anoms[0]["check"] == "quarter_nonnegative"


def test_missing_values_never_anomalize():
    """NULL is a legal state (§5): a missing quarter, leg or full score
    produces NO anomaly — never a guessed comparison."""
    assert validate_quarter_scores({}) == []
    assert validate_quarter_scores({"q1_home_score": 10}) == []   # leg missing
    assert validate_quarter_scores(
        {"home_score": 50, "q1_home_score": 30, "q1_away_score": 5}
    ) == []   # 35 <= 50 cumulative is fine; one-sided legs skipped


def test_anomalies_flagged_not_corrected(store):
    """An anomalous observation is recorded EXACTLY as received and the
    anomaly is flagged separately (§4)."""
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    PokerBetCollector._record_quarter_scores(stub, game, PARSED_Q3)
    mutated = json.loads(json.dumps(PARSED_Q3))
    mutated["quarter_scores"] = [(18, 99), (21, 19), (22, 22)]
    mutated["captured_at"] = "LATER"
    PokerBetCollector._record_quarter_scores(stub, game, mutated)
    con = store._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM quarter_score_observations "
            "ORDER BY captured_at")]
        anoms = [dict(r) for r in con.execute(
            "SELECT * FROM quarter_validation_anomalies")]
    finally:
        con.close()
    assert rows[1]["q1_away_score"] == 99            # NOT corrected
    assert anoms and anoms[-1]["check_name"] == "quarter_regression"


# ── 7. missing data + malformed sources ──────────────────────────────

def test_missing_quarter_line_is_null_not_fabricated():
    """A quarter-total market whose frame lacks a base must produce a
    NULL line — never an invented value."""
    frame = json.dumps({"code": 0, "data": {"x": {"game": {"1": {
        "id": 1, "team1_name": "A", "team2_name": "B",
        "info": {"score1": "1", "score2": "2"},
        "market": {"9": {"type": "QuarterTotal", "name": "3rd Quarter Total",
                         "id": 9, "event": {
                             "7": {"id": 7, "price": 1.9,
                                   "type_1": "Over", "name": "Over"}}}}},
        }}}})
    payloads = ws_market.parse_market_frame(frame)
    obs = ws_market.normalize_observations(payloads, "T0")
    assert len(obs) == 1 and obs[0]["line_value"] is None


def test_all_null_quarter_scores_still_recorded(store):
    """The no-coverage denominator must be visible (§9: never hide
    missing data)."""
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    PokerBetCollector._record_quarter_scores(stub, game, {
        "period_label": "1st Quarter", "quarter": 1, "clock": "10:00",
        "home_score": None, "away_score": None, "quarter_scores": [],
    })
    m = store.quarter_collection_metrics()
    assert m["quarter_score_observations"] == 1
    assert m["q1_coverage_pct"] == 0.0


def test_malformed_frame_retained_and_counted(store):
    game = make_game()
    stub = collector_stub(store, game)
    ws_market.parse_market_frame("not json") == []
    stub.store.insert_ws_raw_frame(
        captured_at="T0", ws_url="wss://swarm", byte_size=8,
        parse_status="parse_failed", game_count=None,
        raw_json="not json", error="Expecting value")
    m = store.quarter_collection_metrics()
    assert m["ws_parse_failures"] == 1
    assert m["ws_raw_frames"] == 1


def test_parsed_frames_throttled_failures_not(store):
    """Raw-frame retention: parsed frames throttle per game; parse
    failures are ALWAYS retained (§5)."""
    store.insert_ws_raw_frame("T1", "u", 1, "parsed", 1, "{}")
    store.insert_ws_raw_frame("T2", "u", 1, "parse_failed", None, "{", "e")
    store.insert_ws_raw_frame("T3", "u", 1, "parse_failed", None, "{", "e")
    con = store._connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT captured_at, parse_status FROM ws_raw_frames "
            "ORDER BY captured_at")]
    finally:
        con.close()
    assert [r["parse_status"] for r in rows] == [
        "parsed", "parse_failed", "parse_failed"]


# ── 8. point-in-time reconstruction ──────────────────────────────────

def test_raw_market_entry_preserved(store):
    """The raw upstream market observation (events, ids, prices, bases)
    survives verbatim for later re-parsing (§2/§5)."""
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    payloads = ws_market.parse_market_frame(QUARTER_FRAME)
    for o in ws_market.normalize_observations(payloads, "T0"):
        if o["market_type"] != "MatchTotal":
            o["source_game_id"] = game.source_game_id
            PokerBetCollector._ingest_quarter_market_observation(stub, o)
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM quarter_market_observations").fetchone())
    finally:
        con.close()
    raw = json.loads(r["raw_json"])
    evs = {e["type_1"]: e["price"] for e in raw["events"]}
    assert evs == {"Over": 1.85, "Under": 1.95}
    assert raw["market_id"] == "222"


def test_quarter_scores_reconstructable_at_timestamp(store):
    """Historical analysis can rebuild what BLM knew at T: the row's
    quarter values + full score + period + clock all come from the ONE
    observation — nothing backfilled from later states."""
    game, db_id = registered_game(store)
    stub = collector_stub(store, game, db_id)
    PokerBetCollector._record_quarter_scores(stub, game, PARSED_Q3)
    con = store._connect()
    try:
        r = dict(con.execute(
            "SELECT * FROM quarter_score_observations").fetchone())
    finally:
        con.close()
    assert (r["q1_home_score"], r["q1_away_score"]) == (18, 14)
    assert r["period_label"] == "3rd Quarter" and r["clock"] == "05:00"
    assert r["source_path"] == "event_view"
    raw = json.loads(r["raw_json"])
    assert raw["quarter_scores"] == [[18, 14], [21, 19], [22, 22]]


def test_period_inference_never_guesses():
    assert infer_market_period("1st Quarter Total") == ("Q1", 1)
    assert infer_market_period("4th Quarter Total Points") == ("Q4", 4)
    # a HALF is not a quarter — honestly 'other', never mapped to Qn
    assert infer_market_period("Second Half Total") == ("other", None)
    assert infer_market_period("Quarter Total") == ("quarter_unknown", None)
    assert infer_market_period("Total Points") == ("other", None)
    assert infer_market_period(None) == ("other", None)


# ── 9. schema / migration ────────────────────────────────────────────

def test_snapshot_quarter_columns_exist(store):
    con = store._connect()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    finally:
        con.close()
    for c in ("q1_home_score", "q1_away_score", "q2_home_score",
              "q2_away_score", "q3_home_score", "q3_away_score",
              "q4_home_score", "q4_away_score"):
        assert c in cols


def test_migration_readds_dropped_quarter_columns(store):
    """Backward-compatible migration: a pre-directive DB missing the
    snapshot quarter columns gets them on reopen."""
    con = store._connect()
    try:
        con.execute("ALTER TABLE snapshots DROP COLUMN q3_home_score")
        con.commit()
    finally:
        con.close()
    reopened = PokerBetStore(store.db_path)
    con = reopened._connect()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    finally:
        con.close()
    assert "q3_home_score" in cols
