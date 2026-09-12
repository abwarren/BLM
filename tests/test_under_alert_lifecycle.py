"""ACTIVE UNDER ALERTS vs UNDER ALERT HISTORY — separate stores, full lifecycle.

Contract under test (directive ADDENDUM, sections 1-10):

  * TWO separate surfaces in the served HTML, in the order
    LIVE GAME BOXES -> ACTIVE UNDER ALERTS -> UNDER ALERT HISTORY
  * condition: actual_pace < required_pace AND required_pace > league_avg
    (league-specific average, never one global number)
  * identity = game_id + checkpoint, so 25% / 50% / 75% are independent
  * lifecycle INACTIVE -> ACTIVE -> RESOLVED
      FALSE->TRUE  new history record + entry in active
      TRUE->TRUE   active values refresh, NO second history record
      TRUE->FALSE  leaves active, history record marked RESOLVED, kept
  * terminating game resolves its active records; history survives
  * a polling cycle never promotes a resolved record back to active and
    never erases history
  * both empty states render explicit text, never a blank panel

The state machine is extracted from the served JS between the
__ALERT_STORE_BEGIN__ / __ALERT_STORE_END__ markers (plus the pure
derivations block) and executed in Node.js against stub DOM/storage, so
this drives the SHIPPED code rather than a copy of it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"


@pytest.fixture
def client():
    from fastapi.responses import FileResponse

    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")

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


node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

# ── stub environment: only what the shipped code reads from outside ────
# isActuallyLive / esc / fmtTime / $ / localStorage live outside both
# extracted blocks; the payload's `live` flag IS the backend verdict, so
# the stub mirrors that single dependency.  alertEligible is NOT stubbed —
# the real one ships inside the pure block and is what runs.
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
  activeAlertsHTML, historyAlertsHTML, underAlertId, fmtDuration,
  loadAlertHistory, __setT: (t) => { __T = t; },
  __getT: () => __T };
"""


def _module(js: str, tmp_path: Path) -> Path:
    mod = tmp_path / "alert_store.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    return mod


def _run(js: str, tmp_path: Path, expr: str):
    mod = _module(js, tmp_path)
    script = ("const m = require(%s); console.log(JSON.stringify(%s));"
              % (json.dumps(str(mod)), expr))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


# ══════════════════════════════════════════════════════════════════════
# 1. structure — two surfaces, correct order, separate stores
# ══════════════════════════════════════════════════════════════════════

def test_two_alert_sections_exist_in_the_required_order(client):
    html = client.get("/").text
    assert 'id="activeAlerts"' in html
    assert 'id="alertHistory"' in html
    assert "ACTIVE UNDER ALERTS" in html
    assert "UNDER ALERT HISTORY" in html
    # order: live game boxes -> active alerts -> alert history
    grid = html.index('id="grid"')
    active = html.index('id="activeAlerts"')
    hist = html.index('id="alertHistory"')
    assert grid < active < hist


def test_active_and_history_are_separate_containers(client):
    """Directive 9 — never one array doing both jobs."""
    js = _js(client)
    assert "const UNDER_ALERTS = { active: new Map(), history: [] }" in js
    assert "active: new Map()" in js
    assert "history: []" in js
    # the two renderers read different containers
    assert 'paintAlerts("activeAlerts", activeAlertsHTML())' in js
    assert 'paintAlerts("alertHistory", historyAlertsHTML())' in js


def test_empty_states_are_explicit(client):
    """Directive 8 — no confusing blank panels."""
    js = _js(client)
    html = client.get("/").text
    assert "No active UNDER alerts" in js
    assert "No alerts triggered" in js
    assert "No active UNDER alerts" in html
    assert "No alerts triggered" in html


def test_alert_identity_is_game_plus_checkpoint(client):
    """Directive 4 — 25 / 50 / 75 are independent identities."""
    js = _js(client)
    assert "function underAlertId(gameId, checkpoint)" in js
    assert "return `${gameId}|${checkpoint}`" in js
    # the checkpoint DERIVATION is the backend's (under_alert.checkpoint)
    assert "const cp = ua.checkpoint;" in js


