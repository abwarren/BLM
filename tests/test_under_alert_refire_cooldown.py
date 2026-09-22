"""UNDER ALERT RE-FIRE COOLING-OFF — policy 2026-09-20.

A RE-FIRE (reopening a record whose condition already fired once) is cooled
off by UNDER_ALERT_REFIRES_COOLDOWN_S seconds, measured from the record's
LAST ACTUAL ACTIVATION:

    first activation          T0            immediate, never cooled
    re-fire at T0 + 30 s                    SUPPRESSED
    re-fire at T0 + 299 s                   SUPPRESSED
    re-fire at T0 + 300 s                   allowed  -> last_refire_at
    re-fire at T0 + 599 s                   SUPPRESSED
    re-fire at T0 + 600 s                   allowed

The first activation is ALWAYS immediate and a standing alert is never
cooled — only the reopen is.  Deliberately a separate suite from
test_under_alert_lifecycle.py, whose dedup test proves the orthogonal fact
(one history record per identity, EVER); this one proves WHEN a reopen is
permitted.  Nothing here redefines that test.

The state machine is extracted from the SHIPPED dashboard.js between the
__PURE_ALERT__ / __ALERT_STORE__ markers and executed in Node.js, so this
drives production code rather than a copy of it.  Time and localStorage are
in-process stubs — never the browser's real storage.
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
ROOT = HERE.parent
DASH_STATIC = ROOT / "blm_v4" / "dashboard" / "static"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

COOLDOWN_S = 300
ID = "G1|75"


@pytest.fixture
def client():
    from fastapi.responses import FileResponse

    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_cooldown_static")

    @app.get("/", include_in_schema=False)
    async def dash():
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

# ── stub environment ───────────────────────────────────────────────────
# Time and the storage backend live on globalThis, NOT in module scope: a
# reload test re-executes the module body (fresh UNDER_ALERTS) while the
# persisted history and the clock must survive, exactly like a real reload.
STUBS = """
globalThis.__T = globalThis.__T || Date.parse("2026-09-12T15:42:08Z");
Date.now = () => globalThis.__T;
globalThis.__LS = globalThis.__LS || {};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(globalThis.__LS, k)
    ? globalThis.__LS[k] : null),
  setItem: (k, v) => { globalThis.__LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s);
const fmtTime = (iso) => (iso ? new Date(iso).toISOString() : "--");
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, Q3B_ALERTS, reconcileUnderAlerts,
  reconcileQ3BreakAlerts, underAlertId, q3bAlertId, loadAlertHistory,
  saveAlertHistory, underAlertCooldownRemainingMs, underAlertLastActivationMs,
  UNDER_ALERT_REFIRES_COOLDOWN_S, UNDER_ALERT_REFIRES_COOLDOWN_MS,
  __setT: (t) => { globalThis.__T = t; },
  __getT: () => globalThis.__T,
  __ls: () => globalThis.__LS };
"""

# ── fixture payloads + helpers, evaluated inside the Node process ──────
PRELUDE = """
const LABELS = { "betual-nba": "NBA" };
const ua = (active, cp, a, r, avg) => ({ active, checkpoint: cp,
  actual_pace: a, required_pace: r, league_average_pace: avg,
  league_reference_games: 1852, pace_gap: (a != null && r != null) ? a - r : null });
const GAME = (over) => Object.assign({
  game_id: "G1", competition_slug: "betual-nba", live: true, live_reason: null,
  alert: { eligible: true },
  projector: { progress_pct: 80, actual_pts_per_min: 3.0,
               required_pts_per_min: 5.2 },
  under_alert: ua(true, 75, 3.0, 5.2, 4.155),
}, over || {});
// the SAME identity with the condition reading FALSE — how a resolve arrives
const OFF = (over) => GAME(Object.assign({
  under_alert: ua(false, 75, 6.0, 5.0, 4.155) }, over || {}));
const CP = (cp, over) => GAME(Object.assign({
  under_alert: ua(true, cp, 3.0, 5.2, 4.155) }, over || {}));
