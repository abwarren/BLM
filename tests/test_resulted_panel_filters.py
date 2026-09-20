"""RESULTED-PANEL FILTERS — Date / Time / League / Result (directive 2026-09-19).

The RESULTED panel gains display filters: LEAGUE, DATE, TIME (from/to
window) and RESULT (UNDER / OVER / PUSH / NO LINE / NO FINAL / PENDING).
The contract pinned here:

1. REAL DATA — filters run against the record's actual fields: the verdict
   identity comes from the SAME alertVerdictStateFor the row renders with;
   date/time come from the record's TRIGGER timestamp (the one displayed).
2. DISPLAY ONLY — filtering never mutates the stores, never touches
   settlement (applyFinalOutcomes / sealOutcome / correctOutcome), never
   rewrites a record, and cannot alter any verdict colour: a row hidden by
   a filter and re-shown renders its identical class.
3. RESET — clearResultFilters() (the Clear button) restores the COMPLETE
   unfiltered panel: every record reappears with its own class.
4. TIME WINDOW — HH:MM is minutes-after-midnight UTC, matching the panel's
   own UTC rendering; records outside [from, to] are hidden; unparseable
   bounds hide nothing by themselves.
5. NEW STATES — NO LINE / NO FINAL / PENDING each select exactly the rows
   now labelled with those explicit words; settled UNDER/OVER/PUSH keep
   their colours; NO LINE / NO FINAL rows are never coloured.
6. UNDECIDABLE — a record with no trigger timestamp is never hidden by the
   time/date filters (a filter hides rows, never misfiles them).

The state machine + renderers are extracted from the SHIPPED dashboard asset
between the __ALERT_STORE_BEGIN__ / __ALERT_STORE_END__ markers and executed
in Node.js against stub DOM/storage — the same harness as the other panels'
tests.
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
globalThis.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => c);
const fmtTime = (iso) => !iso ? "--" : new Date(iso).toISOString().slice(11, 19);
function fmtDuration(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000), mm = Math.floor(s / 60), h = Math.floor(mm / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" : "") + pad(mm % 60) + ":" + pad(s % 60);
}
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, Q3B_ALERTS, resultColorClass,
  alertOutcomeClass, alertVerdictStateFor, alertResultHTML, alertOutcomeLine,
  historyAlertsHTML, historyRowHTML, sealOutcome, applyFinalOutcomes,
  loadAlertHistory, saveAlertHistory, resultFilters, RESULT_FILTER_STATES,
  resultedRowFilterFacts, resultedRowVisible, filterResultedRows,
  timeWindowMinutes, triggeredMinutesUTC, anyResultFilterActive,
  clearResultFilters };
"""

