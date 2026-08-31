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
from zoneinfo import ZoneInfo

from blm_v4.projection import FULL_GAME_MINUTES, MODEL_VERSION, clock_minutes, project
from blm_v4.trends import analytics_tz

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

-- Historical market-outcome record: ONE row per CLEAN (OK, non-fragment)
-- completed game.  Opening and closing lines are preserved SEPARATELY
-- (OLVC is never overwritten by CLV); every outcome classifies against
-- both.  The analytical timezone is stored with the derived hour/day so
-- the bucketing basis is explicit and never silently re-interpreted.
CREATE TABLE IF NOT EXISTS market_history (
    source_game_id      TEXT PRIMARY KEY,
    classification      TEXT NOT NULL,
    competition         TEXT,
    home_team           TEXT,
    away_team           TEXT,
    started_at          TEXT NOT NULL,       -- UTC (first snapshot)
    analytics_tz        TEXT NOT NULL,       -- timezone basis for the local fields
    started_hour        INTEGER,             -- local hour 0..23
    started_dow         INTEGER,             -- local day of week (0=Mon .. 6=Sun)
    started_date        TEXT,                -- local YYYY-MM-DD
    duration_min        REAL,                -- wall clock first -> last snapshot
    opening_total       REAL,                -- OLVC: first observed total line
    closing_total       REAL,                -- CLV:  last observed total line
    opening_spread      REAL,
    closing_spread      REAL,
    total_line_move     REAL,                -- CLV - OLVC
    spread_line_move    REAL,
    market_move         TEXT,                -- UP | DOWN | UNCHANGED (total line)
    final_home          INTEGER,
    final_away          INTEGER,
    final_total         INTEGER,
    outcome_olvc        TEXT,                -- OVER | UNDER | PUSH
    outcome_clv         TEXT,
    opening_total_edge  REAL,                -- actual_total - OLVC (unrounded)
    closing_total_edge  REAL,                -- actual_total - CLV (unrounded)
    model_versions      TEXT,                -- comma-joined distinct versions seen
    recorded_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mh_hour    ON market_history(started_hour);
CREATE INDEX IF NOT EXISTS idx_mh_clv     ON market_history(closing_total);
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


def _frozen_market_line(conn, source_game_id: str, rows: list[dict],
                        idx: int) -> Optional[float]:
    """Latest verified market total AT-OR-BEFORE rows[idx] (inclusive).

    Snapshot (event-view) lines are primary: the last line observed on ANY
    snapshot up to and including the checkpoint — line moves recorded on
    non-checkpoint snapshots count.  When NO snapshot line was ever
    observed (event-view route down), the eu-swarm WS MatchTotal
    observation at-or-before the checkpoint is the frozen line.

    Never a later observation, never the closing line, never reconstructed
    from later data, never model-derived.  The WS fallback mirrors
    storage.market_observations_before: the LOWEST line of the latest
    batch at-or-before (event-view parity — the feed carries 3 O/U
    variants per capture; the lowest is the main line).
    """
    line: Optional[float] = None
    for rr in rows[: idx + 1]:
        if rr.get("total_line") is not None:
            line = float(rr["total_line"])
    if line is None:
        ws = conn.execute(
            """SELECT line_value FROM market_observations
               WHERE source_game_id=? AND market_type='MatchTotal'
                 AND captured_at = (
                     SELECT MAX(captured_at) FROM market_observations
                     WHERE source_game_id=? AND market_type='MatchTotal'
                       AND captured_at <= ?)
               ORDER BY line_value ASC LIMIT 1""",
            (source_game_id, source_game_id, rows[idx]["captured_at"]),
        ).fetchone()
        if ws and ws["line_value"] is not None:
            line = float(ws["line_value"])
    return line


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


def _outcome_vs_line(final_total: Optional[int], line: Optional[float]) -> Optional[str]:
    """OVER | UNDER | PUSH against a market line — exact comparison, no
    rounding of the underlying values before classification."""
    if final_total is None or line is None:
        return None
    return "OVER" if final_total > line else "UNDER" if final_total < line else "PUSH"


def _edge_vs_line(final_total: Optional[int], line: Optional[float]) -> Optional[float]:
    """actual_total - line, unrounded edge (rounded only for storage)."""
    if final_total is None or line is None:
        return None
    return round(final_total - line, 2)


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
    #    impossible transitions (a big regression = wrong event/instance).
    #    A >50pt hop is only impossible when the wall-clock gap between
    #    the two captures is short: virtual games run ~7x real speed
    #    (~33 pts/real-min), so a big hop across a multi-minute capture
    #    gap is a legitimate fast game, while 50+ pts in under 90s is
    #    physically impossible = foreign/contaminated state (observed:
    #    the lobby-attribution jumps of 55-108 pts in 9-12s ticks).
    last = None
    last_ts = None
    for r in rows:
        hs, as_ = r.get("home_score"), r.get("away_score")
        if hs is None or as_ is None:
            continue
        if last is not None:
            lh, la = last
            if hs < lh or as_ < la:
                return "INVALID", "score regression (contamination?)"
            if hs - lh > 50 or as_ - la > 50:
                gap_sec = None
                if last_ts:
                    try:
                        gap_sec = (datetime.fromisoformat(
                            r["captured_at"].replace("Z", "+00:00"))
                            - datetime.fromisoformat(
                                last_ts.replace("Z", "+00:00"))).total_seconds()
                    except Exception:
                        pass
                if gap_sec is None or gap_sec < 90.0:
                    return "INVALID", "impossible score jump"
        last = (hs, as_)
        last_ts = r.get("captured_at")
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
                              AND {_STARTS_Q1_SQL}
                              AND NOT EXISTS (SELECT 1 FROM game_quality q
                                              WHERE q.source_game_id = g.source_game_id
                                                AND q.status = 'INVALID'))
                        THEN 0 ELSE 1 END""")
                conn.commit()
            finally:
                conn.close()

    # ── Record predictions ─────────────────────────────────────────

    def record_predictions(self) -> dict[str, int]:
        """Record checkpoint predictions for every game — and REBASE any
        existing row onto the CURRENT projection code.

        v4-pace-1 is defined by projection.py as it exists today: a
        prediction stored by an older build (pre live-score-floor, pre
        single-source) is NOT a v4-pace-1 output.  Every run recomputes
        each checkpoint from the same snapshots (deterministic, no
        final-result leakage — snapshots are immutable inputs) and
        upserts, so the stored table always reflects the current model
        version definition and the scorecard never measures a dead build.
        """
        stats = {"checked": 0, "recorded": 0, "rebased": 0,
                 "skipped_no_checkpoint": 0}
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
                    recorded, rebased = self._record_game(conn, g, rows)
                    stats["recorded"] += recorded
                    stats["rebased"] += rebased
                conn.commit()
            finally:
                conn.close()
        return stats

    def _record_game(self, conn, g, rows: list[dict]) -> tuple[int, int]:
        """Record or REBASE checkpoints for one game.

        Returns (recorded, rebased): newly inserted rows vs existing rows
        whose projection changed (i.e. an older model build had stored it).
        Each checkpoint is defined by the FIRST snapshot that carries it
        (the moment the quarter started) and is written exactly once per
        run — deterministic and idempotent."""
        n = 0
        rb = 0
        seen: set[str] = set()
        for i, r in enumerate(rows):
            cp = _checkpoint_for(r.get("quarter"), r.get("clock"),
                                 r.get("period_label"))
            if cp is None or cp in seen:
                continue
            seen.add(cp)
            # FREEZE the market: the last PokerBet-observed total line at
            # or before THIS checkpoint snapshot — never a later line,
            # never model-derived.  Snapshot lines (event-view) are
            # primary and may arrive on any snapshot, not only checkpoint
            # rows; when the event-view route is down, the eu-swarm WS
            # feed's MatchTotal observation at-or-before is the frozen
            # line (re-evaluated per checkpoint, so moves are captured).
            last_line = _frozen_market_line(conn, g["source_game_id"], rows, i)
            # Pin the frozen line into the projection (same value as the
            # snapshot-derived line when both exist — the override only
            # matters when the WS feed supplied the market).
            proj = project(rows[: i + 1], last_line)
            if proj["home_projection"] is None or proj["away_projection"] is None:
                continue
            combined = (proj["home_score"] or 0) + (proj["away_score"] or 0) \
                if proj["home_score"] is not None else None
            cur = conn.execute(
                "SELECT projected_total FROM predictions "
                "WHERE source_game_id=? AND checkpoint=? AND model_version=?",
                (g["source_game_id"], cp, MODEL_VERSION),
            ).fetchone()
            conn.execute(
                """INSERT INTO predictions (
                    source_game_id, classification, model_version, checkpoint,
                    quarter, predicted_at, source_snapshot_at, elapsed_minutes,
                    progress, home_score, away_score, combined,
                    projected_home, projected_away, projected_total, market_total, valid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(source_game_id, checkpoint, model_version) DO UPDATE SET
                    classification = excluded.classification,
                    quarter = excluded.quarter,
                    source_snapshot_at = excluded.source_snapshot_at,
                    elapsed_minutes = excluded.elapsed_minutes,
                    progress = excluded.progress,
                    home_score = excluded.home_score,
                    away_score = excluded.away_score,
                    combined = excluded.combined,
                    projected_home = excluded.projected_home,
                    projected_away = excluded.projected_away,
                    projected_total = excluded.projected_total,
                    market_total = excluded.market_total,
                    valid = 1""",
                (
                    g["source_game_id"], g["classification"], MODEL_VERSION, cp,
                    r.get("quarter"), _utcnow(), r["captured_at"],
                    proj["elapsed_minutes"], proj["progress"],
                    proj["home_score"], proj["away_score"], combined,
                    proj["home_projection"], proj["away_projection"],
                    proj["expected_total"], last_line,
                ),
            )
            if cur is None:
                n += 1
            elif abs((cur["projected_total"] or 0) - (proj["expected_total"] or 0)) > 0.05:
                rb += 1
        return n, rb

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
        """Record or REBASE fixed % checkpoints for one game (same
        current-code-wins semantics as _record_game)."""
        n = 0
        for pct in FIXED_CHECKPOINT_PCTS:
            target = pct / 100.0
            cp_key = f"pct{pct}"
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
            # FREEZE the market: last PokerBet-observed line at/before this
            # checkpoint snapshot — same at-or-before rule as the quarter
            # checkpoints (snapshot lines primary, eu-swarm WS fallback).
            line = _frozen_market_line(conn, g["source_game_id"], rows, idx)
            proj = project(rows[: idx + 1], line)
            if proj["home_projection"] is None or proj["away_projection"] is None:
                stats["skipped_no_snapshot"] += 1
                continue
            combined = ((proj["home_score"] or 0) + (proj["away_score"] or 0)
                        if proj["home_score"] is not None else None)
            cur = conn.execute(
                "SELECT projected_total FROM predictions "
                "WHERE source_game_id=? AND checkpoint=? AND model_version=?",
                (g["source_game_id"], cp_key, MODEL_VERSION),
            ).fetchone()
            conn.execute(
                """INSERT INTO predictions (
                    source_game_id, classification, model_version, checkpoint,
                    checkpoint_percent, distance_pct, quarter, predicted_at,
                    source_snapshot_at, elapsed_minutes, progress,
                    home_score, away_score, combined,
                    projected_home, projected_away, projected_total, market_total, valid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(source_game_id, checkpoint, model_version) DO UPDATE SET
                    classification = excluded.classification,
                    checkpoint_percent = excluded.checkpoint_percent,
                    distance_pct = excluded.distance_pct,
                    quarter = excluded.quarter,
                    source_snapshot_at = excluded.source_snapshot_at,
                    elapsed_minutes = excluded.elapsed_minutes,
                    progress = excluded.progress,
                    home_score = excluded.home_score,
                    away_score = excluded.away_score,
                    combined = excluded.combined,
                    projected_home = excluded.projected_home,
                    projected_away = excluded.projected_away,
                    projected_total = excluded.projected_total,
                    market_total = excluded.market_total,
                    valid = 1""",
                (
                    g["source_game_id"], g["classification"], MODEL_VERSION, cp_key,
                    target, dist_pct, r.get("quarter"), _utcnow(), r["captured_at"],
                    proj["elapsed_minutes"], proj["progress"],
                    proj["home_score"], proj["away_score"], combined,
                    proj["home_projection"], proj["away_projection"],
                    proj["expected_total"], line,
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
                    # M007-M8: re-verify OK + UNKNOWN results against the
                    # CURRENT quality gate every run — an OK recorded under an
                    # older, laxer gate must not survive a now-failing history.
                    # INVALID is final and never rescored (idempotent).
                    if has and has["final_result_status"] == "INVALID":
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
                # game wasn't captured from its 1st quarter OR its tracking
                # history fails the current quality gate (INVALID).  Fragments
                # are scored for diagnostics but EXCLUDED from headline metrics.
                comp = {}
                for r in conn.execute(
                        f"""SELECT g.source_game_id,
                                  (SELECT COUNT(*) FROM snapshots s
                                   WHERE s.game_id = g.id) AS n,
                                  {_STARTS_Q1_SQL} AS starts_q1,
                                  EXISTS (SELECT 1 FROM game_quality q
                                          WHERE q.source_game_id = g.source_game_id
                                            AND q.status = 'INVALID') AS bad_quality
                           FROM games g"""):
                    comp[r["source_game_id"]] = (r["n"], r["starts_q1"], r["bad_quality"])
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
                    n, starts_q1, bad_quality = comp.get(r["source_game_id"], (0, 0, 1))
                    fragment = 0 if (n >= 15 and starts_q1 and not bad_quality) else 1
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

    # ── Historical market-outcome records ──────────────────────────

    def record_market_history(self) -> dict[str, int]:
        """Persist one market_history row per CLEAN completed game (OK +
        non-fragment): OLVC/CLV preserved separately, unrounded edges,
        outcome vs both lines, line movement, and local hour/day/date in
        the configured analytics timezone.  Idempotent upsert — re-runs
        never duplicate.  Fragments/INVALID games never enter the table
        (they would poison the historical base)."""
        stats = {"recorded": 0, "skipped_fragment": 0}
        with self._lock:
            conn = self._connect()
            try:
                comp = {}
                for r in conn.execute(
                        f"""SELECT g.source_game_id,
                                  (SELECT COUNT(*) FROM snapshots s
                                   WHERE s.game_id = g.id) AS n,
                                  {_STARTS_Q1_SQL} AS starts_q1
                           FROM games g"""):
                    comp[r["source_game_id"]] = (r["n"], r["starts_q1"])
                tz_name = analytics_tz()
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = timezone.utc
                rows = conn.execute(
                    """SELECT r.source_game_id, r.classification, r.final_home,
                              r.final_away, r.final_total,
                              g.competition, g.home_team, g.away_team
                       FROM game_results r
                       JOIN games g ON g.source_game_id = r.source_game_id
                       WHERE r.final_result_status = 'OK'"""
                ).fetchall()
                for r in rows:
                    n, starts_q1 = comp.get(r["source_game_id"], (0, 0))
                    if not (n >= 15 and starts_q1):
                        stats["skipped_fragment"] += 1
                        continue
                    snaps = conn.execute(
                        """SELECT captured_at, total_line, spread
                           FROM snapshots WHERE source_game_id = ?
                           ORDER BY captured_at""",
                        (r["source_game_id"],),
                    ).fetchall()
                    if not snaps:
                        continue
                    lines = [s["total_line"] for s in snaps
                             if s["total_line"] is not None]
                    olvc = lines[0] if lines else None
                    clv = lines[-1] if lines else None
                    spreads = [s["spread"] for s in snaps
                               if s["spread"] is not None]
                    osp = spreads[0] if spreads else None
                    csp = spreads[-1] if spreads else None
                    ft = r["final_total"]
                    move = None if olvc is None or clv is None \
                        else round(clv - olvc, 2)
                    mv = None
                    if move is not None:
                        mv = ("UP" if move > 0 else "DOWN" if move < 0
                              else "UNCHANGED")
                    started = snaps[0]["captured_at"]
                    local = datetime.fromisoformat(
                        started.replace("Z", "+00:00")).astimezone(tz)
                    dur = None
                    try:
                        d0 = datetime.fromisoformat(
                            started.replace("Z", "+00:00"))
                        d1 = datetime.fromisoformat(
                            snaps[-1]["captured_at"].replace("Z", "+00:00"))
                        dur = round((d1 - d0).total_seconds() / 60.0, 1)
                    except Exception:
                        pass
                    versions = [v[0] for v in conn.execute(
                        "SELECT DISTINCT model_version FROM predictions "
                        "WHERE source_game_id = ?", (r["source_game_id"],))]
                    conn.execute(
                        """INSERT INTO market_history (
                            source_game_id, classification, competition,
                            home_team, away_team, started_at, analytics_tz,
                            started_hour, started_dow, started_date,
                            duration_min, opening_total, closing_total,
                            opening_spread, closing_spread, total_line_move,
                            spread_line_move, market_move, final_home,
                            final_away, final_total, outcome_olvc,
                            outcome_clv, opening_total_edge,
                            closing_total_edge, model_versions, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_game_id) DO UPDATE SET
                            classification = excluded.classification,
                            closing_total = excluded.closing_total,
                            closing_spread = excluded.closing_spread,
                            total_line_move = excluded.total_line_move,
                            spread_line_move = excluded.spread_line_move,
                            market_move = excluded.market_move,
                            final_home = excluded.final_home,
                            final_away = excluded.final_away,
                            final_total = excluded.final_total,
                            outcome_clv = excluded.outcome_clv,
                            closing_total_edge = excluded.closing_total_edge,
                            model_versions = excluded.model_versions,
                            recorded_at = excluded.recorded_at""",
                        (
                            r["source_game_id"], r["classification"],
                            r["competition"], r["home_team"], r["away_team"],
                            started, tz_name, local.hour, local.weekday(),
                            local.strftime("%Y-%m-%d"), dur,
                            olvc, clv, osp, csp, move,
                            None if osp is None or csp is None
                            else round(csp - osp, 2), mv,
                            r["final_home"], r["final_away"], ft,
                            _outcome_vs_line(ft, olvc),
                            _outcome_vs_line(ft, clv),
                            _edge_vs_line(ft, olvc),
                            _edge_vs_line(ft, clv),
                            ",".join(versions) or None,
                            _utcnow(),
                        ),
                    )
                    stats["recorded"] += 1
                conn.commit()
            finally:
                conn.close()
        return stats

    # ── Run all phases ─────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        rec = self.record_predictions()
        fx = self.record_fixed_checkpoints()
        res = self.capture_results()
        sco = self.score_all()
        mkt = self.record_market_history()
        return {"recorded": rec, "fixed": fx, "results": res,
                "scored": sco, "market": mkt}

    # ── Read API (used by /api/v4/scorecard) ───────────────────────

    def summary(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            out = _summary_sql(conn)
            out["eligible_games"] = self._eligible_games_sql(conn)
            return out
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

    def eligible_games(self, limit: int = 200) -> list[dict[str, Any]]:
        """M007-M8 auditability: per-game eligibility trace.

        For every completed-or-flagged game:
          game_id -> quality status -> result status -> final score ->
          eligible/ineligible -> predictions used (headline) -> contribution.
        """
        conn = self._connect()
        try:
            return self._eligible_games_sql(conn, limit)
        finally:
            conn.close()

    @staticmethod
    def _eligible_games_sql(conn, limit: int = 200) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT g.source_game_id, g.home_team, g.away_team,
                      COALESCE(q.status, '-') AS quality_status,
                      r.final_result_status AS result_status,
                      r.final_home, r.final_away, r.final_total,
                      (SELECT COUNT(*) FROM snapshots s
                       WHERE s.game_id = g.id) AS snapshots,
                      (SELECT COUNT(*) FROM prediction_scores s
                       WHERE s.source_game_id = g.source_game_id) AS scored_predictions,
                      (SELECT COUNT(*) FROM prediction_scores s
                       WHERE s.source_game_id = g.source_game_id AND s.fragment = 0) AS predictions_used,
                      (SELECT ROUND(AVG(abs_total_error), 2) FROM prediction_scores s
                       WHERE s.source_game_id = g.source_game_id AND s.fragment = 0) AS contribution_mae,
                      CASE WHEN r.final_result_status = 'OK'
                            AND NOT EXISTS (SELECT 1 FROM game_quality q2
                                            WHERE q2.source_game_id = g.source_game_id
                                              AND q2.status = 'INVALID')
                      THEN 1 ELSE 0 END AS eligible
               FROM games g
               LEFT JOIN game_results r ON r.source_game_id = g.source_game_id
               LEFT JOIN game_quality q ON q.source_game_id = g.source_game_id
               WHERE r.source_game_id IS NOT NULL OR q.source_game_id IS NOT NULL
               ORDER BY g.source_game_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


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


def _market_history_sql(conn) -> list[dict]:
    """M008-SCORE-M1: per-game OLV/CLV record (distinct fields; missing
    values stay NULL — never substituted).  Used by OLV/CLV accounting."""
    rows = conn.execute(
        """SELECT source_game_id, classification,
                  started_at,
                  opening_total,
                  closing_total,
                  final_home, final_away, final_total,
                  outcome_olvc, outcome_clv,
                  opening_total_edge, closing_total_edge
           FROM market_history
           ORDER BY source_game_id"""
    ).fetchall()
    return [dict(r) for r in rows]


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
    # data-quality: four DISTINCT concepts — recorded predictions vs
    # completed games vs valid scored games vs invalid/excluded.  These
    # must never be conflated (an INVALID game's predictions exist and
    # are recorded, but the game is excluded; fragment games are scored
    # for diagnostics only).
    total["_quality"] = {
        "recorded_predictions": conn.execute(
            "SELECT COUNT(*) c FROM predictions").fetchone()["c"],
        "headline_predictions": conn.execute(
            "SELECT COUNT(*) c FROM prediction_scores "
            "WHERE fragment = 0").fetchone()["c"],
        "completed_games": conn.execute(
            "SELECT COUNT(*) c FROM game_results "
            "WHERE final_result_status='OK'").fetchone()["c"],
        "valid_scored_games": conn.execute(
            "SELECT COUNT(DISTINCT source_game_id) c FROM prediction_scores "
            "WHERE fragment = 0").fetchone()["c"],
        "valid": conn.execute(
            "SELECT COUNT(*) c FROM game_results r "
            "WHERE r.final_result_status='OK' AND NOT EXISTS ("
            "  SELECT 1 FROM game_quality q "
            "  WHERE q.source_game_id = r.source_game_id AND q.status='INVALID')"
        ).fetchone()["c"],
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
    """M008-SCORE-M1 forensic market comparison (checkpoint-market line).

    Line type used: the market total frozen at the prediction's checkpoint
    (prediction_scores.market_total — the line that existed AT that
    checkpoint, never the closing line).  BLM error = abs(BLM - actual);
    Market error = abs(market - actual).  Every rate carries explicit
    numerator + denominator; O/U accounting names the line type.
    """
    rows = conn.execute(
        """SELECT model_total, market_total, actual_total, total_error,
                  market_error, model_beat_market, ou_prediction, ou_result, ou_correct
           FROM prediction_scores WHERE market_total IS NOT NULL AND fragment = 0""",
    ).fetchall()
    rows = [dict(r) for r in rows]
    n = len(rows)
    if not n:
        return {"n": 0, "line_type": "checkpoint_market"}
    # BLM abs errors (real MAE — never negative) + signed bias
    blm_abs = [abs(r["total_error"]) for r in rows if r["total_error"] is not None]
    blm_sgn = [r["total_error"] for r in rows if r["total_error"] is not None]
    mkt_errs = [r["market_error"] for r in rows if r["market_error"] is not None]
    model_mae = round(statistics.mean(blm_abs), 2) if blm_abs else None
    model_bias = round(statistics.mean(blm_sgn), 2) if blm_sgn else None
    market_mae = round(statistics.mean([abs(e) for e in mkt_errs]), 2) if mkt_errs else None
    market_bias = round(statistics.mean(mkt_errs), 2) if mkt_errs else None
    beat = [r["model_beat_market"] for r in rows if r["model_beat_market"] is not None]
    model_beat_n = sum(1 for b in beat if b == 1)
    market_beat_n = sum(1 for b in beat if b == 0)
    ties_n = sum(1 for b in beat if b is not None and b not in (0, 1))  # 0==0 case: equal abs
    # explicit win/loss/tie via abs errors (recompute — model_beat_market is 1/0 only)
    w_ = [1 if abs(r["total_error"]) < abs(r["market_error"]) else 0
          for r in rows if r["total_error"] is not None and r["market_error"] is not None]
    t_ = [1 if abs(r["total_error"]) == abs(r["market_error"]) else 0
          for r in rows if r["total_error"] is not None and r["market_error"] is not None]
    model_beat_n = sum(w_)
    market_beat_n = len(w_) - model_beat_n - sum(t_)
    ties_n = sum(t_)
    ou = [r["ou_correct"] for r in rows if r["ou_correct"] is not None]
    # M008-SCORE-M1 item 6: hit rate = hits / (hits + misses).  Pushes
    # (ou_result == 0) are NOT misses — excluded from the denominator.
    ou_hits = sum(1 for r in rows if r["ou_correct"] == 1)
    ou_decided = [r for r in rows if r["ou_correct"] is not None and r["ou_result"] != 0]
    ou_over = sum(1 for r in rows if r["ou_result"] == 1)
    ou_under = sum(1 for r in rows if r["ou_result"] == -1)
    ou_push = sum(1 for r in rows if r["ou_result"] == 0)
    # signed disparity = BLM prediction - market line (both directions kept)
    disp = [(r["model_total"] or 0) - (r["market_total"] or 0) for r in rows
            if r["model_total"] is not None and r["market_total"] is not None]
    return {
        "n": n,
        "line_type": "checkpoint_market",
        "model_mae": model_mae,
        "model_bias": model_bias,
        "market_mae": market_mae,
        "market_bias": market_bias,
        "model_beat_market_rate": round(model_beat_n / n, 3) if n else None,
        "model_beat_market_n": model_beat_n,
        "model_beat_market_d": n,
        "market_beat_blm_n": market_beat_n,
        "ties_n": ties_n,
        "ou_predictions": len(ou),
        "ou_hit_rate": round(ou_hits / len(ou_decided), 3) if ou_decided else None,
        "ou_hit_n": ou_hits,
        "ou_hit_d": len(ou_decided),
        "ou_over": ou_over,
        "ou_under": ou_under,
        "ou_push": ou_push,
        "ou_line_type": "checkpoint_market",
        "disparity_min": round(min(disp), 2) if disp else None,
        "disparity_max": round(max(disp), 2) if disp else None,
        "disparity_abs_max": round(max(abs(d) for d in disp), 2) if disp else None,
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
