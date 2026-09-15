"""THE PRODUCTION UNDER TRIGGER — locked to the historical cohort.

Directive 2026-09-14.  ONE statistical rule, ONE alert decision:

    UNDER ALERT = progress >= 75%
                  AND required pace above league_average_pace * 1.04
                  AND genuinely live
                  AND LIVE market
                  AND valid inputs

Nothing else.  There is no 50% tier, no ``actual < league_average`` leg, no
other pace condition and no margin other than the 4% relative one.  The
2.5-minute remaining-time rule is NOT a statistical trigger and can no
longer suppress a qualifying alert.

One named test per directive requirement (A..N).  The pure trigger is
exercised through the SHIPPED ``under_alert_state``; the display rules
through the SHIPPED dashboard state machine, extracted between the
``__PURE_ALERT__`` / ``__ALERT_STORE__`` markers and run in Node.js.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.live_analytics.competition_pace import (
    competition_pace_reference,
)
from blm_v4.live_analytics.under_alert import (
    under_alert_eligibility,
    under_alert_state,
)

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

# the directive's league reference and its 4% relative margin
LEAGUE = 4.5
MARGIN = 1.04
THRESHOLD = LEAGUE * MARGIN          # 4.68 — the STRICT boundary


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="prod_trigger_static")

    @app.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))

    return TestClient(app)


def _js(client) -> str:
    resp = client.get("/static/dashboard.js")
    assert resp.status_code == 200
    return resp.text


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _state(actual, required, progress, *, league=LEAGUE, eligible=True):
    return under_alert_state(actual, required, league, progress,
                             eligible=eligible)


# ══════════════════════════════════════════════════════════════════════
# 1. the statistical trigger itself (A..F, J)
# ══════════════════════════════════════════════════════════════════════

def test_requirement_A_progress_74_99_does_not_alert():
    """A. progress = 74.99% -> NO ALERT (the 75% floor is absolute)."""
    block = _state(4.0, 5.0, 74.99)
    assert block["active"] is False, block
    # everything else is already satisfied — only progress is short
    assert block["required_pace"] > block["league_average_pace"] * MARGIN
    # ...and the boundary is INCLUSIVE, so 75.00 flips it on
    assert _state(4.0, 5.0, 75.00)["active"] is True


def test_requirement_B_progress_75_live_market_and_genuinely_live_alerts():
    """B. progress = 75.00% + required past the 4% margin + a LIVE market
    on a genuinely live game -> ALERT."""
    elig = under_alert_eligibility("LIVE", True, None)
    assert elig == {"eligible": True, "reason": "market_live"}
    block = _state(4.0, 5.0, 75.00, eligible=elig["eligible"])
    assert block["active"] is True, block
    assert block["checkpoint"] == 75


def test_requirement_C_required_exactly_at_the_margin_does_not_alert():
    """C. progress = 80% + required EXACTLY league_average * 1.04 -> NO
    ALERT (the comparison is STRICT)."""
    block = _state(4.0, THRESHOLD, 80.0)
    assert block["required_pace"] == THRESHOLD
    assert block["active"] is False, block


def test_requirement_D_required_above_the_margin_alerts():
    """D. progress = 80% + required above the margin -> ALERT."""
    assert _state(4.0, THRESHOLD + 0.01, 80.0)["active"] is True
    assert _state(4.0, 99.0, 80.0)["active"] is True


def test_requirement_E_required_below_the_margin_does_not_alert():
    """E. progress = 80% + required below the margin -> NO ALERT."""
    assert _state(4.0, THRESHOLD - 0.01, 80.0)["active"] is False
    assert _state(4.0, 0.0, 80.0)["active"] is False


def test_requirement_F_actual_above_the_league_average_still_alerts():
    """F. actual pace ABOVE the league average + all canonical conditions
    pass -> ALERT.  This proves the old ``actual < league_average`` leg has
    been REMOVED: actual pace is reported, never decisive."""
    for actual in (LEAGUE + 9.0, LEAGUE + 0.01, LEAGUE, LEAGUE - 0.01):
        assert _state(actual, 5.0, 80.0)["active"] is True, actual
    # the decision is IDENTICAL for every actual pace — the leg is gone
    verdicts = {_state(a, 5.0, 80.0)["active"]
                for a in (0.0, 1.0, 4.5, 9.0, 50.0)}
    assert verdicts == {True}
    # ...and the reported value still travels with the verdict
    assert _state(9.0, 5.0, 80.0)["actual_pace"] == 9.0


def test_requirement_J_missing_or_non_finite_required_pace_does_not_alert():
    """J. A missing / non-finite required pace -> NO ALERT (fail closed)."""
    for required in (None, float("nan"), float("inf"), float("-inf"),
                     True, "x", ""):
        block = _state(4.0, required, 80.0)
        assert block["active"] is False, required
    # ...and the same for the other required operands
    assert _state(4.0, 5.0, None)["active"] is False
    assert _state(4.0, 5.0, float("nan"))["active"] is False
    assert under_alert_state(4.0, 5.0, None, 80.0)["active"] is False


# ══════════════════════════════════════════════════════════════════════
# 2. the permitted technical gates only (G, H, I)
# ══════════════════════════════════════════════════════════════════════

def test_requirement_G_one_minute_remaining_still_alerts():
    """G. remaining = 1 minute + canonical conditions pass + LIVE market ->
    ALERT.  This proves the old 2.5-minute suppression is GONE: the alert
    decision has no remaining-time input, and the LIVE-market gate still
    passes at one minute."""
    import inspect

    src = inspect.getsource(under_alert_state)
    for banned in ("remaining_game_minutes", "ALERT_MIN_REMAINING",
                   "ANALYTICAL_MIN_REMAINING", "below_min_remaining"):
        assert banned not in src, banned

    live_market = under_alert_eligibility("LIVE", True, None)
    assert live_market["eligible"] is True
    block = _state(4.0, 5.0, 80.0, eligible=live_market["eligible"])
    assert block["active"] is True, block

    # the backend DOES still carry such a rule in its separate liveness gate
    # — which is exactly why the display must not consult it (requirement N)
    assert v4api.ALERT_MIN_REMAINING_MINUTES == 2.5


def test_requirement_H_a_stale_market_does_not_alert():
    """H. market_status = STALE -> NO ALERT, however perfect the numbers."""
    stale = under_alert_eligibility("STALE", True, None)
    assert stale == {"eligible": False, "reason": "market_stale"}
    block = _state(99.0, 99.0, 99.0, eligible=stale["eligible"])
    assert block["active"] is False, block
    # the numbers are still served — suppression is never a blank verdict
    assert block["required_pace"] == 99.0 and block["checkpoint"] == 75
    # MISSING behaves the same
    missing = under_alert_eligibility(None, True, None)
    assert missing["eligible"] is False
    assert _state(99.0, 99.0, 99.0,
                  eligible=missing["eligible"])["active"] is False


def test_requirement_I_a_non_live_game_does_not_alert():
    """I. A game that is not genuinely LIVE -> NO ALERT."""
    for reason in (None, "game_finished", "no_live_observation"):
        not_live = under_alert_eligibility("LIVE", False, reason)
        assert not_live["eligible"] is False, reason
        block = _state(99.0, 99.0, 99.0, eligible=not_live["eligible"])
        assert block["active"] is False, (reason, block)
    # a non-live game keeps the EXISTING exclusion vocabulary
    assert under_alert_eligibility("LIVE", False, "game_finished") == {
        "eligible": False, "reason": "game_finished"}


# ══════════════════════════════════════════════════════════════════════
# 3. the league reference (K)
# ══════════════════════════════════════════════════════════════════════

def test_requirement_K_invalid_historical_results_are_excluded():
    """K. An INVALID / UNKNOWN / unset final is NOT an authoritative settled
    result and must not enter the league average.

    The three non-authoritative rows below would move the average to 5.0 or
    worse if they leaked in; only the single OK row may count."""
    c = sqlite3.connect(str(Path(tempfile.mkdtemp()) / "authoritative.db"))
    c.executescript(
        "CREATE TABLE game_results (source_game_id TEXT, final_total REAL,"
        " final_result_status TEXT);"
        "CREATE TABLE games (source_game_id TEXT, competition_slug TEXT,"
        " classification TEXT);")
    rows = [
        ("ok1", 160.0, "OK"),          # authoritative -> 160/40 = 4.0
        ("ok2", 168.0, "OK"),          # authoritative -> 168/40 = 4.2
        ("bad1", 400.0, "INVALID"),    # must NOT count
        ("bad2", 999.0, "UNKNOWN"),    # must NOT count
        ("bad3", 999.0, None),         # must NOT count
        ("bad4", 999.0, ""),           # must NOT count
    ]
    for gid, total, status in rows:
        c.execute("INSERT INTO games VALUES (?,?,?)",
                  (gid, "betual-nba", "BETUAL_NBA"))
        c.execute("INSERT INTO game_results VALUES (?,?,?)",
                  (gid, total, status))
    c.commit()
    ref = competition_pace_reference(c)
    c.close()

    assert ref["betual-nba"]["games"] == 2, ref
    assert ref["betual-nba"]["avg_pace"] == pytest.approx((160.0 + 168.0)
                                                          / 2.0 / 40.0)
    # the invalid rows would have dragged the reference far upwards
    assert ref["betual-nba"]["avg_pace"] < 4.5


def test_requirement_K_docstring_states_the_authoritative_population():
    """K (cont.) — the contract is written down, not just implemented."""
    import blm_v4.live_analytics.competition_pace as cp_mod
    doc = (cp_mod.__doc__ or "") + (competition_pace_reference.__doc__ or "")
    assert "authoritative" in doc.lower(), doc
    src = Path(competition_pace_reference.__code__.co_filename).read_text(
        encoding="utf-8")
    assert "final_result_status = 'OK'" in src


# ══════════════════════════════════════════════════════════════════════
# 4. ONE decision, and it is the one displayed (L, M, N)
# ══════════════════════════════════════════════════════════════════════

STUBS = """
let __T = Date.parse("2026-09-12T15:42:08Z");
Date.now = () => __T;
const __LS = {};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (iso) => {
  if (!iso) return "--";
  return new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
};
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, reconcileUnderAlerts, renderUnderAlerts,
  activeAlertsHTML };