const OTHER = (over) => GAME(Object.assign({ game_id: "G2" }, over || {}));
const Q3 = (active, over) => GAME(Object.assign({
  under_alert: ua(false, null, null, null, null),
  under_alert_q3_break: { checkpoint: "Q3_BREAK", active,
    score_at_trigger: 120, triggered_line: 190.5, remaining_minutes: 6.2,
    required_pts_per_min: 5.5, league_average_pace: 4.2, gap: 1.3 },
}, over || {}));
const adv = (s) => { globalThis.__T += s * 1000; };
const T0 = () => globalThis.__T;
const fire = () => m.reconcileUnderAlerts([GAME()], LABELS);
const off = () => m.reconcileUnderAlerts([OFF()], LABELS);
const find = (id) => m.UNDER_ALERTS.history.find((r) => r.id === id);
const cooling = (id) => m.underAlertCooldownRemainingMs(id, globalThis.__T);
const state = (id) => {
  const r = find(id) || {};
  return { hist: m.UNDER_ALERTS.history.length,
    active: m.UNDER_ALERTS.active.size,
    isActive: m.UNDER_ALERTS.active.has(id),
    activeIds: Array.from(m.UNDER_ALERTS.active.keys()),
    resolved: !!r.resolved_at,
    refires: r.refire_count || 0,
    lastRefireAt: r.last_refire_at || null,
    triggeredAt: r.triggered_at || null,
    coolingMs: cooling(id) };
};
"""


def _module(js: str, tmp_path: Path) -> Path:
    mod = tmp_path / "cooldown_store.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    return mod


def _run(js: str, tmp_path: Path, expr: str):
    """One isolated Node process per scenario — fresh storage unless the
    scenario asks for a reload."""
    mod = _module(js, tmp_path)
    script = ("const m = require(%s);" % json.dumps(str(mod)) + PRELUDE
              + "console.log(JSON.stringify(%s));" % expr)
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


# ══════════════════════════════════════════════════════════════════════
# A/B/C — the unchanged half: first activation, steady state, resolve
# ══════════════════════════════════════════════════════════════════════

@node
def test_A_first_activation_is_immediate(client, tmp_path):
    """FALSE -> TRUE opens at once.  A cooldown on the FIRST alert would be
    the worst possible failure, so it is asserted explicitly."""
    got = _run(_js(client), tmp_path,
               "(() => { const t = T0(); fire();"
               " return Object.assign(state('%s'), { sameTick:"
               " Date.parse(find('%s').triggered_at) === t }); })()" % (ID, ID))
    assert got["active"] == 1 and got["hist"] == 1, got
    assert got["resolved"] is False, got
    assert got["refires"] == 0, got
    assert got["lastRefireAt"] is None, got    # never re-fired
    assert got["sameTick"] is True, got        # no delay whatsoever
    assert got["coolingMs"] == 0, got          # a standing alert is not cooled


@node
def test_B_six_identical_polls_make_one_record(client, tmp_path):
    """TRUE -> TRUE refreshes; no second record, no re-fire counted."""
    got = _run(_js(client), tmp_path,
               "(() => { for (let i = 0; i < 6; i += 1) fire();"
               " return state('%s'); })()" % ID)
    assert got["hist"] == 1, got
    assert got["active"] == 1, got
    assert got["refires"] == 0, got
    assert got["lastRefireAt"] is None, got


@node
def test_C_resolution_keeps_the_record(client, tmp_path):
    """TRUE -> FALSE resolves; the record is kept, never deleted."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off();"
               " const r = find('%s');"
               " return Object.assign(state('%s'), { reason: r.resolved_reason,"
               "   duration: r.duration_ms }); })()" % (ID, ID))
    assert got["resolved"] is True and got["hist"] == 1, got
    assert got["active"] == 0, got
    assert got["duration"] == 20000, got
    assert got["refires"] == 0, got


# ══════════════════════════════════════════════════════════════════════
# D/F — suppression inside the window
# ══════════════════════════════════════════════════════════════════════

def _suppressed_at(tmp_path, js, t_from_t0):
    """Resolve at T0+20, then attempt the re-fire at T0+<t_from_t0>: the
    directive's own clock, measured from the ACTIVATION."""
    return _run(js, tmp_path,
                "(() => { fire(); adv(20); off(); adv(%d); fire();"
                " return state('%s'); })()" % (t_from_t0 - 20, ID))


@node
def test_D_refire_at_30s_stays_suppressed(client, tmp_path):
    """The directive's T0+30s: no reopen, no refire_count, no
    last_refire_at, no cue, still resolved and inactive."""
    got = _suppressed_at(tmp_path, _js(client), 30)
    assert got["isActive"] is False and got["active"] == 0, got
    assert got["resolved"] is True, got
    assert got["hist"] == 1, got
    assert got["refires"] == 0, got
    assert got["lastRefireAt"] is None, got
    assert got["coolingMs"] == 270000, got     # 300 s - 30 s still to run


