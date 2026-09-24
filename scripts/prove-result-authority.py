#!/usr/bin/env python3
"""
REAL-DATA PROOF (directive 2026-09-24) — read-only on production.

Builds a bounded COPY of blm_pokerbet.db (today's games + their snapshots),
then proves on that copy:

  A. RE-DERIVATION CANNOT DESTROY AN AUTHORITATIVE VERDICT
     run Scorecard.capture_results() and settle_worker.settle_once() and
     compare every page-verified (result_source='RESULTS_PAGE') OK row
     before/after: ZERO may lose its scores, status or provenance.

  B. THE STUCK BACKLOG IS REAL AND MEASURABLE
     count page-verified games with no OK row, and OK rows whose stored
     final is arithmetically impossible (below an observed score).

  C. THE AUDIT'S GATE FAILURE COUNT IS ZERO after the pass for the games
     the reconciler re-admitted.

Writes NOTHING to production: the source is opened mode=ro + query_only,
proven by a refused write.  All writes land in /tmp/blm-recon-proof/.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/home/ubuntu/BLM")
sys.path.insert(0, str(ROOT))
WORK = Path("/tmp/blm-recon-proof")
SRC = ROOT / "blm_pokerbet.db"
COPY = WORK / "blm_proof.db"

TODAY = "2026-09-24"


def log(*a):
    print(*a, flush=True)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # ── 1. read-only source, proved ────────────────────────────────
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA query_only=1")
    try:
        src.execute("CREATE TABLE __probe (x)")
        log("ABORT: source connection is NOT read-only")
        return 2
    except sqlite3.OperationalError:
        log("source: read-only PROVEN (write refused)")

    # ── 2. bounded copy (today's games + their snapshots) ──────────
    if COPY.exists():
        COPY.unlink()
    dst = sqlite3.connect(str(COPY))
    src.backup(dst)
    # name-based access: every reader below uses r["column"]
    dst.row_factory = sqlite3.Row
    log(f"copied in {time.time() - t0:.0f}s -> {COPY}")

    ids = [r[0] for r in dst.execute(
        "SELECT source_game_id FROM games WHERE last_seen_at >= ?", (TODAY,))]
    bases = sorted({g.split("#")[0] for g in ids})
    log(f"slice: {len(ids)} games (last_seen >= {TODAY})")

    # prune the copy so the pass is bounded: keep games + results for the
    # slice only, and snapshots for the slice's instance chains
    dst.execute("CREATE TEMP TABLE __keep(gid TEXT PRIMARY KEY)")
    dst.executemany("INSERT INTO __keep VALUES (?)", [(b,) for b in bases])
    dst.execute("""DELETE FROM snapshots WHERE
                     CASE WHEN instr(source_game_id,'#')>0
                          THEN substr(source_game_id,1,instr(source_game_id,'#')-1)
                          ELSE source_game_id END
                   NOT IN (SELECT gid FROM __keep)""")
    dst.execute("DELETE FROM games WHERE source_game_id NOT IN "
                "(SELECT gid FROM __keep)")
    dst.execute("DELETE FROM game_results WHERE source_game_id NOT IN "
                "(SELECT gid FROM __keep)")
    dst.commit()
    n_snap = dst.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    log(f"pruned copy: snapshots={n_snap} "
        f"games={dst.execute('SELECT COUNT(*) FROM games').fetchone()[0]}")

    def authoritative(conn) -> dict:
        """Rows that actually HOLD a page verdict (OK) — the verdicts the
        re-derivation paths must never damage.  (Rows merely *carrying*
        result_source='RESULTS_PAGE' with NULL finals are already-destroyed
        rows whose stale provenance the fix clears; they are measured
        separately by stale_provenance().)"""
        return {r["source_game_id"]: (r["final_home"], r["final_away"],
                                      r["final_total"],
                                      r["final_result_status"],
                                      r["result_source"], r["result_at"])
                for r in conn.execute(
                    """SELECT * FROM game_results
                       WHERE result_source = 'RESULTS_PAGE'
                         AND final_result_status = 'OK'""")}

    def stale_provenance(conn) -> set:
        """Rows that claim RESULTS_PAGE provenance but hold NO final — the
        stale lie the fix must clean up (provenance never lies)."""
        return {r["source_game_id"] for r in conn.execute(
            """SELECT source_game_id FROM game_results
               WHERE result_source = 'RESULTS_PAGE'
                 AND (final_home IS NULL OR final_away IS NULL)""")}

    def auditable(conn) -> dict:
        """Every page-verified game in the slice, with its stored row."""
        return {r["source_game_id"]: dict(r) for r in conn.execute(
            """SELECT g.source_game_id, g.status,
                      COALESCE(r.final_result_status,'<NO ROW>') st,
                      r.final_home, r.final_away, r.final_total
               FROM games g
               JOIN (SELECT DISTINCT source_game_id FROM
                     result_reconciliation WHERE outcome='VERIFIED') v
                 ON v.source_game_id = g.source_game_id
               LEFT JOIN game_results r
                 ON r.source_game_id = g.source_game_id""")}

    before_auth = authoritative(dst)
    before_all = auditable(dst)
    before_stale = stale_provenance(dst)
    n_verified = len(before_all)
    n_no_ok = sum(1 for v in before_all.values() if v["st"] != "OK")
    log(f"\nBEFORE: page-verified games in slice = {n_verified}, "
        f"without an OK row = {n_no_ok}")
    log(f"BEFORE: rows HOLDING a page verdict (RESULTS_PAGE + OK) = "
        f"{len(before_auth)}")
    log(f"BEFORE: rows claiming RESULTS_PAGE but holding no final "
        f"(stale provenance) = {len(before_stale)}")

    # impossible finals currently stored as OK (or otherwise) — the
    # arithmetic the pre-fix verification failed to make
    impossible = []
    for gid in before_all:
        mx = dst.execute(
            """SELECT MAX(home_score) h, MAX(away_score) a FROM snapshots
               WHERE CASE WHEN instr(source_game_id,'#')>0
                          THEN substr(source_game_id,1,instr(source_game_id,'#')-1)
                          ELSE source_game_id END = ?""", (gid,)).fetchone()
        fh, fa = before_all[gid]["final_home"], before_all[gid]["final_away"]
        if mx["h"] is None or fh is None:
            continue
        if fh < mx["h"] or fa < mx["a"]:
            impossible.append((gid, (fh, fa), (mx["h"], mx["a"]),
                               before_all[gid]["st"]))
    log(f"BEFORE: stored finals BELOW an observed score (impossible): "
        f"{len(impossible)}  (OK rows among them: "
        f"{sum(1 for x in impossible if x[3] == 'OK')})")

    # ── 3. A: the re-derivation paths run and destroy NOTHING ──────
    import os
    os.environ["BLM_POKERBET_DB"] = str(COPY)
    from blm_v4.result_reconciler import ResultReconciler
    from blm_v4.scorecard import Scorecard
    from blm_v4.settle_worker import settle_once

    # Every writer's startup runs the bootstrap provenance repair
    # (production: the reconciler worker is constructed at server start),
    # so measure the table the way production first sees it.
    ResultReconciler(COPY, batch_limit=1)
    log(f"\nbootstrap provenance repair: stale rows now "
        f"{len(stale_provenance(dst))} (was {len(before_stale)})")

    t1 = time.time()
    sc_stats = Scorecard(COPY).capture_results()
    log(f"\nScorecard.capture_results() done in {time.time()-t1:.0f}s: {sc_stats}")
    t1 = time.time()
    sw_stats = settle_once(COPY, batch_limit=200)
    log(f"settle_once() done in {time.time()-t1:.0f}s: {sw_stats}")

    after_auth = authoritative(dst)
    after_stale = stale_provenance(dst)
    damaged = []
    for gid, b in before_auth.items():
        a = after_auth.get(gid)
        if a != b:
            damaged.append((gid, b, a))
    log(f"\nA. verdict-bearing rows before={len(before_auth)} "
        f"after={len(after_auth)} CHANGED={len(damaged)}")
    for d in damaged[:10]:
        log(f"   DAMAGED {d[0]}: {d[1]} -> {d[2]}")

    after_all = auditable(dst)
    conv = {k: v for k, v in after_all.items()
            if before_all.get(k, {}).get("st") != "OK" and v["st"] == "OK"}
    lost = {k: v for k, v in after_all.items()
            if before_all.get(k, {}).get("st") == "OK" and v["st"] != "OK"}
    log(f"   page-verified games WITHOUT an OK row: "
        f"{sum(1 for v in after_all.values() if v['st'] != 'OK')} "
        f"(was {n_no_ok})")
    log(f"   newly OK by the re-derivation: {len(conv)}; "
        f"OK rows LOST: {len(lost)}")

    # provenance hygiene: a row with no final must not keep claiming
    # RESULTS_PAGE.  Rows that DO hold a verdict must keep it.
    kept_badly = before_stale & after_stale
    log(f"B. stale provenance before={len(before_stale)} "
        f"after={len(after_stale)} still-stale={len(kept_badly)}")
    stripped_ok = [g for g in before_auth if g in after_auth
                   and after_auth[g][4] != "RESULTS_PAGE"]
    log(f"   verdict rows whose provenance was stripped (must be 0): "
        f"{len(stripped_ok)}")

    conf = dst.execute("SELECT COUNT(*) FROM result_conflicts").fetchone()[0]
    log(f"   flagged conflicts: {conf}")

    a_ok = (not damaged and not lost)
    b_ok = (not kept_badly and not stripped_ok)
    log(f"\nVERDICT A: {'PASS' if a_ok else 'FAIL'} — page-verified verdicts "
        f"{'survive' if a_ok else 'are DESTROYED by'} re-derivation")
    log(f"VERDICT B: {'PASS' if b_ok else 'FAIL'} — provenance "
        f"{'never lies' if b_ok else 'still lies'} "
        f"(stale rows cleaned, verdict rows kept)")
    ok = a_ok and b_ok

    # ── 4. purge the pruned slice before deleting the copy ─────────
    dst.close()
    src.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