"""

# The server's verdict is supplied EXPLICITLY — the store is a pure consumer
# of g.under_alert.active, so these fixtures state what the backend would
# have computed rather than re-deriving the condition in the test.
DISPLAY = """
const LABELS = { "betual-tbsl": "TBSL" };
const game = (over) => Object.assign({
  game_id: "G-PROD", competition_slug: "betual-tbsl",
  live: true, live_reason: null,
  alert: { eligible: true },
  under_alert_eligibility: { eligible: true, reason: "market_live" },
  projector: { progress_pct: 80 },
  under_alert: { active: true, checkpoint: 75, actual_pace: 3.84,
    required_pace: 5.02, league_average_pace: 4.155,
    league_reference_games: 1852, pace_gap: 3.84 - 5.02 },
}, over || {});
const out = {};
const show = (g) => { m.renderUnderAlerts([g], LABELS);
                      return m.activeAlertsHTML(); };

// L — active:true  -> DISPLAYED
out.active_true = show(game());
// M — active:false -> NOT displayed (and no record opened)
out.active_false = show(game({ under_alert: Object.assign(
  game().under_alert, { active: false }) }));
out.active_false_records = m.UNDER_ALERTS.active.size;
// N — active:true while the SEPARATE backend liveness gate says INELIGIBLE
//     (below_min_remaining) and the market block says stale, on a game the
//     payload no longer calls live.  The authoritative active alert MUST
//     still be displayed — no second decision may suppress it.
out.active_true_despite_gate = show(game({
  live: false, live_reason: "game_finished",
  alert: { eligible: false, reason: "below_min_remaining" },
  under_alert_eligibility: { eligible: false, reason: "market_stale" },
}));
out.active_true_records = m.UNDER_ALERTS.active.size;
console.log(JSON.stringify(out));
"""

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


def _display(js: str, tmp_path: Path) -> dict:
    mod = tmp_path / "prod_display.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    script = ("const m = require(%s);\n" % json.dumps(str(mod))) + DISPLAY
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


@node
def test_requirement_L_api_active_true_is_displayed(client, tmp_path):
    """L. under_alert.active == true -> the dashboard DISPLAYS the alert."""
    got = _display(_js(client), tmp_path)
    html = got["active_true"]
    assert "UNDER ALERT" in html, html
    assert "TBSL | Game G-PROD" in html, html
    assert "No active UNDER alerts" not in html


@node
def test_requirement_M_api_active_false_is_not_displayed(client, tmp_path):
    """M. under_alert.active == false -> the dashboard displays NOTHING."""
    got = _display(_js(client), tmp_path)
    html = got["active_false"]
    assert "No active UNDER alerts" in html, html
    assert "UNDER ALERT — 75%" not in html, html
    assert got["active_false_records"] == 0, got


@node
def test_requirement_N_no_second_frontend_gate_suppresses_an_active_alert(
        client, tmp_path):
    """N. The dashboard must NOT independently gate an authoritative
    active alert on the separate backend _alert_gate.

    This is the exact production regression observed on game 30914532:
    required 6.5 vs a 4.334 threshold at 97.5% progress — a qualifying
    alert whose display was suppressed because the backend's SEPARATE
    liveness gate returned below_min_remaining.  Here the payload carries
    active:true alongside alert.eligible:false, a stale market block and
    live:false; the alert must STILL be displayed."""
    got = _display(_js(client), tmp_path)
    html = got["active_true_despite_gate"]
    assert "UNDER ALERT" in html, html
    assert "UNDER ALERT — 75%" in html, html
    assert "TBSL | Game G-PROD" in html, html
    assert got["active_true_records"] == 1, got


@node
def test_requirement_N_the_under_alert_path_consults_nothing_but_active(
        client):
    """N (source invariant).  reconcileUnderAlerts — the only consumer that
    opens, holds and closes UNDER alert records — reads the server's
    under_alert.active and nothing else."""
    js = _js(client)
    body = _block(js, "function reconcileUnderAlerts", "\nfunction ")
    assert "const ok = cp != null && ua.active === true;" in body
    assert "const ok = cp != null && ua.active === true;" in js
    for banned in ("alertEligible", "mktEligible", "g.alert",
                   "isActuallyLive", "under_alert_eligibility.eligible"):
        assert banned not in body, banned