def test_condition_is_the_directive_condition():
    """actual < required AND required > league average — evaluated ONCE, in
    the backend, so no surface can disagree with another."""
    from blm_v4.live_analytics.under_alert import under_alert_state
    t = lambda a, r, avg: under_alert_state(a, r, avg)["active"]   # noqa: E731
    assert t(3.0, 5.0, 4.155) is True
    assert t(6.0, 5.0, 4.155) is False      # actual above required
    assert t(3.0, 3.5, 4.155) is False      # required below the league rate
    assert t(3.0, 5.0, 5.5) is False        # required below the league rate
    assert t(5.0, 5.0, 4.155) is False      # strict: actual == required
    assert t(3.0, 4.155, 4.155) is False    # strict: required == average
    # a missing number is never a claim
    for args in ((None, 5.0, 4.155), (3.0, None, 4.155), (3.0, 5.0, None)):
        assert t(*args) is False, args
    # non-finite and booleans never activate
    assert t(float("nan"), 5.0, 4.155) is False
    assert t(3.0, float("inf"), 4.155) is False
    assert t(True, 5.0, 4.155) is False
    # the served block carries the numbers the verdict was built from
    assert under_alert_state(3.84, 5.02, 4.155, 80, 1852) == {
        "active": True, "checkpoint": 75, "actual_pace": 3.84,
        "required_pace": 5.02, "league_average_pace": 4.155,
        "league_reference_games": 1852, "pace_gap": 3.84 - 5.02}


def test_checkpoint_boundaries():
    from blm_v4.live_analytics.under_alert import checkpoint_for
    assert [checkpoint_for(p) for p in
            (10, 20, 24.9, 25, 40, 49.9, 50, 60, 74.9, 75, 99, 100)] == \
        [None, None, None, 25, 25, 25, 50, 50, 50, 75, 75, 75]
    assert [checkpoint_for(p) for p in
            (None, float("nan"), float("inf"), "x")] == [None] * 4


def test_frontend_consumes_the_server_verdict(client):
    """The browser must NOT reconstruct the condition — it renders the
    backend's verdict, so the two can never disagree."""
    js = _js(client)
    assert "const ua = g.under_alert || {};" in js
    assert "const ok = live && cp != null && ua.active === true;" in js
    # the condition itself no longer exists in the browser
    assert "underConditionTrue" not in js
    assert "actual < required" not in js
    assert "currentCheckpoint" not in js


def test_frontend_carries_no_competition_identifier_of_its_own(client):
    """The reference is keyed by the canonical slug server-side, so the JS
    holds no competition literal and cannot merge two leagues."""
    js = _js(client)
    for slug in ("betual-nba", "betual-kbl", "betual-cba", "betual-tbsl",
                 "betual-euroleague", "cyber-basketball-2k26-matches"):
        assert slug not in js, slug
    assert "LEAGUE_AVG_PACE" not in js


def test_every_game_carries_its_own_verdict():
    """Each live game is served its own under_alert block, built from its
    OWN competition's reference."""
    from blm_v4.api import v4_live
    payload = v4_live(classification=None)
    ref = payload["pace_reference"]
    assert payload["games"]
    for g in payload["games"]:
        ua = g["under_alert"]
        assert set(ua) == {"active", "checkpoint", "actual_pace",
                           "required_pace", "league_average_pace",
                           "league_reference_games", "pace_gap"}, ua
        # the block's league average is this game's own competition entry
        entry = ref.get(g["competition_slug"])
        if entry:
            assert ua["league_average_pace"] == entry["avg_pace"], g["game_id"]
            assert ua["league_reference_games"] == entry["games"]
        else:
            assert ua["active"] is False


def test_pace_gap_is_served_for_every_block(monkeypatch):
    """The served block carries pace_gap == actual_pace - required_pace.

    Reads the REAL pipeline DB explicitly: other suites in this repo leak
    ``BLM_POKERBET_DB`` into ``os.environ`` without teardown, so an ambient
    read would silently resolve to some other test's temp DB.
    """
    if not (HERE.parent / "blm_pokerbet.db").exists():
        pytest.skip("pipeline DB not present")
    monkeypatch.delenv("BLM_POKERBET_DB", raising=False)
    from blm_v4.api import v4_live
    games = v4_live(classification=None)["games"]
    assert games

    defined = undefined = 0
    for g in games:
        ua = g["under_alert"]
        assert "pace_gap" in ua, g["game_id"]
        actual, required = ua["actual_pace"], ua["required_pace"]
        if actual is None or required is None:
            # a gap needs both numbers — never a fabricated 0.0
            assert ua["pace_gap"] is None, (g["game_id"], ua)
            undefined += 1
        else:
            assert ua["pace_gap"] == actual - required, (g["game_id"], ua)
            defined += 1
        if ua["active"]:
            assert ua["pace_gap"] < 0, g["game_id"]      # signed with the verdict
    assert defined, "no game in the payload carried both operands"
    assert defined + undefined == len(games)


