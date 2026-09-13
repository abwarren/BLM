"""RESULTED ALERTS — historical review of every triggered alert (directive
RESULTED ALERTS MUST REMAIN VISIBLE AND COLOURED, 2026-09-13).

The panel exists to answer one question: of everything that actually
triggered, which alerts finished UNDER?  So a triggered alert must SURVIVE
its game (LIVE → ENDED), stay in RESULTED/HISTORY, and carry a verdict that
is coloured:

    UNDER → GREEN      final_total <  triggered_line
    OVER  → RED        final_total >  triggered_line
    PUSH  → NEUTRAL    final_total == triggered_line

and every row must name the IMMUTABLE TRIGGERED LINE the verdict was settled
against — never the opening, latest, closing or corrected market line.

Everything runs against the SHIPPED dashboard state machine (the
__PURE_ALERT__ / __ALERT_STORE__ blocks executed in Node.js), and the
rendered HTML asserted here is the real rendered HTML of those rows.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
STYLES_CSS = DASH_STATIC / "styles.css"
INDEX_HTML = DASH_STATIC / "index.html"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

TRIGGER = 187.5

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

STUBS = """
Date.now = () => Date.parse("2026-09-12T20:00:00Z");
const localStorage = { getItem: () => null, setItem: () => {} };
function $(id) { return { innerHTML: "", textContent: "" }; }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => c);
const fmtTime = (iso) => !iso ? "--" : new Date(iso).toISOString().slice(11, 19);
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, reconcileUnderAlerts, activeAlertsHTML,
  historyAlertsHTML, alertOutcomeClass };
"""

# A game as the API serves it.  `live` drives the LIVE → ENDED transition;
# the outcome block carries the backend's own verdict + provenance, which the
# store consumes verbatim (it never compares lines itself).
FIXTURE = """
const LABELS = { "betual-nba": "NBA" };
const base = (o) => Object.assign({
  game_id: "G-A", competition_slug: "betual-nba", live: true, live_reason: null,
  alert: { eligible: true },
  under_alert_eligibility: { eligible: true, reason: "market_live" },
  home_team: "Home Virtual", away_team: "Away Virtual",
  projector: { progress_pct: 26, market_status: "LIVE", live_total_line: 189.5 },
  market: { opening_line: 210.5, closing_line: 205.5, total_line: 189.5 },
  under_alert: { active: true, checkpoint: 25, actual_pace: 1.5,
                 required_pace: 4.69, league_average_pace: 9.0,
                 league_reference_games: 100, pace_gap: -3.19 },
}, o || {});
// one game's outcome block for one checkpoint, as the API serves it
const ocBlock = (cp, status, trig, fin, auth) => ({
  status: fin == null ? "pending" : "resolved",
  final_total: fin,
  resolved_at: fin == null ? null : "2026-09-12T21:00:00Z",
  final_source: fin == null ? null : (auth ? "settled" : "observation"),
  authoritative: auth === true,
  by_checkpoint: { [cp]: { status: status, trigger_total: trig,
    final_total: fin,
    resolved_at: fin == null ? null : "2026-09-12T21:00:00Z" } },
});
const game = (cp, status, trig, fin, o, auth) => base(Object.assign({
  under_alert_outcome: ocBlock(cp, status, trig, fin, auth === true),
}, o || {}));
// an ENDED game: no longer live, still served so its final can land
const ended = (cp, status, trig, fin, o, auth) => game(cp, status, trig, fin,
  Object.assign({ live: false, live_reason: "game_finished", status: "ended",
    under_alert: Object.assign({}, base().under_alert, { active: false }) },
    o || {}), auth);
