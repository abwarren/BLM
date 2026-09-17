"""RESULTED ALERTS — 100% RESULT COVERAGE (directive, 2026-09-16).

Requirement: EVERY row in the RESULTED ALERTS panel must receive a result
colour, decided ONLY by its final calculated result:

    UNDER → UNDER styling      OVER → OVER styling      PUSH → PUSH styling
    PENDING → pending styling ONLY when the game has not actually resulted

No surface may decide colouring from alert-activity, recency or optional
fields, and no RESULTED row may render uncoloured.

THE RULES PINNED HERE
---------------------
1. NORMALIZATION — the authoritative status is compared after normalization:
   ``under`` / ``Under`` / ``UNDER`` / ``"UNDER"`` (quoted) all classify the
   same.  The verdict WORD is produced from the same normalization, so no
   surface can disagree with another about a verdict.
2. ZERO UNCOLOURED RESULTED ROWS — for every settled outcome the rendered
   history row carries a result colour class (the verdict's own class, or the
   explicit ``al-unknown`` fallback for unknown/malformed statuses).  A row
   only renders without a verdict class while the game has no result at all
   (genuinely pending: status null/absent/pending/no_final).
3. EXPLICIT FALLBACK — an unknown/malformed status must NOT silently render
   as a normal uncoloured settled row: it gets the ``al-unknown`` fallback
   state, a ``RESULT UNKNOWN`` word, and is logged once for diagnosis.
4. RE-RENDER INVARIANCE — adding, removing and reordering history records
   can never change any row's colour class: the class is a pure function of
   the record's own settled result.

The state machine + renderers are extracted from the SHIPPED dashboard asset
between the __ALERT_STORE_BEGIN__ / __ALERT_STORE_END__ markers and executed
in Node.js against stub DOM/storage — the same harness the lifecycle tests
use, so this drives the shipped code rather than a copy of it.
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
Date.now = () => Date.parse("2026-09-16T12:00:00Z");
const __LS = {};
globalThis.localStorage = {          // a true global: visible to the harness
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
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
  alertVerdictState, alertResultHTML, alertOutcomeLine, historyAlertsHTML,
  historyRowHTML, sealOutcome, applyFinalOutcomes, loadAlertHistory,
  saveAlertHistory };
"""

# A history record factory — the record shape the reconcile loop creates,
# with the outcome block supplied directly (the store consumes it verbatim).
FIXTURE = """
const rec = (status, o) => Object.assign({
  id: "G-1|25", game_id: "G-1", checkpoint: 25, league: "NBA",
  triggered_at: "2026-09-16T10:00:00.000Z", resolved_at: "2026-09-16T11:00:00.000Z",
  duration_ms: 3600000, resolved_reason: "condition_false",
  actual_pace: 1.5, required_pace: 4.7, league_average_pace: 9.0,
  triggered_line: 187.5, alert_rule: "v4-progress-75-margin-2026-09-14",
  outcome: status === undefined ? null
    : (status === null ? { status: null, final_total: null }
       : { status: status, final_total: 186, trigger_total: 187.5 }),
}, o || {});
const recN = (n, status, o) => rec(status, Object.assign(
  { id: "G-" + n + "|25", game_id: "G-" + n }, o || {}));
// seed the history store with fresh COPIES (the store mutates records)
const seed = (recs) => {
  m.UNDER_ALERTS.history.length = 0;
  m.UNDER_ALERTS.active.clear();
  for (const r of recs) m.UNDER_ALERTS.history.push(Object.assign({}, r));
};
// the first rendered row's class attribute
const rowClassOf = (html) => {
  const mm = html.match(/class="al-row[^"]*"/);
  return mm ? mm[0].slice('class="'.length, -1) : null;
};
// colour classes carried by a row class string (resolved/pending excluded)
const colourClasses = (row) => !row ? [] : row.split(" ").filter((c) =>
  ["al-under", "al-over", "al-push", "al-unknown"].indexOf(c) !== -1);
const VERDICTS = ["al-under", "al-over", "al-push", "al-unknown"];
"""


