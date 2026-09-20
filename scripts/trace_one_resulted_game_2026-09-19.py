"""ONE-GAME TRACE — a completed RESULTED-panel row with no verdict colour.

Directive 2026-09-19: pick one real game, trace it end-to-end through the
exact production path and PRINT the trace.  Read-only; no code changed here.

  1. raw backend/API alert block      (under_alert_outcome, as served)
  2. final_total                      (block + game_results provenance)
  3. triggered_line / market line     (by_checkpoint trigger_total + market)
  4. checkpoint
  5. resolved/final state             (block status + record resolution)
  6. status/result the backend returns
  7. the exact object dashboard.js receives
  8. alertVerdictStateFor() output    (shipped function, run in Node)
  9. the exact CSS class on the rendered row
 10. line × final colourability check (per the invariant, printed only)

Selection: the most recent game whose 75%-checkpoint block is UNCOLOURED
(status null) while its game HAS a settled final — i.e. a visibly completed
row with no verdict colour — plus, for contrast, the most recent COLOURED
row.  READ-ONLY: sqlite mode=ro, no writes anywhere.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, ".")

from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    CHECKPOINTS, outcome_status, trigger_market_total)
from blm_v4.api import (  # noqa: E402
    _connect, _settled_result_map, _load_snapshot_tail)

DB = Path("blm_pokerbet.db")
DASH = Path("blm_v4/dashboard/static/dashboard.js")


def _ro(path: Path):
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _settled_provenance(conn, gid):
    row = conn.execute(
        "SELECT final_total, result_at, final_result_status FROM game_results "
        "WHERE source_game_id = ? AND final_result_status = 'OK'", (gid,)
    ).fetchone()
    return dict(row) if row else None


def collect_candidates():
    conn = _connect()
    try:
        games = conn.execute(
            "SELECT source_game_id, classification FROM games "
            "WHERE source_game_id IN (SELECT source_game_id FROM game_results "
            "WHERE final_result_status='OK') "
            "ORDER BY last_seen_at DESC LIMIT 400").fetchall()
        ids = [g["source_game_id"] for g in games]
        settled = _settled_result_map(ids)
        uncoloured, coloured = [], []
        for g in games:
            gid, cls = g["source_game_id"], g["classification"]
            tail = list(reversed(_load_snapshot_tail(conn, gid, limit=500)))
            if not tail:
                continue
            blk = under_alert_outcome_safe(tail, cls, settled.get(gid))
            if blk is None:
                continue
            for cp in CHECKPOINTS:
                one = (blk.get("by_checkpoint") or {}).get(cp) or {}
                entry = {
                    "gid": gid, "cls": cls, "cp": cp, "blk": blk, "one": one,
                    "tail_rows": len(tail), "settled": settled.get(gid),
                }
                if one.get("status") is None:
                    uncoloured.append(entry)
                elif one.get("status") in ("under", "over", "push"):
                    coloured.append(entry)
            if len(uncoloured) >= 12 and len(coloured) >= 12:
                break
        return uncoloured, coloured
    finally:
        conn.close()


def under_alert_outcome_safe(tail, cls, settled):
    from blm_v4.live_analytics.under_outcome import under_alert_outcome
    try:
        return under_alert_outcome(tail, cls, settled=settled)
    except Exception:
        return None


def market_lines_summary(conn, gid):
    rows = conn.execute(
        "SELECT total_line, captured_at FROM snapshots "
        "WHERE game_id = (SELECT id FROM games WHERE source_game_id = ?) "
        "AND total_line IS NOT NULL ORDER BY captured_at ASC LIMIT 3",
        (gid,)).fetchall()
    return [dict(r) for r in rows]


def trace(rec, conn):
    gid, cp, blk, one = rec["gid"], rec["cp"], rec["blk"], rec["one"]
    settled_row = _settled_provenance(conn, gid)
    trig_full = None
    try:
        trig_full = trigger_market_total(
            _full_rows(conn, gid), cp, rec["cls"])
    except Exception:
        pass
    trig = one.get("trigger_total")
    fin = one.get("final_total")
    would_be = outcome_status(trig, fin)

    print("=" * 78)
    print(f"GAME {gid}   checkpoint {cp}%   classification {rec['cls']}")
    print("=" * 78)

    print("\n[1] RAW BACKEND/API ALERT BLOCK (under_alert_outcome, as served)")
    api_block = json.dumps(blk, indent=2, default=str, sort_keys=False)
    print(_indent(api_block, "    "))

    print("\n[2] FINAL_TOTAL")
    print(f"    block.final_total      : {fin!r}")
    print(f"    block.final_source     : {blk.get('final_source')!r}")
    print(f"    block.authoritative    : {blk.get('authoritative')!r}")
    print(f"    game_results OK row    : {settled_row!r}")

    print("\n[3] TRIGGERED_LINE / MARKET LINE")
    print(f"    block.trigger_total    : {trig!r}")
    print(f"    full-stream trigger    : {trig_full!r}  "
          f"(for reference; the API serves the 500-row tail)")
    print(f"    resolved_at            : {one.get('resolved_at')!r}")
    print(f"    first market lines     : {market_lines_summary(conn, gid)}")

    print("\n[4] CHECKPOINT")
    print(f"    checkpoint             : {cp}%  (by_checkpoint key type: "
          f"{type(cp).__name__} pre-JSON; string after JSON)")

    print("\n[5] RESOLVED / FINAL STATE")
    print(f"    block.status           : {blk.get('status')!r} "
          f"(top-level: 'resolved' when a final exists, else 'pending')")
    print(f"    record would resolve at: {one.get('resolved_at')!r}")

    print("\n[6] BACKEND STATUS/RESULT")
    print(f"    by_checkpoint[{cp}].status = {one.get('status')!r} "
          f"<-- the backend's verdict for this checkpoint")
    print(f"    outcome_status({trig!r}, {fin!r}) = {outcome_status(trig, fin)!r}")

    print("\n[7] OBJECT dashboard.js RECEIVES")
    js_obj = {
        "id": f"{gid}|{cp}", "game_id": gid, "checkpoint": cp,
        "league": "(from game)", "resolved_at": one.get("resolved_at"),
        "triggered_line": None,
        "outcome": {"status": one.get("status"),
                     "trigger_total": trig, "final_total": fin,
                     "resolved_at": one.get("resolved_at")},
    }
    print(_indent(json.dumps(js_obj, indent=2, default=str), "    "))

    print("\n[8] alertVerdictStateFor() OUTPUT (shipped dashboard.js, in Node)")
    state = _run_verdict_state(js_obj)
    print(_indent(json.dumps(state, indent=2, default=str), "    "))

    print("\n[9] EXACT CSS CLASS ON THE RENDERED ROW")
    row = _run_row_html(js_obj)
    cls_attr = row.split(">")[0] if row else "(row not found)"
    print(f"    <li {cls_attr} ...>")
    colour = [c for c in cls_attr.split() if c in
              ("al-under", "al-over", "al-push", "al-unknown")]
    print(f"    verdict colour classes : {colour or 'NONE (uncoloured row)'}")

    print("\n[10] COLOURABILITY CHECK (invariant, printed only — no change)")
    if trig is None and trig_full is not None:
        print(f"    line provable in the FULL stream ({trig_full}) but the "
              f"API tail missed it → the 500-row window is the defect")
    elif trig is None:
        print("    NO market line was ever observed at/ before the boundary "
              "in the stored stream → verdict mathematically unprovable; "
              "the backend's fail-closed rule returns status=None")
    else:
        print(f"    line {trig} vs final {fin} → {would_be} — a settled "
              f"verdict EXISTS; if uncoloured, the defect is downstream")
    print()
    return {"gid": gid, "cp": cp, "status": one.get("status"),
            "state": state, "cls": cls_attr}


def _full_rows(conn, gid):
    return [dict(r) for r in conn.execute(
        "SELECT s.home_score, s.away_score, s.quarter, s.clock, "
        "s.period_label, s.game_status, s.total_line, s.captured_at, "
        "s.classification "
        "FROM snapshots s JOIN games gg ON gg.id = s.game_id "
        "WHERE gg.source_game_id = ? ORDER BY s.captured_at ASC "
        "LIMIT 50000", (gid,)).fetchall()]


def _indent(text, pad):
    return "\n".join(pad + ln for ln in text.splitlines())


def _node_extract(js, begin, end):
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _node_run(store_js, rec_js, extra):
    with tempfile.TemporaryDirectory() as td:
        mod = os.path.join(td, "dash_mod.js")
        Path(mod).write_text(store_js + "\n" + "module.exports = "
                             "{ UNDER_ALERTS, historyAlertsHTML, "
                             "alertVerdictStateFor };\n", encoding="utf-8")
        code = (
            "const m = require(" + json.dumps(mod) + ");\n"
            "const REC = " + json.dumps(rec_js) + ";\n"
            "Date.now = () => Date.parse('2026-09-19T12:00:00Z');\n"
            "const localStorage = { getItem: () => null, setItem: () => {} };\n"
            "function $(id) { return { innerHTML: '', textContent: '' }; }\n"
            "const esc = (s) => String(s == null ? '' : s);\n"
            "const fmtTime = (iso) => !iso ? '--' : "
            "new Date(iso).toISOString().slice(11, 19);\n"
            "function fmtDuration(ms) { return ms == null ? '—' : '2h 00m'; }\n"
            "function isActuallyLive(g) { return !!(g && g.live === true); }\n"
            "const warns = [];\n"
            "console.warn = (...a) => warns.push(a.join(' '));\n"
            "const OUT = {};\n"
            + extra +
            "console.log(JSON.stringify({ OUT, warns }));\n")
        out = subprocess.run(["node", "-e", code], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr + out.stdout
        return json.loads(out.stdout)


def _run_verdict_state(rec_js):
    js = DASH.read_text(encoding="utf-8")
    store = _node_extract(js, "/* __ALERT_STORE_BEGIN__ */",
                          "/* __ALERT_STORE_END__ */")
    r = _node_run(store, rec_js, "OUT.state = "
                  "m.alertVerdictStateFor(REC.outcome, { resolved: "
                  "!!REC.resolved_at, rec: REC, onUnprovable: () => {} });\n")
    return r["OUT"]["state"]


def _run_row_html(rec_js):
    js = DASH.read_text(encoding="utf-8")
    store = _node_extract(js, "/* __ALERT_STORE_BEGIN__ */",
                          "/* __ALERT_STORE_END__ */")
    r = _node_run(store, rec_js,
                  "m.UNDER_ALERTS.history.push(REC);\n"
                  "OUT.row = (m.historyAlertsHTML().split('<li class=\"')[1]"
                  " || '').split('data-alert-id')[0];\n")
    return r["OUT"]["row"]


def main() -> int:
    if not DB.exists():
        print(f"missing {DB}")
        return 1
    uncoloured, coloured = collect_candidates()
    if not uncoloured:
        print("no uncoloured completed rows found — nothing to trace")
        return 1
    conn = _ro(DB)
    try:
        print("SELECTED: most recent UNCOLOURED completed row "
              "(status=null with a settled final)")
        trace(uncoloured[0], conn)
        if coloured:
            print("\n\nFOR CONTRAST: most recent COLOURED row")
            trace(coloured[0], conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