@node
def test_D2_suppressed_polls_do_not_consume_the_window(client, tmp_path):
    """The condition may read TRUE for poll after poll inside the window and
    must stay suppressed the whole time — and the window must still be
    measured from the ACTIVATION, not reset by the polls."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off();"
               " for (let i = 0; i < 9; i += 1) { adv(30); fire(); }"   # +270 s
               " const mid = state('%s');"                              # T0+290
               " adv(10); fire();"                                      # T0+300
               " const end = state('%s');"
               " return { mid, end }; })()" % (ID, ID))
    assert got["mid"]["active"] == 0 and got["mid"]["hist"] == 1, got
    assert got["mid"]["refires"] == 0, got
    assert got["mid"]["coolingMs"] == 10000, got   # 10 s left, not reset
    assert got["end"]["active"] == 1, got          # exactly on expiry
    assert got["end"]["refires"] == 1, got


@node
def test_F_refire_at_299s_stays_suppressed(client, tmp_path):
    """One second short of the window — the boundary is inclusive at 300."""
    got = _suppressed_at(tmp_path, _js(client), 299)
    assert got["isActive"] is False, got
    assert got["resolved"] is True, got
    assert got["refires"] == 0, got
    assert got["lastRefireAt"] is None, got
    assert got["coolingMs"] == 1000, got


# ══════════════════════════════════════════════════════════════════════
# E — the reopen at the boundary
# ══════════════════════════════════════════════════════════════════════

@node
def test_E_refire_at_300s_reopens_and_counts(client, tmp_path):
    """At exactly the window the existing reopen behaviour applies: cleared
    resolved_at/duration, refire_count += 1, last_refire_at stamped, ACTIVE."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off(); adv(280); fire();"
               " return Object.assign(state('%s'),"
               "   { coolingAfter: cooling('%s') }); })()" % (ID, ID))
    assert got["active"] == 1 and got["activeIds"] == [ID], got
    assert got["resolved"] is False, got
    assert got["hist"] == 1, got
    assert got["refires"] == 1, got
    assert got["lastRefireAt"] is not None, got
    assert got["coolingAfter"] == 0, got
    # stamped with the RE-FIRE time, not the original trigger
    assert got["lastRefireAt"] != got["triggeredAt"], got
    # ...and the trigger snapshot itself is NEVER rewritten
    assert got["triggeredAt"] == "2026-09-12T15:42:08.000Z", got


# ══════════════════════════════════════════════════════════════════════
# G/H — the clock re-arms from the RE-FIRE, not from the first trigger
# ══════════════════════════════════════════════════════════════════════

@node
def test_G_second_refire_299s_after_the_first_is_suppressed(client, tmp_path):
    """Directive item 6: after a legitimate re-fire the NEXT window is
    measured from last_refire_at, so +299 s from it is still cooled."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off(); adv(280); fire();"   # refire @T0+300
               " off(); adv(299); fire();"                            # @T0+599
               " return state('%s'); })()" % ID)
    assert got["active"] == 0, got
    assert got["resolved"] is True, got
    assert got["refires"] == 1, got          # unchanged by the suppressed poll
    assert got["hist"] == 1, got
    assert got["coolingMs"] == 1000, got


@node
def test_H_third_refire_300s_after_the_second_is_allowed(client, tmp_path):
    """@T0+600 from the re-fire at T0+300 the window has elapsed again."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off(); adv(280); fire();"
               " off(); adv(300); fire();"                            # @T0+600
               " return state('%s'); })()" % ID)
    assert got["active"] == 1, got
    assert got["resolved"] is False, got
    assert got["refires"] == 2, got
    assert got["hist"] == 1, got


# ══════════════════════════════════════════════════════════════════════
# I/J/K — independence: other game, other checkpoint, Q3 BREAK
# ══════════════════════════════════════════════════════════════════════