def test_pace_gap_is_consistent_for_qualifying_and_non_qualifying():
    """Both populations, deterministically — the directive's two cases
    without depending on whatever the live population happens to be."""
    from blm_v4.live_analytics.under_alert import under_alert_state

    qualifying = under_alert_state(3.84, 5.02, 4.155, 80, 1852)
    assert qualifying["active"] is True
    assert qualifying["pace_gap"] == 3.84 - 5.02
    assert qualifying["pace_gap"] < 0

    # every way the condition can fail, with the gap still exact
    for a, r, avg in ((6.0, 5.0, 4.155),      # actual above required
                      (3.0, 3.5, 4.155),      # required below the league rate
                      (5.0, 5.0, 4.155),      # strict: actual == required
                      (3.0, 4.155, 4.155)):   # strict: required == average
        block = under_alert_state(a, r, avg)
        assert block["active"] is False, (a, r, avg)
        assert block["pace_gap"] == a - r, (a, r, avg)


def test_pace_gap_has_no_operands_to_derive_from():
    """A missing operand must not yield a fabricated 0.0 — the frontend
    renders '–' for it, so None is the only honest value."""
    from blm_v4.live_analytics.under_alert import under_alert_state
    assert under_alert_state(None, 5.0, 4.155)["pace_gap"] is None
    assert under_alert_state(3.0, None, 4.155)["pace_gap"] is None
    assert under_alert_state(3.0, 5.0, None)["pace_gap"] == 3.0 - 5.0
    assert under_alert_state(None, None, None)["pace_gap"] is None


def test_frontend_consumes_the_served_pace_gap(client):
    """The browser must read g.under_alert.pace_gap, never subtract the
    operands itself."""
    js = _js(client)
    block = _block(js, "function underAlertValues(g, ua, labels) {",
                   "\nfunction reconcileUnderAlerts")
    # exactly one pace_gap assignment, and it is the served field
    lines = [l.strip() for l in block.splitlines()
             if l.strip().startswith("pace_gap:")]
    assert lines == ["pace_gap: ua.pace_gap,"], lines
    # the one subtraction left in this block is the PERCENTAGE, not the gap
    assert "actual_vs_required_pct: required ? (actual - required)" in block


def test_history_record_keeps_the_served_gap(client):
    """The frozen trigger snapshot stores the server's gap verbatim."""
    js = _js(client)
    assert "rec.final_pace_gap = act.pace_gap;" in js
    assert "paceGap: p.pace_gap" in js


def test_league_reference_is_league_specific(client):
    """Server side: one entry per competition, computed over settled games.
    A single global average must not exist."""
    from blm_v4.live_analytics.competition_pace import (
        competition_pace_reference,
    )
    import sqlite3
    db = HERE.parent / "blm_pokerbet.db"
    if not db.exists():
        pytest.skip("pipeline DB not present")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ref = competition_pace_reference(conn)
    finally:
        conn.close()
    assert ref, "reference must not be empty"
    for slug, entry in ref.items():
        assert slug and slug != "UNKNOWN", slug
        assert entry["avg_pace"] > 0
        assert entry["games"] > 0
    # the six canonical competitions are separately keyed, never merged
    keys = set(ref)
    assert {"betual-nba", "betual-tbsl"} <= keys
    assert len(keys) == len(set(keys))
    # a plausible scoring rate, not a blended global figure
    assert 3.0 < ref["betual-tbsl"]["avg_pace"] < 6.0
    assert ref["betual-nba"]["avg_pace"] != ref["betual-tbsl"]["avg_pace"]


