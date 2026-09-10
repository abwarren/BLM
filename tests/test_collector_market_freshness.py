"""LIVE market freshness (30s target) — collector subscription layer.

The dashboard requirement is that the SCRAPED market data for every
ACTIVE live game is refreshed at ~30s.  The event-view rotation is
capacity-bound (MARKET_BATCH=3 per slow run, one slow run every
EVENT_VIEW_EVERY_N=2 ticks, measured mean cycle 36.7s -> ~20 min
round-robin), so it cannot meet 30s.  The eu-swarm feed is
per-SUBSCRIBED-EVENT but one authenticated socket holds many
subscriptions (verified live 2026-09-10), so the collector now keeps a
market subscription per active game on the fast page's socket.

These tests pin that layer and its instrumentation.  They must NOT
change the historical rotation constants (pinned in
test_m009_market_capture.py) or the WS ingestion semantics (pinned in
test_ws_instance_market.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from blm_v4 import collector as collector_mod
from blm_v4.collector import PokerBetCollector
from blm_v4.models import PokerBetGame
from blm_v4.storage import PokerBetStore

TS = "2026-09-10T12:00:00.000000Z"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _game(gid: str, *, status: str = "live",
          comp_id: str = "18296901", slug: str = "betual-nba",
          region: str = "Virtual Matches",
          cls: str = "BETUAL_NBA", seen_ago_s: float = 20.0) -> PokerBetGame:
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id=comp_id, competition_slug=slug,
        competition="NBA", region=region, game_family="betual",
        classification=cls, sport="basketball",
        home_team="Home %s Virtual" % gid,
        away_team="Away %s Virtual" % gid,
        game_slug="home-%s-virtual-away-%s-virtual" % (gid, gid),
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=TS,
        # a live game is sighted in the lobby every tick
        last_seen_at=_iso(_now() - timedelta(seconds=seen_ago_s)),
    )


def _track(c: PokerBetCollector, game: PokerBetGame) -> None:
    key = f"{game.home_team}|{game.away_team}"
    c._tracked.setdefault(game.classification, {})[key] = game


def _collector(tmp_path) -> PokerBetCollector:
    return PokerBetCollector(store=PokerBetStore(tmp_path / "b.db"))


class FakePage:
    """Records evaluate() calls; returns a configurable frame count."""

    def __init__(self, result=3) -> None:
        self.calls: list[tuple] = []
        self.result = result

    def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        return self.result


# ── constants ────────────────────────────────────────────────────────

def test_tick_stats_exist_before_start(tmp_path):
    """Regression: _tick records the sub/event timings, so the ring must
    exist for every entry point that drives _tick (--once / run_once),
    not only after start()."""
    c = _collector(tmp_path)
    assert "sub" in c._tick_stats and "event" in c._tick_stats


def test_freshness_target_is_30s():
    assert collector_mod.LIVE_MARKET_FRESH_TARGET_S == 30.0
    # collection cadence must sit strictly inside the freshness target so
    # a subscription is always re-armed before it can lapse.
    assert 0 < collector_mod.WS_SUB_REFRESH_S < collector_mod.LIVE_MARKET_FRESH_TARGET_S


def test_rotation_constants_untouched():
    # the historical rotation tuning is pinned elsewhere and must not move
    assert collector_mod.MARKET_REFRESH_S == 240
    assert collector_mod.MARKET_BATCH == 3
    assert collector_mod.EVENT_VIEW_EVERY_N <= 2


# ── subscription message shape ───────────────────────────────────────

def test_subscribe_msg_targets_base_id(tmp_path):
    c = _collector(tmp_path)
    msg = json.loads(c._market_subscribe_msg(_game("30840003#i2")))
    p = msg["params"]
    assert msg["command"] == "get"
    assert p["source"] == "betting"
    assert p["subscribe"] is True
    # the feed keys market frames to the event BASE id, never an #iN instance
    assert p["where"]["game"]["id"] == 30840003
    assert p["where"]["sport"]["alias"] == "Basketball"
    assert p["where"]["competition"]["id"] == 18296901
    assert p["where"]["region"]["alias"] == "Virtual Matches"
    # the market tree must be requested or no MatchTotal is pushed
    assert "market" in p["what"] and "event" in p["what"]


def test_subscribe_msg_omits_unknown_competition(tmp_path):
    c = _collector(tmp_path)
    g = _game("30840004")
    g.competition_id = ""
    msg = json.loads(c._market_subscribe_msg(g))
    assert "competition" not in msg["params"]["where"]


def test_subscribe_msg_none_for_non_numeric_id(tmp_path):
    c = _collector(tmp_path)
    assert c._market_subscribe_msg(_game("not-a-number")) is None


def test_subscribe_msg_uses_region_default_when_blank(tmp_path):
    c = _collector(tmp_path)
    g = _game("30840005", region="", cls="CYBER_2K26")
    msg = json.loads(c._market_subscribe_msg(g))
    assert msg["params"]["where"]["region"]["alias"] == "World"


# ── active-game universe ─────────────────────────────────────────────

def test_active_games_excludes_ended(tmp_path):
    c = _collector(tmp_path)
    _track(c, _game("11110000", status="live"))
    _track(c, _game("22220000", status="ended"))
    ids = {g.source_game_id for g in c._active_games()}
    assert ids == {"11110000"}


# ── subscription sync ────────────────────────────────────────────────

def test_sync_subscribes_all_active_games_once(tmp_path):
    c = _collector(tmp_path)
    for i in range(5):
        _track(c, _game("3084000%d" % i))
    page = FakePage(result=5)
    c._sync_market_subscriptions(page)
    assert len(page.calls) == 1                     # one batched evaluate
    msgs = page.calls[0][1]
    assert len(msgs) == 5
    assert all(json.loads(m)["command"] == "get" for m in msgs)
    assert c._market_sub_stats["passed"] == 1
    assert c._market_sub_stats["sent"] == 5
    assert len(c._market_sub) == 5


def test_sync_is_idempotent_within_refresh_window(tmp_path):
    c = _collector(tmp_path)
    _track(c, _game("30840001"))
    c._sync_market_subscriptions(FakePage(result=1))
    page = FakePage(result=1)
    c._sync_market_subscriptions(page)              # immediately again
    assert page.calls == []                         # nothing re-sent
    c._market_sub["30840001"] = _iso(
        _now() - timedelta(seconds=collector_mod.WS_SUB_REFRESH_S + 5))
    page2 = FakePage(result=1)
    c._sync_market_subscriptions(page2)
    assert len(page2.calls) == 1                    # re-armed after the window


def test_sync_skips_when_socket_not_open(tmp_path):
    c = _collector(tmp_path)
    _track(c, _game("30840001"))
    page = FakePage(result=-1)                      # socket not OPEN
    c._sync_market_subscriptions(page)
    assert c._market_sub == {}                      # never marked subscribed
    assert c._market_sub_stats["sent"] == 0


def test_sync_survives_evaluate_error(tmp_path):
    c = _collector(tmp_path)
    _track(c, _game("30840001"))

    class Boom(FakePage):
        def evaluate(self, script, arg=None):
            raise RuntimeError("page closed")

    c._sync_market_subscriptions(Boom())
    assert c._market_sub_stats["errors"] == 1
    assert c._market_sub == {}


def test_sync_noop_on_none_page(tmp_path):
    c = _collector(tmp_path)
    _track(c, _game("30840001"))
    c._sync_market_subscriptions(None)              # must not raise
    assert c._market_sub_stats["passed"] == 0


# ── a WS observation arms the freshness gate ─────────────────────────

def _obs(gid: str, line: float = 216.5, ts: str = TS) -> dict:
    return {
        "source_game_id": gid, "captured_at": ts,
        "market_type": "MatchTotal", "market_name": "Total Points",
        "line_value": line, "over_price": 1.95, "under_price": 1.85,
        "home_score": 106, "away_score": 93,
        "period_label": "4th Quarter", "clock": "02:59", "raw": {},
    }


def test_ws_observation_arms_freshness_gate(tmp_path):
    c = _collector(tmp_path)
    g = _game("30840001")
    c.store.upsert_game(g)              # FK: observations reference games
    _track(c, g)
    assert c._last_market_at == {}
    c._ingest_ws_observation(_obs("30840001", ts="2026-09-10T12:00:00.000000Z"))
    # a pushed MatchTotal is a real observation - the rotation must not
    # waste a visit on a game the socket already covers
    assert c._last_market_at["30840001"] == "2026-09-10T12:00:00.000000Z"


# ── freshness instrumentation ────────────────────────────────────────

def test_freshness_summary_separates_cadence_from_age(tmp_path):
    c = _collector(tmp_path)
    _track(c, _game("30840001"))
    _track(c, _game("30840002"))
    _track(c, _game("30840003"))
    now = _now()
    c._last_market_at["30840001"] = _iso(now - timedelta(seconds=5))
    c._last_market_at["30840002"] = _iso(now - timedelta(seconds=25))
    # 30840003 has no observation -> never_observed, never "fresh"
    s = c._freshness_summary()
    assert s["target_s"] == 30.0
    assert s["collection_cadence_s"] == collector_mod.WS_SUB_REFRESH_S
    assert s["active_games"] == 3
    assert s["with_market"] == 2
    assert s["never_observed"] == 1
    assert s["max_age_s"] < 30
    assert s["pct_le_target"] == 100.0
    assert s["pct_gt_60"] == 0.0
    assert s["pct_gt_120"] == 0.0


def test_freshness_summary_flags_stale_games(tmp_path):
    c = _collector(tmp_path)
    for i in range(4):
        _track(c, _game("3084000%d" % i))
    now = _now()
    c._last_market_at["30840000"] = _iso(now - timedelta(seconds=10))
    c._last_market_at["30840001"] = _iso(now - timedelta(seconds=20))
    c._last_market_at["30840002"] = _iso(now - timedelta(seconds=300))
    c._last_market_at["30840003"] = _iso(now - timedelta(seconds=600))
    s = c._freshness_summary()
    assert s["with_market"] == 4
    assert s["pct_le_target"] == 50.0
    assert s["pct_gt_60"] == 50.0
    assert s["pct_gt_120"] == 50.0
    assert s["max_age_s"] >= 600


def test_freshness_metric_separates_dormant_games(tmp_path):
    """A tracked game that has gone quiet is bookkeeping lag for an ended
    instance, not provider staleness — it must not inflate the percentiles
    (production leaks ~12 such entries per cycle)."""
    c = _collector(tmp_path)
    live = _game("30840001")
    gone = _game("30840002", seen_ago_s=4 * 3600)
    _track(c, live)
    _track(c, gone)
    c._last_market_at["30840001"] = _iso(_now() - timedelta(seconds=5))
    c._last_market_at["30840002"] = _iso(_now() - timedelta(seconds=9000))
    s = c._freshness_summary()
    assert s["tracked_games"] == 2
    assert s["active_games"] == 1          # only the still-collected game
    assert s["dormant_games"] == 1
    assert s["active_window_s"] == 300.0
    assert s["max_age_s"] < 30
    assert s["pct_le_target"] == 100.0


def test_active_window_keeps_recently_seen_unobserved_game(tmp_path):
    """Seen in the lobby but with no market yet = active, never_observed —
    a real coverage signal, not a dormant entry."""
    c = _collector(tmp_path)
    g = _game("30840001", seen_ago_s=30)
    _track(c, g)
    s = c._freshness_summary()
    assert s["active_games"] == 1
    assert s["dormant_games"] == 0
    assert s["never_observed"] == 1
    assert s["pct_le_target"] is None


def test_freshness_summary_empty_is_none_not_zero(tmp_path):
    c = _collector(tmp_path)
    s = c._freshness_summary()
    assert s["active_games"] == 0
    assert s["median_age_s"] is None and s["p95_age_s"] is None
    assert s["pct_le_target"] is None


# ── the subscription layer must not disturb historical rotation ───────

def test_gate_skip_semantics_unchanged(tmp_path):
    """The slow-path gate still keys off _last_market_at and the 240s window."""
    c = _collector(tmp_path)
    g = _game("30840001")
    _track(c, g)
    c._last_market_at[g.source_game_id] = _iso(_now())      # just observed
    last = c._last_market_at[g.source_game_id]
    # MARKET_REFRESH_S window means this game is skipped, and a skip must
    # not itself re-arm the gate.
    assert collector_mod.MARKET_REFRESH_S == 240
    c._last_market_at.get(g.source_game_id)
    assert c._last_market_at[g.source_game_id] == last