@node
def test_I_another_game_is_unaffected(client, tmp_path):
    """GAME1|75 cooling must not touch GAME2|75 — a first activation is
    immediate even while a neighbour is in cooldown."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off(); adv(10);"        # T0+30
               " m.reconcileUnderAlerts([OTHER()], LABELS);"
               " return { one: state('%s'), two: state('G2|75'),"
               "   oneCooling: cooling('%s') }; })()" % (ID, ID))
    assert got["one"]["isActive"] is False, got       # still suppressed
    assert got["one"]["resolved"] is True, got
    assert got["oneCooling"] == 270000, got
    assert got["two"]["isActive"] is True, got
    assert got["two"]["refires"] == 0, got         # first activation, no cooldown
    assert got["two"]["lastRefireAt"] is None, got
    assert got["two"]["activeIds"] == ["G2|75"], got


@node
def test_J_another_checkpoint_is_unaffected(client, tmp_path):
    """game_id + checkpoint identity: G1|25 in cooldown must not suppress a
    first activation of G1|75."""
    got = _run(_js(client), tmp_path,
               "(() => { m.reconcileUnderAlerts([CP(25)], LABELS); adv(20);"
               " m.reconcileUnderAlerts([CP(25, { under_alert: ua(false, 25, 6.0, 5.0, 4.155) })],"
               "   LABELS); adv(30);"
               " m.reconcileUnderAlerts([CP(75)], LABELS);"
               " return { cp25: state('G1|25'), cp75: state('G1|75'),"
               "   activeIds: Array.from(m.UNDER_ALERTS.active.keys()) }; })()")
    assert got["cp25"]["resolved"] is True, got
    assert got["cp25"]["coolingMs"] == 250000, got
    assert got["cp75"]["active"] == 1, got
    assert got["cp75"]["refires"] == 0, got
    assert got["activeIds"] == ["G1|75"], got


@node
def test_K_q3_break_is_not_cooled(client, tmp_path):
    """Q3 BREAK keeps its OWN store and its existing lifecycle: a re-fire
    30 s later still reopens immediately, because the cooldown reads
    UNDER_ALERTS history and a Q3 identity is not in it."""
    got = _run(_js(client), tmp_path,
               "(() => { const id = m.q3bAlertId('G1');"
               " m.reconcileQ3BreakAlerts([Q3(true)], LABELS); adv(20);"
               " m.reconcileQ3BreakAlerts([Q3(false)], LABELS);"
               " const resolvedAfterClose = !!(m.Q3B_ALERTS.history[0].resolved_at);"
               " adv(30);"                                        # well inside 300 s
               " m.reconcileQ3BreakAlerts([Q3(true)], LABELS);"
               " const r = m.Q3B_ALERTS.history[0];"
               " return { id, resolvedAfterClose,"
               "   reopened: r.resolved_at === null,"
               "   refires: r.refire_count || 0,"
               "   hist: m.Q3B_ALERTS.history.length,"
               "   active: m.Q3B_ALERTS.active.size,"
               "   underStoreCooling: m.underAlertCooldownRemainingMs(id, globalThis.__T),"
               "   underHist: m.UNDER_ALERTS.history.length }; })()")
    assert got["id"] == "G1|Q3_BREAK", got
    assert got["resolvedAfterClose"] is True, got
    assert got["reopened"] is True, got        # NOT suppressed — unchanged
    assert got["refires"] == 1, got
    assert got["hist"] == 1, got
    assert got["active"] == 1, got
    # the Q3 identity never enters the UNDER store, so nothing to cool
    assert got["underStoreCooling"] == 0, got
    assert got["underHist"] == 0, got


# ══════════════════════════════════════════════════════════════════════
# L — survives a reload (state comes only from localStorage)
# ══════════════════════════════════════════════════════════════════════

@node
def test_L_cooldown_survives_a_reload(client, tmp_path):
    """Re-execute the module body with the SAME storage and clock — a fresh
    page load.  The window must still be enforced, and only then expire."""
    js = _js(client)
    mod = _module(js, tmp_path)
    prelude = PRELUDE.replace("const m = require(", "const m = require(")
    script = (
        "let m = require(%s);" % json.dumps(str(mod))
        + prelude
        + "fire(); adv(20); off();"                    # T0, resolve at T0+20
        + "const persisted = globalThis.__LS['pz.underAlertHistory'];"
        # ── reload: fresh module body, same storage + clock ──
        + "delete require.cache[require.resolve(%s)];" % json.dumps(str(mod))
        + "let m2 = require(%s);" % json.dumps(str(mod))
        + "m2.loadAlertHistory();"
        + "const restored = m2.UNDER_ALERTS.history.length;"
        + "const coolingAfterLoad ="
        + " m2.underAlertCooldownRemainingMs('G1|75', globalThis.__T);"
        + "adv(30);"                                    # T0+50, inside window
        + "m2.reconcileUnderAlerts([GAME()], LABELS);"
        + "const inside = { active: m2.UNDER_ALERTS.active.size,"
        + "  hist: m2.UNDER_ALERTS.history.length,"
        + "  refires: (m2.UNDER_ALERTS.history[0].refire_count || 0) };"
        + "adv(250);"                                   # T0+300
        + "m2.reconcileUnderAlerts([GAME()], LABELS);"
        + "const after = { active: m2.UNDER_ALERTS.active.size,"
        + "  refires: (m2.UNDER_ALERTS.history[0].refire_count || 0) };"
        + "console.log(JSON.stringify({ persisted: !!persisted, restored,"
        + "  coolingAfterLoad, inside, after }));")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    got = json.loads(out.stdout)
    assert got["persisted"] is True, got       # it really went to storage
    assert got["restored"] == 1, got           # one record came back
    assert got["coolingAfterLoad"] == 280000, got
    assert got["inside"]["active"] == 0, got   # suppressed after the reload
    assert got["inside"]["hist"] == 1, got
    assert got["inside"]["refires"] == 0, got
    assert got["after"]["active"] == 1, got    # and expires on time
    assert got["after"]["refires"] == 1, got


# ══════════════════════════════════════════════════════════════════════
# M — never a duplicate record, whatever the polls do
# ══════════════════════════════════════════════════════════════════════

@node
def test_M_no_duplicate_records_across_the_whole_cycle(client, tmp_path):
    """Trigger, resolve, ten suppressed polls, an allowed re-fire, another
    resolve: exactly ONE history record and ONE identity throughout."""
    got = _run(_js(client), tmp_path,
               "(() => { fire(); adv(20); off();"
               " for (let i = 0; i < 10; i += 1) { adv(10); fire(); }"
               " const suppressed = state('%s');"
               " adv(180); fire();"                              # T0+300
               " const reopened = state('%s');"
               " off(); adv(300); fire();"
               " const again = state('%s');"
               " return { suppressed, reopened, again,"
               "   ids: m.UNDER_ALERTS.history.map((r) => r.id) }; })()"
               % (ID, ID, ID))
    assert got["suppressed"]["active"] == 0, got
    assert got["suppressed"]["hist"] == 1, got
    assert got["suppressed"]["refires"] == 0, got
    assert got["reopened"]["active"] == 1, got
    assert got["reopened"]["refires"] == 1, got
    assert got["again"]["refires"] == 2, got
    for step in ("suppressed", "reopened", "again"):
        assert got[step]["hist"] == 1, (step, got[step])
    assert got["ids"] == [ID], got


# ══════════════════════════════════════════════════════════════════════
# source guards — the constant, both cue sites, and the untouched gate
# ══════════════════════════════════════════════════════════════════════

def test_constant_is_exactly_300_seconds(client):
    js = _js(client)
    assert "const UNDER_ALERT_REFIRES_COOLDOWN_S = 300;" in js
    assert ("const UNDER_ALERT_REFIRES_COOLDOWN_MS ="
            " UNDER_ALERT_REFIRES_COOLDOWN_S * 1000;") in js


def test_both_cue_call_sites_obey_the_cooldown(client):
    """A suppressed re-fire must be silent — both places that sound a cue."""
    js = _js(client)
    assert "if (entered && !underAlertRefireCooling(g)) alertAudio(entered);" in js
    assert "if (rose && !underAlertRefireCooling(g)) {" in js
    # ...and no UNGATED cue call survives
    assert "if (entered) alertAudio(entered);" not in js
    assert "if (rose) alertAudio(rose);" not in js


def test_suppression_guard_never_touches_a_standing_alert(client):
    """The guard is scoped to a resolved record with no active entry, so it
    cannot suppress a first activation or a standing alert."""
    js = _js(client)
    assert "if (!act && underAlertCooldownRemainingMs(id, now) > 0) continue;" in js
    # the guard sits BEFORE the identity enters trueNow, so a suppressed poll
    # cannot reach closeUnderAlert either
    guard = js.index("if (!act && underAlertCooldownRemainingMs(id, now)")
    assert js.index("trueNow.add(id);", guard) > guard


def test_the_75_percent_gate_is_unchanged(client):
    """This change adds a cooling-off window and touches no threshold: the
    checkpoint set, the 75% progress gate and the 4% margin are as shipped."""
    src = (ROOT / "blm_v4" / "live_analytics" / "under_alert.py").read_text(
        encoding="utf-8")
    assert "CHECKPOINTS = (25, 50, 75)" in src
    assert "ALERT_PROGRESS_PCT = 75.0" in src
    assert "REQUIRED_MARGIN = 1.04" in src
    # and the browser still derives nothing — one authoritative verdict
    js = _js(client)
    assert "const ok = cp != null && ua.active === true && !gameOver;" in js