"""


def _js() -> str:
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _run(tmp_path: Path, script: str):
    js = _js()
    mod = tmp_path / "resulted_store.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{}};\n{FIXTURE}\n{script}\n"
            "console.log(JSON.stringify(Object.assign({"
            "history: m.UNDER_ALERTS.history,"
            "activeSize: m.UNDER_ALERTS.active.size,"
            "actHTML: m.activeAlertsHTML(),"
            "histHTML: m.historyAlertsHTML()}, OUT)));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


def _rows(html: str, gid: str) -> str:
    """The one rendered <li> for a game — the real rendered row."""
    for chunk in html.split('<li class="')[1:]:
        if "Game " + gid in chunk:
            return chunk
    return ""


# ══════════════════════════════════════════════════════════════════════
# 1 + 5. a triggered alert SURVIVES LIVE → ENDED into RESULTED ALERTS
# ══════════════════════════════════════════════════════════════════════

@node
def test_triggered_alert_survives_the_game_ending(tmp_path):
    """LIVE → ENDED: the active row retires, the record STAYS in the
    RESULTED panel with its verdict — it is never dropped."""
    r = _run(tmp_path, """
      // live, alert standing, no final yet
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      OUT.activeWhileLive = m.UNDER_ALERTS.active.size;
      OUT.histWhileLive = m.UNDER_ALERTS.history.length;
      // the game ends and the authoritative final arrives in the same poll
      m.reconcileUnderAlerts([ended(25, "under", 187.5, 186)], LABELS);
      OUT.activeAfterEnd = m.UNDER_ALERTS.active.size;
      OUT.histAfterEnd = m.UNDER_ALERTS.history.length;
      OUT.idsAfterEnd = m.UNDER_ALERTS.history.map((x) => x.id);
      OUT.verdict = m.UNDER_ALERTS.history[0].outcome.status;
      OUT.histHTML = m.historyAlertsHTML();
      OUT.actHTML = m.activeAlertsHTML();
    """)
    assert r["activeWhileLive"] == 1 and r["histWhileLive"] == 1, r
    # the alert leaves the ACTIVE panel...
    assert r["activeAfterEnd"] == 0, r
    # ...but remains, once, in RESULTED ALERTS
    assert r["histAfterEnd"] == 1 and r["idsAfterEnd"] == ["G-A|25"], r
    assert r["verdict"] == "under", r
    assert "No alerts triggered" not in r["histHTML"], r["histHTML"]
    assert "No active UNDER alerts" in r["actHTML"], r["actHTML"]


@node
def test_triggered_alert_survives_the_game_leaving_the_payload(tmp_path):
    """Even once the game is no longer served at all, the resulted record
    persists (with the verdict it settled on)."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([ended(25, "under", 187.5, 186)], LABELS);
      // next poll: the game is gone from the payload entirely
      m.reconcileUnderAlerts([], LABELS);
      OUT.hist = m.UNDER_ALERTS.history.length;
      OUT.active = m.UNDER_ALERTS.active.size;
      OUT.verdict = m.UNDER_ALERTS.history[0].outcome.status;
      OUT.histHTML = m.historyAlertsHTML();
    """)
    assert r["hist"] == 1 and r["active"] == 0, r
    assert r["verdict"] == "under", r
    assert "al-under" in r["histHTML"], r["histHTML"]


# ══════════════════════════════════════════════════════════════════════
# 2 + 7 + 8. the resulted row DISPLAYS game, checkpoint, line, final, verdict
# ══════════════════════════════════════════════════════════════════════

@node
def test_resulted_under_row_is_complete_and_green(tmp_path):
    """Requirement 7: Triggered Line 187.5, Final 186, UNDER — visible and
    GREEN after the game leaves LIVE."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([ended(25, "under", 187.5, 186)], LABELS);
      OUT.row = m.historyAlertsHTML();
    """)
    row = _rows(r["row"], "G-A")
    assert row, r["row"]
    # game
    assert "Game G-A" in row, row
    # checkpoint
    assert "[25%]" in row, row
    # triggered line — prominent, immutable, its own class
    assert 'Triggered Line: <span class="al-trigger-line">187.5</span>' in row, row
    # final total
    assert "Final: <span class=\"al-num\">186.0</span>" in row, row
    # verdict
    assert '<span class="al-outcome">UNDER</span>' in row, row
    # GREEN: the row carries the under class (coloured by the stylesheet)
    assert "al-under" in row, row
    # and no other market line was substituted
    for banned in ("210.5", "205.5", "189.5"):
        assert banned not in row, (banned, row)


@node
def test_resulted_over_row_is_complete_and_red(tmp_path):
    """Requirement 8: Triggered Line 187.5, Final 190, OVER — RED."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([ended(25, "over", 187.5, 190)], LABELS);
      OUT.row = m.historyAlertsHTML();
    """)
    row = _rows(r["row"], "G-A")
    assert 'Triggered Line: <span class="al-trigger-line">187.5</span>' in row, row
    assert "Final: <span class=\"al-num\">190.0</span>" in row, row
    assert '<span class="al-outcome">OVER</span>' in row, row
    assert "al-over" in row, row
    assert "al-under" not in row, row


@node
def test_resulted_push_row_is_neutral(tmp_path):
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 188, null)], LABELS);
      m.reconcileUnderAlerts([ended(25, "push", 188, 188)], LABELS);
      OUT.row = m.historyAlertsHTML();
      OUT.cls = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
    """)
    row = _rows(r["row"], "G-A")
    assert '<span class="al-outcome">PUSH</span>' in row, row
    assert "al-push" in row, row
    assert "al-under" not in row and "al-over" not in row, row
    assert r["cls"] == "al-push", r


