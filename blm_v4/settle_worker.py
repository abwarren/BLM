"""DEDICATED SETTLE WORKER (directive 2026-09-19) — prompt game_results settlement.

Mandate: newly finished games must be reconciled PROMPTLY — game_results
populated/corrected when a game reaches final — without waiting for the
4-hour full-history scorecard sweep, whose cadence and behaviour are
UNTOUCHED.  This worker is the sibling of server.py's game_finished
reconciler: a small, dedicated, bounded loop whose ONLY responsibility is
to detect newly-ended games and compute/update game_results.

Settlement semantics are REUSED verbatim from the scorecard — this module
imports the SAME authority the 4-hour sweep runs:

  * ``_snapshot_history_quality``   — the identical quality gate
  * ``Scorecard._final_result``     — the identical OK/UNKNOWN rule
  * ``SCORECARD_SCHEMA``            — the identical game_results table
  * the identical INSERT..ON CONFLICT upsert (the sweep's own statement)

so a verdict can never disagree with the one the sweep would write.  No
alert threshold, gate, trigger or classification logic is consulted,
imported or changed; no other table is written; the sweep itself is not
invoked.

Conservation properties:
  * BOUNDED — at most ``batch_limit`` games per pass, oldest first; the
    pass is minutes-long-workload-free by construction (no scorecard,
    no model math), so the API event loop and the collector never starve.
  * IDEMPOTENT — a game already holding an OK row is skipped; an
    INVALID verdict is final and never rescored (the sweep's own rule);
    the upsert targets the row's PRIMARY KEY (UNIQUE source_game_id), so
    a re-run updates the same row and can never duplicate it.  A
    corrected final (score revision after the initial observation) is
    re-written through the same upsert, exactly as the sweep's
    M007-M8 re-verification would.
  * FAIL-CLOSED — unreadable games are logged and skipped; one bad
    game never breaks the pass.

INSTANCE-CHAIN BRIDGE (defect 30964771, 2026-09-20): a game whose feed
identity was split by the collector's (now guarded, but historically
fired) virtual-replay machinery continues in a ``base#iN`` sibling whose
snapshots carry the real Q3/Q4 and the final.  A base game that ends
without a provable final — UNKNOWN or no row — is settled from the
CHAINED series: base snapshots plus every ``base#i*`` sibling's,
concatenated in capture order, dedup by ``source_game_id`` prefix strip.
The same ``_snapshot_history_quality`` gate and ``_final_result`` rule
decide the chained verdict; a chained OK final is written to the BASE
game_results row (the identity every alert, panel and audit reads).
Siblings are found by a single LIKE scan per candidate, bounded by the
same batch limit — no unbounded fan-out.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from blm_v4.scorecard import SCORECARD_SCHEMA, Scorecard, _snapshot_history_quality

logger = logging.getLogger("blm_v4.settle_worker")

#: dedicated worker cadence — between the reconciler's 30s and a poll
#: cycle, far inside any 4-hour sweep gap.  Env-overridable so ops can
#: bound it without a code change (mirrors RECONCILER_INTERVAL_S).
DEFAULT_INTERVAL_S = 45.0
#: per-pass game bound — the worker stays a small, read-mostly job
DEFAULT_BATCH_LIMIT = 25


def settle_once(db_path: Path | str, *,
                log: logging.Logger | None = None,
                batch_limit: int = DEFAULT_BATCH_LIMIT) -> dict:
    """One settlement pass over ``db_path`` — the worker's whole job.

    Returns ``{"checked": int, "settled": int, "unknown": int,
    "invalid": int, "skipped": int}``.  Never raises for per-game
    problems (fail-closed, logged); only a wholly unreadable DB raises.
    """
    log = log or logger
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # The scorecard's own schema — on a fresh DB the worker creates the
        # SAME game_results table the sweep would (idempotent CREATE IF
        # NOT EXISTS); on a production DB it is a no-op.
        conn.executescript(SCORECARD_SCHEMA)
        conn.commit()
        # Candidates, bounded and oldest-first so a backlog drains
        # predictably:
        #   (a) a newly ended game with NO game_results row at all,
        #   (b) an UNKNOWN row — re-verified every pass (the sweep's own
        #       M007-M8 rule), and
        #   (c) an OK row written BEFORE the game's newest observation —
        #       the verified final arrived after the initial observation,
        #       so the row is corrected in place through the same upsert.
        # INVALID is final and never rescored — the sweep's idempotence
        # rule; a settled OK row with no newer observation is untouched.
        rows = conn.execute(
            """SELECT g.id, g.source_game_id, g.classification
               FROM games g
               LEFT JOIN game_results r
                      ON r.source_game_id = g.source_game_id
               WHERE g.status = 'ended'
                 AND (r.source_game_id IS NULL
                      OR r.final_result_status = 'UNKNOWN'
                      OR (r.final_result_status != 'INVALID'
                          AND EXISTS (
                              SELECT 1 FROM snapshots s
                              WHERE s.game_id = g.id
                                AND s.captured_at > r.result_at)))
               ORDER BY g.last_seen_at ASC
               LIMIT ?""",
            (batch_limit,)).fetchall()
        stats = {"checked": 0, "settled": 0, "unknown": 0, "invalid": 0,
                 "skipped": 0}
        for g in rows:
            stats["checked"] += 1
            try:
                outcome = _settle_game(conn, g)
            except Exception:
                # fail-closed per game: one unreadable history never
                # breaks the pass — exactly like the sweep's per-game
                # isolation
                stats["skipped"] += 1
                log.exception("settle_worker_game_failed %s",
                              g["source_game_id"])
                continue
            conn.commit()
            if outcome is None:
                continue          # no snapshots — nothing to verify
            kind, status, final = outcome
            stats[kind] += 1
            log.info("settle_worker_result %s verdict=%s final=%s",
                     g["source_game_id"], status, final)
        return stats
    finally:
        conn.close()


def _chain_snapshots(conn: sqlite3.Connection, base_id: str) -> list[dict]:
    """The base game's snapshots plus every ``base#i*`` sibling's, in
    capture order (ties broken by row id).  Suffix ids are discovered by
    one bounded LIKE scan; rows keep their original source_game_id so the
    quality gate's identity check sees one consistent event (the gate's
    per-game identity is the ROW's own id — chained rows are rewritten to
    the BASE id first, so a chained history can never be rejected as
    cross-event contamination for carrying sibling ids)."""
    rows: list[dict] = []
    for r in conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE s.source_game_id = ?
                  OR s.source_game_id LIKE ?
               ORDER BY s.captured_at ASC, s.id ASC""",
            (base_id, base_id + "#i%")):
        d = dict(r)
        d["source_game_id"] = base_id      # bridge: one identity for the gate
        rows.append(d)
    return rows


