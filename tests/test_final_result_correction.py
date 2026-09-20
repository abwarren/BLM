"""RESULTED ALERTS — CONTROLLED FINAL-RESULT CORRECTION (directive 2026-09-13).

Normal settlement is WRITE-ONCE.  Ordinary live polling can never restate a
settled UNDER / OVER / PUSH verdict, the triggered line is permanent, and no
duplicate alert may ever be created.

A VERIFIED / CORRECTED game final is a different event and takes a separate,
explicitly gated path:

    correctOutcome(rec, cand)

which may revise an EXISTING record's verdict — and nothing else:

  * the block must be the backend's AUTHORITATIVE final (the scorecard's
    settled ``game_results`` row: ``authoritative === true``).  A verdict
    derived from a terminal OBSERVATION, or any ordinary poll, is refused,
    so polling cannot invoke the path;
  * the record must already be settled (the first settlement is still
    written once, by ``sealOutcome``);
  * the verdict must actually change;
  * it settles against the record's OWN immutable triggered line and
    refuses a block that would restate it;
  * it revises the record IN PLACE — one alert, never a second — and the
    ACTIVE and HISTORY surfaces both read that one sealed record.

Everything runs against the SHIPPED API and the SHIPPED dashboard state
machine (the __PURE_ALERT__ / __ALERT_STORE__ blocks executed in Node.js).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.live_analytics.under_outcome import (
    outcome_status,
    under_alert_outcome,
)
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
STYLES_CSS = DASH_STATIC / "styles.css"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

TRIGGER = 187.5          # the immutable triggered line
ORIGINAL_FINAL = 186     # -> UNDER
CORRECTED_FINAL = 190    # -> OVER


# ══════════════════════════════════════════════════════════════════════
# 1. BACKEND — the provenance that authorises a correction
# ══════════════════════════════════════════════════════════════════════

def _row(minutes_elapsed: float, *, line: float | None, hs: int, as_: int,
         status: str) -> dict:
    q = min(4, int(minutes_elapsed // 10) + 1)
    return {
        "classification": "BETUAL_NBA",
        "captured_at": f"2026-09-13T19:{int(minutes_elapsed):02d}:00.000Z",
        "quarter": q, "clock": "00:00" if minutes_elapsed >= 40 else "05:00",
        "period_label": f"{q}th Quarter" if status == "live" else "Finished",
        "game_status": status, "total_line": line,
        "home_score": hs, "away_score": as_,
    }


def _live_rows():
    return [
        _row(2.0, line=180.5, hs=4, as_=3, status="live"),
        _row(11.0, line=TRIGGER, hs=24, as_=20, status="live"),
        _row(21.0, line=189.5, hs=45, as_=40, status="live"),
    ]


def test_observation_derived_final_is_not_authoritative():
    """A final read off the game's own terminal row is NOT a verified final:
    it settles the verdict but can never authorise a correction."""
    rows = _live_rows() + [_row(40.0, line=192.5, hs=93, as_=93, status="ended")]
    oc = under_alert_outcome(rows)
    assert oc["status"] == "resolved"
    assert oc["final_total"] == 186.0
    assert oc["final_source"] == "observation"
    assert oc["authoritative"] is False
    assert oc["by_checkpoint"][25]["status"] == "under"
    assert oc["by_checkpoint"][25]["trigger_total"] == TRIGGER


def test_settled_record_is_authoritative():
    """The scorecard's settled game-final record (game_results) IS the
    authority — the thing a correction is delivered through."""
    oc = under_alert_outcome(_live_rows(), settled=(CORRECTED_FINAL,
                                                  "2026-09-13T21:00:00Z"))
    assert oc["status"] == "resolved"
    assert oc["final_total"] == 190.0
    assert oc["final_source"] == "settled"
    assert oc["authoritative"] is True
    # the verdict is the rule applied to the corrected final vs the trigger
    assert oc["by_checkpoint"][25]["status"] == "over"
    assert oc["by_checkpoint"][25]["trigger_total"] == TRIGGER
    assert outcome_status(TRIGGER, CORRECTED_FINAL) == "over"


def test_pending_game_carries_no_final_and_no_authority():
    oc = under_alert_outcome(_live_rows())
    assert oc["status"] == "pending"
    assert oc["final_total"] is None
    assert oc["final_source"] is None
    assert oc["authoritative"] is False


def test_provenance_never_enters_the_per_checkpoint_contract():
    """Provenance describes the FINAL, so it is top-level only: the
    per-checkpoint entries (and with them the immutable trigger line
    contract) are unchanged."""
    settled = under_alert_outcome(_live_rows(), settled=(190.0, "x"))
    for cp in (25, 50, 75):
        assert set(settled["by_checkpoint"][cp]) == {
            "status", "trigger_total", "final_total", "resolved_at"}, cp
    assert set(settled) == {"status", "final_total", "resolved_at",
                            "final_source", "authoritative", "by_checkpoint"}


# ── the API serves the provenance end-to-end ───────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    monkeypatch.setattr(v4api, "_pace_reference",
                        lambda conn: {"betual-nba": {"avg_pace": 9.0,
                                                     "games": 100}})
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")     # noqa: E731
    import sqlite3

    def add(gid: str, home: str, away: str):
        st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="comp-b", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=home, away_team=away,
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status="live", first_seen_at=iso(now - timedelta(minutes=60)),
            last_seen_at=iso(now)))

        def snap(t, hs, as_, q, clock, total, status="live",
                 period_label=None):
            st.insert_snapshot(st.upsert_game(PokerBetGame(
                source="PokerBet", source_game_id=gid,
                competition_id="comp-b", competition_slug="betual-nba",
                competition="Betual NBA", region="Virtual Matches",
                game_family="betual", classification="BETUAL_NBA",
                sport="basketball", home_team=home, away_team=away,
                game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
                status="live", first_seen_at=iso(now - timedelta(minutes=60)),
                last_seen_at=iso(now))),
                MarketObservation(
                    source="PokerBet", source_game_id=gid,
                    classification="BETUAL_NBA", captured_at=iso(t),
                    home_team=home, away_team=away, home_score=hs,
                    away_score=as_, period_label=period_label or f"{q}th Quarter",
                    quarter=q, clock=clock, game_status=status,
                    total_line=total, spread=None, w1_odds=None,
                    w2_odds=None, markets_json="{}"), force=True)
        return snap
    return st, add, now, iso, db


def _write_settled(db, gid, final_total, at, status="OK"):
    import sqlite3
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS game_results ("
        "id INTEGER PRIMARY KEY, source_game_id TEXT NOT NULL, "
        "classification TEXT NOT NULL, final_home INTEGER, final_away INTEGER, "
        "final_total INTEGER, result_at TEXT NOT NULL, "
        "final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN', "
        "UNIQUE(source_game_id))")
    con.execute(
        "INSERT INTO game_results (source_game_id, classification, final_home,"
        " final_away, final_total, result_at, final_result_status)"
        " VALUES (?, 'BETUAL_NBA', ?, ?, ?, ?, ?)"
        " ON CONFLICT(source_game_id) DO UPDATE SET"
        " final_total=excluded.final_total, result_at=excluded.result_at,"
        " final_result_status=excluded.final_result_status",
        (gid, final_total // 2, final_total - final_total // 2, final_total,
         at, status))
    con.commit()
    con.close()


def _live(gid):
    app = FastAPI()
    app.include_router(v4_router)
    body = TestClient(app).get("/api/v4/live").json()
    return next(g for g in body["games"] if g["game_id"] == gid)


def test_api_serves_authority_and_the_corrected_verdict(store):
    """The whole path in one place: observation settles UNDER, then the
    backend's verified final arrives (186 -> 190) and the API serves the
    SAME trigger line with a corrected OVER verdict."""
    st, add, now, iso, db = store
    snap = add("6101", "Corr Home Virtual", "Corr Away Virtual")
    snap(now - timedelta(minutes=52), 10, 8, 1, "05:00", 210.5)   # opening
    snap(now - timedelta(minutes=30), 40, 38, 2, "08:00", TRIGGER)
    snap(now - timedelta(minutes=20), 45, 40, 3, "08:00", 189.5)
    snap(now - timedelta(minutes=2), 93, 93, 4, "00:00", 192.5,
         status="ended", period_label="Finished")

    # 1. settled from the terminal observation — not authoritative
    g = _live("6101")
    oc = g["under_alert_outcome"]
    assert oc["authoritative"] is False and oc["final_source"] == "observation"
    assert oc["final_total"] == ORIGINAL_FINAL
    assert oc["by_checkpoint"]["25"]["status"] == "under"
    assert oc["by_checkpoint"]["25"]["trigger_total"] == TRIGGER

    # 2. the backend receives the verified final — 190, not 186
    _write_settled(db, "6101", CORRECTED_FINAL, iso(now))
    g2 = _live("6101")
    oc2 = g2["under_alert_outcome"]
    assert oc2["authoritative"] is True and oc2["final_source"] == "settled"
    assert oc2["final_total"] == CORRECTED_FINAL
    # the verdict follows the corrected final; the line never moved
    assert oc2["by_checkpoint"]["25"]["status"] == "over"
    assert oc2["by_checkpoint"]["25"]["trigger_total"] == TRIGGER
    assert g2["market"]["opening_line"] == 210.5
    assert g2["market"]["total_line"] == 192.5


# ══════════════════════════════════════════════════════════════════════
# 2. FRONTEND — normal settlement vs the controlled correction
# ══════════════════════════════════════════════════════════════════════

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
  historyAlertsHTML, correctOutcome, sealOutcome, alertOutcomeClass };
"""

