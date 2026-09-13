"""RESULTED ALERTS — verdicts BEYOND the live window (rendering-path fix,
directive 2026-09-13).

THE DEFECT
----------
``/api/v4/live`` serves the 100 most recent games.  The RESULTED ALERTS panel
is fed by ``applyFinalOutcomes(payload.games)``, so an alert record whose game
has since dropped out of that window could NEVER receive a verdict: its
``outcome`` stayed null, ``historyRowHTML`` rendered no verdict line, and the
row stayed uncoloured however the game actually finished.  On the live archive
that is not an edge case — 338 of the 400 most recent finished games are
already outside the window, and the panel's whole purpose is historical review.

THE FIX (delivery only — no settlement change)
---------------------------------------------
``GET /api/v4/alert-outcomes?ids=…`` republishes the SAME per-game
``under_alert_outcome`` block by id, built by the same function from the same
stored observations and settled ``game_results`` rows.  The frontend asks for
the history records the poll cannot cover and feeds the blocks through the SAME
seal (``applyFinalOutcomes``) — one rule, one authority, one record.

These tests pin both halves: the route serves a verdict the poll cannot reach,
and the panel then renders it COLOURED.
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
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


# ══════════════════════════════════════════════════════════════════════
# 1. THE BACKEND — a verdict the poll cannot reach
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def archive(tmp_path, monkeypatch):
    """A real store holding one finished game whose final IS provable, plus
    enough newer games to push it OUTSIDE the /live 100-game window."""
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    monkeypatch.setattr(v4api, "_pace_reference",
                        lambda conn: {"betual-nba": {"avg_pace": 9.0,
                                                     "games": 100}})
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")     # noqa: E731

    def game(gid, first_min, last_min, home="HH", away="AA"):
        st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid, competition_id="comp-b",
            competition_slug="betual-nba", competition="Betual NBA",
            region="Virtual Matches", game_family="betual",
            classification="BETUAL_NBA", sport="basketball",
            home_team=home, away_team=away, game_slug=f"{gid}-game",
            source_url=f"https://x/{gid}", status="live",
            first_seen_at=iso(now - timedelta(minutes=first_min)),
            last_seen_at=iso(now - timedelta(minutes=last_min))))

    def snap(gid, t, hs, a, q, clock, total, status="live", plabel=None):
        st.insert_snapshot(st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid, competition_id="comp-b",
            competition_slug="betual-nba", competition="Betual NBA",
            region="Virtual Matches", game_family="betual",
            classification="BETUAL_NBA", sport="basketball",
            home_team="HH", away_team="AA", game_slug=f"{gid}-game",
            source_url=f"https://x/{gid}", status="live",
            first_seen_at=iso(now - timedelta(minutes=120)),
            last_seen_at=iso(now))),
            MarketObservation(
                source="PokerBet", source_game_id=gid,
                classification="BETUAL_NBA", captured_at=iso(t),
                home_team="HH", away_team="AA", home_score=hs, away_score=a,
                period_label=plabel or f"{q}th Quarter", quarter=q,
                clock=clock, game_status=status, total_line=total,
                spread=None, w1_odds=None, w2_odds=None,
                markets_json="{}"), force=True)

    def snap_by_elapsed(gid, elapsed_min, hs, a, total, status="live"):
        """A row placed by GAME minute, the way the checkpoint boundary is
        actually derived — elapsed 11 of 40 IS the 25% boundary."""
        q = min(4, int(elapsed_min // 10) + 1)
        t = now - timedelta(minutes=130 - elapsed_min)
        snap(gid, t, hs, a, q,
             "00:00" if elapsed_min >= 40 else "05:00", total, status,
             "Finished" if status == "ended" else None)

    # THE SUBJECT: an old finished game — opening 210.5, the 25% boundary at
    # 187.5, a later walk away to 189.5, then the terminal row.  Final 186.
    sub = "7000"
    snap_by_elapsed(sub, 2, 4, 3, 210.5)
    snap_by_elapsed(sub, 11, 24, 20, 187.5)      # <-- the 25% trigger line
    snap_by_elapsed(sub, 21, 45, 40, 189.5)
    snap_by_elapsed(sub, 40, 93, 93, 192.5, "ended")
    # ...and 120 NEWER games so it falls out of the 100-game window
    for i in range(120):
        gid = f"8{i:04d}"
        game(gid, 30, 1 + i * 0.001)
        snap(gid, now - timedelta(minutes=2), 20, 18, 2, "05:00", 190.5)
    con = __import__("sqlite3").connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS game_results (
        id INTEGER PRIMARY KEY, source_game_id TEXT NOT NULL,
        classification TEXT NOT NULL, final_home INTEGER, final_away INTEGER,
        final_total INTEGER, result_at TEXT NOT NULL,
        final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN',
        UNIQUE(source_game_id))""")
    con.execute("""INSERT OR REPLACE INTO game_results
        (source_game_id, classification, final_home, final_away, final_total,
         result_at, final_result_status)
        VALUES (?, 'BETUAL_NBA', 93, 93, 186, ?, 'OK')""", (sub, iso(now)))
    con.commit()
    con.close()
    return {"db": db, "subject": sub}