# Records whose identities cover every filter surface.  All are RESOLVED
# (the resulted panel); verdicts settle against the immutable lines.
FIXTURE = """
const REC = (o) => Object.assign({
  id: "G|25", game_id: "G", checkpoint: 25, league: "NBA",
  competition_slug: "betual-nba",
  triggered_at: "2026-09-18T13:05:00.000Z",        // 13:05 UTC
  resolved_at: "2026-09-18T21:00:00.000Z", duration_ms: 28200000,
  resolved_reason: "game_finished", triggered_line: 187.5,
  alert_rule: "v4-progress-75-margin-2026-09-14",
  outcome: null,
}, o || {});
const recUnder   = REC({ id: "U|25", game_id: "U",
  outcome: { status: "under", trigger_total: 187.5, final_total: 186 } });
const recOver    = REC({ id: "O|25", game_id: "O",
  outcome: { status: "over", trigger_total: 187.5, final_total: 190 } });
const recPush    = REC({ id: "P|25", game_id: "P",
  outcome: { status: "push", trigger_total: 187.5, final_total: 187.5 } });
const recNoLine  = REC({ id: "NL|25", game_id: "NL",
  triggered_line: null,
  outcome: { status: null, trigger_total: null, final_total: 166 } });
const recNoFinal = REC({ id: "NF|25", game_id: "NF", triggered_line: null,
  outcome: { status: null, trigger_total: null, final_total: null } });
const recPending = REC({ id: "PD|25", game_id: "PD", resolved_at: null,
  triggered_line: null,
  outcome: { status: null, trigger_total: null, final_total: null } });
const recKblLate = REC({ id: "K|25", game_id: "K",
  competition_slug: "betual-kbl",
  triggered_at: "2026-09-17T21:40:00.000Z",        // different date + league
  outcome: { status: "over", trigger_total: 200.5, final_total: 203 } });
const ALL = () => [recUnder, recOver, recPush, recNoLine, recNoFinal,
                   recPending, recKblLate];
const seed = (recs) => {
  m.UNDER_ALERTS.history.length = 0;
  m.UNDER_ALERTS.active.clear();
  m.Q3B_ALERTS.history.length = 0;
  for (const r of recs) m.UNDER_ALERTS.history.push(Object.assign({}, r));
};
const resetFilters = () => m.clearResultFilters();
const rowHtmls = (html) => (html.match(/<li class="al-row[^"]*"/g) || []);
const colourOf = (clsAttr) => {
  const hit = (clsAttr.match(/al-(under|over|push|unknown)/g) || []);
  return hit.length ? hit[0] : null;
};
const rowColour = (html, gid) => {
  const chunk = html.split('<li class="').find((c) => c.includes("Game " + gid));
  return chunk ? colourOf(chunk.split(">")[0]) : "(missing)";
};
const rowWord = (html, gid) => {
  const chunk = html.split('<li class="').find((c) => c.includes("Game " + gid));
  if (!chunk) return "(missing)";
  const mm = chunk.match(/al-outcome[^>]*>([A-Z ]+)</) ||
             chunk.match(/al-pending[^"]*">([A-Z ]+)</);
  return mm ? mm[1] : "";
};
const shownIds = () => m.filterResultedRows(
  m.UNDER_ALERTS.history, m.resultFilters).map((r) => r.game_id);
"""


def _js() -> str:
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


COLOUR_RE = None


def row_colour(html: str, gid: str):
    """The verdict colour class of the rendered row for one game (Python)."""
    import re
    pat = re.compile(r'Game ' + re.escape(gid) + r'\b')
    for chunk in html.split('<li class="')[1:]:
        if pat.search(chunk):
            cls = chunk.split(">")[0]
            for c in ("al-under", "al-over", "al-push", "al-unknown"):
                if c in cls:
                    return c
            return None
    return "(missing)"


def row_word(html: str, gid: str) -> str:
    """The verdict word shown on the rendered row for one game."""
    import re
    pat = re.compile(r'Game ' + re.escape(gid) + r'\b')
    for chunk in html.split('<li class="')[1:]:
        if pat.search(chunk):
            m = (re.search(r'al-outcome[^>]*>([A-Z ]+)<', chunk)
                 or re.search(r'al-pending[^"]*">([A-Z ]+)<', chunk))
            return m.group(1) if m else ""
    return "(missing)"


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
# 1. THE FACTS — filter identity from the same classifier as the row
# ══════════════════════════════════════════════════════════════════════

@node
def test_row_filter_facts_match_the_rendered_state(tmp_path):
    """resultedRowFilterFacts classifies each record exactly as the rendered
    row: under/over/push verdicts, noline/nofinal kinds, pending."""

    r = _run(tmp_path, """
      OUT.facts = ALL().map((rec) => Object.assign(
        { gid: rec.game_id }, m.resultedRowFilterFacts(rec)));
    """)
    got = {f["gid"]: f for f in r["facts"]}
    assert got["U"]["result"] == "under", got
    assert got["O"]["result"] == "over", got
    assert got["P"]["result"] == "push", got
    assert got["NL"]["result"] == "noline", got
    assert got["NF"]["result"] == "nofinal", got
    assert got["PD"]["result"] == "pending", got
    assert got["K"]["result"] == "over", got
    # date/minute identity from the TRIGGER timestamp (UTC)
    assert got["U"]["date"] == "2026-09-18", got
    assert got["U"]["minutes"] == 13 * 60 + 5, got
    assert got["K"]["date"] == "2026-09-17" and got["K"]["minutes"] == 21 * 60 + 40
    assert got["U"]["league"] == "betual-nba" and got["K"]["league"] == "betual-kbl"