# a qualifying live game whose checkpoint block is supplied explicitly — the
# store is a pure consumer of the backend's verdict and provenance.
FIXTURE = """
const LABELS = { "betual-nba": "NBA" };
const base = (o) => Object.assign({
  game_id: "G-A", competition_slug: "betual-nba", live: true,
  live_reason: null, alert: { eligible: true },
  under_alert_eligibility: { eligible: true, reason: "market_live" },
  home_team: "Corr Home", away_team: "Corr Away",
  projector: { progress_pct: 26, market_status: "LIVE", live_total_line: 189.5 },
  market: { opening_line: 210.5, closing_line: null, total_line: 189.5 },
  under_alert: { active: true, checkpoint: 25, actual_pace: 1.5,
                 required_pace: 4.69, league_average_pace: 9.0,
                 league_reference_games: 100, pace_gap: -3.19 },
}, o || {});
// the game's outcome block as the API serves it.  `authoritative` marks the
// scorecard's settled game-final record (game_results) — the ONLY block that
// may revise an existing settlement.
const block = (cp, status, trig, fin, authoritative, o) => base(Object.assign({
  under_alert_outcome: Object.assign({
    status: fin == null ? "pending" : "resolved", final_total: fin,
    resolved_at: fin == null ? null : "2026-09-12T21:00:00Z",
    final_source: fin == null ? null
      : (authoritative ? "settled" : "observation"),
    authoritative: authoritative === true,
    by_checkpoint: { [cp]: { status: status, trigger_total: trig,
      final_total: fin, resolved_at: fin == null ? null
        : "2026-09-12T21:00:00Z" } },
  }),
}, o || {}));
// an ORDINARY poll: the same payload shape, no authority (observations only)
const poll = (cp, status, trig, fin, o) =>
  block(cp, status, trig, fin, false, o);
// the AUTHORITATIVE final as the backend serves it after a correction
const auth = (cp, status, trig, fin, o) =>
  block(cp, status, trig, fin, true, o);
"""