def test_league_reference_does_not_require_row_factory():
    """A caller that never set row_factory must still get the reference.

    Reading rows by NAME raises on a plain tuple, which the loop's
    except-and-continue would swallow — yielding an empty reference and
    silently disabling every alert instead of failing loudly."""
    from blm_v4.live_analytics.competition_pace import (
        competition_pace_reference,
    )
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp()) / "nofactory.db"
    c = sqlite3.connect(str(tmp))          # deliberately NO row_factory
    c.executescript(
        "CREATE TABLE game_results (source_game_id TEXT, final_total REAL);"
        "CREATE TABLE games (source_game_id TEXT, competition_slug TEXT,"
        " classification TEXT);")
    c.execute("INSERT INTO games VALUES ('g1','betual-tbsl','BETUAL_NBA')")
    c.execute("INSERT INTO game_results VALUES ('g1',160.0)")
    c.commit()
    ref = competition_pace_reference(c)
    c.close()
    assert ref.get("betual-tbsl", {}).get("avg_pace") == 4.0, ref


def test_league_reference_cache_is_database_specific():
    """The cache is keyed by the database behind the connection: a second
    connection to a DIFFERENT file must never read the first one's numbers,
    and vice versa."""
    from blm_v4.live_analytics.competition_pace import (
        CACHE_TTL_SECONDS, competition_pace_reference,
    )
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    assert CACHE_TTL_SECONDS == 300.0

    def db(*rows):
        c = sqlite3.connect(str(_P(tempfile.mkdtemp()) / "pop.db"))
        c.executescript(
            "CREATE TABLE game_results (source_game_id TEXT, final_total REAL);"
            "CREATE TABLE games (source_game_id TEXT, competition_slug TEXT,"
            " classification TEXT);")
        for gid, slug, cls, tot in rows:
            c.execute("INSERT INTO games VALUES (?,?,?)", (gid, slug, cls))
            c.execute("INSERT INTO game_results VALUES (?,?)", (gid, tot))
        c.commit()
        return c

    populated = db(("g1", "betual-tbsl", "BETUAL_NBA", 160.0))
    empty = db()
    first = competition_pace_reference(populated)
    # the empty DB must NOT see the populated one's numbers
    assert competition_pace_reference(empty) == {}
    # a third DB with its OWN population gets its OWN value
    other = db(("g9", "betual-tbsl", "BETUAL_NBA", 200.0))
    assert competition_pace_reference(other)["betual-tbsl"]["avg_pace"] == 5.0
    # and the originals are untouched by either call
    assert competition_pace_reference(populated) == first == {
        "betual-tbsl": {"avg_pace": 4.0, "games": 1}}
    for c in (populated, empty, other):
        c.close()


def test_league_reference_failure_is_isolated():
    """An unreadable population yields {} — the alert layer then produces
    nothing rather than borrowing a rate."""
    from blm_v4.live_analytics.competition_pace import (
        competition_pace_reference,
    )
    import sqlite3

    class Broken(sqlite3.Connection):
        def execute(self, *a, **k):
            raise sqlite3.Error("boom")

    assert competition_pace_reference(Broken(":memory:")) == {}


def test_liveness_gate_resolves_alerts(client):
    """Directive 7 — a non-live game cannot hold an active record.

    Pins the INTENT, not a frozen source line: the live gate composes the
    server's own predicates, and since the LIVE MARKETS ONLY directive the
    market-eligibility gate is one of them (a stale or missing line can no
    longer hold a record either).
    """
    js = _js(client)
    assert "const live = isActuallyLive(g) && alertEligible(g) && mktEligible;" in js
    assert "g.under_alert_eligibility.eligible === true" in js


def test_history_record_carries_the_trigger_snapshot_fields(client):
    """Directive 5 — every named field is present on the record."""
    js = _js(client)
    for field in ("checkpoint", "actual_pace", "required_pace",
                  "league_average_pace", "pace_gap",
                  "required_vs_league_avg_pct", "actual_vs_required_pct",
                  "triggered_at", "resolved_at", "duration_ms"):
        assert field in js, field
    assert "game_id: g.game_id" in js


def _source(js: str, name: str) -> str:
    """Source of a top-level ``function name(...) {...}`` or
    ``const name = {...};`` — brace-matched, so nested literals survive."""
    starts = [js.find(f"function {name}("), js.find(f"const {name} = ")]
    i = min(s for s in starts if s >= 0)
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                if js.startswith("const", i) and js[end:end + 1] == ";":
                    end += 1
                return js[i:end]
    raise AssertionError("unterminated " + name)