def _js() -> str:
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


COLOUR_CLASSES = ("al-under", "al-over", "al-push", "al-unknown")


def colourClasses(row: str | None) -> list:
    """The result colour classes carried by a row's class attribute."""
    return [c for c in (row or "").split() if c in COLOUR_CLASSES]


def rowClassOf(html: str) -> str | None:
    """The first al-row class attribute in a rendered panel."""
    i = html.find('class="al-row')
    if i == -1:
        return None
    j = html.find('"', i + len('class="'))
    return html[i + len('class="'):j]


def _run(js: str, tmp_path: Path, script: str):
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
# 1. THE CLASSIFIER — normalization + explicit fallback
# ══════════════════════════════════════════════════════════════════════

@node
def test_result_color_class_normalizes_case_whitespace_and_quotes(tmp_path):
    """under / Under / UNDER / "UNDER" / ' UNDER ' all → al-under (and the
    same for over / push): the comparison is normalized, never raw."""
    r = _run(_js(), tmp_path, """
      OUT.m = {};
      for (const kind of ["under", "over", "push"]) {
        OUT.m[kind] = [
          m.resultColorClass(kind),
          m.resultColorClass(kind.toUpperCase()),
          m.resultColorClass(kind[0].toUpperCase() + kind.slice(1)),
          m.resultColorClass('"' + kind.toUpperCase() + '"'),
          m.resultColorClass("  " + kind + "  "),
          m.resultColorClass("'" + kind.toUpperCase() + "'"),
        ];
      }
    """)
    for kind, variants in r["m"].items():
        expect = "al-" + kind
        assert variants[0] == expect, (kind, variants)
        assert all(v == expect for v in variants), (kind, variants)


@node
def test_pending_family_is_never_a_verdict(tmp_path):
    """PENDING / pending / no_final / unknown / null / undefined / "" → null:
    a game that has not actually resulted keeps the pending look and never
    receives a verdict colour or a fake verdict word."""
    r = _run(_js(), tmp_path, """
      OUT.pending = ["pending", "PENDING", " Pending ", "no_final", "NO_FINAL",
                     "unknown", null, undefined, ""].map(
        (s) => m.resultColorClass(s));
    """)
    assert r["pending"] == [None] * 9, r


@node
def test_unknown_malformed_statuses_get_the_explicit_fallback(tmp_path):
    """A malformed/foreign status → al-unknown (never silently uncoloured,
    never faked as a verdict) and is logged ONCE per distinct value for
    diagnosis."""
    r = _run(_js(), tmp_path, """
      const warns = [];
      const orig = console.warn;
      console.warn = (...a) => { warns.push(a.join(" ")); };
      OUT.junk = ["weird", "void", "1", "nan", "cancelled", "resolved"]
        .map((s) => m.resultColorClass(s));
      OUT.state = m.alertVerdictState({ status: "weird" });
      OUT.warnCount = warns.length;
      OUT.warnText = warns[0] || "";
      console.warn = orig;
    """)
    assert all(v == "al-unknown" for v in r["junk"]), r
    assert r["state"]["cls"] == "al-unknown" and r["state"]["word"] == "RESULT UNKNOWN", r
    assert r["warnCount"] == 1 and "weird" in r["warnText"], r


# ══════════════════════════════════════════════════════════════════════
# 2. THE PANEL — every settled result renders its result colour
# ══════════════════════════════════════════════════════════════════════