@node
def test_time_window_minutes_parses_utc_hhmm(tmp_path):
    r = _run(tmp_path, """
      OUT.parsed = ["13:05", "9:40", "00:00", "23:59"].map(m.timeWindowMinutes);
      OUT.bad = ["", "7:60", "24:00", "abc", "12"].map(m.timeWindowMinutes);
      OUT.trigger = m.triggeredMinutesUTC("2026-09-18T13:05:00.000Z");
      OUT.nullTrigger = m.triggeredMinutesUTC(null);
    """)
    assert r["parsed"] == [13 * 60 + 5, 9 * 60 + 40, 0, 23 * 60 + 59], r
    assert r["bad"] == [None] * 5, r
    assert r["trigger"] == 13 * 60 + 5 and r["nullTrigger"] is None, r


# ══════════════════════════════════════════════════════════════════════
# 2. EACH FILTER — on the actual data
# ══════════════════════════════════════════════════════════════════════

@node
def test_result_filter_selects_exactly_that_state(tmp_path):
    """Each RESULT value shows exactly the rows carrying that verdict/state —
    derived from the same classifier the rows render with."""
    for state, gids in (("under", ["U"]), ("over", ["O", "K"]),
                        ("push", ["P"]), ("noline", ["NL"]),
                        ("nofinal", ["NF"]), ("pending", ["PD"])):
        r = _run(tmp_path, f"""
          seed(ALL());
          m.resultFilters.result = "{state}";
          OUT.shown = shownIds();
          OUT.html = m.historyAlertsHTML();
        """)
        assert sorted(r["shown"]) == sorted(gids), (state, r["shown"])
        assert r["html"].count('<li class="al-row') == len(gids), state


@node
def test_league_and_date_filters_use_record_fields(tmp_path):
    r = _run(tmp_path, """
      seed(ALL());
      m.resultFilters.league = "betual-kbl";
      OUT.kbl = shownIds();
      m.resultFilters.league = "";
      m.resultFilters.date = "2026-09-18";
      OUT.d18 = shownIds();
      m.resultFilters.date = "2026-09-17";
      OUT.d17 = shownIds();
      m.resultFilters.date = "";
      // league AND result combined
      m.resultFilters.league = "betual-nba";
      m.resultFilters.result = "over";
      OUT.combined = shownIds();
    """)
    assert r["kbl"] == ["K"], r
    assert sorted(r["d18"]) == ["NF", "NL", "O", "P", "PD", "U"], r["d18"]
    assert r["d17"] == ["K"], r
    assert r["combined"] == ["O"], r


@node
def test_time_window_filters_on_trigger_minutes_utc(tmp_path):
    r = _run(tmp_path, """
      seed(ALL());   // U..PD fired 13:05 UTC on 09-18; K fired 21:40 on 09-17
      m.resultFilters.timeFrom = "13:00";
      OUT.after1300 = shownIds();   // K fired 21:40 — INSIDE a 13:00+ window
                                    // (time-of-day only, no date bound)
      m.resultFilters.timeFrom = "";
      m.resultFilters.timeTo = "14:00";
      OUT.before1400 = shownIds();
      m.resultFilters.timeFrom = "13:05";
      m.resultFilters.timeTo = "13:05";
      OUT.exact = shownIds();
      m.resultFilters.timeFrom = "";
      m.resultFilters.timeTo = "";
      // date + time narrow to the window on that date
      m.resultFilters.date = "2026-09-17";
      m.resultFilters.timeFrom = "21:00";
      m.resultFilters.timeTo = "22:00";
      OUT.dateAndTime = shownIds();
    """)
    assert len(r["after1300"]) == 7, r          # every fixture is ≥ 13:00
    assert sorted(r["before1400"]) == ["NF", "NL", "O", "P", "PD", "U"], r
    assert sorted(r["exact"]) == ["NF", "NL", "O", "P", "PD", "U"], r
    assert r["dateAndTime"] == ["K"], r
    # a record with NO trigger time is hidden while a time filter is active
    # (strict narrowing: missing field fails the match) — and restoring the
    # filter brings it back
    r2 = _run(tmp_path, """
      seed([REC({ id: "NT|25", game_id: "NT", triggered_at: null })]);
      m.resultFilters.timeFrom = "09:00";
      OUT.shown = shownIds();
      m.resultFilters.timeFrom = "";
      m.resultFilters.date = "2026-09-18";
      OUT.byDate = shownIds();
      m.resultFilters.date = "";
      OUT.afterReset = shownIds();
    """)
    assert r2["shown"] == [], r2               # no trigger time → fails match
    assert r2["byDate"] == [], r2              # no trigger date → fails match
    assert r2["afterReset"] == ["NT"], r2      # restored once filters clear