# ── the "two surfaces can disagree" module ─────────────────────────────
# Everything histBadgeHTML reads from outside the pure block, so the REAL
# badge renderer runs against the REAL store.
CONTRAST_EXTRA = """
function liveLineState(g) { return { line: 160.5, age: 5, mstatus: "LIVE" }; }
function periodQOf(g) { return "Q3"; }
const num = (v, d = 1) => (v == null ? "-" : Number(v).toFixed(d));
const signedNum = (v, d = 2) => (v == null ? "\\u2013"
  : (v > 0 ? "+" : "") + Number(v).toFixed(d));
function marketChipHTML() { return ""; }
function cyberNoteHTML() { return ""; }
"""


def _contrast_module(js: str, tmp_path: Path) -> Path:
    mod = tmp_path / "contrast.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END)
                   + _source(js, "HIST_ALERTS")
                   + _source(js, "isCyberGame")
                   + _source(js, "liveAlertOf")
                   + _source(js, "histBadgeHTML")
                   + CONTRAST_EXTRA
                   + "module.exports = { histBadgeHTML, renderUnderAlerts,"
                     " UNDER_ALERTS, activeAlertsHTML };",
                   encoding="utf-8")
    return mod


@node
def test_the_two_under_surfaces_are_distinct_and_never_conflated(client,
                                                                tmp_path):
    """Directive 5 — the in-card HISTORICAL indicator and the actionable
    UNDER alert are DIFFERENT conditions.  Both must be reachable
    independently, and each must wear its own label, so a user never reads
    one as the other."""
    js = _js(client)
    mod = _contrast_module(js, tmp_path)
    script = ("const m = require(%s);\nconsole.log(JSON.stringify((() => {"
              """
        // historical indicator TRUE on a mature benchmark, actionable FALSE
        const hist = { status: "matched", eligible: true, benchmark_n: 50,
          under_state: true, actual_pace: 5.0, required_pace: 6.0,
          actual_below_state_mean: true, required_ge_state_mean: true,
          both_conditions_true: true, historical_avg_pace: 5.5 };
        const card = (ctx, uaActive) => ({
          game_id: "G", competition_slug: "betual-tbsl", live: true,
          live_reason: null, alert: { eligible: true },
          projector: { progress_pct: 80, live_total_line: 160.5,
                       market_age_seconds: 5, pace_gap: 2.0 },
          historical_context: ctx,
          under_alert: { active: uaActive, checkpoint: 75, actual_pace: 5.0,
            required_pace: 6.0, league_average_pace: 4.155,
            league_reference_games: 1852, pace_gap: 5.0 - 6.0 },
        });
        const out = {};
        out.badge_hist_true = m.histBadgeHTML(card(hist, false));
        m.renderUnderAlerts([card(hist, false)], {});
        out.active_when_historical_only = m.UNDER_ALERTS.active.size;
        out.active_html_when_historical_only = m.activeAlertsHTML();
        // actionable TRUE with NO historical context at all
        m.renderUnderAlerts([card(null, true)], {});
        out.active_when_actionable_only = m.UNDER_ALERTS.active.size;
        out.badge_no_context = m.histBadgeHTML(card(null, true));
        out.active_html = m.activeAlertsHTML();
        // and both can hold at once
        m.renderUnderAlerts([card(hist, true)], {});
        out.active_when_both = m.UNDER_ALERTS.active.size;
        return out;
      })()));""" % json.dumps(str(mod)))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    got = json.loads(out.stdout)

    # the badge answers the HISTORICAL question and says so
    assert "UNDER CONDITION" in got["badge_hist_true"]
    assert "UNDER ALERT" not in got["badge_hist_true"]
    # ...so a TRUE badge with a FALSE actionable verdict is a coherent state:
    # nothing entered the actionable surface
    assert got["active_when_historical_only"] == 0
    assert "No active UNDER alerts" in got["active_html_when_historical_only"]
    # and the reverse is equally coherent
    assert got["active_when_actionable_only"] == 1
    assert "🔥 UNDER ALERT — 75%" in got["active_html"]
    assert "UNDER CONDITION" not in got["active_html"]
    assert "UNDER ALERT" not in got["badge_no_context"]
    # both may hold together — neither suppresses the other
    assert got["active_when_both"] == 1