@node
def test_every_resulted_row_renders_its_result_colour(tmp_path):
    """UNDER → al-under, OVER → al-over, PUSH → al-push — as a class on the
    row and on the verdict word, with the immutable triggered line and the
    settled final shown.  Lowercase and quoted variants of the SAME result
    classify identically."""
    r = _run(_js(), tmp_path, """
      OUT.rows = {};
      for (const [name, status] of [["under", "under"], ["UNDER", "UNDER"],
                                    ["quoted", '"Under"'], ["over", "over"],
                                    ["push", "push"]]) {
        seed([rec(status)]);
        const html = m.historyAlertsHTML();
        OUT.rows[name] = { row: rowClassOf(html), html };
      }
    """)
    for name, expect in (("under", "al-under"), ("UNDER", "al-under"),
                         ("quoted", "al-under"), ("over", "al-over"),
                         ("push", "al-push")):
        got = r["rows"][name]
        assert colourClasses(got["row"]) == [expect], (name, got)
        html = got["html"]
        assert "Triggered Line:" in html          # the immutable line
        assert "Final:" in html                   # the settled final shown
        assert 'class="al-outcome">' in html      # the verdict word
    assert "al-under" in r["rows"]["under"]["html"]
    assert "al-over" in r["rows"]["over"]["html"]
    assert "al-push" in r["rows"]["push"]["html"]


@node
def test_zero_uncoloured_resulted_rows_across_every_status(tmp_path):
    """THE acceptance invariant: for EVERY status a block can carry, a
    settled row renders WITH a result colour class — the verdict's own class
    for UNDER/OVER/PUSH (any spelling), the explicit al-unknown fallback for
    unknown/malformed values.  Only a genuinely-pending record (status
    null/absent/pending/no_final) renders without a verdict class, and it
    then carries the pending look instead — never silently uncoloured."""
    r = _run(_js(), tmp_path, """
      OUT.checked = [];
      for (const status of ["under", "UNDER", "Under", '"under"', "over",
                            "OVER", "push", "PUSH", "weird", "void",
                            "no_final", "pending", null, undefined]) {
        seed([rec(status)]);
        const html = m.historyAlertsHTML();
        OUT.checked.push({ status: String(status), row: rowClassOf(html) });
      }
    """)
    verdicts = {"under": "al-under", "UNDER": "al-under", "Under": "al-under",
                '"under"': "al-under", "over": "al-over", "OVER": "al-over",
                "push": "al-push", "PUSH": "al-push",
                "weird": "al-unknown", "void": "al-unknown"}
    pending_family = {"null", "undefined", "pending", "no_final"}
    for c in r["checked"]:
        assert c["row"], c                       # a row element always renders
        if c["status"] in pending_family:
            # genuinely pending: no verdict colour class, pending look
            assert colourClasses(c["row"]) == [], c
        else:
            expect = verdicts[c["status"]]
            assert colourClasses(c["row"]) == [expect], c
    # every settled status (not in the pending family) was coloured
    assert len(r["checked"]) == 14, r


@node
def test_genuinely_pending_rows_render_pending_styling_only(tmp_path):
    """A record with no settled outcome (game still running) renders PENDING
    with no verdict colour class and no fake verdict word."""
    r = _run(_js(), tmp_path, """
      seed([rec(undefined)]);                       // no outcome block at all
      OUT.noneHtml = m.historyAlertsHTML();
      OUT.noneWord = m.alertResultHTML(m.UNDER_ALERTS.history[0]);
      seed([rec(null)]);                            // a block with status null
      OUT.nullHtml = m.historyAlertsHTML();
      seed([rec("pending")]);                       // literal PENDING status
      OUT.pendingHtml = m.historyAlertsHTML();
      OUT.pendingWord = m.alertResultHTML(m.UNDER_ALERTS.history[0]);
    """)
    for html in (r["noneHtml"], r["nullHtml"], r["pendingHtml"]):
        assert colourClasses(rowClassOf(html)) == [], html
        assert "al-under" not in html and "al-over" not in html, html
    for word in (r["noneWord"], r["pendingWord"]):
        assert "PENDING" in word and "al-pending" in word, word
        assert "al-outcome" not in word, word    # never a coloured verdict


# ══════════════════════════════════════════════════════════════════════
# 3. THE PIPELINE — the settlement path keeps colouring every row
# ══════════════════════════════════════════════════════════════════════

