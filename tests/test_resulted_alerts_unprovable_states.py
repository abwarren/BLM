"""UNPROVABLE RESULT STATES — explicit NO LINE / NO FINAL / PENDING words on
resulted rows that can carry no verdict (directive 2026-09-19).

The 2026-09-17 production validation proved the classifier colours 100% of
rows that RECEIVE a status; the uncoloured rows are status=None — the
backend's deliberate fail-closed settlement.  This pins the DISPLAY contract
for those rows: none of them may render as an anonymous blank or look like a
bypassed result, and none of them may ever receive a verdict colour class.

    status null + block carries a final  → NO LINE   (line never captured)
    status null + record resolved        → NO FINAL  (backend proves neither)
    status null + nothing resolved       → PENDING   (may still settle)
    settled statuses                     → unchanged verdict colouring

Each unprovable cause is logged ONCE per (cause, game) per page load, naming
the game id, so the data-path defect behind an unclassified row is visible.

The state machine + renderers are extracted from the SHIPPED dashboard asset
between the __ALERT_STORE_BEGIN__ / __ALERT_STORE_END__ markers and executed
in Node.js against stub DOM/storage — the same harness the color-coverage
tests use, so this drives the shipped code rather than a copy of it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

STUBS = """
Date.now = () => Date.parse("2026-09-19T12:00:00Z");
const __LS = {};
globalThis.localStorage = {          // a true global: visible to the harness
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => c);
const fmtTime = (iso) => !iso ? "--" : new Date(iso).toISOString().slice(11, 19);
function fmtDuration(ms) {                     // the shipped implementation
  if (ms == null || !isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  const mm = Math.floor(s / 60);
  const h = Math.floor(mm / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" : "") + pad(mm % 60) + ":" + pad(s % 60);
}
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, resultColorClass, alertOutcomeClass,
  alertVerdictState, alertVerdictStateFor, alertResultHTML, alertOutcomeLine,
  historyAlertsHTML, historyRowHTML, sealOutcome, applyFinalOutcomes,
  loadAlertHistory, saveAlertHistory };
"""

# A history record factory — the record shape the reconcile loop creates,
# with the outcome block supplied directly (the store consumes it verbatim).
FIXTURE = """
const rec = (o) => Object.assign({
  id: "G-1|25", game_id: "G-1", checkpoint: 25, league: "NBA",
  triggered_at: "2026-09-19T10:00:00.000Z", resolved_at: null,
  duration_ms: null, resolved_reason: null,
  actual_pace: 1.5, required_pace: 4.7, league_average_pace: 9.0,
  triggered_line: null, alert_rule: "v4-progress-75-margin-2026-09-14",
  outcome: null,
}, o || {});
const recN = (n, o) => rec(Object.assign(
  { id: "G-" + n + "|25", game_id: "G-" + n }, o || {}));
// a status=None block the backend serves while nothing is provable
const pendingBlock = (o) => Object.assign(
  { status: null, trigger_total: null, final_total: null }, o || {});
// seed the history store with fresh COPIES (the store mutates records)
const seed = (recs) => {
  m.UNDER_ALERTS.history.length = 0;
  m.UNDER_ALERTS.active.clear();
  for (const r of recs) m.UNDER_ALERTS.history.push(Object.assign({}, r));
};
const rowClassOf = (html) => {
  const mm = html.match(/class="al-row[^"]*"/);
  return mm ? mm[0].slice('class="'.length, -1) : null;
};
const colourClasses = (row) => !row ? [] : row.split(" ").filter((c) =>
  ["al-under", "al-over", "al-push", "al-unknown"].indexOf(c) !== -1);
"""


def _js() -> str:
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


def _run(tmp_path: Path, script: str):
    js = _js()
    mod = tmp_path / "resulted_store.js"
    i = js.index(STORE_BEGIN) + len(STORE_BEGIN)
    store = js[i:js.index(STORE_END, i)]
    mod.write_text(STUBS + store + EXPORTS, encoding="utf-8")
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{}};\n{FIXTURE}\n{script}\n"
            "console.log(JSON.stringify(OUT));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


# ══════════════════════════════════════════════════════════════════════
# 1. THE STATE MACHINE — the three explicit unprovable states
# ══════════════════════════════════════════════════════════════════════

@node
def test_null_status_with_a_provable_final_is_no_line(tmp_path):
    """A block with no status but a KNOWN final means the trigger line was
    never observed at the boundary: the explicit NO LINE state — never an
    anonymous blank, never a verdict colour."""
    r = _run(tmp_path, """
      OUT.state = m.alertVerdictState(pendingBlock({ final_total: 186 }));
      OUT.resolved = m.alertVerdictStateFor(
        pendingBlock({ final_total: 186 }),
        { resolved: true, rec: recN(1), onUnprovable: () => {} });
      OUT.unresolved = m.alertVerdictStateFor(
        pendingBlock({ final_total: 186 }),
        { resolved: false, rec: recN(1), onUnprovable: () => {} });
    """)
    for st in (r["state"], r["resolved"], r["unresolved"]):
        assert st["word"] == "NO LINE", st
        assert st["cls"] is None and st["kind"] == "noline", st


@node
def test_null_status_resolved_without_a_final_is_no_final(tmp_path):
    """A record that has RESOLVED (game ended / left the window) and still
    proves neither a final nor a line: the explicit NO FINAL state."""
    r = _run(tmp_path, """
      OUT.state = m.alertVerdictStateFor(pendingBlock(),
        { resolved: true, rec: recN(1), onUnprovable: () => {} });
      OUT.emptyBlock = m.alertVerdictStateFor(null,
        { resolved: true, rec: recN(1), onUnprovable: () => {} });
    """)
    for st in (r["state"], r["emptyBlock"]):
        assert st["word"] == "NO FINAL", st
        assert st["cls"] is None and st["kind"] == "nofinal", st


@node
def test_null_status_unresolved_is_plain_pending(tmp_path):
    """Nothing resolved yet: the ordinary PENDING state — it may still
    settle, so it is NOT an unprovable cause and must never log."""
    r = _run(tmp_path, """
      const warns = [];
      const orig = console.warn;
      console.warn = (...a) => { warns.push(a.join(" ")); };
      OUT.state = m.alertVerdictStateFor(pendingBlock(),
        { resolved: false, rec: recN(1), onUnprovable: () => {} });
      OUT.plain = m.alertVerdictState(pendingBlock());
      console.warn = orig;
      OUT.warnCount = warns.length;
    """)
    for st in (r["state"], r["plain"]):
        assert st["word"] == "PENDING" and st["cls"] is None, st
        assert st.get("kind") == null_kind(), st
    assert r["warnCount"] == 0, r


def null_kind():
    """PENDING carries no unprovable kind marker (JSON has no undefined)."""
    return None


COLOUR_CLASSES = ("al-under", "al-over", "al-push", "al-unknown")


def colour_classes(row: str | None) -> list:
    """The result colour classes carried by a rendered row class attribute."""
    return [c for c in (row or "").split() if c in COLOUR_CLASSES]


# ══════════════════════════════════════════════════════════════════════
# 2. THE PANEL — explicit states render; no verdict colour ever
# ══════════════════════════════════════════════════════════════════════

@node
def test_resulted_rows_render_their_explicit_unprovable_state(tmp_path):
    """Every status=None row states WHY it has no verdict: NO LINE (with the
    known final shown), NO FINAL, or PENDING — never an empty result line,
    and NEVER a verdict colour class on the row."""
    r = _run(tmp_path, """
      OUT.rows = {};
      // NO LINE: final provable, line never captured
      seed([recN(1, { resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: pendingBlock({ final_total: 186 }) })]);
      OUT.rows.noline = { cls: rowClassOf(m.historyAlertsHTML()),
        html: m.historyAlertsHTML(),
        line: m.alertOutcomeLine(m.UNDER_ALERTS.history[0]) };
      // NO FINAL: resolved, backend proves neither
      seed([recN(2, { resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: pendingBlock() })]);
      OUT.rows.nofinal = { cls: rowClassOf(m.historyAlertsHTML()),
        html: m.historyAlertsHTML(),
        line: m.alertOutcomeLine(m.UNDER_ALERTS.history[0]) };
      // PENDING: nothing resolved — may still settle
      seed([recN(3, { outcome: pendingBlock() })]);
      OUT.rows.pending = { cls: rowClassOf(m.historyAlertsHTML()),
        html: m.historyAlertsHTML(),
        line: m.alertOutcomeLine(m.UNDER_ALERTS.history[0]) };
    """)
    noline, nofinal, pending = (r["rows"][k] for k in
                                ("noline", "nofinal", "pending"))
    # the explicit words appear on the rendered rows
    assert "NO LINE" in noline["line"] and "no line captured" in noline["line"]
    assert "Final:" in noline["line"] and "186" in noline["line"], noline
    assert "NO FINAL" in nofinal["line"], nofinal
    assert "PENDING" in pending["line"], pending
    # none of the three is an anonymous blank
    for got in (noline, nofinal, pending):
        assert got["line"].strip() != "", got
    # and none ever receives a verdict colour class
    for got in (noline, nofinal, pending):
        assert colour_classes(got["cls"]) == [], got
        assert "al-under" not in got["html"], got
        assert "al-over" not in got["html"], got
        assert "al-push" not in got["html"], got


@node
def test_active_surface_renders_the_explicit_states_too(tmp_path):
    """The ACTIVE row's Result line uses the same explicit states — a settled
    NO LINE shows on the active row as well, never a fake verdict."""
    r = _run(tmp_path, """
      OUT.noline = m.alertResultHTML(rec({
        resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: pendingBlock({ final_total: 186 }) }));
      OUT.pending = m.alertResultHTML(rec({ outcome: pendingBlock() }));
      OUT.nofinal = m.alertResultHTML(rec({
        resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: pendingBlock() }));
    """)
    assert "NO LINE" in r["noline"] and "al-noline" in r["noline"], r
    assert "PENDING" in r["pending"] and "al-noline" not in r["pending"], r
    assert "NO FINAL" in r["nofinal"] and "al-nofinal" in r["nofinal"], r
    # never a verdict-coloured outcome on an unprovable row
    for key in ("noline", "pending", "nofinal"):
        assert "al-outcome" not in r[key], (key, r[key])


# ══════════════════════════════════════════════════════════════════════
# 3. THE LOG — each cause logged once per game, PENDING never
# ══════════════════════════════════════════════════════════════════════

@node
def test_unprovable_causes_log_once_per_game_and_pending_never(tmp_path):
    """NO LINE / NO FINAL each log once per (cause, game) with the game id;
    ordinary PENDING rows never log; settled rows never log."""
    r = _run(tmp_path, """
      const warns = [];
      const orig = console.warn;
      console.warn = (...a) => { warns.push(a.join(" ")); };
      // NO LINE for two different games + the same game again
      seed([recN(1, { resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: pendingBlock({ final_total: 186 }) }),
            recN(2, { resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: pendingBlock({ final_total: 190 }) })]);
      m.historyAlertsHTML();
      m.historyAlertsHTML();          // repaint: no new lines
      seed(m.UNDER_ALERTS.history.concat([
        recN(1, { resolved_at: "2026-09-19T11:00:00.000Z",
          outcome: pendingBlock({ final_total: 186 }) })]));
      m.historyAlertsHTML();          // same game again: still one line
      // NO FINAL for a third game
      seed(m.UNDER_ALERTS.history.concat([
        recN(3, { resolved_at: "2026-09-19T11:00:00.000Z",
          outcome: pendingBlock() })]));
      m.historyAlertsHTML();
      // PENDING rows (nothing resolved) must never log
      seed([recN(4, { outcome: pendingBlock() })]);
      m.historyAlertsHTML();
      // a settled row must never log either
      seed([recN(5, { resolved_at: "2026-09-19T11:00:00.000Z",
        outcome: { status: "under", trigger_total: 187.5,
                   final_total: 186 } })]);
      m.historyAlertsHTML();
      console.warn = orig;
      OUT.warnCount = warns.length;
      OUT.warns = warns;
    """)
    # 2 games × NO LINE + 1 game × NO FINAL — no cause or game repeats
    assert r["warnCount"] == 3, r["warns"]
    noline = [w for w in r["warns"] if "NO LINE" in w]
    nofinal = [w for w in r["warns"] if "NO FINAL" in w]
    assert len(noline) == 2 and len(nofinal) == 1, r["warns"]
    # the game id is named so the data-path defect is traceable
    assert any("G-1" in w for w in noline), r["warns"]
    assert any("G-2" in w for w in noline), r["warns"]
    assert any("G-3" in w for w in nofinal), r["warns"]
    assert not any("G-4" in w or "G-5" in w for w in r["warns"]), r["warns"]


# ══════════════════════════════════════════════════════════════════════
# 4. THE SEAL — an upgraded verdict replaces the explicit state
# ══════════════════════════════════════════════════════════════════════

@node
def test_settled_verdict_still_upgrades_over_the_explicit_states(tmp_path):
    """A NO LINE / PENDING row that later receives a real settled verdict is
    upgraded by the SAME seal and renders its verdict colour — the explicit
    state is a display of the gap, never a settlement of its own."""
    r = _run(tmp_path, """
      seed([recN(1, { outcome: pendingBlock({ final_total: 186 }) })]);
      OUT.beforeLine = m.alertOutcomeLine(m.UNDER_ALERTS.history[0]);
      OUT.beforeCls = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
      const games = [{ game_id: "G-1", under_alert_outcome:
        { by_checkpoint: { "25": { status: "under", trigger_total: 187.5,
          final_total: 186 } } } }];
      m.applyFinalOutcomes(games);
      OUT.afterCls = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
      OUT.afterRow = rowClassOf(m.historyAlertsHTML());
      OUT.afterLine = m.alertOutcomeLine(m.UNDER_ALERTS.history[0]);
    """)
    assert r["beforeLine"].strip() != "" and "NO LINE" in r["beforeLine"], r
    assert r["beforeCls"] is None, r
    assert r["afterCls"] == "al-under", r
    assert "al-under" in (r["afterRow"] or ""), r
    assert "UNDER" in r["afterLine"], r


# ══════════════════════════════════════════════════════════════════════
# 5. THE STYLES — the explicit states are styled, verdicts unchanged
# ══════════════════════════════════════════════════════════════════════

def test_unprovable_states_are_styled_and_verdicts_unchanged():
    """CSS carries the .al-noline / .al-nofinal display states (and the
    data-attribute hooks the rendered spans carry) while every settled
    verdict rule stays exactly as validated on 2026-09-17."""
    css = (DASH_STATIC / "styles.css").read_text(encoding="utf-8")
    assert ".al-noline" in css
    assert ".al-nofinal" in css
    assert '[data-noline]' in css and '[data-nofinal]' in css
    # the settled verdict palette is untouched
    assert ".al-row.al-under { border-left-color: #22c55e;" in css
    assert ".al-row.al-over { border-left-color: #ef4444;" in css
    assert ".al-row.al-push { border-left-color: rgba(148, 163, 184, .8);" in css
    assert ".al-row.al-unknown { border-left-color: #f59e0b;" in css


def test_source_single_classifier_and_explicit_state_branch():
    """Source-level: the explicit states live in the ONE verdict-state
    function the surfaces share, and the no-verdict branch never returns a
    verdict class."""
    js = _js()
    assert "function alertVerdictStateFor" in js
    assert js.count("function alertVerdictStateFor") == 1
    body = js[js.index("function alertVerdictStateFor"):]
    body = body[:body.index("\n}\n")]
    for token in ('"NO LINE"', '"NO FINAL"', '"PENDING"'):
        assert token in body, token
    # every no-verdict branch is cls:null — a colour only from a verdict
    # (3 unprovable branches + the pending-family status branch = 4)
    assert body.count("cls: null") == 4, body
    # ...and no branch in the no-status path yields a verdict colour class
    no_status = body[:body.index("const cls = resultColorClass")]
    assert no_status.count("cls: null") == 3, no_status