def test_no_second_history_record_on_steady_state(client):
    """Directive 3 — TRUE->TRUE must not append another record."""
    js = _js(client)
    body = _block(js, "function reconcileUnderAlerts(games, labels) {",
                  "\nfunction closeUnderAlert")
    # the update branch continues BEFORE any history push
    assert "Object.assign(act, vals);" in body
    upd = body.index("Object.assign(act, vals);")
    push = body.index("UNDER_ALERTS.history.push(rec);")
    assert upd < push
    assert "continue;" in body[upd:push]


def test_no_alert_vocabulary_regression(client):
    js = _js(client)
    assert "OVER" not in js
    low = js.lower()
    for banned in ("edge", "signal", "momentum", "win rate", "probab",
                   "calibrat", "forecast", "predict", "fair", "z_score",
                   "betting", "staking"):
        assert banned not in low, banned


def test_poll_cycle_does_not_restore_active_from_history(client):
    """Directive 9 — history is restored, the active store never is."""
    js = _js(client)
    assert "loadAlertHistory();" in js
    assert "UNDER_ALERTS.active = " not in js
    assert "UNDER_ALERTS.active.set(" in js


# ══════════════════════════════════════════════════════════════════════
# 2. pure derivations (node)
# ══════════════════════════════════════════════════════════════════════

@node
def test_duration_formatting(client, tmp_path):
    js = _js(client)
    got = _run(js, tmp_path,
               "[null,0,1000,59000,60000,111000,253000,3661000," +
               "-5].map(m.fmtDuration)")
    assert got == ["—", "0s", "1s", "59s", "1m 0s", "1m 51s", "4m 13s",
                   "61m 1s", "—"]


@node
def test_identity_independence(client, tmp_path):
    js = _js(client)
    got = _run(js, tmp_path,
               "[m.underAlertId('30818986',25),m.underAlertId('30818986',50),"
               "m.underAlertId('30818986',75)]")
    assert got == ["30818986|25", "30818986|50", "30818986|75"]
    assert len(set(got)) == 3


# ══════════════════════════════════════════════════════════════════════
# 3. the full lifecycle on the shipped state machine (directive 10)
# ══════════════════════════════════════════════════════════════════════