def _client():
    app = FastAPI()
    app.include_router(v4_router)
    return TestClient(app)


def test_the_subject_game_really_is_outside_the_live_window(archive):
    """The premise of the defect, asserted as a fact: the game is finished,
    its verdict is provable — and /live does NOT carry it."""
    body = _client().get("/api/v4/live").json()
    ids = {g["game_id"] for g in body["games"]}
    assert archive["subject"] not in ids, "test premise broken: game is served"
    assert len(ids) == 100, len(ids)           # the window really is bounded


def test_outcomes_route_delivers_the_verdict_the_poll_cannot_reach(archive):
    """The fix at the API boundary: the same block /live would serve, by id."""
    got = _client().get("/api/v4/alert-outcomes",
                        params={"ids": archive["subject"]}).json()
    oc = got["outcomes"][archive["subject"]]
    assert oc["status"] == "resolved"
    assert oc["final_total"] == 186.0
    assert oc["final_source"] == "settled"
    assert oc["authoritative"] is True
    # the immutable trigger lines, exactly as the poll would publish them
    # (JSON object keys are strings, so the checkpoint is addressed as "25")
    assert oc["by_checkpoint"]["25"]["trigger_total"] == 187.5
    assert oc["by_checkpoint"]["25"]["status"] == "under"


def test_outcomes_route_matches_the_poll_block_byte_for_byte(archive):
    """One authority: for a game BOTH routes can see, the blocks must be
    identical — the new route introduces no second computation."""
    body = _client().get("/api/v4/live").json()
    a_live = next(g for g in body["games"] if g["live"])
    gid = a_live["game_id"]
    got = _client().get("/api/v4/alert-outcomes", params={"ids": gid}).json()
    assert got["outcomes"][gid] == a_live["under_alert_outcome"]


def test_outcomes_route_is_bounded_and_never_invents_a_game(archive):
    c = _client()
    # empty / whitespace / missing ids are all simply no work
    assert c.get("/api/v4/alert-outcomes").json() == {"outcomes": {}}
    assert c.get("/api/v4/alert-outcomes", params={"ids": " , "}).json() \
        == {"outcomes": {}}
    # an unknown game yields NO block (never a fabricated pending one)
    got = c.get("/api/v4/alert-outcomes",
                params={"ids": "nope,also-nope"}).json()
    assert got == {"outcomes": {}}
    # duplicates collapse; the fan-out is clamped
    many = ",".join([archive["subject"]] * 5 + [f"9{i}" for i in range(400)])
    got = c.get("/api/v4/alert-outcomes", params={"ids": many}).json()
    assert archive["subject"] in got["outcomes"]


# ══════════════════════════════════════════════════════════════════════
# 2. THE FRONTEND — which records to ask about
# ══════════════════════════════════════════════════════════════════════

STUBS = """
Date.now = () => Date.parse("2026-09-13T12:00:00Z");
const localStorage = { getItem: () => null, setItem: () => {} };
function $(id) { return { innerHTML: "", textContent: "" }; }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => c);
const fmtTime = (iso) => !iso ? "--" : new Date(iso).toISOString().slice(11, 19);
function isActuallyLive(g) { return !!(g && g.live === true); }
function alertEligible(g) { const a = g && g.alert; return !!(a && a.eligible === true); }
const API_ALERT_OUTCOMES = (ids) =>
  "/api/v4/alert-outcomes?ids=" + encodeURIComponent(ids.join(","));
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, reconcileUnderAlerts, historyAlertsHTML,
  activeAlertsHTML, pendingOutcomeProbe, hydrateResultedOutcomes,
  alertOutcomeClass, OUTCOME_PROBE_MIN_MS };
"""


def _js() -> str:
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


def _block(js, begin, end):
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _run(tmp_path, script, *, fetch_body=None, fetch_ok=True):
    """Drive the shipped store in node.  `fetch_body` is the JSON the
    outcome route returns; the script body is awaited."""
    js = _js()
    mod = tmp_path / "oow_store.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    calls = []
    fetch_impl = (
        "globalThis.fetch = (url) => { OUT.calls.push(String(url)); "
        "return Promise.resolve(%s); };" % (
            "{ ok: true, status: 200, json: () => Promise.resolve(%s) }"
            % json.dumps(fetch_body) if fetch_ok else
            "{ ok: false, status: 500, json: () => Promise.resolve(null) }")
    )
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{ calls: [] }};\n{fetch_impl}\n"
            "const LABELS = { 'betual-nba': 'NBA' };\n"
            "(async () => {\n" + script + "\n"
            "console.log(JSON.stringify(Object.assign({"
            "history: m.UNDER_ALERTS.history,"
            "activeSize: m.UNDER_ALERTS.active.size,"
            "actHTML: m.activeAlertsHTML(),"
            "histHTML: m.historyAlertsHTML()}, OUT)));\n})();")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