# ══════════════════════════════════════════════════════════════════════
# 3. the verdict is the rule applied to the IMMUTABLE TRIGGERED LINE
# ══════════════════════════════════════════════════════════════════════

@node
def test_verdict_brackets_the_immutable_triggered_line(tmp_path):
    """< → UNDER, > → OVER, == → PUSH — all against the SAME sealed line,
    with the verdict word and the colour class agreeing in every case."""
    r = _run(tmp_path, """
      const mk = (gid, status, fin) => [game(25, null, 187.5, null,
        { game_id: gid }), ended(25, status, 187.5, fin, { game_id: gid })];
      m.reconcileUnderAlerts([mk("G-U", "under", 186)[0], mk("G-O", "over", 190)[0],
                              mk("G-P", "push", 187.5)[0]], LABELS);
      m.reconcileUnderAlerts([mk("G-U", "under", 186)[1], mk("G-O", "over", 190)[1],
                              mk("G-P", "push", 187.5)[1]], LABELS);
      OUT.cases = m.UNDER_ALERTS.history.map((x) => [x.id, x.triggered_line,
        x.outcome.final_total, x.outcome.status,
        m.alertOutcomeClass(x.outcome)]);
      OUT.histHTML = m.historyAlertsHTML();
    """)
    got = {c[0]: c for c in r["cases"]}
    assert got["G-U|25"][3] == "under" and got["G-U|25"][4] == "al-under", got
    assert got["G-O|25"][3] == "over" and got["G-O|25"][4] == "al-over", got
    assert got["G-P|25"][3] == "push" and got["G-P|25"][4] == "al-push", got
    # every case settled against the SAME line
    assert {c[1] for c in r["cases"]} == {TRIGGER}, r["cases"]
    assert all(c[2] is not None for c in r["cases"]), r["cases"]


# ══════════════════════════════════════════════════════════════════════
# 4. the colours are real CSS, not just class strings
# ══════════════════════════════════════════════════════════════════════

def test_resulted_verdict_classes_resolve_to_green_red_and_neutral():
    """GREEN #22c55e / RED #ef4444 / neutral — the exact declarations the
    resulted rows' classes resolve to."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    # GREEN
    assert ".al-row.al-under { border-left-color: #22c55e;" in css
    assert ".al-under .al-outcome { color: #22c55e; }" in css
    # RED
    assert ".al-row.al-over { border-left-color: #ef4444;" in css
    assert ".al-over .al-outcome { color: #ef4444; }" in css
    # NEUTRAL
    assert ".al-push .al-outcome { color: var(--muted, #94a3b8); }" in css
    # and the two verdict colours are never swapped
    assert "#ef4444" not in css.split(".al-under .al-outcome")[1].split("\n")[0]
    assert "#22c55e" not in css.split(".al-over .al-outcome")[1].split("\n")[0]


def test_resulted_panel_is_labelled_for_historical_review():
    """Requirement 10: the panel says what it is for, and its legend names
    UNDER / OVER / PUSH in their own colours."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "RESULTED ALERTS" in html
    assert 'id="alertHistory"' in html
    for lg in ('class="lg-under">UNDER<', 'class="lg-over">OVER<',
               'class="lg-push">PUSH<'):
        assert lg in html, lg
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".lg-under { color: #22c55e; font-weight: 700; }" in css
    assert ".lg-over { color: #ef4444; font-weight: 700; }" in css
    # the legend must NOT reuse the row classes — a legend is not an alert
    assert 'class="al-under"' not in html.split("alerts-sub")[1].split("</div>")[0]


# ══════════════════════════════════════════════════════════════════════
# 6. the triggered line is never substituted
# ══════════════════════════════════════════════════════════════════════