@node
def test_outcome_blocks_through_the_seal_colour_every_row(tmp_path):
    """The production path: a poll payload's under_alert_outcome blocks go
    through applyFinalOutcomes → sealOutcome → historyAlertsHTML.  Every
    settled status (including normalized/junk variants) lands coloured."""
    r = _run(_js(), tmp_path, """
      seed([recN(1, undefined), recN(2, undefined),
            recN(3, undefined), recN(4, undefined)]);
      const blocks = (status) => ({ by_checkpoint: {
        "25": { status, trigger_total: 187.5, final_total: 186 } } });
      const games = ["under", "OVER", '"Push"', "junk"].map((status, i) => (
        { game_id: "G-" + (i + 1), under_alert_outcome: blocks(status) }));
      m.applyFinalOutcomes(games);
      OUT.html = m.historyAlertsHTML();
      OUT.classes = m.UNDER_ALERTS.history.map(
        (r2) => m.alertOutcomeClass(r2.outcome));
    """)
    assert r["classes"] == ["al-under", "al-over", "al-push", "al-unknown"], r
    assert "al-under" in r["html"] and "al-over" in r["html"], r["html"]
    assert "al-push" in r["html"] and "al-unknown" in r["html"], r["html"]


@node
def test_add_remove_reorder_never_changes_any_rows_colour(tmp_path):
    """THE invariance requirement: a row's colour class is a pure function of
    its own settled result — adding other records, removing records and
    reversing the panel order can never change it, and the persisted records
    keep their classes (a reload cannot lose a colour)."""
    r = _run(_js(), tmp_path, """
      seed([recN(1, "under"), recN(2, "over"), recN(3, "push")]);
      OUT.before = m.UNDER_ALERTS.history.map((x) => m.alertOutcomeClass(x.outcome));
      // ADD two more (one genuinely pending, one malformed)
      seed(m.UNDER_ALERTS.history.concat(
        [recN(9, undefined), recN(8, "weird")]));
      OUT.afterAdd = m.UNDER_ALERTS.history.map((x) => m.alertOutcomeClass(x.outcome));
      // REMOVE the first record
      seed(m.UNDER_ALERTS.history.slice(1));
      OUT.afterRemove = m.UNDER_ALERTS.history.map((x) => m.alertOutcomeClass(x.outcome));
      // REORDER (the panel itself renders .slice().reverse())
      seed(m.UNDER_ALERTS.history.slice().reverse());
      OUT.afterReorder = m.UNDER_ALERTS.history.map((x) => m.alertOutcomeClass(x.outcome));
      m.saveAlertHistory();
      const html = m.historyAlertsHTML();
      OUT.rowClasses = (html.match(/class="al-row[^"]*"/g) || []).map((s) =>
        colourClasses(s.slice('class="'.length, -1)));
      OUT.saved = JSON.parse(localStorage.getItem("pz.underAlertHistory") || "[]")
        .map((x) => m.alertOutcomeClass(x.outcome));
    """)
    base = ["al-under", "al-over", "al-push"]
    assert r["before"] == base, r
    assert r["afterAdd"] == base + [None, "al-unknown"], r
    assert r["afterRemove"] == ["al-over", "al-push", None, "al-unknown"], r
    assert r["afterReorder"] == ["al-unknown", None, "al-push", "al-over"], r
    # the rendered rows (reversed) carry exactly those classes — none lost
    assert r["rowClasses"] == [["al-over"], ["al-push"], [],
                               ["al-unknown"]], r
    # persisted records keep their classes too (survives a reload)
    assert r["saved"] == r["afterReorder"], r


@node
def test_unknown_status_is_logged_once_across_repaints(tmp_path):
    """Repainting the panel many times must not spam the log: one line per
    distinct malformed value, ever (per page load)."""
    r = _run(_js(), tmp_path, """
      const warns = [];
      const orig = console.warn;
      console.warn = (...a) => { warns.push(a.join(" ")); };
      seed([recN(1, "bogus"), recN(2, "bogus")]);
      for (let i = 0; i < 3; i++) m.historyAlertsHTML();   // 3 repaints
      OUT.warnCount = warns.length;
      console.warn = orig;
    """)
    assert r["warnCount"] == 1, r