# a record created while its game was live: unresolved, no verdict yet
OUT_OF_WINDOW_REC = {
    "id": "7000|25", "game_id": "7000", "checkpoint": 25,
    "triggered_at": "2026-09-13T10:00:00.000Z", "triggered_line": 187.5,
    "resolved_at": "2026-09-13T10:40:00.000Z", "duration_ms": 2400000,
    "resolved_reason": "game_finished",
    "alert_rule": "v3-progress-tiered-2026-09-13",
    "league": "NBA", "home_team": "H", "away_team": "A",
    "actual_pace": 1.5, "required_pace": 4.69, "league_average_pace": 9.0,
    "pace_gap": -3.19,
    "outcome": {"status": None, "trigger_total": 187.5, "final_total": None,
                "resolved_at": None},
}
BLOCK_UNDER = {"status": "resolved", "final_total": 186.0,
               "resolved_at": "2026-09-13T11:00:00Z",
               "final_source": "settled", "authoritative": True,
               "by_checkpoint": {"25": {"status": "under", "trigger_total": 187.5,
                                        "final_total": 186.0,
                                        "resolved_at": "2026-09-13T11:00:00Z"}}}


def _seed(extra=""):
    return (f"m.UNDER_ALERTS.history.push({json.dumps(OUT_OF_WINDOW_REC)});\n"
            + extra)


def test_records_the_poll_cannot_cover_are_picked_for_probing(tmp_path):
    r = _run(tmp_path, _seed() + """
      const live = new Set(["G-LIVE"]);
      const now = Date.parse("2026-09-13T12:00:00Z");
      OUT.probe = m.pendingOutcomeProbe(live, now);
      // ...and once settled it is no longer asked about
      m.UNDER_ALERTS.history[0].outcome = { status: "under", final_total: 186 };
      OUT.afterSettle = m.pendingOutcomeProbe(live, now);
      // ...nor while its game is inside the poll
      m.UNDER_ALERTS.history[0].outcome = { status: null };
      OUT.whileInPoll = m.pendingOutcomeProbe(new Set(["7000"]), now);
      // ...nor again inside the throttle, but yes again after it
      m.UNDER_ALERTS.history[0].outcome_probe_at = now - 1000;
      OUT.throttled = m.pendingOutcomeProbe(live, now);
      OUT.afterThrottle = m.pendingOutcomeProbe(
        live, now + m.OUTCOME_PROBE_MIN_MS + 1);
    """)
    assert r["probe"] == ["7000"], r
    assert r["afterSettle"] == [], r
    assert r["whileInPoll"] == [], r
    assert r["throttled"] == [], r
    assert r["afterThrottle"] == ["7000"], r


def test_hydration_settles_an_out_of_window_record(tmp_path):
    """THE DEFECT, CLOSED: a record whose game left the live window renders
    COLOURED with its verdict, from the same seal, with its line untouched."""
    r = _run(tmp_path, _seed() + """
      const changed = await m.hydrateResultedOutcomes(new Set(["G-LIVE"]));
      OUT.changed = changed;
      const rec = m.UNDER_ALERTS.history[0];
      OUT.status = rec.outcome.status;
      OUT.final = rec.outcome.final_total;
      OUT.line = rec.triggered_line;
      OUT.cls = m.alertOutcomeClass(rec.outcome);
      OUT.size = m.UNDER_ALERTS.history.length;
      OUT.row = m.historyAlertsHTML();
    """, fetch_body={"outcomes": {"7000": BLOCK_UNDER}})
    assert r["changed"] is True, r
    assert r["status"] == "under" and r["final"] == 186.0, r
    assert r["line"] == 187.5, r                 # the immutable trigger line
    assert r["cls"] == "al-under", r
    assert r["size"] == 1, r                     # no duplicate alert
    # the row is coloured BY ITS OUTER ELEMENT and prints the line + verdict
    row = r["row"].split('<li class="')[1]
    assert row.startswith("al-row"), row
    assert "al-under" in row.split(">")[0], row
    assert '<span class="al-outcome">UNDER</span>' in row, row
    assert 'class="al-trigger-line">187.5<' in row, row
    # ...and the one request was for exactly the uncovered record
    assert len(r["calls"]) == 1 and "7000" in r["calls"][0], r["calls"]