@node
def test_resulted_row_never_substitutes_another_market_line(tmp_path):
    """The seeded opening (210.5), a later live line (189.5→192.5) and a
    closing line (205.5) all move; the row still prints the sealed 187.5."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      // the market walks away from the trigger line, then the game ends
      m.reconcileUnderAlerts([game(25, null, 187.5, null, {
        projector: { progress_pct: 30, market_status: "LIVE", live_total_line: 189.5 },
        market: { opening_line: 210.5, closing_line: 205.5, total_line: 189.5 } })],
        LABELS);
      m.reconcileUnderAlerts([game(25, null, 187.5, null, {
        projector: { progress_pct: 40, market_status: "LIVE", live_total_line: 192.5 },
        market: { opening_line: 210.5, closing_line: 205.5, total_line: 192.5 } })],
        LABELS);
      m.reconcileUnderAlerts([ended(25, "under", 187.5, 186, {
        market: { opening_line: 210.5, closing_line: 205.5, total_line: 192.5 } })],
        LABELS);
      OUT.line = m.UNDER_ALERTS.history[0].triggered_line;
      OUT.row = m.historyAlertsHTML();
    """)
    assert r["line"] == TRIGGER, r
    row = _rows(r["row"], "G-A")
    assert 'class="al-trigger-line">187.5<' in row, row
    for banned in ("210.5", "205.5", "192.5", "189.5"):
        assert banned not in row, (banned, row)


# ══════════════════════════════════════════════════════════════════════
# 9. an authoritative correction rewrites the SAME resulted row
# ══════════════════════════════════════════════════════════════════════

@node
def test_correction_rewrites_the_same_resulted_row_in_place(tmp_path):
    """UNDER (green) → corrected OVER (red): one row, one idle line, no
    second alert, and the verdict is recomputed against the SAME line."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([ended(25, "under", 187.5, 186)], LABELS);
      const before = m.historyAlertsHTML();
      const t0 = m.UNDER_ALERTS.history[0].triggered_at;
      // the backend's verified final: 190, not 186 (authoritative)
      m.reconcileUnderAlerts([ended(25, "over", 187.5, 190, {}, true)], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.beforeRow = before;
      OUT.afterRow = m.historyAlertsHTML();
      OUT.size = m.UNDER_ALERTS.history.length;
      OUT.line = rec.triggered_line;
      OUT.status = rec.outcome.status;
      OUT.sameTrigger = rec.triggered_at === t0;
      OUT.corrected = rec.outcome_corrected === true;
    """)
    assert r["size"] == 1, r                    # SAME record — no new alert
    assert r["sameTrigger"] is True, r          # trigger snapshot untouched
    assert r["line"] == TRIGGER, r              # same immutable line
    assert r["status"] == "over", r             # verdict recomputed
    assert r["corrected"] is True, r
    # the SAME row moved from green to red
    assert "al-under" in r["beforeRow"], r["beforeRow"]
    assert "al-over" in r["afterRow"] and "al-under" not in r["afterRow"], r
    row = _rows(r["afterRow"], "G-A")
    assert 'class="al-trigger-line">187.5<' in row, row
    assert ">190.0<" in row, row


# ══════════════════════════════════════════════════════════════════════
# 5 + 11. ACTIVE and RESULTED read ONE authority; polling cannot rewrite
# ══════════════════════════════════════════════════════════════════════