# ══════════════════════════════════════════════════════════════════════
# 3. DISPLAY ONLY — settlement, stores and colours untouched
# ══════════════════════════════════════════════════════════════════════

@node
def test_filtering_never_mutates_stores_or_settlement(tmp_path):
    """Filtering hides rows in the RENDER only: the history store keeps every
    record, a poll's applyFinalOutcomes still settles hidden rows, and after
    clear ALL rows return with their identical classes."""
    r = _run(tmp_path, """
      seed(ALL());
      OUT.totalBefore = m.UNDER_ALERTS.history.length;
      m.resultFilters.result = "under";
      OUT.shownUnder = shownIds();
      OUT.rowCountUnder = (m.historyAlertsHTML()
        .match(/<li class="al-row/g) || []).length;
      // a poll arrives while the filter hides most rows: settlement is
      // untouched — the hidden OVER record still receives its verdict
      const blocks = { game_id: "O", under_alert_outcome: { by_checkpoint:
        { "25": { status: "over", trigger_total: 187.5, final_total: 190 } } } };
      // recOver is already settled; settle the PENDING one instead
      const pd = m.UNDER_ALERTS.history.find((x) => x.game_id === "PD");
      m.applyFinalOutcomes([{ game_id: "PD", under_alert_outcome:
        { by_checkpoint: { "25": { status: "push", trigger_total: 187.5,
          final_total: 187.5 } } } }]);
      OUT.pdSettledWhileHidden = m.alertOutcomeClass(pd.outcome);
      // clear → complete panel, identical classes
      m.clearResultFilters();
      OUT.shownAfterClear = shownIds();
      OUT.htmlAfterClear = m.historyAlertsHTML();
      OUT.totalAfter = m.UNDER_ALERTS.history.length;
    """)
    assert r["totalBefore"] == 7 and r["totalAfter"] == 7, r
    assert r["shownUnder"] == ["U"], r
    assert r["rowCountUnder"] == 1, r
    assert r["pdSettledWhileHidden"] == "al-push", r   # settled while hidden
    assert sorted(r["shownAfterClear"]) == sorted(
        ["U", "O", "P", "NF", "NL", "PD", "K"]), r
    html = r["htmlAfterClear"]
    assert html.count('<li class="al-row') == 7, html
    assert row_colour(html, "U") == "al-under", html
    assert row_colour(html, "O") == "al-over", html
    assert row_colour(html, "P") == "al-push", html
    assert row_colour(html, "K") == "al-over", html
    assert row_colour(html, "NL") is None and row_colour(html, "NF") is None
    # PD settled while hidden shows PUSH, coloured neutral, after clear
    assert "PUSH" in row_word(html, "PD"), html


@node
def test_filtered_repaint_keeps_verdict_colours_and_new_states(tmp_path):
    """While a filter is active, every SHOWN settled row keeps its verdict
    colour; NO LINE / NO FINAL rows stay uncoloured with their words."""
    r = _run(tmp_path, """
      seed(ALL());
      m.resultFilters.league = "betual-nba";   // hides only K
      OUT.html = m.historyAlertsHTML();
      m.resultFilters.league = "";
      m.resultFilters.result = "noline";
      OUT.nolineHtml = m.historyAlertsHTML();
      m.resultFilters.result = "nofinal";
      OUT.nofinalHtml = m.historyAlertsHTML();
      m.resultFilters.result = "";
    """)
    html = r["html"]
    assert row_colour(html, "U") == "al-under", html
    assert row_colour(html, "O") == "al-over", html
    assert row_colour(html, "P") == "al-push", html
    assert row_colour(html, "NL") is None and "NO LINE" in html, html
    assert row_colour(html, "NF") is None and "NO FINAL" in html, html
    assert "Game K" not in html.replace("Game KBL", ""), html  # hidden
    assert r["nolineHtml"].count('<li class="al-row') == 1, r
    assert "NO LINE" in r["nolineHtml"] and "166" in r["nolineHtml"], r
    assert "al-under" not in r["nolineHtml"] and "al-over" not in r["nolineHtml"]
    assert r["nofinalHtml"].count('<li class="al-row') == 1, r
    assert "NO FINAL" in r["nofinalHtml"], r


