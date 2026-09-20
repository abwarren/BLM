"""DEDICATED SETTLE WORKER (directive 2026-09-19) — prompt game_results settlement.

Mandate: newly finished games must be reconciled PROMPTLY — game_results
populated/corrected when a game reaches final — without waiting for the
4-hour full-history scorecard sweep.  This suite pins:

  1. a newly finished game is DETECTED by the worker's candidate query
  2. game_results is CREATED for a game that had no row at all
  3. an existing result is CORRECTED when the verified final arrives
     after the initial observation
  4. a valid UNDER/OVER/PUSH reaches the frontend with the correct colour
  5. NO LINE remains NO LINE and is never coloured
  6. NO FINAL remains NO FINAL and is never coloured
  7. PENDING remains PENDING and is never coloured
  8. the worker is IDEMPOTENT — no duplicates, no corruption
  9. the 4-hour full-history sweep (scorecard run + its server loop)
     remains UNCHANGED

Everything runs against the SHIPPED worker, the SHIPPED API and the
SHIPPED dashboard state machine (the __ALERT_STORE__ block executed in
Node.js) — no copies of any logic under test.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import SCORECARD_SCHEMA, Scorecard
from blm_v4.settle_worker import settle_once

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

TRIGGER = 187.5


# ══════════════════════════════════════════════════════════════════════
# Fixtures — a real pipeline DB (storage schema) + the worker
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def pipeline(tmp_path):
    """A real PokerBetStore DB plus a snapshot factory."""
    from blm_v4.storage import PokerBetStore
    db = tmp_path / "blm_pokerbet.db"
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")     # noqa: E731

    def add_game(gid, status="ended"):
        st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="c", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team="Home Five", away_team="Away Six",
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status=status, first_seen_at=iso(now - timedelta(hours=1)),
            last_seen_at=iso(now)))

    def snap(gid, t, hs, as_, q, clock, total=None, status="live",
             period_label=None):
        # reuse the game's CURRENT status — an observation never resurrects
        # an ended game (the collector's own ended-reconciliation keeps it)
        existing = st.get_game(gid)
        game_status = existing["status"] if existing else "live"
        game_id = st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="c", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team="Home Five", away_team="Away Six",
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status=game_status, first_seen_at=iso(now - timedelta(hours=1)),
            last_seen_at=iso(now)))
        st.insert_snapshot(game_id, MarketObservation(
            source="PokerBet", source_game_id=gid,
            classification="BETUAL_NBA", captured_at=iso(t),
            home_team="Home Five", away_team="Away Six",
            home_score=hs, away_score=as_,
            period_label=period_label or f"{q}th Quarter",
            quarter=q, clock=clock, game_status=status,
            total_line=total, spread=None, w1_odds=None,
            w2_odds=None, markets_json="{}"), force=True)
    return {"db": db, "store": st, "add": add_game, "snap": snap,
            "now": now, "iso": iso}


def _results(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        try:
            return {r["source_game_id"]: dict(r) for r in conn.execute(
                "SELECT * FROM game_results")}
        except sqlite3.OperationalError:
            return {}       # a DB the worker/sweep has never touched yet
    finally:
        conn.close()


def _game_status(db: Path, gid: str):
    conn = sqlite3.connect(f"file:{db}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM games WHERE source_game_id=?",
            (gid,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# 1 + 2. DETECTION and CREATION — a newly finished game is settled
# ══════════════════════════════════════════════════════════════════════

def test_newly_ended_game_is_detected_and_game_results_created(pipeline):
    """A live game with no game_results row finishes; ONE worker pass
    detects it, settles it OK and CREATES the game_results row."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    add("7001", status="live")
    # a clean BETUAL_NBA history: Q1 -> Q4 end, line observed at the 75%
    # boundary and a final (186 < 187.5 -> UNDER).  5+ snapshots: fewer is
    # a list-stub game and stays UNKNOWN by the sweep's own rule.
    snap("7001", now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
    snap("7001", now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
    snap("7001", now - timedelta(minutes=40), 38, 36, 2, "05:00", TRIGGER)
    snap("7001", now - timedelta(minutes=30), 55, 52, 3, "05:00", 189.5)
    snap("7001", now - timedelta(minutes=10), 93, 93, 4, "00:00", 192.5)
    assert _game_status(db, "7001") == "live"
    assert _results(db) == {}

    # the game reaches final (the reconciler's status flip — unchanged)
    conn = sqlite3.connect(f"file:{db}", uri=True, timeout=30)
    conn.execute("UPDATE games SET status='ended' WHERE source_game_id='7001'")
    conn.commit()
    conn.close()

    stats = settle_once(db)
    assert stats["checked"] == 1 and stats["settled"] == 1, stats
    row = _results(db)["7001"]
    assert row["final_result_status"] == "OK"
    assert row["final_total"] == 186          # 93 + 93
    assert row["final_home"] == 93 and row["final_away"] == 93


def test_live_game_is_never_settled(pipeline):
    """A game that has NOT ended stays out of the worker entirely — the
    worker settles newly-ended games, nothing else."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    add("7002", status="live")
    snap("7002", now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
    snap("7002", now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
    snap("7002", now - timedelta(minutes=40), 38, 36, 2, "05:00", TRIGGER)
    snap("7002", now - timedelta(minutes=10), 55, 52, 3, "05:00", 189.5)
    stats = settle_once(db)
    assert stats["checked"] == 0, stats
    assert _results(db) == {}


# ══════════════════════════════════════════════════════════════════════
# 3. CORRECTION — the verified final arrives after the initial observation
# ══════════════════════════════════════════════════════════════════════

def test_corrected_final_rewrites_the_same_row(pipeline):
    """Requirement 3: the final arrives AFTER the initial observation.  A
    mid-game observation is settled first (UNKNOWN — the game had not
    finished); once the game's verified final is captured (a LATER
    observation) the next pass CORRECTS the SAME row — never a second
    row — to OK with the true final."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    add("7003", status="ended")
    # initial capture: the game is observed mid-game (5+ rows, no final
    # state) — settleable, but only UNKNOWN
    snap("7003", now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
    snap("7003", now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
    snap("7003", now - timedelta(minutes=40), 38, 36, 2, "05:00", TRIGGER)
    snap("7003", now - timedelta(minutes=30), 55, 52, 3, "05:00", 189.5)
    stats = settle_once(db)
    assert stats["unknown"] == 1, stats
    first = _results(db)["7003"]
    assert first["final_result_status"] == "UNKNOWN"
    assert first["final_total"] is None

    # the verified final arrives AFTER that initial observation: the
    # source publishes the endpoint (88+90=178 -> UNDER vs 187.5) as a
    # LATER capture
    snap("7003", now - timedelta(minutes=10), 88, 90, 4, "00:00", 192.5,
         status="ended", period_label="Finished")
    stats2 = settle_once(db)
    assert stats2["settled"] == 1, stats2
    rows = _results(db)
    assert len(rows) == 1, rows
    corrected = rows["7003"]
    assert corrected["id"] == first["id"]     # the SAME row — no duplicate
    assert corrected["final_result_status"] == "OK"
    assert corrected["final_total"] == 178    # 88 + 90 -> UNDER
    assert corrected["result_at"] > first["result_at"]

    # settled OK with no newer observation -> never touched again
    settle_once(db)
    assert _results(db)["7003"] == corrected


def test_worker_correction_matches_the_sweep_verdict(pipeline):
    """Parity: for the same game state the worker's verdict is EXACTLY what
    a scorecard capture_results run would write (same rule, same row)."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    add("7004", status="ended")
    snap("7004", now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
    snap("7004", now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
    snap("7004", now - timedelta(minutes=40), 38, 36, 2, "05:00", TRIGGER)
    snap("7004", now - timedelta(minutes=30), 55, 52, 3, "05:00", 189.5)
    snap("7004", now - timedelta(minutes=10), 95, 95, 4, "00:00", 192.5,
         status="ended", period_label="Finished")
    settle_once(db)
    worker_row = _results(db)["7004"]

    # an identical second DB — the 4-hour sweep's own settle step
    db2 = db.with_name("sweep.db")
    shutil.copy(db, db2)
    conn = sqlite3.connect(f"file:{db2}", uri=True, timeout=30)
    conn.execute("DELETE FROM game_results")
    conn.commit()
    conn.close()
    sc = Scorecard(db2)
    stats = sc.capture_results()
    assert stats["ok"] == 1, stats
    sweep_row = _results(db2)["7004"]
    for k in ("final_result_status", "final_home", "final_away",
              "final_total", "result_at"):
        assert worker_row[k] == sweep_row[k], (k, worker_row, sweep_row)


# ══════════════════════════════════════════════════════════════════════
# 4–7. THE FRONTEND VERDICT + the uncoloured unprovable states
# ══════════════════════════════════════════════════════════════════════

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
  const s = Math.floor(ms / 1000);
  const mm = Math.floor(s / 60);
  const h = Math.floor(mm / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" : "") + pad(mm % 60) + ":" + pad(s % 60);
}
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

FIXTURE = """
const seed = (recs) => {
  m.UNDER_ALERTS.history.length = 0;
  m.UNDER_ALERTS.active.clear();
  for (const r of recs) m.UNDER_ALERTS.history.push(Object.assign({}, r));
};
// the record shape the reconcile loop creates, with the outcome block the
// WORKER's settlement serves (via the API's under_alert_outcome block)
const rec = (o) => Object.assign({
  id: "G-1|25", game_id: "G-1", checkpoint: 25, league: "NBA",
  competition_slug: "betual-nba",
  triggered_at: "2026-09-19T10:00:00.000Z",
  resolved_at: "2026-09-19T11:00:00.000Z",
  duration_ms: 3600000, resolved_reason: "game_finished",
  actual_pace: 1.5, required_pace: 4.7, league_average_pace: 9.0,
  triggered_line: 187.5, alert_rule: "v4-progress-75-margin-2026-09-14",
  outcome: null,
}, o || {});
const settled = (status, trig, fin) => ({
  status, trigger_total: trig, final_total: fin,
  resolved_at: "2026-09-19T11:00:00.000Z",
});
const rowClassOf = (html) => {
  const mm = html.match(/class="al-row[^"]*"/);
  return mm ? mm[0].slice('class="'.length, -1) : null;
};
const colourClasses = (row) => !row ? [] : row.split(" ").filter((c) =>
  ["al-under", "al-over", "al-push", "al-unknown"].indexOf(c) !== -1);
"""


def _run(js: str, tmp_path: Path, script: str):
    mod = tmp_path / "settle_store.js"
    i = js.index(STORE_BEGIN) + len(STORE_BEGIN)
    store = js[i:js.index(STORE_END, i)]
    mod.write_text(STUBS + store +
                   "module.exports = { UNDER_ALERTS, historyAlertsHTML,"
                   " applyFinalOutcomes, sealOutcome, alertOutcomeClass };\n",
                   encoding="utf-8")
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{}};\n{FIXTURE}\n{script}\n"
            "console.log(JSON.stringify(OUT));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


def _dash_js() -> str:
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


@node
def test_valid_verdicts_reach_the_frontend_with_the_correct_colour(tmp_path):
    """Requirement 4: a completed game with a valid final + valid triggered
    line resolves to UNDER/OVER/PUSH and the resulted row renders with the
    corresponding verdict colour class — through the SHIPPED settle path
    (applyFinalOutcomes -> historyAlertsHTML)."""
    r = _run(_dash_js(), tmp_path, """
      // three records — one per verdict, each settled from the block the
      // worker's settlement serves (via the API's under_alert_outcome)
      const blocks = (status, fin) => ({ by_checkpoint: {
        "25": { status, trigger_total: 187.5, final_total: fin } } });
      const games = [
        { game_id: "G-1", under_alert_outcome: blocks("under", 186) },
        { game_id: "G-2", under_alert_outcome: blocks("over", 190) },
        { game_id: "G-3", under_alert_outcome: blocks("push", 187.5) },
      ];
      seed([rec({ id: "G-1|25", game_id: "G-1" }),
            rec({ id: "G-2|25", game_id: "G-2" }),
            rec({ id: "G-3|25", game_id: "G-3" })]);
      m.applyFinalOutcomes(games);
      const html = m.historyAlertsHTML();
      OUT.rows = (html.match(/class="al-row[^"]*"/g) || [])
        .map((s) => s.slice('class="'.length, -1));
      OUT.html = html;
    """)
    rows = [c.split() for c in r["rows"]]
    # each record renders with exactly its own verdict colour, one class
    # each (the panel renders newest-first: push, over, under)
    assert [c[2] for c in rows] == ["al-push", "al-over", "al-under"], rows
    assert all(c[0] == "al-row" and c[1] == "al-resolved" and len(c) == 3
               for c in rows), rows
    for name, cls in (("UNDER", "al-under"), ("OVER", "al-over"),
                      ("PUSH", "al-push")):
        assert name in r["html"] and cls in r["html"], (name, r["html"])
    assert "Triggered Line:" in r["html"]      # the immutable line
    assert "Final:" in r["html"]               # the settled final shown


@node
def test_no_line_remains_no_line_and_never_coloured(tmp_path):
    """Requirement 5: final provable, no line ever observed at the boundary
    -> status null, the row states NO LINE and carries NO verdict colour."""
    r = _run(_dash_js(), tmp_path, """
      seed([rec({ outcome: { status: null, trigger_total: null,
        final_total: 186, resolved_at: "2026-09-19T11:00:00.000Z" } })]);
      OUT.html = m.historyAlertsHTML();
      OUT.word = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
      OUT.row = rowClassOf(OUT.html);
    """)
    assert "NO LINE" in r["html"], r
    assert colourClasses(r["row"]) == [], r
    assert r["word"] is None                  # no verdict class, ever
    assert "al-under" not in r["html"] and "al-over" not in r["html"]
    assert "al-push" not in r["html"] and "al-unknown" not in r["html"]


@node
def test_no_final_remains_no_final_and_never_coloured(tmp_path):
    """Requirement 6: record resolved, backend proves neither final nor
    line -> NO FINAL, no verdict colour."""
    r = _run(_dash_js(), tmp_path, """
      seed([rec({ outcome: { status: null, trigger_total: null,
        final_total: null } })]);
      OUT.html = m.historyAlertsHTML();
      OUT.cls = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
      OUT.row = rowClassOf(OUT.html);
    """)
    assert "NO FINAL" in r["html"], r
    assert colourClasses(r["row"]) == [], r
    assert r["cls"] is None
    assert "al-under" not in r["html"] and "al-over" not in r["html"]
    assert "al-push" not in r["html"] and "al-unknown" not in r["html"]


@node
def test_pending_remains_pending_and_never_coloured(tmp_path):
    """Requirement 7: nothing resolved yet -> PENDING, no verdict colour."""
    r = _run(_dash_js(), tmp_path, """
      seed([rec({ outcome: { status: null, trigger_total: null,
        final_total: null } })]);
      m.UNDER_ALERTS.history[0].resolved_at = null;   // still live
      m.UNDER_ALERTS.history[0].duration_ms = null;
      OUT.html = m.historyAlertsHTML();
      OUT.word = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
      OUT.row = rowClassOf(OUT.html);
    """)
    assert "PENDING" in r["html"], r
    assert colourClasses(r["row"]) == [], r
    assert r["word"] is None
    assert "al-under" not in r["html"] and "al-over" not in r["html"]
    assert "al-push" not in r["html"] and "al-unknown" not in r["html"]


# ══════════════════════════════════════════════════════════════════════
# 8. IDEMPOTENCE — no duplicates, no corruption
# ══════════════════════════════════════════════════════════════════════

def test_worker_is_idempotent_never_duplicates_or_corrupts(pipeline):
    """Ten consecutive passes over the same settled game leave EXACTLY one
    row, byte-identical after the first — the second pass skips the OK row
    entirely (no churn, no duplicate, no corruption)."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    add("7005", status="ended")
    snap("7005", now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
    snap("7005", now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
    snap("7005", now - timedelta(minutes=40), 38, 36, 2, "05:00", TRIGGER)
    snap("7005", now - timedelta(minutes=30), 55, 52, 3, "05:00", 189.5)
    snap("7005", now - timedelta(minutes=10), 93, 93, 4, "00:00", 192.5)
    settle_once(db)
    first = _results(db)
    for _ in range(9):
        settle_once(db)
    again = _results(db)
    assert len(first) == 1 and len(again) == 1
    assert first["7005"] == again["7005"]     # untouched after first pass
    assert again["7005"]["final_result_status"] == "OK"
    assert again["7005"]["final_total"] == 186


def test_invalid_verdict_is_final_and_never_rescored(pipeline):
    """A poisoned history is INVALID once and stays INVALID — the sweep's
    own rule; repeated passes never re-verify or corrupt it."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    add("7006", status="ended")
    snap("7006", now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
    snap("7006", now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
    # contamination: a score REGRESSION the gate flags -> INVALID
    snap("7006", now - timedelta(minutes=40), 2, 1, 2, "05:00", TRIGGER)
    snap("7006", now - timedelta(minutes=30), 55, 52, 3, "05:00", 189.5)
    snap("7006", now - timedelta(minutes=10), 93, 93, 4, "00:00", 192.5)
    stats = settle_once(db)
    assert stats["invalid"] == 1, stats
    inv = _results(db)["7006"]
    assert inv["final_result_status"] == "INVALID"
    assert inv["final_total"] is None
    for _ in range(3):
        settle_once(db)
    assert _results(db)["7006"] == inv        # byte-identical, never touched


def test_batch_limit_bounds_each_pass_and_backlog_drains(pipeline):
    """The worker is bounded: one pass settles at most batch_limit games,
    oldest first, and repeated passes drain the backlog without loss."""
    db, snap, now, add = (pipeline["db"], pipeline["snap"],
                          pipeline["now"], pipeline["add"])
    for i in range(7):
        gid = f"701{i}"
        add(gid, status="ended")
        snap(gid, now - timedelta(minutes=58), 4, 2, 1, "10:00", 184.5)
        snap(gid, now - timedelta(minutes=55), 8, 6, 1, "09:00", 184.5)
        snap(gid, now - timedelta(minutes=50), 20, 18, 1, "05:00", 184.5)
        snap(gid, now - timedelta(minutes=40), 38, 36, 2, "05:00", TRIGGER)
        snap(gid, now - timedelta(minutes=10), 93, 93, 4, "00:00", 192.5)
    s1 = settle_once(db, batch_limit=3)
    assert s1["settled"] == 3, s1
    s2 = settle_once(db, batch_limit=3)
    assert s2["settled"] == 3, s2
    s3 = settle_once(db, batch_limit=3)
    assert s3["settled"] == 1, s3
    assert len(_results(db)) == 7


# ══════════════════════════════════════════════════════════════════════
# 9. THE 4-HOUR FULL-HISTORY SWEEP REMAINS UNCHANGED
# ══════════════════════════════════════════════════════════════════════

def test_sweep_run_phases_unchanged():
    """Scorecard.run() still executes the SAME phases in the SAME order —
    the worker added nothing to, and removed nothing from, the sweep."""
    import inspect
    src = inspect.getsource(Scorecard.run)
    for phase in ("record_predictions", "record_fixed_checkpoints",
                  "capture_results", "score_all", "record_market_history",
                  "record_checkpoint_market"):
        assert f"self.{phase}()" in src, phase
    assert "settle" not in src and "settle_once" not in src


def test_sweep_loop_cadence_untouched():
    """The server's scorecard loop still sleeps SCORECARD_INTER_RUN_GAP_S
    between full runs and still calls sc.run() — the 4-hour sweep's
    behaviour is exactly what it was; the worker is a separate loop."""
    import server as blm_server
    src = Path(blm_server.__file__).read_text(encoding="utf-8")
    assert "await asyncio.to_thread(sc.run)" in src
    assert "await asyncio.sleep(SCORECARD_INTER_RUN_GAP_S)" in src
    # the worker is its own thread with its own cadence — never nested in
    # the scorecard loop
    assert "SettleWorker(" in src
    assert "settle_worker_started" in src
    assert blm_server.SETTLE_INTERVAL_S >= 30          # the 30–60 s window
    assert blm_server.SETTLE_INTERVAL_S <= 60
    # the worker's env cadence is independent of the scorecard's
    assert "BLM_SETTLE_INTERVAL_S" in src
    assert "BLM_SCORECARD_INTER_RUN_GAP_S" in src


def test_worker_reuses_the_sweep_settlement_rule():
    """The worker imports the scorecard's OWN gate/rule/schema — it can
    never drift from the sweep's verdicts (no second settlement logic)."""
    import blm_v4.settle_worker as sw
    from blm_v4.scorecard import _snapshot_history_quality as sweep_gate
    assert sw._snapshot_history_quality is sweep_gate
    assert sw.SCORECARD_SCHEMA is SCORECARD_SCHEMA
    assert sw.Scorecard is Scorecard
    # the upsert in the worker is the sweep's own statement
    worker_src = Path(sw.__file__).read_text(encoding="utf-8")
    for frag in ("ON CONFLICT(source_game_id) DO UPDATE SET",
                 "final_result_status = excluded.final_result_status"):
        assert frag in worker_src, frag


def test_worker_does_not_touch_thresholds_gates_or_triggers():
    """The worker module references no alert threshold, gate, trigger or
    classification logic — settlement only, by construction."""
    import blm_v4.settle_worker as sw
    src = Path(sw.__file__).read_text(encoding="utf-8")
    # scan CODE only — strip docstrings FIRST (regex over the whole source,
    # so multi-line docstrings vanish with their delimiters), then comment
    # tails.  The prose describing this guarantee must not trip the scan.
    code = re.sub(r'""".*?"""', ' ', src, flags=re.S)
    code = "\n".join(line.split("#")[0] for line in code.splitlines())
    for banned in ("under_alert", "required_pace", "league_average",
                   "threshold", "momentum", "checkpoint_market",
                   "predictions", "1.04", "Q3_BREAK"):
        assert banned not in code, banned