# The server verdict (g.under_alert) is supplied EXPLICITLY below — the
# store is a pure consumer of it, so the fixtures state what the backend
# would have computed rather than re-deriving the condition in the test.
LIFECYCLE = """
const LABELS = { "betual-tbsl": "TBSL", "betual-nba": "NBA" };
const ua = (active, cp, a, r, avg) => ({ active, checkpoint: cp,
  actual_pace: a, required_pace: r, league_average_pace: avg,
  league_reference_games: 1852,
  pace_gap: (a != null && r != null) ? a - r : null });
const g = (over) => Object.assign({
  game_id: "G1", competition_slug: "betual-tbsl", live: true, live_reason: null,
  alert: { eligible: true },
  projector: { progress_pct: 30, actual_pts_per_min: 3.0, required_pts_per_min: 5.0 },
  under_alert: ua(true, 25, 3.0, 5.0, 4.155),
}, over || {});
const snap = (label) => ({
  step: label,
  active: m.UNDER_ALERTS.active.size,
  history: m.UNDER_ALERTS.history.length,
  activeIds: Array.from(m.UNDER_ALERTS.active.keys()),
  histIds: m.UNDER_ALERTS.history.map((r) => r.id),
  openRecords: m.UNDER_ALERTS.history.filter((r) => !r.resolved_at).length,
  activeActual: Array.from(m.UNDER_ALERTS.active.values()).map((a) => a.actual_pace),
  triggerActual: m.UNDER_ALERTS.history.map((r) => r.actual_pace),
  resolved: m.UNDER_ALERTS.history.map((r) => !!r.resolved_at),
  reasons: m.UNDER_ALERTS.history.map((r) => r.resolved_reason),
  durations: m.UNDER_ALERTS.history.map((r) => m.fmtDuration(r.duration_ms)),
  activeHTML: m.activeAlertsHTML(),
  historyHTML: m.historyAlertsHTML(),
});
const poll = (gs) => m.renderUnderAlerts(gs, LABELS);
const tr = [];

tr.push(snap("false-before"));            // nothing qualifies yet
poll([]);
tr.push(snap("poll1-empty"));

poll([g()]);               // FALSE -> TRUE
tr.push(snap("poll2-trigger"));

m.__setT(m.__getT() + 253000);            // +4m13s
poll([g({ projector: { progress_pct: 32,
  actual_pts_per_min: 3.5, required_pts_per_min: 5.2 },
  under_alert: ua(true, 25, 3.5, 5.2, 4.155) })]);
tr.push(snap("poll3-still-true"));        // TRUE -> TRUE

poll([g({ projector: { progress_pct: 34,
  actual_pts_per_min: 6.0, required_pts_per_min: 5.0 },
  under_alert: ua(false, 25, 6.0, 5.0, 4.155) })]);
tr.push(snap("poll4-condition-false"));   // TRUE -> FALSE

// the same identity qualifies again AFTER resolving: a NEW record opens
m.__setT(m.__getT() + 60000);
poll([g({ projector: { progress_pct: 36,
  actual_pts_per_min: 3.0, required_pts_per_min: 5.2 },
  under_alert: ua(true, 25, 3.0, 5.2, 4.155) })]);
tr.push(snap("poll5-re-trigger"));

// phase advances 25 -> 50: old identity resolves as checkpoint_passed
poll([g({ projector: { progress_pct: 55,
  actual_pts_per_min: 3.0, required_pts_per_min: 5.4 },
  under_alert: ua(true, 50, 3.0, 5.4, 4.155) })]);
tr.push(snap("poll6-phase-advance"));

// and advances again 50 -> 75
poll([g({ projector: { progress_pct: 80,
  actual_pts_per_min: 3.0, required_pts_per_min: 5.6 },
  under_alert: ua(true, 75, 3.0, 5.6, 4.155) })]);
tr.push(snap("poll7-phase-advance-75"));

// the game stops being genuinely live -> active resolves, history remains.
// The server verdict is deliberately still TRUE here, so the LIVE GATE
// alone must resolve the record.
poll([g({ live: false, live_reason: "game_finished",
  alert: { eligible: false } })]);
tr.push(snap("poll8-game-ended"));

// a poll with no games at all must not erase anything
poll([]);
tr.push(snap("poll9-empty-payload"));

// two independent games, each with its own identity
const g2 = (over) => Object.assign({
  game_id: "G2", competition_slug: "betual-nba", live: true, live_reason: null,
  alert: { eligible: true },
  projector: { progress_pct: 30, actual_pts_per_min: 4.0, required_pts_per_min: 6.0 },
  under_alert: ua(true, 25, 4.0, 6.0, 5.6339),
}, over || {});
poll([g({ live: true, live_reason: null,
  alert: { eligible: true }, projector: { progress_pct: 30,
  actual_pts_per_min: 3.0, required_pts_per_min: 5.0 } }), g2()]);
tr.push(snap("poll10-two-games"));

// removing one game resolves only that one
poll([g2()]);
tr.push(snap("poll11-one-removed"));

// a competition with no served reference yields active:false server-side,
// so nothing is raised — no record is ever filled in from another league
poll([g2({ competition_slug: "betual-nogame",
  under_alert: ua(false, 25, 4.0, 6.0, null) })]);
tr.push(snap("poll12-unknown-competition"));

tr.push({ emptyActive: m.activeAlertsHTML(), emptyHistory: "" });
module.exports = tr;
"""


