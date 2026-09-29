"""ACTIVE ALERT GAME CARD (directive 2026-09-28).

When a BLM alert triggers for a live game, THAT game card — matched by
CANONICAL game_id from the backend's alert block, never team names —
enters an explicit alert state:

    NORMAL        no border, no badge, no bet action
    ACTIVE_ALERT  yellow border + 🟡 UNDER ALERT badge + triggered line +
                  [ PLACE BET ] (submits through the SAME validated
                  /betting/manual endpoint as the manual form)
    RESULTED      border removed; verdict handled by RESULTED ALERTS

Frontend tests run the SHIPPED card-alert block (__CARD_ALERT_BEGIN__ /
__CARD_ALERT_END__) in Node with stub DOM.  The backend stale-line test
proves the existing /betting/manual revalidation rejects a wager whose
line no longer matches the current observed Total (the frontend is never
the authority for wager validation).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from datetime import datetime, timezone

import pytest

from blm_v4.betting import api as betting_api
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.store import BettingStore

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
DASH_JS = DASH_STATIC / "dashboard.js"
STYLES_CSS = DASH_STATIC / "styles.css"

CARD_ALERT_BEGIN = "/* __CARD_ALERT_BEGIN__ */"
CARD_ALERT_END = "/* __CARD_ALERT_END__ */"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

STUBS = """
const esc = (s) => String(s == null ? "" : s);
const fmtTime = (iso) => !iso ? "--" : new Date(iso).toISOString().slice(11, 19);
const num1 = (x) => (x == null || !isFinite(x)) ? "–" : x.toFixed(1);
"""

EXPORTS = """
module.exports = { cardAlertState, cardAlertBadgeHTML };
"""


def _js() -> str:
    return DASH_JS.read_text(encoding="utf-8")


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _card_mod(tmp_path: Path) -> Path:
    js = _js()
    mod = tmp_path / "card_alert.js"
    mod.write_text(STUBS + _block(js, CARD_ALERT_BEGIN, CARD_ALERT_END)
                   + EXPORTS, encoding="utf-8")
    return mod


def _run_card(tmp_path: Path, script: str) -> dict:
    mod = _card_mod(tmp_path)
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{}};\n{script}\n"
            "console.log(JSON.stringify(OUT));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


# ══════════════════════════════════════════════════════════════════
# backend fixture — the same shape as test_betting_manual_contract.py
# ══════════════════════════════════════════════════════════════════

def _cfg(tmp_path, **kwargs):
    base = dict(dry_run=True, max_stake_per_bet=50, max_bets_per_day=10,
                max_daily_exposure=500, stake_units=1.0, min_unit_price=1,
                max_unit_price=1000, db_path=str(tmp_path / "betting.db"))
    base.update(kwargs)
    return BettingConfig(**base)


def _game(line=193.5):
    captured = datetime.now(timezone.utc).isoformat()
    return {
        "game_id": "evt-canonical-001", "live": True,
        "market": {"total_line": line, "market_status": "LIVE"},
        "projector": {"live_total_line": line, "market_status": "LIVE",
                      "market_age_seconds": 1, "captured_at": captured,
                      "required_pts_per_min": 5, "progress_pct": 76},
        "under_alert_eligibility": {"eligible": True},
        "under_alert": {"active": True, "checkpoint": 75,
                         "trigger_line": line},
        "under_alert_fingerprint": {"fingerprints_fired": []},
    }


def _client_for(tmp_path, games):
    store = BettingStore(str(tmp_path / "betting.db"))
    config = _cfg(tmp_path)
    betting_api.configure_betting(store, config, live_payload_fn=lambda: games)
    app = FastAPI()
    app.include_router(betting_api.router)
    return TestClient(app), store, games


# ══════════════════════════════════════════════════════════════════
# 1. normal game → no yellow border, no badge, no bet action
# ══════════════════════════════════════════════════════════════════

@node
def test_normal_game_has_no_alert_state_or_bet_action(tmp_path):
    out = _run_card(tmp_path, """
    OUT.state = m.cardAlertState({ game_id: "G-1", live: true });
    OUT.banner = m.cardAlertBadgeHTML({ game_id: "G-1", live: true });
    """)
    assert out["state"] == "NORMAL"
    assert out["banner"].strip() == ""


# ══════════════════════════════════════════════════════════════════
# 2 + 4. active alert → state, badge, triggered line, PLACE BET
# ══════════════════════════════════════════════════════════════════

@node
def test_active_alert_state_badge_info_and_place_bet(tmp_path):
    out = _run_card(tmp_path, """
    const g = { game_id: "evt-canonical-001", live: true, status: "live",
      under_alert: { active: true, checkpoint: 75, trigger_line: 162.5,
                     triggered_at: "2026-09-28T21:43:00Z" } };
    OUT.state = m.cardAlertState(g);
    const banner = m.cardAlertBadgeHTML(g);
    OUT.banner = banner;
    OUT.hasBadge = banner.includes("\\u{1F7E1}") && banner.includes("UNDER ALERT");
    OUT.hasLine = banner.includes("Triggered Line: 162.5");
    OUT.hasCheckpoint = banner.includes("Q75");
    OUT.hasPlaceBet = banner.includes(">PLACE BET</button>");
    OUT.hasGameId = banner.includes('data-game-id="evt-canonical-001"');
    OUT.hasSide = banner.includes('data-side="UNDER"');
    OUT.hasSealedLine = banner.includes('data-line="162.5"');
    """)
    assert out["state"] == "ACTIVE_ALERT"
    assert out["hasBadge"] and out["hasLine"] and out["hasCheckpoint"]
    assert out["hasPlaceBet"]
    # the button carries the SERVER-SERVED identity only
    assert out["hasGameId"] and out["hasSide"] and out["hasSealedLine"]


@node
def test_active_q3_break_alert_also_enters_alert_state(tmp_path):
    out = _run_card(tmp_path, """
    const g = { game_id: "evt-canonical-002", live: true,
      under_alert: { active: false },
      under_alert_q3_break: { active: true, checkpoint: "Q3_BREAK",
                              trigger_line: 158.5 } };
    OUT.state = m.cardAlertState(g);
    OUT.banner = m.cardAlertBadgeHTML(g);
    """)
    assert out["state"] == "ACTIVE_ALERT"
    assert "Q3" in out["banner"] and "Triggered Line: 158.5" in out["banner"]


# ══════════════════════════════════════════════════════════════════
# 3. canonical game_id binding — the button inherits the game's own id
# ══════════════════════════════════════════════════════════════════

@node
def test_alert_identity_is_canonical_game_id_not_team_names(tmp_path):
    out = _run_card(tmp_path, """
    const g = { game_id: "evt-canonical-009", live: true,
      home_team: "Team A", away_team: "Team B",
      under_alert: { active: true, checkpoint: 75, trigger_line: 162.5 } };
    const banner = m.cardAlertBadgeHTML(g);
    OUT.boundToId = banner.includes('data-game-id="evt-canonical-009"');
    OUT.noTeamNameInAction = !/data-game-id="Team/.test(banner);
    """)
    assert out["boundToId"] and out["noTeamNameInAction"]


# ══════════════════════════════════════════════════════════════════
# 6. RESULTED → no alert state (border removed), data untouched
# ══════════════════════════════════════════════════════════════════

@node
def test_resulted_alert_loses_the_alert_state(tmp_path):
    out = _run_card(tmp_path, """
    const ended = { game_id: "G-1", status: "ended", live: false,
      under_alert: { active: false, checkpoint: 75, trigger_line: 162.5 },
      under_alert_outcome: { status: "under", final_total: 151.0 } };
    OUT.endedState = m.cardAlertState(ended);
    OUT.endedBanner = m.cardAlertBadgeHTML(ended);
    const settledLive = { game_id: "G-2", status: "live", live: true,
      under_alert: { active: false },
      under_alert_outcome: { status: "over", final_total: 164.0 } };
    OUT.settledState = m.cardAlertState(settledLive);
    """)
    assert out["endedState"] == "RESULTED"
    assert out["endedBanner"].strip() == ""      # no badge / bet on a result
    assert out["settledState"] == "RESULTED"


@node
def test_border_and_state_classes_are_toggled_not_left_stale():
    """renderCards must ADD card-alert-active only for ACTIVE_ALERT and
    REMOVE it the moment the alert is no longer active (class toggle)."""
    js = _js()
    assert 'classList.toggle("card-alert-active"' in js
    assert 'classList.toggle("card-alert-resulted"' in js
    # applied from the per-game payload, not from bet/execution state
    assert "cardAlertState(g)" in js
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".card.card-alert-active" in css          # yellow border rule
    assert "#ffd400" in css                          # yellow/gold colour


# ══════════════════════════════════════════════════════════════════
# backend: the validated endpoint revalidates the CURRENT market
# ══════════════════════════════════════════════════════════════════

def test_backend_rejects_stale_line_before_execution(tmp_path):
    """The card sends the sealed TRIGGER line; if the market has moved on,
    the backend must reject (409) rather than execute a stale wager."""
    c, store, current = _client_for(tmp_path, [_game(line=193.5)])
    store.set_unit_price(20)
    store.set_config("auto_betting_enabled", "true")
    payload = {"game_id": "evt-canonical-001", "market": "TOTAL",
               "direction": "UNDER", "line": 189.5, "stake": 20,
               "idempotency_key": "card-stale-line-0001"}
    resp = c.post("/api/v4/betting/manual", json=payload)
    assert resp.status_code == 409
    assert "not the current observed Total" in resp.json()["detail"]
    assert store.recent() == []            # nothing was recorded/executed


def test_backend_rejects_unknown_game_and_off_market(tmp_path):
    c, store, _ = _client_for(tmp_path, [_game()])
    store.set_unit_price(20)
    store.set_config("auto_betting_enabled", "true")
    base = {"market": "TOTAL", "direction": "UNDER", "line": 193.5,
            "stake": 20, "idempotency_key": "card-unknown-0001"}
    # wrong game_id → the alert can never land on another game's wager
    wrong_game = c.post("/api/v4/betting/manual",
                        json={**base, "game_id": "evt-canonical-999"})
    assert wrong_game.status_code == 404
    # non-TOTAL market → rejected
    bad_market = c.post("/api/v4/betting/manual",
                        json={**base, "game_id": "evt-canonical-001",
                              "market": "SPREAD"})
    assert bad_market.status_code == 400
    assert store.recent() == []


def test_backend_executes_when_current_line_matches(tmp_path):
    c, store, _ = _client_for(tmp_path, [_game(line=193.5)])
    store.set_unit_price(20)
    store.set_config("auto_betting_enabled", "true")
    payload = {"game_id": "evt-canonical-001", "market": "TOTAL",
               "direction": "UNDER", "line": 193.5, "stake": 20,
               "idempotency_key": "card-current-line-0001"}
    resp = c.post("/api/v4/betting/manual", json=payload)
    assert resp.status_code == 200, resp.text
    assert store.recent() != []            # the wager went through the ledger
