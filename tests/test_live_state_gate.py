"""LIVE-STATE gate — display surface must show only genuinely live games.

DEFECT (2026-09-12): ``/api/v4/live`` decided liveness from observation
freshness ALONE —

    "live": bool(age is not None and age <= LIVE_AGE_S)

so a FINISHED game whose last snapshot was still inside the 15-minute
window was reported live and counted in ``totals.live``.  Observed live
on the running platform: game 30876289, status=ended, period "Finished",
age 509s -> live=true.  The frontend then papered over it with a SECOND,
undocumented liveness rule (``g.live === true && g.status !== "ended"``)
applied in the card grid only — leaving the header pill, the summary
counters, the live-market line, the pace strip, the UNDER/historical
panels and the card alert level reading the raw flag.

FIX: ONE authoritative determination (``api._live_state``) — supported
in-progress status + non-terminal per-frame game state (the existing
``terminal_basis`` authority) + freshness — surfaced as ``live`` /
``live_reason`` and enforced through ONE frontend predicate
(``isActuallyLive``).  Freshness is necessary, never sufficient.

Coverage: the directive's status matrix, at both the predicate level and
through ``/api/v4/live``, the alert interaction, and the frontend's single
gate.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import _alert_gate, _live_state, router as v4_router
from blm_v4.clean_boundary import CLEAN_DATA_EPOCH
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

DASH_JS = (Path(__file__).resolve().parent.parent
           / "blm_v4" / "dashboard" / "static" / "dashboard.js")

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── fixture: a real DB with one game per provider state ─────────────────

def _game(gid: str, status: str, *, cls: str = "BETUAL_NBA") -> PokerBetGame:
    first = NOW - timedelta(minutes=10)
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id=f"comp-{cls}", competition_slug=cls.lower(),
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification=cls, sport="basketball",
        home_team=f"Home {gid} Virtual", away_team=f"Away {gid} Virtual",
        game_slug=f"home-{gid}", source_url=f"https://x/{gid}",
        status=status, first_seen_at=_iso(first), last_seen_at=_iso(NOW),
    )


def _snap(store: PokerBetStore, gid: str, db_id: int, *, ago_s: float,
          label: str, quarter: int, clock: str, hs: int = 50,
          as_: int = 48) -> None:
    store.insert_snapshot(db_id, MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=_iso(NOW - timedelta(seconds=ago_s)),
        home_team=f"Home {gid} Virtual", away_team=f"Away {gid} Virtual",
        home_score=hs, away_score=as_, period_label=label, quarter=quarter,
        clock=clock, game_status="live", total_line=210.5, spread=-3.0,
        w1_odds=1.9, w2_odds=1.85, markets_json="{}",
    ), force=True)


# The directive's matrix: (status, period, quarter, clock, age_s, live?)
MATRIX = [
    # not-yet / not-playing states can NEVER be live, however fresh
    ("scheduled",   "1st Quarter", 1, "10:00", 5,    False),
    ("not_started", "1st Quarter", 1, "10:00", 5,    False),
    ("postponed",   "1st Quarter", 1, "10:00", 5,    False),
    ("cancelled",   "1st Quarter", 1, "10:00", 5,    False),
    # genuinely in progress — every quarter is live
    ("live", "1st Quarter", 1, "08:00", 5, True),
    ("live", "2nd Quarter", 2, "05:00", 5, True),
    ("live", "3rd Quarter", 3, "03:00", 5, True),
    ("live", "4th Quarter", 4, "02:00", 5, True),
    # half time: the collector models it as its own in-progress state and
    # terminal_basis does not treat it as an end-of-game condition
    ("halftime", "Half End", 2, "", 5, True),
    # ended, however the end is expressed
    ("ended",     "Finished",   4, "00:00", 5,   False),
    ("finished",  "Full Time",  4, "00:00", 5,   False),
    ("live",      "Finished",   4, "00:00", 5,   False),
    ("live",      "Full Time",  4, "00:00", 5,   False),
    # a stale observation is not live even when the status says live
    ("live", "4th Quarter", 4, "02:00", 901, False),
    ("live", "4th Quarter", 4, "02:00", 3600, False),
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    st = PokerBetStore(db)
    for i, (status, label, q, clock, ago, _exp) in enumerate(MATRIX):
        gid = f"9{i:03d}"
        db_id = st.upsert_game(_game(gid, status))
        _snap(st, gid, db_id, ago_s=ago, label=label, quarter=q, clock=clock)
    return st


@pytest.fixture
def client(store):
    from blm_v2.api.v2_fastapi import create_v2_app
    app = create_v2_app()
    app.include_router(v4_router)
    return TestClient(app)


# ═════════════════════════════════════════════════════════════════════
# 1. the predicate — api._live_state is the single authority
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status,label,q,clock,ago,expected", MATRIX)
def test_live_state_status_matrix(status, label, q, clock, ago, expected):
    game = {"status": status, "classification": "BETUAL_NBA"}
    latest = {"period_label": label, "quarter": q, "clock": clock}
    got = _live_state(game, latest, None, NOW, float(ago))
    assert got["live"] is expected, f"{status}/{label} -> {got}"
    assert got["live"] is (got["reason"] is None)


def test_freshness_alone_is_never_sufficient():
    """THE defect: a finished game inside the freshness window used to be
    live.  It must not be, no matter how recent its observation."""
    game = {"status": "ended", "classification": "BETUAL_NBA"}
    latest = {"period_label": "Finished", "quarter": 4, "clock": "00:00"}
    assert _live_state(game, latest, None, NOW, 1.0)["live"] is False
    # ...and an unknown provider state fails closed for the same reason
    game["status"] = "postponed"
    assert _live_state(game, latest, None, NOW, 1.0)["live"] is False


def test_every_hidden_game_carries_an_explainable_reason():
    for status, label, q, clock, ago, expected in MATRIX:
        got = _live_state({"status": status, "classification": "BETUAL_NBA"},
                          {"period_label": label, "quarter": q,
                           "clock": clock}, None, NOW, float(ago))
        if not expected:
            assert got["reason"], f"{status}/{label} hidden with no reason"


def test_supported_live_statuses_are_explicit():
    """Liveness is an ALLOW-LIST — a new provider state cannot default to
    live by omission."""
    assert "live" in v4api.LIVE_STATUSES
    assert "halftime" in v4api.LIVE_STATUSES      # collector's own state
    for s in ("scheduled", "not_started", "cancelled", "postponed",
              "ended", "finished"):
        assert s not in v4api.LIVE_STATUSES, s


# ═════════════════════════════════════════════════════════════════════
# 2. the endpoint — /api/v4/live exposes the authoritative state
# ═════════════════════════════════════════════════════════════════════

def test_endpoint_live_flag_matches_the_matrix(client):
    games = {g["game_id"]: g for g in
             client.get("/api/v4/live").json()["games"]}
    for i, (status, label, q, clock, ago, expected) in enumerate(MATRIX):
        g = games[f"9{i:03d}"]
        assert g["status"] == status
        assert g["live"] is expected, \
            f"{status}/{label} age={ago} -> live={g['live']}"
        assert ("live_reason" in g)


def test_endpoint_totals_count_only_genuinely_live(client):
    body = client.get("/api/v4/live").json()
    expected = sum(1 for row in MATRIX if row[5])
    assert body["totals"]["live"] == expected
    assert body["totals"]["live"] == sum(
        1 for g in body["games"] if g["live"])
    # the historical defect: totals.live overcounted finished games
    assert body["totals"]["live"] < body["totals"]["total"]


def test_no_finished_game_is_ever_reported_live(client):
    for g in client.get("/api/v4/live").json()["games"]:
        if g["status"] in ("ended", "finished"):
            assert g["live"] is False, g["game_id"]


# ═════════════════════════════════════════════════════════════════════
# 3. alert interaction — same authority, and non-live can never alert
# ═════════════════════════════════════════════════════════════════════

def _qualifying_proj():
    """A stored state that WOULD satisfy the UNDER condition on its own
    (7:00 remaining, plenty of headroom) — i.e. the exact stale-alert trap."""
    return {"captured_at": _iso(NOW), "classification": "BETUAL_NBA",
            "elapsed_game_minutes": 33.0, "progress_pct": 82.5,
            "remaining_game_minutes": 7.0, "period_label": "4th Quarter",
            "clock": "07:00"}


@pytest.mark.parametrize("status", ["scheduled", "not_started", "postponed",
                                    "cancelled", "ended", "finished"])
def test_non_live_state_cannot_alert_however_good_the_condition(status):
    game = {"status": status, "classification": "BETUAL_NBA"}
    got = _alert_gate(game, _qualifying_proj(), NOW, 5.0, None, 5.0)
    assert got["eligible"] is False
    assert got["reason"] in ("game_finished", "unsupported_status")


def test_alert_gate_and_live_state_share_one_status_vocabulary():
    """Directive §5: the display gate and the alert gate must not drift."""
    for status in ("live", "halftime", "ended", "finished", "scheduled",
                   "not_started", "postponed", "cancelled"):
        live = _live_state({"status": status, "classification": "BETUAL_NBA"},
                           {"period_label": "3rd Quarter", "quarter": 3,
                            "clock": "04:00"}, None, NOW, 5.0)
        # AMENDMENT (audit 2026-09-16 §6): the alert gate now also verifies
        # accepted-game-state freshness; fixtures pass a verified-fresh
        # state so the vocabulary comparison stays status-only.
        alert = _alert_gate({"status": status, "classification": "BETUAL_NBA"},
                            _qualifying_proj(), NOW, 5.0, None, 5.0)
        assert live["live"] == alert["eligible"], status


# ═════════════════════════════════════════════════════════════════════
# 4. the frontend — ONE predicate, applied on EVERY live path
# ═════════════════════════════════════════════════════════════════════

def _js() -> str:
    return DASH_JS.read_text()


def test_frontend_defines_one_live_predicate():
    js = _js()
    assert "function isActuallyLive(g)" in js
    # it must require the backend flag AND an explicit supported state
    assert "g.live !== true) return false" in js
    assert "LIVE_STATUSES.indexOf(s) !== -1" in js


def test_frontend_mirrors_the_backend_status_vocabulary():
    from blm_v4.api import LIVE_STATUSES
    js = _js()
    for s in LIVE_STATUSES:
        assert f'"{s}"' in js, f"frontend gate missing supported status {s!r}"


def test_no_rendering_path_reads_the_raw_live_flag():
    """Every live read routes through the predicate — the old inline
    ``g.live === true`` / ``g.live !== true`` checks are what let a
    finished game keep its LIVE chip, market line, pace strip and UNDER
    panel.  The predicate body is the ONLY place allowed to read it."""
    js = _js()
    body_start = js.index("function isActuallyLive(g)")
    body_end = js.index("}", js.index("indexOf(s) !== -1", body_start))
    outside = js[:body_start] + js[body_end:]
    for probe in ("g.live === true", "g.live !== true", "g.live) ", "!g.live"):
        assert probe not in outside, f"ungated live read: {probe!r}"
    # ...and the predicate is actually used on the card, alert, market,
    # pace, summary, header and modal paths
    assert outside.count("isActuallyLive(") >= 7


def test_every_live_gated_panel_uses_the_predicate():
    js = _js()
    for fn in ("liveAlertOf", "histBadgeHTML", "histPanelHTML",
               "noContextPanelHTML", "paceStripHTML", "liveMarketHTML",
               "renderModal", "cardHTML"):
        start = js.index(f"function {fn}(")
        window = js[start:start + 900]
        assert "isActuallyLive(" in window or "liveNow" in window, \
            f"{fn} does not gate on the live predicate"


def test_card_grid_and_toggle_gate_on_the_predicate():
    js = _js()
    assert "games.filter(isActuallyLive)" in js
    assert 'g.status !== "ended")' not in js   # the old second rule is gone


def test_summary_and_header_counts_use_the_predicate():
    js = _js()
    assert "(payload.games || []).filter(isActuallyLive).length" in js
    assert "if (isActuallyLive(g)) live++;" in js