def _js() -> str:
    """The SHIPPED dashboard asset (the same file the route serves)."""
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _run(js: str, tmp_path: Path, script: str):
    mod = tmp_path / "correction_store.js"
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


def _settle(js, tmp_path, script):
    """Run a scenario whose FIRST poll settles the alert (ordinary polling,
    observation-derived) and then applies the given correction scenario."""
    return _run(js, tmp_path, script)


# ── 9.1 settled UNDER → authoritative correction → OVER ────────────────

@node
def test_corrected_under_becomes_over(tmp_path):
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      OUT.before = m.UNDER_ALERTS.history[0].outcome.status;
      OUT.actBefore = m.UNDER_ALERTS.active.get("G-A|25").outcome.status;
      // the backend's VERIFIED final: 190, not 186
      m.reconcileUnderAlerts([auth(25, "over", 187.5, 190)], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.after = rec.outcome.status;
      OUT.final = rec.outcome.final_total;
      OUT.line = rec.triggered_line;
      OUT.corrected = rec.outcome_corrected === true;
      OUT.histSize = m.UNDER_ALERTS.history.length;
      OUT.actAfter = m.UNDER_ALERTS.active.get("G-A|25").outcome.status;
      OUT.actHTML = m.activeAlertsHTML();
      OUT.histHTML = m.historyAlertsHTML();
    """)
    assert r["before"] == "under" and r["actBefore"] == "under", r
    assert r["after"] == "over", r                      # verdict revised
    assert r["final"] == 190.0, r                       # ...from the new final
    assert r["line"] == TRIGGER, r                      # line NEVER moved
    assert r["corrected"] is True, r                    # flagged as a correction
    assert r["histSize"] == 1, r                        # one alert, not two
    assert r["actAfter"] == "over", r                   # active follows suit


# ── 9.2 settled OVER → authoritative correction → UNDER ────────────────

@node
def test_corrected_over_becomes_under(tmp_path):
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "over", 187.5, 190)], LABELS);
      OUT.before = m.UNDER_ALERTS.history[0].outcome.status;
      m.reconcileUnderAlerts([auth(25, "under", 187.5, 186)], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.after = rec.outcome.status;
      OUT.final = rec.outcome.final_total;
      OUT.line = rec.triggered_line;
      OUT.histSize = m.UNDER_ALERTS.history.length;
      OUT.actOutcome = m.UNDER_ALERTS.active.get("G-A|25").outcome.status;
    """)
    assert r["before"] == "over", r
    assert r["after"] == "under" and r["final"] == 186.0, r
    assert r["line"] == TRIGGER and r["histSize"] == 1, r
    assert r["actOutcome"] == "under", r


