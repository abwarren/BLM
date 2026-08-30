"""BLM V4 — Projection Accuracy Scorecard.

Continuously measures how accurate BLM's projections are, persisted in
SQLite so history survives server/collector restarts and dashboard
refreshes.

Tables
------
predictions        — one projection observation per game per checkpoint.
                     Checkpoints: first snapshot of Q1..Q4, plus the first
                     Q4 snapshot with clock <= 2:00 ("final").  At most one
                     prediction per checkpoint per model version, so a game
                     yields at most 5 predictions (never hundreds).
game_results       — final result per finished game with
                     final_result_status = OK | UNKNOWN.  UNKNOWN results
                     are never scored (data-quality rule).
prediction_scores  — error metrics per prediction vs the actual result.

Methodology
-----------
- A prediction is ``projection.project(snapshots_up_to_checkpoint)`` — the
  exact function the live dashboard displays.  No final-result data is
  ever used while computing a projection (no look-ahead / no leakage).
- A final result is accepted (OK) only when the game is marked ended AND
  the last snapshot shows a completed event (4th Quarter clock 00:00 or
  a Full Time / End / Finished label) AND the game has >= 5 snapshots.
  Anything else (ended at half-time, disappeared mid-game, stub-only) is
  UNKNOWN and never scored.
- Over/Under and market-error comparisons are computed only where a valid
  market total existed at prediction time.  No market -> no edge.
- All aggregates are split by model_version (see projection.MODEL_VERSION);
  different versions are never combined.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.projection import FULL_GAME_MINUTES, MODEL_VERSION, clock_minutes, project

SCORECARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id    TEXT NOT NULL,
    classification    TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    checkpoint        TEXT NOT NULL,          -- q1|q2|q3|q4|final|pct10..pct90
    checkpoint_percent REAL,                  -- fixed-checkpoint target (0.10..0.90)
    distance_pct      REAL,                   -- |selected progress - target| in pp
    quarter           INTEGER,
    predicted_at      TEXT NOT NULL,          -- when the record was written
    source_snapshot_at TEXT NOT NULL,         -- captured_at of the snapshot used
    elapsed_minutes   REAL,
    progress          REAL,                   -- 0..1 game completed
    home_score        INTEGER,
    away_score        INTEGER,
    combined          INTEGER,
    projected_home    REAL,
    projected_away    REAL,
    projected_total   REAL,
    market_total      REAL,
    valid             INTEGER NOT NULL DEFAULT 1,  -- 0 = malformed, never scored
    UNIQUE(source_game_id, checkpoint, model_version)
);

CREATE TABLE IF NOT EXISTS game_quality (
    source_game_id   TEXT PRIMARY KEY,
    classification   TEXT NOT NULL,
    status           TEXT NOT NULL,          -- OK | INVALID
    reason           TEXT NOT NULL DEFAULT '',
    checked_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS game_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id      TEXT NOT NULL,
    classification      TEXT NOT NULL,
    final_home          INTEGER,
    final_away          INTEGER,
    final_total         INTEGER,
    result_at           TEXT NOT NULL,
    final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN',  -- OK | UNKNOWN
    UNIQUE(source_game_id)
);

CREATE TABLE IF NOT EXISTS prediction_scores (
    prediction_id     INTEGER PRIMARY KEY REFERENCES predictions(id),
    source_game_id    TEXT NOT NULL,
    classification    TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    home_error        REAL,   -- projected_home - actual_home
    away_error        REAL,
    total_error       REAL,   -- projected_total - actual_total
    abs_home_error    REAL,
    abs_away_error    REAL,
    abs_total_error   REAL,
    total_pct_error   REAL,   -- abs_total_error / actual_total * 100
    model_total       REAL,
    market_total      REAL,
    actual_total      REAL,
    market_error      REAL,   -- market_total - actual_total
    model_beat_market INTEGER,  -- 1 = |model err| < |market err|, 0 = no, NULL = no market
    ou_prediction     INTEGER,  -- model vs market: 1 OVER, -1 UNDER, 0 push
    ou_result         INTEGER,  -- actual vs market:  1 OVER, -1 UNDER, 0 push
    ou_correct        INTEGER,  -- 1/0, NULL when no valid market
    scored_at         TEXT NOT NULL,
    fragment          INTEGER NOT NULL DEFAULT 0,  -- 1 = incomplete history (diagnostics only)
    UNIQUE(prediction_id)
);

CREATE INDEX IF NOT EXISTS idx_pred_games   ON predictions(source_game_id);
CREATE INDEX IF NOT EXISTS idx_scores_ver   ON prediction_scores(model_version);
CREATE INDEX IF NOT EXISTS idx_scores_game  ON prediction_scores(source_game_id);
"""

