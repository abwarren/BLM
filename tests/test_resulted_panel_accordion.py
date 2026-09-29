"""RESULTED ALERTS — accordion panel behaviour (directive 2026-09-28).

The RESULTED ALERTS panel is a reusable accordion.  The contract pinned
here (every rule from the directive):

1.  COLLAPSED BY DEFAULT      — no stored preference ⇒ the panel renders
                                with open=false.
2.  COUNT IN THE HEADER       — the header carries the result count
                                (alertHistoryCount span, painted by
                                paintResultedPanel).
3.  PERSISTED STATE           — a user toggle stores the preference and a
                                fresh init (page load) restores it; the
                                default stays collapsed when unset.
4.  ACTIVE ALERTS SEPARATE    — the ACTIVE surface (activeAlertsHTML) is
                                a different element from the resulted
                                body (alertHistory); collapsing the
                                resulted panel never touches active rows.
5.  RESULT COLOURS UNCHANGED  — rows keep rendering through
                                historyRowHTML / q3HistoryRowHTML and
                                alertOutcomeClass (green/red/neutral).
6.  FIELDS UNCHANGED          — checkpoint, league, game, triggered line,
                                triggered time, actual, required, league
                                average, gap, result all come from the
                                untouched row renderers.
7.  NEWEST FIRST (both kinds) — 75% checkpoint records and Q3 BREAK
                                records merge into ONE list sorted by
                                trigger time descending; a record with
                                no parseable timestamp sorts last and
                                keeps its store order.
8.  PRESENTATION-ONLY         — the stores are never mutated by sorting
                                or collapsing: filtering/sorting reorder
                                a COPY; the history arrays keep their
                                append order.
9.  SCROLLABLE BODY           — .resulted-details .alerts-body is capped
                                (max-height + overflow-y: auto) in the
                                shipped styles.css.
10. REUSABLE ACCORDION        — initAccordionSection() wires ONE
                                <details>: session-persisted state, the
                                ▼/▲ arrow synced from BOTH the header
                                button and a native <summary> click, and
                                it works for any details/button/pref
                                triple (directive: same pattern for
                                future historical/result panels).

The accordion state machine is extracted from the SHIPPED dashboard asset
between the __ACCORDION_BEGIN__ / __ACCORDION_END__ markers and executed
in Node.js against a stub DOM — the same harness as the other resulted
panels' tests.  Row-order assertions run the real historyAlertsHTML()
from the __ALERT_STORE__ block.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
STYLES_CSS = DASH_STATIC / "styles.css"
INDEX_HTML = DASH_STATIC / "index.html"
DASH_JS = DASH_STATIC / "dashboard.js"

STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"
ACCORDION_BEGIN = "/* __ACCORDION_BEGIN__ */"
ACCORDION_END = "/* __ACCORDION_END__ */"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

STUBS = """
Date.now = () => Date.parse("2026-09-28T12:00:00Z");
const __LS = {};
globalThis.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", title: "", open: false,
    dataset: {},
    querySelector: () => null,
    querySelectorAll: () => [],
    _listeners: {},
    addEventListener(kind, fn) { (this._listeners[kind] ||= []).push(fn); },
    dispatch(kind) { (this._listeners[kind] || []).forEach((fn) => fn({
      preventDefault() {}, stopPropagation() {}, key: "" })); },
  };
}
function $(id) { return __EL[id] || (__EL[id] = makeEl(id)); }
globalThis.$ = $;   // the eval scope drives the DOM through the same stubs
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => c);
const fmtTime = (iso) => !iso ? "--" : new Date(iso).toISOString().slice(11, 19);
function fmtDuration(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000), mm = Math.floor(s / 60), h = Math.floor(mm / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" : "") + pad(mm % 60) + ":" + pad(s % 60);
}
function isActuallyLive(g) { return !!(g && g.live === true); }
// faithful copies of the shipped pref helpers (dashboard.js:81-89 —
// outside every extraction marker, same reason the other suites stub
// $/esc): storage-guarded reads/writes of "1"/"0".
const prefGet = (key, dflt) => {
  try {
    const v = localStorage.getItem(key);
    return v == null ? dflt : v === "1";
  } catch (_) { return dflt; }
};
const prefSet = (key, collapsed) => {
  try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch (_) {}
};
"""

STORE_EXPORTS = ""
ACCORDION_EXPORTS = """
module.exports = { UNDER_ALERTS, Q3B_ALERTS, historyAlertsHTML,
  paintResultedPanel, resultFilters,
  initAccordionSection, prefGet, prefSet };