# ── 9.3 PUSH → authoritative correction → UNDER / OVER ─────────────────

@node
def test_corrected_push_becomes_under_and_over(tmp_path):
    js = _js()
    r = _settle(js, tmp_path, """
      // PUSH on an exact line, then a verified final either side of it
      m.reconcileUnderAlerts([poll(25, "push", 188, 188)], LABELS);
      OUT.push = m.UNDER_ALERTS.history[0].outcome.status;
      m.reconcileUnderAlerts([auth(25, "over", 188, 190, { game_id: "G-O" })],
                             LABELS);
      m.reconcileUnderAlerts([poll(25, "push", 188, 188, { game_id: "G-O" })],
                             LABELS);
      const o = m.UNDER_ALERTS.history.find((x) => x.id === "G-O|25");
      OUT.pushToOver = o.outcome.status;
      m.reconcileUnderAlerts([auth(25, "under", 188, 186, { game_id: "G-U" })],
                             LABELS);
      m.reconcileUnderAlerts([poll(25, "push", 188, 188, { game_id: "G-U" })],
                             LABELS);
      const u = m.UNDER_ALERTS.history.find((x) => x.id === "G-U|25");
      OUT.pushToUnder = u.outcome.status;
      OUT.uLine = u.triggered_line;
      OUT.ids = m.UNDER_ALERTS.history.map((x) => x.id).sort();
      OUT.actHTML = m.activeAlertsHTML();
    """)
    assert r["push"] == "push", r
    assert r["pushToOver"] == "over", r
    assert r["pushToUnder"] == "under", r
    assert r["uLine"] == 188.0, r
    # one record per identity — three games, three alerts, no duplicates
    assert r["ids"] == ["G-A|25", "G-O|25", "G-U|25"], r["ids"]


# ── 9.4 the triggered line never changes during a correction ────────────

@node
def test_triggered_line_never_changes_during_correction(tmp_path):
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      const line0 = m.UNDER_ALERTS.history[0].triggered_line;
      // the correction names its own line in the block — it must MATCH
      m.reconcileUnderAlerts([auth(25, "over", 187.5, 190)], LABELS);
      const line1 = m.UNDER_ALERTS.history[0].triggered_line;
      // ...and repeated corrections never restate it
      m.reconcileUnderAlerts([auth(25, "under", 187.5, 180)], LABELS);
      const line2 = m.UNDER_ALERTS.history[0].triggered_line;
      OUT.lines = [line0, line1, line2];
      OUT.histHTML = m.historyAlertsHTML();
    """)
    assert r["lines"] == [TRIGGER, TRIGGER, TRIGGER], r
    assert "187.5" in r["histHTML"], r


@node
def test_a_correction_that_would_move_the_line_is_refused(tmp_path):
    """Integrity guard: a block whose trigger line differs from the record's
    sealed line is not a correction of this alert — it is refused outright."""
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      // authoritative, settled, but naming a DIFFERENT line (192.5)
      m.reconcileUnderAlerts([auth(25, "over", 192.5, 190)], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.status = rec.outcome.status;
      OUT.final = rec.outcome.final_total;
      OUT.line = rec.triggered_line;
      OUT.corrected = rec.outcome_corrected === true;
      // the guard itself, directly
      OUT.refused = m.correctOutcome({ id: "X|25", triggered_line: 187.5,
        outcome: { status: "under", final_total: 186 } },
        { status: "over", final_total: 190, authoritative: true,
          trigger_total: 192.5 });
    """)
    assert r["status"] == "under", r          # unchanged — refused
    assert r["final"] == 186.0, r
    assert r["line"] == TRIGGER, r
    assert r["corrected"] in (False, None), r
    assert r["refused"] is False, r