# ══════════════════════════════════════════════════════════════════════
# 4. THE BAR — markup, persistence-free state, clear control
# ══════════════════════════════════════════════════════════════════════

@node
def test_filter_bar_renders_all_controls_and_states(tmp_path):
    r = _run(tmp_path, """
      seed(ALL());
      OUT.html = m.historyAlertsHTML();
      OUT.states = m.RESULT_FILTER_STATES;
    """)
    html = r["html"]
    assert r["states"] == ["under", "over", "push", "noline", "nofinal",
                           "pending"], r
    for control in ("rfLeague", "rfDate", "rfTimeFrom", "rfTimeTo",
                    "rfResult", "rfClear"):
        assert f'id="{control}"' in html, control
    assert "betual-kbl" in html and "betual-nba" in html   # league options
    for label in ("NO LINE", "NO FINAL", "PENDING", "UNDER", "OVER", "PUSH"):
        assert label in html, label
    # clear button hidden while no filter is active
    assert 'class="rf-clear"' in html, html
    assert "hidden" in html.split('id="rfClear"')[1][:40], html


@node
def test_filters_do_not_persist_across_a_reload(tmp_path):
    """Filter state is module state: a fresh module load (reload) starts
    unfiltered — the panel shows everything."""
    r = _run(tmp_path, """
      seed(ALL());
      m.resultFilters.result = "under";
      OUT.activeBefore = m.anyResultFilterActive(m.resultFilters);
      OUT.cleared = m.clearResultFilters();
      OUT.activeAfter = m.anyResultFilterActive(m.resultFilters);
      OUT.shown = shownIds();
    """)
    assert r["activeBefore"] is True and r["activeAfter"] is False, r
    assert len(r["shown"]) == 7, r


@node
def test_empty_state_names_the_filters(tmp_path):
    """With filters active and zero matches, the panel says so — it never
    renders the plain 'No alerts triggered' empty state."""
    r = _run(tmp_path, """
      seed(ALL());
      m.resultFilters.result = "push";
      m.resultFilters.league = "betual-kbl";   // no push in kbl fixture
      OUT.html = m.historyAlertsHTML();
    """)
    assert "No alerts match the active filters" in r["html"], r
    assert "No alerts triggered" not in r["html"], r


# ══════════════════════════════════════════════════════════════════════
# 5. SOURCE — the bar ships in the page and the panel paints through it
# ══════════════════════════════════════════════════════════════════════

def test_filter_contract_source_pins():
    js = _js()
    # one visibility predicate, one facts function, one pure applier
    assert js.count("function resultedRowVisible(") == 1
    assert js.count("function resultedRowFilterFacts(") == 1
    assert js.count("function filterResultedRows(") == 1
    # the applier is pure: it must not splice/reorder the stores
    body = js[js.index("function filterResultedRows("):]
    body = body[:body.index("\n}\n")]
    assert "filter(" in body and "sort(" not in body
    # the panel renders THROUGH the applier (not its own subset logic)
    hist = js[js.index("function historyAlertsHTML("):]
    hist = hist[:hist.index("\n}\n")]
    assert "filterResultedRows(" in hist
    # the verdict identity comes from the shipped state function
    facts = js[js.index("function resultedRowFilterFacts("):]
    facts = facts[:facts.index("\n}\n")]
    assert "alertVerdictStateFor(" in facts
    # clear restores everything
    assert "function clearResultFilters(" in js