@node
def test_active_and_resulted_share_one_sealed_authority(tmp_path):
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([game(25, "under", 187.5, 186)], LABELS);
      const act = m.UNDER_ALERTS.active.get("G-A|25");
      const rec = m.UNDER_ALERTS.history.find((x) => x.id === "G-A|25");
      OUT.sameOutcome = act.outcome === rec.outcome;
      OUT.sameLine = act.triggered_line === rec.triggered_line;
      OUT.actContainsLine = m.activeAlertsHTML().indexOf("al-trigger-line") !== -1;
      OUT.histContainsLine = m.historyAlertsHTML().indexOf("al-trigger-line") !== -1;
    """)
    assert r["sameOutcome"] is True and r["sameLine"] is True, r
    assert r["actContainsLine"] and r["histContainsLine"], r


@node
def test_ordinary_polling_cannot_rewrite_a_resulted_row(tmp_path):
    """Once resulted, the panel is stable: contradicting ordinary polls, a
    re-open and a stale payload all leave the row exactly as it settled."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([ended(25, "under", 187.5, 186)], LABELS);
      const settled = m.historyAlertsHTML();
      // contradicting ordinary polls + a transient re-open inside the phase
      m.reconcileUnderAlerts([game(25, "over", 187.5, 999)], LABELS);
      m.reconcileUnderAlerts([ended(25, "over", 187.5, 999)], LABELS);
      m.reconcileUnderAlerts([], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.status = rec.outcome.status;
      OUT.final = rec.outcome.final_total;
      OUT.line = rec.triggered_line;
      OUT.size = m.UNDER_ALERTS.history.length;
      OUT.row = m.historyAlertsHTML();
      OUT.identical = OUT.row === settled;
    """)
    assert r["status"] == "under" and r["final"] == 186.0, r
    assert r["line"] == TRIGGER and r["size"] == 1, r
    assert r["identical"] is True, r            # the row never flickered
    assert "al-under" in r["row"] and "999" not in r["row"], r["row"]


@node
def test_multiple_resulted_alerts_keep_their_own_line_and_verdict(tmp_path):
    """Historical review across games/checkpoints: each row keeps its own
    line and its own verdict — nothing bleeds between records."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([
        game(25, null, 187.5, null),
        game(50, null, 192.5, null, { game_id: "G-B",
          under_alert: Object.assign({}, base().under_alert, { checkpoint: 50 }) }),
      ], LABELS);
      m.reconcileUnderAlerts([
        ended(25, "under", 187.5, 186),
        ended(50, "over", 192.5, 196, { game_id: "G-B",
          under_alert: Object.assign({}, base().under_alert, { checkpoint: 50 }) }),
      ], LABELS);
      OUT.rows = m.UNDER_ALERTS.history.map((x) => [x.id, x.triggered_line,
        x.outcome.status, m.alertOutcomeClass(x.outcome)]);
      OUT.histHTML = m.historyAlertsHTML();
    """)
    got = {x[0]: x for x in r["rows"]}
    assert got["G-A|25"] == ["G-A|25", 187.5, "under", "al-under"], got
    assert got["G-B|50"] == ["G-B|50", 192.5, "over", "al-over"], got
    # both rows are present in the rendered panel
    assert 'class="al-trigger-line">187.5<' in r["histHTML"], r["histHTML"]
    assert 'class="al-trigger-line">192.5<' in r["histHTML"], r["histHTML"]


@node
def test_pending_resulted_record_is_never_coloured(tmp_path):
    """A record that closed before its game ended stays NEUTRAL until the
    backend proves a final — never green or red on a guess."""
    r = _run(tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      // the game leaves the ACTIVE gate with no final provable yet
      m.reconcileUnderAlerts([game(25, null, 187.5, null, {
        live: false, live_reason: "game_finished", status: "ended",
        under_alert: Object.assign({}, base().under_alert, { active: false }) },
      )], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.settled = !!(rec.outcome && rec.outcome.status != null);
      OUT.cls = m.alertOutcomeClass(rec.outcome);
      OUT.row = m.historyAlertsHTML();
    """)
    assert r["settled"] is False, r
    assert r["cls"] is None, r
    assert "al-under" not in r["row"] and "al-over" not in r["row"], r["row"]


# ══════════════════════════════════════════════════════════════════════
# 12. source-level: one line definition, one authority, no second alert
# ══════════════════════════════════════════════════════════════════════

def test_resulted_row_reads_the_sealed_line_not_the_block():
    """The RESULTED row's triggered line comes from the record's sealed
    triggered_line (falling back to the legacy import), NOT from the
    per-poll block — there is exactly ONE definition of the line."""
    js = _js()
    body = js[js.index("function alertOutcomeLine"):]
    body = body[:body.index("\n}\n")]
    assert "triggeredLineOf(rec)" in body
    assert "oc.trigger_total" not in body
    assert 'class="al-trigger-line"' in body
    # the same helper is the one the ACTIVE row uses
    act = js[js.index("function activeAlertsHTML"):]
    act = act[:act.index("\n}\n")]
    assert "a.triggered_line" in act
    # one writer of the line, one authority for the sealed values
    assert "function sealTriggeredLine" in js
    assert "function syncActiveSealed" in js