# ── 9.5 no duplicate history record ────────────────────────────────────

@node
def test_correction_never_creates_a_second_alert(tmp_path):
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      const t0 = m.UNDER_ALERTS.history[0].triggered_at;
      // a correction, then the same authoritative poll repeated, then another
      // correction — the record count and its identity never move
      m.reconcileUnderAlerts([auth(25, "over", 187.5, 190)], LABELS);
      m.reconcileUnderAlerts([auth(25, "over", 187.5, 190)], LABELS);
      m.reconcileUnderAlerts([auth(25, "push", 187.5, 187.5)], LABELS);
      OUT.ids = m.UNDER_ALERTS.history.map((x) => x.id);
      OUT.size = m.UNDER_ALERTS.history.length;
      OUT.sameTrigger = m.UNDER_ALERTS.history[0].triggered_at === t0;
      OUT.status = m.UNDER_ALERTS.history[0].outcome.status;
      OUT.actSize = m.UNDER_ALERTS.active.size;
    """)
    assert r["ids"] == ["G-A|25"], r
    assert r["size"] == 1, r
    assert r["sameTrigger"] is True, r        # the trigger snapshot is intact
    assert r["status"] == "push", r           # last authoritative final wins
    assert r["actSize"] == 1, r


# ── 9.6 active and history stay synchronized ───────────────────────────

@node
def test_active_and_history_share_the_corrected_record(tmp_path):
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      m.reconcileUnderAlerts([auth(25, "over", 187.5, 190)], LABELS);
      const act = m.UNDER_ALERTS.active.get("G-A|25");
      const rec = m.UNDER_ALERTS.history.find((x) => x.id === "G-A|25");
      OUT.sameOutcome = act.outcome === rec.outcome;
      OUT.sameLine = act.triggered_line === rec.triggered_line;
      OUT.actStatus = act.outcome.status;
      OUT.actHTML = m.activeAlertsHTML();
      OUT.histHTML = m.historyAlertsHTML();
    """)
    assert r["sameOutcome"] is True, r        # literally one sealed object
    assert r["sameLine"] is True, r
    assert r["actStatus"] == "over", r
    assert "al-over" in r["actHTML"], r["actHTML"]
    assert "al-over" in r["histHTML"], r["histHTML"]


# ── 9.7 ordinary contradictory polling still cannot overwrite ───────────

@node
def test_ordinary_polling_cannot_overwrite_a_settled_result(tmp_path):
    """The write-once rule is untouched: an observation-derived (or any
    non-authoritative) block — however contradictory — cannot restate a
    settled verdict, and cannot mark a record corrected."""
    js = _js()
    r = _settle(js, tmp_path, """
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      // a contradicting ORDINARY poll, twice, then a pending one
      m.reconcileUnderAlerts([poll(25, "over", 199.5, 200)], LABELS);
      m.reconcileUnderAlerts([poll(25, "over", 199.5, 200)], LABELS);
      m.reconcileUnderAlerts([poll(25, null, 199.5, null)], LABELS);
      const rec = m.UNDER_ALERTS.history[0];
      OUT.status = rec.outcome.status;
      OUT.final = rec.outcome.final_total;
      OUT.line = rec.triggered_line;
      OUT.corrected = rec.outcome_corrected === true;
      OUT.actStatus = m.UNDER_ALERTS.active.get("G-A|25").outcome.status;
      OUT.actHTML = m.activeAlertsHTML();
    """)
    assert r["status"] == "under" and r["final"] == 186.0, r
    assert r["line"] == TRIGGER, r
    assert r["corrected"] in (False, None), r
    assert r["actStatus"] == "under", r
    assert "al-under" in r["actHTML"] and "al-over" not in r["actHTML"], r