@node
def test_full_alert_lifecycle(client, tmp_path):
    js = _js(client)
    mod = _module(js, tmp_path)
    script = ("const m = require(%s);" % json.dumps(str(mod))
              + LIFECYCLE + "\nconsole.log(JSON.stringify(tr));")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    tr = {s["step"]: s for s in json.loads(out.stdout)
          if isinstance(s, dict) and "step" in s}

    # ── FALSE -> TRUE creates exactly one record in each store ──
    assert tr["poll2-trigger"]["active"] == 1
    assert tr["poll2-trigger"]["history"] == 1
    assert tr["poll2-trigger"]["activeIds"] == ["G1|25"]
    assert tr["poll2-trigger"]["histIds"] == ["G1|25"]
    assert tr["poll2-trigger"]["resolved"] == [False]
    assert tr["poll2-trigger"]["openRecords"] == 1

    # ── TRUE -> TRUE refreshes active, adds NO record, keeps the snapshot ──
    st = tr["poll3-still-true"]
    assert st["active"] == 1 and st["history"] == 1, st
    assert st["activeActual"] == [3.5], st          # active shows current
    assert st["triggerActual"] == [3.0], st         # history keeps trigger
    assert st["resolved"] == [False]

    # ── TRUE -> FALSE leaves active, resolves history, keeps the record ──
    st = tr["poll4-condition-false"]
    assert st["active"] == 0 and st["history"] == 1, st
    assert st["resolved"] == [True]
    assert st["reasons"] == ["condition_false"]
    assert st["durations"] == ["4m 13s"], st
    assert st["triggerActual"] == [3.0]

    # ── a resolved identity can trigger again as a NEW record ──
    st = tr["poll5-re-trigger"]
    assert st["active"] == 1 and st["history"] == 2, st
    assert st["histIds"] == ["G1|25", "G1|25"]
    assert st["resolved"] == [True, False]
    assert st["activeActual"] == [3.0]

    # ── phase advance resolves the old identity, opens the new one ──
    st = tr["poll6-phase-advance"]
    assert st["activeIds"] == ["G1|50"], st
    assert st["histIds"] == ["G1|25", "G1|25", "G1|50"]
    assert st["reasons"][1] == "checkpoint_passed", st["reasons"]
    assert st["active"] == 1 and st["history"] == 3

    st = tr["poll7-phase-advance-75"]
    assert st["activeIds"] == ["G1|75"], st
    assert st["reasons"][2] == "checkpoint_passed", st["reasons"]
    assert st["history"] == 4

    # ── game termination resolves active; history survives ──
    st = tr["poll8-game-ended"]
    assert st["active"] == 0, st
    assert st["history"] == 4, st
    assert st["reasons"][3] == "game_finished", st["reasons"]
    assert all(st["resolved"])

    # ── a poll never erases history ──
    st = tr["poll9-empty-payload"]
    assert st["active"] == 0 and st["history"] == 4, st
    assert st["openRecords"] == 0

    # ── independent games keep independent identities ──
    st = tr["poll10-two-games"]
    assert st["active"] == 2 and st["history"] == 6, st
    assert sorted(st["activeIds"]) == ["G1|25", "G2|25"], st

    st = tr["poll11-one-removed"]
    assert st["activeIds"] == ["G2|25"], st
    assert st["history"] == 6, st
    assert st["reasons"][4] == "no_longer_monitored", st["reasons"]

    # ── a live game with no reference for its competition raises nothing
    # (never a borrowed rate); the record closes as a non-TRUE condition ──
    st = tr["poll12-unknown-competition"]
    assert st["active"] == 0, st
    assert st["history"] == 6, st          # G2|25 closed, nothing opened
    assert st["reasons"][5] == "condition_false", st["reasons"]


@node
def test_rendered_markup_matches_the_directive_shapes(client, tmp_path):
    """Active + history rows carry the specified lines."""
    js = _js(client)
    got = _run(js, tmp_path, """
      (() => {
        const LABELS = { "betual-tbsl": "TBSL" };
        m.renderUnderAlerts([{ game_id: "30818986", competition_slug: "betual-tbsl",
          live: true, live_reason: null, alert: { eligible: true },
          projector: { progress_pct: 80 },
          under_alert: { active: true, checkpoint: 75, actual_pace: 3.84,
            required_pace: 5.02, league_average_pace: 4.155,
            league_reference_games: 1852, pace_gap: 3.84 - 5.02 } }], LABELS);
        const a = m.activeAlertsHTML();
        const h = m.historyAlertsHTML();
        m.renderUnderAlerts([], LABELS);
        return { active: a, history: h, emptyActive: m.activeAlertsHTML() };
      })()
    """)
    a, h = got["active"], got["history"]
    assert "🔥 UNDER ALERT — 75%" in a
    assert "TBSL | Game 30818986" in a
    assert "Actual: " in a and "3.84" in a
    assert "Required: " in a and "5.02" in a
    assert "League Avg: " in a and "4.16" in a
    assert "Gap: " in a and "-1.18" in a
    # history keeps the full trigger + timing block
    assert "[75%] TBSL | Game 30818986" in h
    assert "Triggered: " in h
    assert "Ended: " in h
    assert "Duration: " in h
    assert "Avg: " in h
    assert "still active" in h
    assert "No active UNDER alerts" in got["emptyActive"]