def _settle_game(conn: sqlite3.Connection,
                 g: sqlite3.Row) -> Optional[Tuple[str, str, Optional[int]]]:
    """Settle ONE ended game inside the caller's connection.

    The SAME quality gate, the SAME final-result rule and the SAME upsert
    the scorecard's ``capture_results`` runs — a verdict this worker
    writes is the verdict the sweep would have written.
    """
    # per-game commit first — the same write-lock hygiene as the sweep's
    # capture_results (release the previous game's tx before this one)
    conn.commit()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM snapshots WHERE game_id=? ORDER BY captured_at ASC",
        (g["id"],)).fetchall()]
    if not rows:
        return None                      # never captured — nothing to verify
    qual, reason = _snapshot_history_quality(rows)
    if qual == "INVALID":
        conn.execute(
            """INSERT OR IGNORE INTO game_quality
               (source_game_id, classification, status, reason, checked_at)
               VALUES (?, ?, 'INVALID', ?, ?)""",
            (g["source_game_id"], g["classification"], reason, _utcnow()))
        conn.execute(
            """INSERT INTO game_results (
                source_game_id, classification, final_home, final_away,
                final_total, result_at, final_result_status)
            VALUES (?, ?, NULL, NULL, NULL, ?, 'INVALID')
            ON CONFLICT(source_game_id) DO UPDATE SET
                final_result_status = excluded.final_result_status,
                result_at = excluded.result_at""",
            (g["source_game_id"], g["classification"],
             rows[-1]["captured_at"]))
        return ("invalid", "INVALID", None)
    last = rows[-1]
    status, fh, fa = Scorecard._final_result(last, len(rows))
    # INSTANCE-CHAIN BRIDGE: when the base series alone cannot prove a
    # final (non-OK verdict on a VALID history) but a split sibling carries
    # the continuation, decide from the CHAINED series instead.  A base
    # series that already settles OK on its own is never re-read through
    # the bridge; a chained INVALID history falls back to the base verdict
    # below — the bridge never invents a verdict the gate refuses.
    if status != "OK":
        chained = _chain_snapshots(conn, g["source_game_id"])
        if len(chained) > len(rows):
            c_qual, _c_reason = _snapshot_history_quality(chained)
            if c_qual != "INVALID":
                c_status, c_fh, c_fa = Scorecard._final_result(
                    chained[-1], len(chained))
                if c_status == "OK":
                    conn.execute(
                        """INSERT INTO game_results (
                            source_game_id, classification, final_home,
                            final_away, final_total, result_at,
                            final_result_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_game_id) DO UPDATE SET
                            final_home = excluded.final_home,
                            final_away = excluded.final_away,
                            final_total = excluded.final_total,
                            result_at = excluded.result_at,
                            final_result_status = excluded.final_result_status""",
                        (g["source_game_id"], g["classification"], c_fh,
                         c_fa, (c_fh + c_fa) if (c_fh is not None
                                                 and c_fa is not None) else None,
                         chained[-1]["captured_at"], c_status))
                    return ("settled", c_status,
                            (c_fh + c_fa) if (c_fh is not None
                                              and c_fa is not None) else None)
    conn.execute(
        """INSERT INTO game_results (
            source_game_id, classification, final_home, final_away,
            final_total, result_at, final_result_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_game_id) DO UPDATE SET
            final_home = excluded.final_home,
            final_away = excluded.final_away,
            final_total = excluded.final_total,
            result_at = excluded.result_at,
            final_result_status = excluded.final_result_status""",
        (g["source_game_id"], g["classification"], fh, fa,
         (fh + fa) if (fh is not None and fa is not None) else None,
         last["captured_at"], status))
    return ("settled" if status == "OK" else "unknown", status,
            (fh + fa) if (fh is not None and fa is not None) else None)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SettleWorker:
    """The dedicated settlement thread — a daemon, like the reconciler."""

    def __init__(self, db_path: Path | str, *,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 batch_limit: int = DEFAULT_BATCH_LIMIT,
                 log: logging.Logger | None = None):
        self._db_path = Path(db_path)
        self._interval_s = float(interval_s)
        self._batch_limit = int(batch_limit)
        self._log = log or logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict:
        return settle_once(self._db_path, log=self._log,
                           batch_limit=self._batch_limit)

    def loop(self) -> None:
        import time
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # the WORKER never dies: a locked/unreadable DB is logged
                # and retried on the next cadence tick
                self._log.exception("settle_worker_pass_failed")
            self._stop.wait(self._interval_s)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.loop, name="blm-settle-worker", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