@node
def test_only_an_authoritative_block_can_invoke_the_correction(tmp_path):
    """The gate, directly: same numbers, only `authoritative` differs."""
    js = _js()
    r = _run(js, tmp_path, """
      const settled = { id: "X|25", triggered_line: 187.5,
        outcome: { status: "under", final_total: 186 } };
      const cand = (a) => ({ status: "over", final_total: 190,
        trigger_total: 187.5, authoritative: a });
      OUT.pollRefused = m.correctOutcome(
        Object.assign({}, settled, { outcome: { status: "under", final_total: 186 } }),
        cand(false));
      OUT.authApplied = m.correctOutcome(
        Object.assign({}, settled, { outcome: { status: "under", final_total: 186 } }),
        cand(true));
      // ...and an unsettled record is never corrected by this path — that is
      // sealOutcome's job (write-once)
      OUT.unsettledRefused = m.correctOutcome(
        { id: "Y|25", triggered_line: 187.5, outcome: null }, cand(true));
      OUT.noLineRefused = m.correctOutcome(
        { id: "Z|25", triggered_line: 187.5,
          outcome: { status: "under", final_total: 186 } },
        { status: "over", final_total: 190, authoritative: true });
    """)
    assert r["pollRefused"] is False, r
    assert r["authApplied"] is True, r
    assert r["unsettledRefused"] is False, r
    assert r["noLineRefused"] is False, r


# ── 9.8 legacy records: a correction records its provenance ─────────────

@node
def test_correction_is_marked_and_normal_settlement_is_not(tmp_path):
    js = _js()
    r = _run(js, tmp_path, """
      // G-A settles normally; G-B settles then gets a verified correction
      m.reconcileUnderAlerts([poll(25, "under", 187.5, 186)], LABELS);
      m.reconcileUnderAlerts([poll(25, "under", 210.5, 200,
        { game_id: "G-B" })], LABELS);
      // G-B's authoritative final: the SAME line it fired against (210.5)
      m.reconcileUnderAlerts([auth(25, "over", 210.5, 212,
        { game_id: "G-B" })], LABELS);
      OUT.histHTML = m.historyAlertsHTML();
      OUT.aFlag = m.UNDER_ALERTS.history.find((x) => x.id === "G-A|25")
        .outcome_corrected === true;
      OUT.bFlag = m.UNDER_ALERTS.history.find((x) => x.id === "G-B|25")
        .outcome_corrected === true;
      OUT.bStatus = m.UNDER_ALERTS.history.find((x) => x.id === "G-B|25")
        .outcome.status;
    """)
    assert r["aFlag"] is False, r            # normal settlement: unflagged
    assert r["bFlag"] is True, r             # authoritative correction: flagged
    assert r["bStatus"] == "over", r
    assert r["histHTML"].count("al-corrected") == 1, r["histHTML"]
    assert "AUTHORITATIVE FINAL-RESULT CORRECTION" in r["histHTML"], r


# ══════════════════════════════════════════════════════════════════════
# 3. UI — corrected UNDER = green, OVER = red, PUSH = neutral
# ══════════════════════════════════════════════════════════════════════