_CHECKPOINTS = ("q1", "q2", "q3", "q4", "final")

# A game is a FRAGMENT (diagnostics only, never headline) unless captured
# with >= 15 snapshots starting in its 1st quarter — a short or mid-game
# capture's errors are capture artifacts, not model accuracy (a 96-second
# Q4-only window can't pace a full game).  `starts_q1` uses the quarter
# column when present (list/event rows sometimes carry quarter=NULL).
_STARTS_Q1_SQL = """(
    SELECT CASE WHEN s.quarter IS NOT NULL THEN (s.quarter <= 1)
                ELSE LOWER(COALESCE(s.period_label, '')) LIKE '1st%' END
    FROM snapshots s WHERE s.game_id = g.id
    ORDER BY s.captured_at ASC LIMIT 1)"""

# Fixed game-completion checkpoints (percent of the 40-minute game).
# The snapshot closest to each target (within tolerance) is selected and
# the prediction computed from snapshots up to and including it — the
# prediction that was actually available at that point (no look-ahead).
FIXED_CHECKPOINT_PCTS = [10, 20, 30, 40, 50, 60, 70, 80, 90]
MAX_DISTANCE_PCT = 5.0  # tolerance: closest snapshot must be within ±5pp


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_final_label(period_label: Optional[str]) -> bool:
    p = (period_label or "").lower()
    return any(k in p for k in ("full time", "finished", "end of match", "match ended"))


def _checkpoint_for(quarter: Optional[int], clock: Optional[str],
                    period_label: Optional[str] = None) -> Optional[str]:
    q = quarter
    if q is None:
        q = _period_quarter(period_label)
    if q is None or q < 1:
        return None
    if q >= 4:
        el = clock_minutes(q, clock)
        if el is not None and el >= 38.0:  # Q4 with <= 2:00 left (40-min game)
            return "final"
        return "q4"
    return f"q{q}"


def _period_quarter(period_label: Optional[str]) -> Optional[int]:
    """Derive quarter number from a period label ("3rd Quarter" -> 3)."""
    p = (period_label or "").lower()
    m = re.search(r"(\d)(?:st|nd|rd|th)?\s*quarter", p)
    if m:
        return int(m.group(1))
    if p.startswith("half"):
        return 2
    return None


def _progress_of(r: dict) -> Optional[float]:
    """Game-completion fraction (0..1) for a snapshot row, or None.

    Uses quarter + clock; falls back to the period label when the
    structured quarter is missing (event-view snapshots often store
    only the label).
    """
    q = r.get("quarter")
    if q is None:
        q = _period_quarter(r.get("period_label"))
    el = clock_minutes(q, r.get("clock"))
    if el is None:
        return None
    return round(min(1.0, max(0.0, el / FULL_GAME_MINUTES)), 4)


def _snapshot_history_quality(rows: list[dict]) -> tuple[str, str]:
    """Validate a game's snapshot history for scoreability.

    Returns (status, reason):
      status = OK | INVALID
      reason = short human string describing the first failure.

    Checks (in order): ordering, identity, score monotonicity, no
    impossible transitions, classification consistency.  A single bad
    snapshot poisons the whole game — it must not inflate accuracy.
    """
    if not rows:
        return "INVALID", "no snapshots"
    # 1. timestamp ordering (captured_at must be strictly non-decreasing)
    ts_prev = None
    for r in rows:
        ts = r.get("captured_at")
        if ts is None:
            return "INVALID", "missing captured_at"
        if ts_prev is not None and ts < ts_prev:
            return "INVALID", "timestamp out of order"
        ts_prev = ts
    # 2. event identity: every snapshot must match the game's identity
    gid = rows[0].get("source_game_id")
    for r in rows:
        if r.get("source_game_id") != gid:
            return "INVALID", "cross-event contamination"
        if r.get("classification") != rows[0].get("classification"):
            return "INVALID", "classification changed mid-game"
    # 3. score monotonicity (basketball scores only increase) + no
    #    impossible transitions (a big regression = wrong event/instance)
    last = None
    for r in rows:
        hs, as_ = r.get("home_score"), r.get("away_score")
        if hs is None or as_ is None:
            continue
        if last is not None:
            lh, la = last
            if hs < lh or as_ < la:
                return "INVALID", "score regression (contamination?)"
            if hs - lh > 50 or as_ - la > 50:
                return "INVALID", "impossible score jump"
        last = (hs, as_)
    return "OK", ""