"""


def _js() -> str:
    return DASH_JS.read_text(encoding="utf-8")


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _run(tmp_path: Path, name: str, code_body: str) -> dict:
    js = _js()
    mod = tmp_path / name
    mod.write_text(
        STUBS
        + _block(js, STORE_BEGIN, STORE_END)
        + _block(js, ACCORDION_BEGIN, ACCORDION_END)
        + STORE_EXPORTS + ACCORDION_EXPORTS,
        encoding="utf-8")
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{}};\n{code_body}\n"
            "console.log(JSON.stringify(OUT));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


def _order(html: str) -> list[str]:
    """Game ids in rendered row order (the real rendered list)."""
    return [m.group(1) for m in re.finditer(r"Game (G-[A-Z0-9]+)", html)]


UNDER_REC = {
    "id": "u-1", "game_id": "G-UNDER", "league": "NBA",
    "checkpoint": 25, "alert_rule": "under-75",
    "triggered_at": "2026-09-28T11:00:00Z", "resolved_at": None,
    "actual_pace": 1.5, "required_pace": 4.02,
    "league_average_pace": 5.63, "pace_gap": -1.61,
    "trigger_line": 162.5, "home_team": "Team A", "away_team": "Team B",
    "outcome": { "status": "under", "final_total": 151.0,
                 "authoritative": True },
}
Q3_REC = {
    "id": "q-1", "game_id": "G-Q3BREAK", "league": "NBA",
    "alert_rule": "q3-break",
    "triggered_at": "2026-09-28T11:30:00Z", "resolved_at": None,
    "score_at_trigger": 151.0, "required_pts_per_min": 2.10,
    "league_average_pace": 5.63,
    "triggered_line": 158.5, "home_team": "Team C", "away_team": "Team D",
    "outcome": { "status": "over", "final_total": 164.0,
                 "authoritative": True },
}


# ══════════════════════════════════════════════════════════════════
# 1 + 3. collapsed by default; toggle persists; reload restores it
# ══════════════════════════════════════════════════════════════════

@node
def test_collapsed_by_default_and_persisted(tmp_path):
    out = _run(tmp_path, "acc1.js", """
    // 1. no stored preference → collapsed
    const detA = $("resultedDetails"); const btnA = $("resultedExpandBtn");
    m.initAccordionSection({ detailsId: "resultedDetails",
      buttonId: "resultedExpandBtn",
      prefKey: "pz.resultedAlertsCollapsed", panelLabel: "resulted alerts",
      defaultCollapsed: true });
    OUT.defaultOpen = detA.open;
    OUT.defaultArrow = btnA.textContent;

    // user expands via the header button → stored
    btnA.dispatch("click");
    OUT.afterClickOpen = detA.open;
    OUT.afterClickArrow = btnA.textContent;
    OUT.storedPref = globalThis.localStorage.getItem("pz.resultedAlertsCollapsed");

    // 3. fresh init (page reload): state restored from the preference
    const detB = $("resultedDetails");
    m.initAccordionSection({ detailsId: "resultedDetails",
      buttonId: "resultedExpandBtn",
      prefKey: "pz.resultedAlertsCollapsed", panelLabel: "resulted alerts",
      defaultCollapsed: true });
    OUT.reloadOpen = detB.open;
    OUT.reloadArrow = $("resultedExpandBtn").textContent;
    """ )
    assert out["defaultOpen"] is False            # collapsed by default
    assert out["defaultArrow"] == "▼"             # ▼ ⇒ click expands
    assert out["afterClickOpen"] is True
    assert out["afterClickArrow"] == "▲"          # ▲ ⇒ click collapses
    assert out["storedPref"] == "0"               # persisted expanded
    assert out["reloadOpen"] is True              # restored across reload
    assert out["reloadArrow"] == "▲"


@node
def test_native_summary_click_syncs_arrow_and_persists(tmp_path):
    """A native <summary> click (the '▲ 12' header row itself) toggles the
    <details>; the accordion keeps the arrow and the preference in sync."""
    out = _run(tmp_path, "acc2.js", """
    const det = $("resultedDetails"); const btn = $("resultedExpandBtn");
    m.initAccordionSection({ detailsId: "resultedDetails",
      buttonId: "resultedExpandBtn",
      prefKey: "pz.resultedAlertsCollapsed", panelLabel: "resulted alerts",
      defaultCollapsed: true });
    // user clicks the summary row → native toggle event fires
    det.open = true; det.dispatch("toggle");
    OUT.arrowAfterSummaryClick = btn.textContent;
    OUT.prefAfterSummaryClick =
      globalThis.localStorage.getItem("pz.resultedAlertsCollapsed");
    det.open = false; det.dispatch("toggle");
    OUT.arrowAfterCollapse = btn.textContent;
    OUT.prefAfterCollapse =
      globalThis.localStorage.getItem("pz.resultedAlertsCollapsed");
    """)
    assert out["arrowAfterSummaryClick"] == "▲"
    assert out["prefAfterSummaryClick"] == "0"
    assert out["arrowAfterCollapse"] == "▼"
    assert out["prefAfterCollapse"] == "1"


# ══════════════════════════════════════════════════════════════════
# 7 + 8. ONE newest-first list across both families; stores untouched
# ══════════════════════════════════════════════════════════════════

@node
def test_rows_render_newest_first_across_both_families(tmp_path):
    """The Q3 BREAK record (triggered 11:30) must render ABOVE the 75%
    record (triggered 11:00) — global newest-first, not two blocks."""
    out = _run(tmp_path, "acc3.js", f"""
    m.UNDER_ALERTS.history.push({json.dumps(UNDER_REC)});
    m.Q3B_ALERTS.history.push({json.dumps(Q3_REC)});
    const html = m.historyAlertsHTML();
    OUT.html = html;
    const idx = (gid) => html.indexOf("Game " + gid);
    OUT.q3First = idx("G-Q3BREAK") < idx("G-UNDER");
    """)
    assert out["q3First"] is True
    html = out["html"]
    # 5 + 6. both rows render complete with their own fields + colours
    q3 = html[html.index("Game G-Q3BREAK"):]
    under = html[html.index("Game G-UNDER"):]   # renders through historyRowHTML
    assert "Triggered: 11:30" in q3 and "Required:" in q3
    assert "Triggered: 11:00" in under
    # verdict colours (result classes) survive the reorder:
    # 151 < 162.5 → UNDER row is green; 164 > 158.5 → OVER row is red
    assert "al-under" in html and "al-over" in html


@node
def test_sort_is_presentation_only_stores_keep_append_order(tmp_path):
    out = _run(tmp_path, "acc4.js", f"""
    m.UNDER_ALERTS.history.push({json.dumps(UNDER_REC)});
    m.Q3B_ALERTS.history.push({json.dumps(Q3_REC)});
    m.historyAlertsHTML();                 // render (sorts a COPY)
    OUT.underFirstId = m.UNDER_ALERTS.history[0].id;
    OUT.q3FirstId = m.Q3B_ALERTS.history[0].id;
    OUT.underLen = m.UNDER_ALERTS.history.length;
    OUT.q3Len = m.Q3B_ALERTS.history.length;
    """)
    assert out["underFirstId"] == "u-1" and out["underLen"] == 1
    assert out["q3FirstId"] == "q-1" and out["q3Len"] == 1


@node
def test_record_without_timestamp_sorts_last_and_still_renders(tmp_path):
    rec = dict(UNDER_REC, id="u-2", game_id="G-NOTS", triggered_at=None)
    out = _run(tmp_path, "acc5.js", f"""
    m.UNDER_ALERTS.history.push({json.dumps(rec)});
    m.Q3B_ALERTS.history.push({json.dumps(Q3_REC)});
    const html = m.historyAlertsHTML();
    OUT.notTsLast = html.indexOf("Game G-NOTS") > html.indexOf("Game G-Q3BREAK");
    OUT.bothRendered = html.indexOf("Game G-NOTS") !== -1
      && html.indexOf("Game G-Q3BREAK") !== -1;
    """)
    assert out["notTsLast"] is True and out["bothRendered"] is True


# ══════════════════════════════════════════════════════════════════
# 2. the count lives in the HEADER (independent of body rendering)
# ══════════════════════════════════════════════════════════════════

@node
def test_header_count_counts_every_result_while_collapsed(tmp_path):
    out = _run(tmp_path, "acc6.js", f"""
    m.UNDER_ALERTS.history.push({json.dumps(UNDER_REC)});
    m.Q3B_ALERTS.history.push({json.dumps(Q3_REC)});
    $("resultedDetails").open = false;    // collapsed panel
    m.paintResultedPanel();               // header paints regardless
    OUT.headerCount = $("alertHistoryCount").textContent;
    """)
    assert "2" in out["headerCount"]


# ══════════════════════════════════════════════════════════════════
# 4. ACTIVE surface is a different element; collapsing never touches it
# ══════════════════════════════════════════════════════════════════

def test_active_and_resulted_surfaces_are_distinct_elements():
    html = INDEX_HTML.read_text(encoding="utf-8")
    active = html.index('id="activeAlerts"')
    history = html.index('id="alertHistory"')
    assert active != -1 and history != -1 and active < history
    # the resulted panel is a <details> whose body is #alertHistory —
    # collapsing toggles the DETAILS, never removes the ACTIVE section
    det = html.index('id="resultedDetails"')
    assert det != -1 and det < history


# ══════════════════════════════════════════════════════════════════
# 9. independently scrollable expanded body (shipped CSS)
# ══════════════════════════════════════════════════════════════════

def test_expanded_body_is_independently_scrollable():
    css = STYLES_CSS.read_text(encoding="utf-8")
    m = re.search(
        r"\.resulted-details \.alerts-body\s*\{[^}]*\}", css)
    assert m, "missing .resulted-details .alerts-body scroll cap"
    block = m.group(0)
    assert "max-height" in block and "overflow-y" in block
    assert "auto" in block


# ══════════════════════════════════════════════════════════════════
# 10. reusable: a second, unrelated panel gets the same behaviour
# ══════════════════════════════════════════════════════════════════

@node
def test_same_pattern_serves_a_future_historical_panel(tmp_path):
    out = _run(tmp_path, "acc7.js", """
    const det = $("futurePanel"); const btn = $("futureBtn");
    m.initAccordionSection({ detailsId: "futurePanel", buttonId: "futureBtn",
      prefKey: "pz.futurePanelCollapsed", panelLabel: "settlement history",
      defaultCollapsed: true });
    OUT.defaultOpen = det.open;
    OUT.defaultArrow = btn.textContent;
    btn.dispatch("click");
    OUT.expandedArrow = btn.textContent;
    OUT.expandedTitle = btn.title;
    """)
    assert out["defaultOpen"] is False and out["defaultArrow"] == "▼"
    assert out["expandedArrow"] == "▲"
    assert "settlement history" in out["expandedTitle"]