@node
def test_corrected_verdicts_render_green_red_and_neutral(tmp_path):
    js = _js()
    r = _run(js, tmp_path, """
      // three identities, each settled then corrected to a different verdict
      m.reconcileUnderAlerts([poll(25, "push", 187.5, 187.5)], LABELS);
      m.reconcileUnderAlerts([auth(25, "under", 187.5, 186)], LABELS);
      m.reconcileUnderAlerts([poll(25, "under", 192.5, 186, { game_id: "G-O" })],
                             LABELS);
      m.reconcileUnderAlerts([auth(25, "over", 192.5, 196, { game_id: "G-O" })],
                             LABELS);
      m.reconcileUnderAlerts([poll(25, "under", 188, 186, { game_id: "G-P" })],
                             LABELS);
      m.reconcileUnderAlerts([auth(25, "push", 188, 188, { game_id: "G-P" })],
                             LABELS);
      const rows = m.historyAlertsHTML().split('<li class="').slice(1);
      const pick = (gid) => rows.find((h) => h.indexOf('Game ' + gid) !== -1);
      OUT.under = pick("G-A");
      OUT.over = pick("G-O");
      OUT.push = pick("G-P");
      OUT.classes = m.UNDER_ALERTS.history.map(
        (x) => [x.id, m.alertOutcomeClass(x.outcome)]);
      OUT.correctedMarks = rows.filter(
        (h) => h.indexOf("al-corrected") !== -1).length;
    """)
    # corrected UNDER -> green class on the row + verdict
    assert "al-under" in r["under"] and "UNDER" in r["under"], r["under"]
    # corrected OVER -> red class
    assert "al-over" in r["over"] and "OVER" in r["over"], r["over"]
    # corrected PUSH -> neutral class, and never green or red
    assert "al-push" in r["push"] and "PUSH" in r["push"], r["push"]
    assert "al-under" not in r["push"] and "al-over" not in r["push"], r["push"]
    assert sorted(r["classes"]) == [["G-A|25", "al-under"],
                                    ["G-O|25", "al-over"],
                                    ["G-P|25", "al-push"]], r["classes"]
    # every one of the three is a CORRECTION, and each says so
    assert r["correctedMarks"] == 3, r


def test_corrected_verdict_colours_are_green_red_and_neutral_in_css():
    """The classes the corrected verdicts carry actually resolve to GREEN and
    RED (and neutral for PUSH) in the shipped stylesheet."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    # green = #22c55e, red = #ef4444 — the same pair the settled rows use
    assert ".al-row.al-under { border-left-color: #22c55e;" in css
    assert ".al-under .al-outcome { color: #22c55e; }" in css
    assert ".al-row.al-under .al-result { color: #22c55e; }" in css
    assert ".al-row.al-over { border-left-color: #ef4444;" in css
    assert ".al-over .al-outcome { color: #ef4444; }" in css
    assert ".al-row.al-over .al-result { color: #ef4444; }" in css
    assert ".al-push .al-outcome { color: var(--muted, #94a3b8); }" in css
    # the correction marker itself is informational, never a verdict colour
    assert ".al-corrected { color: #38bdf8;" in css
    assert "#22c55e" not in css.split(".al-corrected")[1].split("}")[0]
    assert "#ef4444" not in css.split(".al-corrected")[1].split("}")[0]


def test_correction_path_is_separate_from_the_write_once_seal():
    """Source-level: the two writers are distinct.  The write-once seal
    performs every FIRST settlement and refuses all later writes; the
    correction is gated on authority + an already-settled record, and it
    never touches the line."""
    js = _js()
    # normal settlement refuses every later write
    assert "if (rec.outcome && rec.outcome.status != null) return false;" in js
    # the correction is gated on authority and on an already-settled record
    assert "if (cand.authoritative !== true) return false;" in js
    assert "if (!prior || prior.status == null) return false;" in js
    assert "if (line == null || line !== rec.triggered_line) return false;" in js
    # the correction writes the verdict and the flag, and NOTHING else
    body = js[js.index("function correctOutcome"):]
    body = body[:body.index("\n}\n")]
    for banned in ("triggered_line =", "resolved_at =", "triggered_at ="):
        assert banned not in body, banned
    assert "rec.outcome = Object.assign({}, cand);" in body
    assert "rec.outcome_corrected = true;" in body
    # exactly ONE writer of a RECORD's triggered line (the write-once seal)
    # and one publisher that copies it onto the active row, PER STORE — the
    # Q3 BREAK store (directive 2026-09-18) has its own independent sync
    # (q3SyncActiveSealed), the same one-publisher shape, not a second
    # writer of the production store's records.
    store = _block(js, STORE_BEGIN, STORE_END)
    assert store.count("rec.triggered_line = ") == 1
    prod_sync = js[js.index("function syncActiveSealed")
                   :js.index("function q3SyncActiveSealed")]
    assert prod_sync.count("act.triggered_line = ") == 1
    q3_sync = js[js.index("function q3SyncActiveSealed"):]
    q3_sync = q3_sync[:q3_sync.index("\n}\n")]
    assert q3_sync.count("act.triggered_line = ") == 1