class Scorecard:
    """Persistent projection-accuracy scorer (thread-safe)."""

    def __init__(self, db_path: Path | str):
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCORECARD_SCHEMA)
                # migrate pre-fragment DBs (ADD COLUMN is cheap + idempotent)
                cols = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(prediction_scores)")}
                if "fragment" not in cols:
                    conn.execute(
                        "ALTER TABLE prediction_scores "
                        "ADD COLUMN fragment INTEGER NOT NULL DEFAULT 0")
                # backfill existing rows with their TRUE fragment (the ADD
                # COLUMN default of 0 would mislabel every old fragment game)
                conn.execute(
                    f"""UPDATE prediction_scores
                        SET fragment = CASE WHEN EXISTS (
                            SELECT 1 FROM games g
                            WHERE g.source_game_id = prediction_scores.source_game_id
                              AND (SELECT COUNT(*) FROM snapshots s
                                   WHERE s.game_id = g.id) >= 15
                              AND {_STARTS_Q1_SQL})
                        THEN 0 ELSE 1 END""")
                conn.commit()
            finally:
                conn.close()

    # ── Record predictions ─────────────────────────────────────────

    def record_predictions(self) -> dict[str, int]:
        """Record missing checkpoint predictions for every game.

        Idempotent: one prediction per (game, checkpoint, model_version).
        Works for live games (new checkpoints appear as quarters advance)
        and, on first run after deploy, backfills ended games from their
        stored mid-game snapshots — recomputing the exact projection the
        model would have shown at that moment (no final-result leakage).
        """
        stats = {"checked": 0, "recorded": 0, "skipped_no_checkpoint": 0}
        with self._lock:
            conn = self._connect()
            try:
                games = conn.execute(
                    "SELECT id, source_game_id, classification FROM games"
                ).fetchall()
                for g in games:
                    stats["checked"] += 1
                    rows = conn.execute(
                        "SELECT * FROM snapshots WHERE game_id=? ORDER BY captured_at ASC",
                        (g["id"],),
                    ).fetchall()
                    rows = [dict(r) for r in rows]
                    recorded = self._record_game(conn, g, rows)
                    stats["recorded"] += recorded
                conn.commit()
            finally:
                conn.close()
        return stats

    def _record_game(self, conn, g, rows: list[dict]) -> int:
        """Record missing checkpoints for one game. Returns count recorded."""
        n = 0
        for i, r in enumerate(rows):
            cp = _checkpoint_for(r.get("quarter"), r.get("clock"),
                                 r.get("period_label"))
            if cp is None:
                continue
            has = conn.execute(
                "SELECT 1 FROM predictions WHERE source_game_id=? AND checkpoint=? AND model_version=?",
                (g["source_game_id"], cp, MODEL_VERSION),
            ).fetchone()
            if has:
                continue
            proj = project(rows[: i + 1])
            if proj["home_projection"] is None or proj["away_projection"] is None:
                continue
            combined = (proj["home_score"] or 0) + (proj["away_score"] or 0) \
                if proj["home_score"] is not None else None
            conn.execute(
                """INSERT OR IGNORE INTO predictions (
                    source_game_id, classification, model_version, checkpoint,
                    quarter, predicted_at, source_snapshot_at, elapsed_minutes,
                    progress, home_score, away_score, combined,
                    projected_home, projected_away, projected_total, market_total, valid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    g["source_game_id"], g["classification"], MODEL_VERSION, cp,
                    r.get("quarter"), _utcnow(), r["captured_at"],
                    proj["elapsed_minutes"], proj["progress"],
                    proj["home_score"], proj["away_score"], combined,
                    proj["home_projection"], proj["away_projection"],
                    proj["expected_total"], proj["market_total"],
                ),
            )
            n += 1
        return n

    # ── Fixed game-completion checkpoints ─────────────────────────

    def record_fixed_checkpoints(self) -> dict[str, int]:
        """Record predictions at fixed 10%..90% game-completion points.

        For each game and each target, select the snapshot whose elapsed
        game time is closest to the target (within MAX_DISTANCE_PCT) and
        record the projection from snapshots up to and including it —
        exactly the prediction that was available at that moment.
        """
        stats = {"checked": 0, "recorded": 0,
                 "skipped_no_snapshot": 0, "skipped_tolerance": 0}
        with self._lock:
            conn = self._connect()
            try:
                games = conn.execute(
                    "SELECT id, source_game_id, classification FROM games"
                ).fetchall()
                for g in games:
                    stats["checked"] += 1
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM snapshots WHERE game_id=? ORDER BY captured_at ASC",
                        (g["id"],),
                    ).fetchall()]
                    stats["recorded"] += self._record_fixed_game(conn, g, rows, stats)
                conn.commit()
            finally:
                conn.close()
        return stats

    def _record_fixed_game(self, conn, g, rows: list[dict], stats: dict) -> int:
        n = 0
        for pct in FIXED_CHECKPOINT_PCTS:
            target = pct / 100.0
            cp_key = f"pct{pct}"
            has = conn.execute(
                "SELECT 1 FROM predictions WHERE source_game_id=? AND checkpoint=? AND model_version=?",
                (g["source_game_id"], cp_key, MODEL_VERSION),
            ).fetchone()
            if has:
                continue
            # closest snapshot to the target
            best: Optional[tuple[float, int]] = None
            for i, r in enumerate(rows):
                prog = _progress_of(r)
                if prog is None:
                    continue
                d = abs(prog - target)
                if best is None or d < best[0]:
                    best = (d, i)
            if best is None:
                stats["skipped_no_snapshot"] += 1
                continue
            dist_pct = round(best[0] * 100.0, 2)
            if dist_pct > MAX_DISTANCE_PCT:
                stats["skipped_tolerance"] += 1
                continue
            idx = best[1]
            r = rows[idx]
            proj = project(rows[: idx + 1])
            if proj["home_projection"] is None or proj["away_projection"] is None:
                stats["skipped_no_snapshot"] += 1
                continue
            combined = ((proj["home_score"] or 0) + (proj["away_score"] or 0)
                        if proj["home_score"] is not None else None)
            conn.execute(
                """INSERT OR IGNORE INTO predictions (
                    source_game_id, classification, model_version, checkpoint,
                    checkpoint_percent, distance_pct, quarter, predicted_at,
                    source_snapshot_at, elapsed_minutes, progress,
                    home_score, away_score, combined,
                    projected_home, projected_away, projected_total, market_total, valid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    g["source_game_id"], g["classification"], MODEL_VERSION, cp_key,
                    target, dist_pct, r.get("quarter"), _utcnow(), r["captured_at"],
                    proj["elapsed_minutes"], proj["progress"],
                    proj["home_score"], proj["away_score"], combined,
                    proj["home_projection"], proj["away_projection"],
                    proj["expected_total"], proj["market_total"],
                ),
            )
            n += 1
        return n

    # ── Final results ──────────────────────────────────────────────

    def capture_results(self) -> dict[str, int]:
        """Capture final results for ended games (once per game).

        Quality-gated: a game whose snapshot history fails validation
        (ordering, identity, monotonicity) is recorded as UNKNOWN and
        never scored.
        """
        stats = {"checked": 0, "ok": 0, "unknown": 0, "invalid": 0}
        with self._lock:
            conn = self._connect()
            try:
                games = conn.execute(
                    "SELECT id, source_game_id, classification, status FROM games"
                ).fetchall()
                for g in games:
                    if g["status"] != "ended":
                        continue
                    stats["checked"] += 1
                    has = conn.execute(
                        "SELECT final_result_status FROM game_results WHERE source_game_id=?",
                        (g["source_game_id"],),
                    ).fetchone()
                    if has and has["final_result_status"] != "UNKNOWN":
                        continue
                    # UNKNOWN rows are re-verified: they may have been recorded
                    # under an older, stricter gate (e.g. quarter=NULL list
                    # stubs before the late-Q4 rule).  Games that are UNKNOWN
                    # BY DESIGN (half-time, <5 snaps) fail the re-check and
                    # stay UNKNOWN — idempotent.
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM snapshots WHERE game_id=? ORDER BY captured_at ASC",
                        (g["id"],),
                    ).fetchall()]
                    if not rows:
                        continue  # never captured — nothing to verify
                    qual, reason = _snapshot_history_quality(rows)
                    if qual == "INVALID":
                        stats["invalid"] += 1
                        conn.execute(
                            """INSERT OR IGNORE INTO game_quality
                               (source_game_id, classification, status, reason, checked_at)
                               VALUES (?, ?, 'INVALID', ?, ?)""",
                            (g["source_game_id"], g["classification"], reason, _utcnow()),
                        )
                        # record an INVALID result so it's never rescored and the
                        # dashboard can show VALID vs INVALID/EXCLUDED distinctly
                        conn.execute(
                            """INSERT INTO game_results (
                                source_game_id, classification, final_home, final_away,
                                final_total, result_at, final_result_status)
                            VALUES (?, ?, NULL, NULL, NULL, ?, 'INVALID')
                            ON CONFLICT(source_game_id) DO UPDATE SET
                                final_result_status = excluded.final_result_status,
                                result_at = excluded.result_at""",
                            (g["source_game_id"], g["classification"], rows[-1]["captured_at"]),
                        )
                        continue
                    last = rows[-1] if rows else None
                    if not last:
                        continue
                    nsnaps = len(rows)
                    status, fh, fa = self._final_result(last, nsnaps)
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
                        (
                            g["source_game_id"], g["classification"],
                            fh, fa, (fh + fa) if (fh is not None and fa is not None) else None,
                            last["captured_at"], status,
                        ),
                    )
                    stats[status.lower()] += 1
                conn.commit()
            finally:
                conn.close()
        return stats

    @staticmethod
    def _final_result(last: dict, nsnaps: int) -> tuple[str, Optional[int], Optional[int]]:
        """(status, final_home, final_away) — OK only for reliably finished games."""
        fh = last.get("home_score")
        fa = last.get("away_score")
        period = (last.get("period_label") or "").lower()
        clock = (last.get("clock") or "").strip()
        quarter = last.get("quarter")
        # Stub-only games (list-level snapshots, no event-view) can't prove a result.
        if nsnaps < 5:
            return "UNKNOWN", None, None
        if fh is None or fa is None:
            return "UNKNOWN", None, None
        if _is_final_label(period):
            return "OK", int(fh), int(fa)
        # 4th-quarter ending.  List-row snapshots carry quarter=NULL (the
        # label is authoritative), so accept the label directly — but only
        # when the clock is within the final 2 game-minutes (or empty),
        # otherwise the game was lost mid-quarter and the score isn't final.
        p4 = period.startswith("4th") or (quarter is not None and quarter >= 4)
        if p4:
            el = clock_minutes(4, clock) if clock else None
            if el is None:
                el = clock_minutes(quarter if quarter is not None else 4, clock)
            if clock in ("00:00", "0:00", "") or (
                    el is not None and el >= FULL_GAME_MINUTES - 2.0):
                return "OK", int(fh), int(fa)
            # Unparseable clock (mm > 12) = the panel's "21:00" sentinel for
            # a finished period.  A clean, monotonic history whose last row
            # is a "4th Quarter" label IS a verified finish — the score is
            # the game's endpoint.  (Mid-Q4 parseable clocks like 05:00 fail
            # the el >= 38 check above, so they stay UNKNOWN.)
            if el is None:
                return "OK", int(fh), int(fa)
        # Half-time / mid-game disappearance -> not a reliably finished game.
        return "UNKNOWN", None, None

    # ── Score predictions ──────────────────────────────────────────

    def score_all(self) -> dict[str, int]:
        """Compute error metrics for every unscored prediction of games
        with an OK final result.  Predictions whose source snapshot is at
        or after the result are rejected (never look-ahead)."""
        stats = {"scored": 0, "rejected": 0}
        with self._lock:
            conn = self._connect()
            try:
                # per-game history completeness: fragment = < 15 snaps OR the
                # game wasn't captured from its 1st quarter.  Fragments are
                # scored for diagnostics but EXCLUDED from headline metrics.
                comp = {}
                for r in conn.execute(
                        f"""SELECT g.source_game_id,
                                  (SELECT COUNT(*) FROM snapshots s
                                   WHERE s.game_id = g.id) AS n,
                                  {_STARTS_Q1_SQL} AS starts_q1
                           FROM games g"""):
                    comp[r["source_game_id"]] = (r["n"], r["starts_q1"])
                rows = conn.execute(
                    """SELECT p.id AS pid, p.source_game_id, p.classification,
                              p.model_version, p.projected_home, p.projected_away,
                              p.projected_total, p.market_total, p.source_snapshot_at,
                              r.final_home, r.final_away, r.final_total, r.result_at
                       FROM predictions p
                       JOIN game_results r ON r.source_game_id = p.source_game_id
                       WHERE r.final_result_status = 'OK' AND p.valid = 1"""
                ).fetchall()
                for r in rows:
                    if r["source_snapshot_at"] >= r["result_at"]:
                        stats["rejected"] += 1
                        continue
                    n, starts_q1 = comp.get(r["source_game_id"], (0, 0))
                    fragment = 0 if (n >= 15 and starts_q1) else 1
                    conn.execute(
                        """INSERT INTO prediction_scores (
                            prediction_id, source_game_id, classification, model_version,
                            home_error, away_error, total_error,
                            abs_home_error, abs_away_error, abs_total_error, total_pct_error,
                            model_total, market_total, actual_total,
                            market_error, model_beat_market,
                            ou_prediction, ou_result, ou_correct, scored_at, fragment)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(prediction_id) DO UPDATE SET
                            fragment = excluded.fragment""",
                        self._score_row(r) + (fragment,),
                    )
                    stats["scored"] += 1
                conn.commit()
            finally:
                conn.close()
        return stats

    @staticmethod
    def _score_row(r) -> tuple:
        fh, fa, ft = r["final_home"], r["final_away"], r["final_total"]
        ph, pa, pt = r["projected_home"], r["projected_away"], r["projected_total"]
        mkt = r["market_total"]
        he = round(ph - fh, 2)
        ae = round(pa - fa, 2)
        te = round(pt - ft, 2)
        ahe, aae = abs(he), abs(ae)
        ate = abs(te)
        pct = round(ate / ft * 100, 2) if ft else None
        market_error = round(mkt - ft, 2) if mkt is not None else None
        beat = (1 if ate < abs(market_error) else 0) if market_error is not None else None
        ou_pred = (1 if pt > mkt else -1 if pt < mkt else 0) if mkt is not None else None
        ou_res = (1 if ft > mkt else -1 if ft < mkt else 0) if mkt is not None else None
        ou_correct = (1 if ou_pred == ou_res and ou_pred != 0 else 0) if mkt is not None else None
        return (
            r["pid"], r["source_game_id"], r["classification"], r["model_version"],
            he, ae, te, ahe, aae, ate, pct,
            pt, mkt, ft, market_error, beat,
            ou_pred, ou_res, ou_correct, _utcnow(),
        )

    # ── Run all phases ─────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        rec = self.record_predictions()
        fx = self.record_fixed_checkpoints()
        res = self.capture_results()
        sco = self.score_all()
        return {"recorded": rec, "fixed": fx, "results": res, "scored": sco}

    # ── Read API (used by /api/v4/scorecard) ───────────────────────

    def summary(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return _summary_sql(conn)
        finally:
            conn.close()

    def fixed_checkpoints(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return _fixed_checkpoints_sql(conn)
        finally:
            conn.close()

    def by_progress(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return _by_progress_sql(conn)
        finally:
            conn.close()

    def market_compare(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return _market_compare_sql(conn)
        finally:
            conn.close()

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT s.source_game_id, s.classification, s.model_version,
                          s.model_total, s.market_total, s.actual_total,
                          s.total_error, s.abs_total_error, s.ou_prediction,
                          s.ou_result, s.ou_correct, s.scored_at, s.fragment,
                          g.home_team, g.away_team,
                          p.checkpoint, p.checkpoint_percent, p.distance_pct,
                          p.progress, p.source_snapshot_at
                   FROM prediction_scores s
                   JOIN predictions p ON p.id = s.prediction_id
                   LEFT JOIN games g ON g.source_game_id = s.source_game_id
                   ORDER BY s.scored_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ── SQL aggregations (shared by Scorecard + /api/v4/scorecard) ─────

def _per_version_metrics(conn, where: str = "1=1", params: tuple = ()) -> dict:
    rows = conn.execute(
        f"""SELECT model_version, COUNT(*) n, COUNT(DISTINCT source_game_id) games,
                   AVG(abs_total_error) mae, AVG(total_error) bias,
                   AVG(total_pct_error) mape
            FROM prediction_scores WHERE {where} GROUP BY model_version""",
        params,
    ).fetchall()
    out = {}
    for r in rows:
        errs = [x["abs_total_error"] for x in conn.execute(
            f"SELECT abs_total_error FROM prediction_scores WHERE model_version=? AND {where}",
            (r["model_version"],) + params,
        ).fetchall()]
        # RMSE = sqrt(mean(err^2)) — the true root-mean-square error.
        # NOTE: NOT pstdev (std dev ignores the mean/bias component and can
        # come out below MAE, which is mathematically impossible for RMSE).
        rmse = None
        if errs:
            rmse = round((sum(e * e for e in errs) / len(errs)) ** 0.5, 2)
        out[r["model_version"]] = {
            "model_version": r["model_version"],
            "predictions": r["n"],
            "games": r["games"],
            "mae": round(r["mae"], 2) if r["mae"] is not None else None,
            "rmse": rmse,
            "bias": round(r["bias"], 2) if r["bias"] is not None else None,
            "median_abs_error": round(statistics.median(errs), 2) if errs else None,
            "mape": round(r["mape"], 2) if r["mape"] is not None else None,
        }
    return out


def _summary_sql(conn) -> dict[str, Any]:
    # Headline metrics: FULL histories only (fragment = 0).  Fragment games
    # (short or mid-game capture) are scored for diagnostics but excluded —
    # their errors are capture artifacts, not model accuracy.
    total = _per_version_metrics(conn, "fragment = 0", ())
    home = _per_version_metrics(conn, "home_error IS NOT NULL AND fragment = 0", ())
    away = _per_version_metrics(conn, "away_error IS NOT NULL AND fragment = 0", ())
    # home/away MAE + bias per version
    for ver in total:
        hs = [x["abs_home_error"] for x in conn.execute(
            "SELECT abs_home_error FROM prediction_scores "
            "WHERE model_version=? AND home_error IS NOT NULL AND fragment = 0",
            (ver,),
        ).fetchall()]
        hb = [x["home_error"] for x in conn.execute(
            "SELECT home_error FROM prediction_scores "
            "WHERE model_version=? AND home_error IS NOT NULL AND fragment = 0",
            (ver,),
        ).fetchall()]
        as_ = [x["abs_away_error"] for x in conn.execute(
            "SELECT abs_away_error FROM prediction_scores "
            "WHERE model_version=? AND away_error IS NOT NULL AND fragment = 0",
            (ver,),
        ).fetchall()]
        ab = [x["away_error"] for x in conn.execute(
            "SELECT away_error FROM prediction_scores "
            "WHERE model_version=? AND away_error IS NOT NULL AND fragment = 0",
            (ver,),
        ).fetchall()]
        total[ver]["home_mae"] = round(statistics.mean(hs), 2) if hs else None
        total[ver]["home_bias"] = round(statistics.mean(hb), 2) if hb else None
        total[ver]["away_mae"] = round(statistics.mean(as_), 2) if as_ else None
        total[ver]["away_bias"] = round(statistics.mean(ab), 2) if ab else None
        ok = conn.execute(
            "SELECT COUNT(*) c FROM game_results WHERE final_result_status='OK'"
        ).fetchone()["c"]
        unk = conn.execute(
            "SELECT COUNT(*) c FROM game_results WHERE final_result_status='UNKNOWN'"
        ).fetchone()["c"]
        total[ver]["completed_games"] = ok
        total[ver]["unscored_ended_games"] = unk
    # data-quality: valid vs excluded
    total["_quality"] = {
        "valid": conn.execute("SELECT COUNT(*) c FROM game_quality WHERE status='OK'").fetchone()["c"],
        "invalid": conn.execute("SELECT COUNT(*) c FROM game_quality WHERE status='INVALID'").fetchone()["c"],
        "excluded_games": conn.execute("SELECT COUNT(*) c FROM game_results WHERE final_result_status!='OK'").fetchone()["c"],
        "excluded_reasons": {r["reason"]: r["c"] for r in conn.execute(
            "SELECT reason, COUNT(*) c FROM game_quality WHERE status='INVALID' GROUP BY reason")},
    }
    # fragment diagnostics — NEVER headline accuracy
    frag = conn.execute(
        """SELECT COUNT(DISTINCT source_game_id) games, COUNT(*) n,
                  AVG(abs_total_error) mae, AVG(total_pct_error) mape
           FROM prediction_scores WHERE fragment = 1"""
    ).fetchone()
    total["_fragments"] = {
        "excluded_from_headline": True,
        "games": frag["games"],
        "predictions": frag["n"],
        "mae": round(frag["mae"], 2) if frag["mae"] is not None else None,
        "mape": round(frag["mape"], 2) if frag["mape"] is not None else None,
    }
    return {"versions": total}


def _fixed_checkpoints_sql(conn) -> list[dict[str, Any]]:
    """MAE + count per fixed game-completion checkpoint (10%..90%)."""
    out = []
    for pct in FIXED_CHECKPOINT_PCTS:
        cp = f"pct{pct}"
        rows = conn.execute(
            """SELECT s.abs_total_error FROM prediction_scores s
               JOIN predictions p ON p.id = s.prediction_id
               WHERE p.checkpoint = ? AND s.fragment = 0""",
            (cp,),
        ).fetchall()
        errs = [r["abs_total_error"] for r in rows]
        out.append({
            "checkpoint": cp,
            "percent": pct,
            "n": len(errs),
            "mae": round(statistics.mean(errs), 2) if errs else None,
            "median": round(statistics.median(errs), 2) if errs else None,
        })
    return out


def _by_progress_sql(conn) -> list[dict[str, Any]]:
    """Banded accuracy by game progress: 0-10, 10-25, 25-50, 50-75, 75-90, 90-100%."""
    bands = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 0.90), (0.90, 1.01)]
    out = []
    for lo, hi in bands:
        rows = conn.execute(
            """SELECT s.abs_total_error FROM prediction_scores s
               JOIN predictions p ON p.id = s.prediction_id
               WHERE p.progress IS NOT NULL AND p.progress >= ? AND p.progress < ?
                 AND s.fragment = 0""",
            (lo, hi),
        ).fetchall()
        errs = [r["abs_total_error"] for r in rows]
        out.append({
            "band": f"{int(lo*100)}-{min(int(hi*100),100)}%",
            "min": round(lo, 2), "max": round(hi, 2),
            "n": len(errs),
            "mae": round(statistics.mean(errs), 2) if errs else None,
        })
    return out


def _market_compare_sql(conn) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT model_total, market_total, actual_total, total_error,
                  market_error, model_beat_market, ou_prediction, ou_result, ou_correct
           FROM prediction_scores WHERE market_total IS NOT NULL AND fragment = 0""",
    ).fetchall()
    rows = [dict(r) for r in rows]
    n = len(rows)
    if not n:
        return {"n": 0}
    model_mae = statistics.mean([r["total_error"] for r in rows if r["total_error"] is not None])
    mkt_errs = [r["market_error"] for r in rows if r["market_error"] is not None]
    market_mae = statistics.mean([abs(e) for e in mkt_errs]) if mkt_errs else None
    beat = [r["model_beat_market"] for r in rows if r["model_beat_market"] is not None]
    ou = [r["ou_correct"] for r in rows if r["ou_correct"] is not None]
    return {
        "n": n,
        "model_mae": round(model_mae, 2),
        "market_mae": round(market_mae, 2) if market_mae is not None else None,
        "model_beat_market_rate": round(sum(beat) / len(beat), 3) if beat else None,
        "ou_predictions": len(ou),
        "ou_hit_rate": round(sum(ou) / len(ou), 3) if ou else None,
        "over": sum(1 for r in rows if r["ou_result"] == 1),
        "under": sum(1 for r in rows if r["ou_result"] == -1),
        "push": sum(1 for r in rows if r["ou_result"] == 0),
    }


# ── CLI ───────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    import os
    ap = argparse.ArgumentParser(description="BLM v4 projection scorecard")
    ap.add_argument("--db", default=os.environ.get(
        "BLM_POKERBET_DB", str(Path(__file__).resolve().parent.parent / "blm_pokerbet.db")))
    ap.add_argument("--once", action="store_true", help="run all phases once and exit")
    args = ap.parse_args()

    sc = Scorecard(args.db)
    if args.once:
        print(json.dumps(sc.run(), indent=2, default=str))
        print(json.dumps(sc.summary(), indent=2, default=str))
        return
    # loop until Ctrl-C
    import time
    while True:
        stats = sc.run()
        print(_utcnow(), json.dumps(stats, default=str))
        time.sleep(30)


if __name__ == "__main__":
    main()