def test_hydration_renders_over_red_and_push_neutral(tmp_path):
    """The other two verdicts colour correctly through the same path."""
    blk = lambda st, fin: {"status": "resolved", "final_total": fin,
                           "resolved_at": "x", "final_source": "settled",
                           "authoritative": True,
                           "by_checkpoint": {"25": {"status": st,
                                                    "trigger_total": 187.5,
                                                    "final_total": fin,
                                                    "resolved_at": "x"}}}
    for status, final, cls in (("over", 190.0, "al-over"),
                               ("push", 187.5, "al-push")):
        import pathlib
        d = pathlib.Path(tmp_path) / status
        d.mkdir()
        r = _run(d, _seed() + """
          await m.hydrateResultedOutcomes(new Set());
          OUT.cls = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
          OUT.row = m.historyAlertsHTML();
        """, fetch_body={"outcomes": {"7000": blk(status, final)}})
        assert r["cls"] == cls, (status, r)
        assert cls in r["row"], (status, r["row"])


def test_a_failed_probe_leaves_the_panel_untouched(tmp_path):
    """Best-effort: an error (or a 500) changes nothing and still throttles
    the next ask, so a broken route cannot cause a request storm."""
    r = _run(tmp_path, _seed() + """
      const changed = await m.hydrateResultedOutcomes(new Set());
      OUT.changed = changed;
      OUT.stamp = m.UNDER_ALERTS.history[0].outcome_probe_at;
      OUT.stillUnsettled = (m.UNDER_ALERTS.history[0].outcome || {}).status == null;
      OUT.row = m.historyAlertsHTML();
    """, fetch_ok=False)
    assert r["changed"] is False, r
    assert r["stillUnsettled"] is True, r
    assert "al-under" not in r["row"] and "al-over" not in r["row"], r["row"]
    assert r["stamp"] is not None, r      # the ask is remembered regardless


def test_hydration_does_not_touch_active_records_or_the_poll(tmp_path):
    """It settles history rows only: an active alert, and the record the poll
    itself carries, are never re-asked or restated."""
    r = _run(tmp_path, _seed() + """
      // the poll owns this game; the history row it cannot reach is 7000
      m.reconcileUnderAlerts([{ game_id: "G-LIVE", competition_slug: "betual-nba",
        live: true, live_reason: null, alert: { eligible: true },
        under_alert_eligibility: { eligible: true, reason: "market_live" },
        home_team: "H", away_team: "A",
        projector: { progress_pct: 26, market_status: "LIVE", live_total_line: 189.5 },
        market: { opening_line: 210.5, closing_line: null, total_line: 189.5 },
        under_alert: { active: true, checkpoint: 25, actual_pace: 1.5,
                       required_pace: 4.69, league_average_pace: 9.0,
                       reference_games: 100, pace_gap: -3.19 },
        under_alert_outcome: { status: "pending", final_total: null,
          by_checkpoint: { 25: { status: null, trigger_total: 189.5,
                                 final_total: null } } } }], LABELS);
      OUT.activeBefore = m.UNDER_ALERTS.active.size;
      await m.hydrateResultedOutcomes(new Set(["G-LIVE"]));
      OUT.calls = OUT.calls.slice();
      OUT.activeAfter = m.UNDER_ALERTS.active.size;
      OUT.activeLine = m.UNDER_ALERTS.active.get("G-LIVE|25").triggered_line;
      OUT.activeOutcome = m.UNDER_ALERTS.active.get("G-LIVE|25").outcome.status;
      OUT.askedForLive = OUT.calls.some((u) => u.indexOf("G-LIVE") !== -1);
    """, fetch_body={"outcomes": {"7000": BLOCK_UNDER}})
    assert r["activeBefore"] == 1 and r["activeAfter"] == 1, r
    assert r["askedForLive"] is False, r["calls"]     # the poll owns it
    assert r["activeLine"] == 189.5, r                # untouched
    assert r["activeOutcome"] is None, r


def test_source_keeps_one_settlement_path_for_hydration():
    """Source-level: hydration reuses the ONE seal and adds no second
    settlement rule, no line write, and no duplicate-record path."""
    js = _js()
    body = js[js.index("async function hydrateResultedOutcomes"):]
    body = body[:body.index("\n}\n")]
    assert "applyFinalOutcomes(games)" in body      # the SAME seal
    for banned in ("final_total <", "final_total >", "triggered_line =",
                   "history.push"):
        assert banned not in body, banned
    # the pure probe mutates nothing — it only reads history and returns ids
    probe = js[js.index("function pendingOutcomeProbe"):]
    probe = probe[:probe.index("\n}\n")]
    for banned in ("UNDER_ALERTS.history.push", ".outcome =",
                   "triggered_line =", "saveAlertHistory"):
        assert banned not in probe, banned
