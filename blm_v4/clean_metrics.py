"""BLM V4 — Clean Metrics Database (statistical foundation).

A SEPARATE SQLite database (``blm_metrics_clean.db``) populated ONLY from
validated observations flowing through the corrected live pipeline:

    PokerBet source
      → existing validated collector (quality + replay protections)
      → CleanMetricsStore.record_snapshot()
      → blm_metrics_clean.db

Historical ``blm_pokerbet.db`` data is deliberately NEVER imported: the
clean statistical population starts at zero and grows only from
post-implementation observations, so future distributions/z-scores are
derived from data collected under the current (trusted) behavior.  The
old database remains untouched.

Design rules implemented here:

  * One observation = one defined state: game/instance + capture time +
    market line identity.  Multiple DISTINCT lines at one capture time
    are preserved as separate ``clean_market_observations`` rows — never
    averaged, never collapsed.
  * Game time is classification-aware (BETUAL_NBA 4×10=40 min,
    CYBER_2K26 4×12=48 min) and derived from the actual period + count-
    down clock, with the period-label fallback for quarter-less rows.
  * Pace is deterministic state information — actual and required
    Pts/min, pace difference — NOT a probability.  No probability
    columns exist; Over/Under prices are preserved as market data only.
  * ``z_score`` is always NULL until a separately authorized clean
    reference population justifies a baseline.
  * Rejected observations are retained with a status/reason (never
    silently discarded) but only ``status='VALID'`` rows form the
    statistical population.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.projection import duration_for, project, row_elapsed_minutes
from blm_v4.terminal_eligibility import (
    TERMINAL_EXCLUSION_REASON,
    PREDICTIVE_LABEL_ELIGIBLE,
    PREDICTIVE_LABEL_EXCLUDED,
    eligibility_state,
    predictive_validation_label,
)

DEFAULT_CLEAN_DB = "blm_metrics_clean.db"

KNOWN_CLASSIFICATIONS = ("BETUAL_NBA", "CYBER_2K26")

# Status vocabulary (directive): VALID enters the statistical population;
# everything else is retained with a reason and excluded from statistics.
VALID = "VALID"
INVALID = "INVALID"
REJECTED = "REJECTED"
STALE = "STALE"
REPLAY = "REPLAY"
MISSING_CLOCK = "MISSING_CLOCK"
INVALID_MARKET = "INVALID_MARKET"

# Same 2-game-minute tolerance as the collector's clock_regression signal.
CLOCK_REGRESSION_TOLERANCE_MIN = 2.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clean_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clean_games (
    source_game_id      TEXT PRIMARY KEY,   -- instance id (incl. #iN)
    base_game_id        TEXT,               -- fixture id (suffix stripped)
    classification      TEXT,
    home_team           TEXT,
    away_team           TEXT,
    status              TEXT,
    first_seen_at       TEXT,
    last_seen_at        TEXT,
    final_home          INTEGER,
    final_away          INTEGER,
    final_total         INTEGER,
    final_result_status TEXT,               -- FINAL | UNKNOWN
    finalized_at        TEXT
);

CREATE TABLE IF NOT EXISTS clean_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    period_label  TEXT,
    quarter       INTEGER,
    clock         TEXT,
    home_score    INTEGER,
    away_score    INTEGER,
    total_points  INTEGER,
    game_status   TEXT,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_clean_snap_game_ts
    ON clean_snapshots(source_game_id, captured_at);

CREATE TABLE IF NOT EXISTS clean_market_observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id TEXT NOT NULL,
    captured_at   TEXT NOT NULL,            -- market capture time
    market_type   TEXT,
    line_value    REAL,
    over_price    REAL,
    under_price   REAL,
    source        TEXT,                     -- event_view | ws
    UNIQUE(source_game_id, captured_at, line_value)
);

CREATE TABLE IF NOT EXISTS clean_observations (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id            TEXT NOT NULL,
    snapshot_id               INTEGER,
    captured_at               TEXT NOT NULL,
    classification            TEXT,
    -- score state (raw reproducibility on every observation)
    home_score                INTEGER,
    away_score                INTEGER,
    total_points              INTEGER,
    -- game time (classification-aware)
    total_game_minutes        REAL,
    elapsed_game_minutes      REAL,
    remaining_game_minutes    REAL,
    progress_pct              REAL,          -- 0..100
    -- pace (deterministic game-state metrics, not probabilities)
    actual_pts_per_min        REAL,
    required_pts_per_min      REAL,
    required_remaining_points REAL,
    pace_difference           REAL,          -- actual - required (signed)
    required_pace_target      TEXT,          -- 'LIVE_TOTAL_LINE' when set
    -- market
    live_total_line           REAL,
    over_price                REAL,
    under_price               REAL,
    market_captured_at        TEXT,
    market_source             TEXT,
    -- model output (authoritative projection; fair == projected total)
    projected_final_total     REAL,
    fair_total                REAL,
    market_fair_difference    REAL,          -- fair - live line (signed)
    -- statistics: NULL until a clean reference population is authorized
    z_score                   REAL,
    -- quality gate
    status                    TEXT NOT NULL,
    reason                    TEXT,
    -- terminal-checkpoint eligibility (directive): TERMINAL =
    -- SETTLEMENT/AUDIT ONLY.  Row is never deleted; it just cannot enter
    -- predictive validation, calibration, interaction or confirmation.
    terminal                  INTEGER NOT NULL DEFAULT 0,
    predictive_eligible       INTEGER NOT NULL DEFAULT 1,
    exclusion_reason          TEXT,
    predictive_validation     TEXT
);
CREATE INDEX IF NOT EXISTS idx_clean_obs_game_ts
    ON clean_observations(source_game_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_clean_obs_status
    ON clean_observations(status);

-- Pace projector / trajectory layer (deterministic; NO statistics).
-- One row per VALID clean observation, recomputed idempotently per game
-- as new observations arrive (subsequent-observation linkage updates in
-- place).  Every trajectory row is reproducible from its stored inputs;
-- future information is stored ONLY as outcome fields (subsequent_*,
-- final_settled_total) and never used in the row's own state metrics.
CREATE TABLE IF NOT EXISTS clean_projections (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id            INTEGER NOT NULL UNIQUE REFERENCES clean_observations(id),
    model_version             TEXT NOT NULL,
    source_game_id            TEXT NOT NULL,
    classification            TEXT,
    captured_at               TEXT NOT NULL,
    period_label              TEXT,
    clock                     TEXT,
    elapsed_game_minutes      REAL,
    remaining_game_minutes    REAL,
    progress_pct              REAL,
    current_total_points      INTEGER,
    -- market
    live_total_line           REAL,
    market_captured_at        TEXT,
    market_age_seconds        REAL,
    market_status             TEXT,          -- LIVE | STALE | MISSING
    -- deterministic pace metrics
    actual_pts_per_min        REAL,
    required_pts_per_min      REAL,
    pace_gap                  REAL,          -- required - actual (signed)
    required_to_actual_ratio  REAL,          -- preserved (incl. negative/large)
    projected_final_total     REAL,          -- trajectory: total + actual*remaining
    projection_vs_live_line   REAL,          -- projected - live line (signed)
    fair_total                REAL,          -- existing model fair total (unchanged)
    -- recent pace (game-clock windows; actual span recorded)
    recent_pace_1m            REAL,
    recent_span_1m            REAL,
    recent_pace_2m            REAL,
    recent_span_2m            REAL,
    recent_pace_3m            REAL,
    recent_span_3m            REAL,
    recent_pace_5m            REAL,
    recent_span_5m            REAL,
    -- acceleration / deceleration (explicit window)
    pace_acceleration         REAL,
    acceleration_window       TEXT,
    -- trajectory state (descriptive category, assigned with hindsight)
    trajectory_state          TEXT,          -- A | B | C | D | NULL
    -- subsequent-observation linkage (outcome storage only)
    subsequent_observation_id INTEGER,
    subsequent_actual_pace    REAL,
    subsequent_pace_change    REAL,
    subsequent_live_line      REAL,
    subsequent_live_line_change REAL,
    final_settled_total       INTEGER,
    -- terminal eligibility propagated from the clean observation
    terminal                  INTEGER NOT NULL DEFAULT 0,
    predictive_eligible       INTEGER NOT NULL DEFAULT 1,
    -- quality / provenance
    status                    TEXT NOT NULL,
    computed_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clean_proj_game_ts
    ON clean_projections(source_game_id, captured_at);
"""

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _i(v: Any) -> Optional[int]:
    return None if v is None else int(v)


def _base_id(source_game_id: str) -> str:
    return re.sub(r"#i\d+$", "", source_game_id or "")


def period_metrics(
    classification: Optional[str], quarter: Optional[int],
    period_label: Optional[str], clock: Optional[str],
) -> dict[str, Any]:
    """Classification-aware game-time metrics for one observation.

    Returns total_game_minutes, elapsed_game_minutes,
    remaining_game_minutes, progress_pct (0..100), plus a status/reason
    pair.  ``status`` is VALID only when the game clock resolves; a
    missing/invalid clock yields MISSING_CLOCK, an unknown classification
    yields INVALID (never a manufactured pace).
    """
    cls = str(classification or "")
    if cls not in KNOWN_CLASSIFICATIONS:
        return {
            "total_game_minutes": None,
            "elapsed_game_minutes": None,
            "remaining_game_minutes": None,
            "progress_pct": None,
            "status": INVALID,
            "reason": f"invalid classification {cls!r}",
        }
    q_min, full = duration_for(cls)
    row = {"quarter": _i(quarter), "clock": clock, "period_label": period_label}
    elapsed = row_elapsed_minutes(row, q_min, full)
    if elapsed is None:
        reason = ("missing period/quarter" if row["quarter"] is None
                  and not (period_label or "").strip()
                  else "missing/invalid game clock")
        return {
            "total_game_minutes": full,
            "elapsed_game_minutes": None,
            "remaining_game_minutes": None,
            "progress_pct": None,
            "status": MISSING_CLOCK,
            "reason": reason,
        }
    if elapsed > full:
        return {
            "total_game_minutes": full,
            "elapsed_game_minutes": elapsed,
            "remaining_game_minutes": None,
            "progress_pct": None,
            "status": INVALID,
            "reason": "impossible duration (elapsed exceeds full game)",
        }
    remaining = round(full - elapsed, 2)
    progress_pct = round(min(100.0, max(0.0, elapsed / full * 100.0)), 4)
    return {
        "total_game_minutes": full,
        "elapsed_game_minutes": round(elapsed, 2),
        "remaining_game_minutes": remaining,
        "progress_pct": progress_pct,
        "status": VALID,
        "reason": None,
    }


def pace_metrics(
    elapsed: Optional[float], remaining: Optional[float],
    total_points: Optional[int], live_line: Optional[float],
) -> dict[str, Any]:
    """Deterministic pace metrics against an EXPLICIT target.

    actual_pts_per_min = total / elapsed (only when elapsed > 0 — never
    manufactured).  required_pts_per_min = (target - total) / remaining
    (only when remaining > 0; a negative value is preserved — the score
    is already past the target).  pace_difference = actual - required.
    """
    actual: Optional[float] = None
    if total_points is not None and elapsed is not None and elapsed > 0:
        actual = round(total_points / elapsed, 4)
    required_remaining: Optional[float] = None
    required: Optional[float] = None
    target = None
    if (live_line is not None and total_points is not None
            and remaining is not None and remaining > 0):
        required_remaining = round(live_line - total_points, 4)
        required = round(required_remaining / remaining, 4)
        target = "LIVE_TOTAL_LINE"
    diff = round(actual - required, 4) \
        if actual is not None and required is not None else None
    return {
        "actual_pts_per_min": actual,
        "required_pts_per_min": required,
        "required_remaining_points": required_remaining,
        "pace_difference": diff,
        "required_pace_target": target,
    }


class CleanMetricsStore:
    """Append-only clean statistical database (own SQLite file)."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else \
            Path(__file__).resolve().parent.parent / DEFAULT_CLEAN_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=rwc", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        """Create schema + record the clean-data start marker once."""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._ensure_terminal_columns(conn)
                start = conn.execute(
                    "SELECT value FROM clean_meta WHERE key='clean_metrics_start_at'"
                ).fetchone()
                if start is None:
                    conn.execute(
                        "INSERT INTO clean_meta(key, value) VALUES (?, ?)",
                        ("clean_metrics_start_at", _now_iso()),
                    )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _ensure_terminal_columns(conn: sqlite3.Connection) -> None:
        """Migration: add the terminal-eligibility columns to databases
        created before the directive (CREATE TABLE IF NOT EXISTS does not
        alter existing tables).  Idempotent."""
        for table, cols in (
            ("clean_observations", (
                "terminal INTEGER NOT NULL DEFAULT 0",
                "predictive_eligible INTEGER NOT NULL DEFAULT 1",
                "exclusion_reason TEXT",
                "predictive_validation TEXT",
            )),
            ("clean_projections", (
                "terminal INTEGER NOT NULL DEFAULT 0",
                "predictive_eligible INTEGER NOT NULL DEFAULT 1",
            )),
        ):
            existing = {r["name"] for r in conn.execute(
                f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for coldef in cols:
                name = coldef.split()[0]
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {coldef}")
        conn.commit()

    # ── metadata ────────────────────────────────────────────────────

    def started_at(self) -> Optional[str]:
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT value FROM clean_meta WHERE key='clean_metrics_start_at'"
                ).fetchone()
                return r["value"] if r else None
            finally:
                conn.close()

    # ── recording ───────────────────────────────────────────────────

    def record_snapshot(
        self, source_game_id: str, classification: str,
        captured_at: str, *, quarter: Optional[int], period_label: Optional[str],
        clock: Optional[str], home_score: Optional[int], away_score: Optional[int],
        game_status: str, source: str,
        snapshot_total_line: Optional[float],
        snapshot_over_odds: Optional[float], snapshot_under_odds: Optional[float],
        main_store: Any,
        home_team: Optional[str] = None, away_team: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record one validated snapshot + computed metrics into the clean DB.

        ``main_store`` (PokerBetStore) is read-only here: it supplies the
        current live MatchTotal line (event-view line wins; otherwise the
        lowest line of the latest eu-swarm WS batch, the repository
        convention) and the game's snapshot history for the authoritative
        projection.  Returns the observation dict (status/reason included).
        """
        total_points = None
        if home_score is not None and away_score is not None:
            total_points = int(home_score) + int(away_score)

        tm = period_metrics(classification, quarter, period_label, clock)
        status, reason = tm["status"], tm["reason"]
        elapsed, remaining = tm["elapsed_game_minutes"], tm["remaining_game_minutes"]

        # ── current live total line (explicit identity, never averaged) ──
        line: Optional[float] = _f(snapshot_total_line)
        over_price: Optional[float] = _f(snapshot_over_odds)
        under_price: Optional[float] = _f(snapshot_under_odds)
        market_captured_at = captured_at
        market_source = "event_view"
        ws_batch: list[dict] = []
        try:
            # Always sync the latest eu-swarm batch so EVERY distinct line
            # identity at this capture is preserved (never averaged) — the
            # event-view line only decides the SELECTED line, not which
            # lines are recorded.
            ws_batch = main_store.latest_market_batch(source_game_id) or []
        except Exception:
            ws_batch = []
        if line is None and ws_batch:
            first = ws_batch[0]
            line = _f(first.get("line_value"))
            over_price = _f(first.get("over_price"))
            under_price = _f(first.get("under_price"))
            market_captured_at = first.get("captured_at") or captured_at
            market_source = "ws"
        if line is not None and (line <= 0 or not _isfinite(line)):
            if status == VALID:
                status = INVALID_MARKET
                reason = "malformed market line"
            line = None

        # ── model projection (authoritative fair total) ──────────────
        projected_total: Optional[float] = None
        try:
            rows = main_store.get_snapshots(source_game_id, ascending=True)
            proj = project(rows)
            projected_total = proj.get("expected_total")
        except Exception:
            projected_total = None
        fair_total = projected_total
        mf_diff = round(fair_total - line, 2) \
            if fair_total is not None and line is not None else None

        # ── pace metrics (deterministic) ─────────────────────────────
        pm = pace_metrics(elapsed, remaining, total_points, line)

        # ── terminal-checkpoint eligibility (research-data boundary) ──
        # Single authoritative predicate: TERMINAL = SETTLEMENT/AUDIT
        # ONLY.  The row is still stored with its quality status; it just
        # cannot enter predictive validation / calibration / interaction /
        # confirmation populations downstream.
        terminal, pred_eligible, excl_reason = eligibility_state(
            classification=classification,
            elapsed_minutes=elapsed,
            progress=(tm["progress_pct"] / 100.0
                      if tm["progress_pct"] is not None else None),
            quarter=quarter,
            clock=clock,
            period_label=period_label,
            game_status=game_status,
        )

        # ── instance-scoped regression gates (positive evidence) ─────
        if status == VALID and total_points is not None:
            prev = self._last_valid(source_game_id)
            if (prev is not None and prev["total_points"] is not None
                    and total_points < prev["total_points"]):
                status = REPLAY
                reason = "score regression vs prior clean observation"
            elif (prev is not None and prev["elapsed_game_minutes"] is not None
                  and elapsed is not None
                  and elapsed < prev["elapsed_game_minutes"]
                  - CLOCK_REGRESSION_TOLERANCE_MIN):
                status = REPLAY
                reason = "game-clock regression vs prior clean observation"

        with self._lock:
            conn = self._connect()
            try:
                # game identity (instance-scoped)
                conn.execute(
                    """INSERT INTO clean_games (
                           source_game_id, base_game_id, classification,
                           home_team, away_team, status, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_game_id) DO UPDATE SET
                           last_seen_at = excluded.last_seen_at,
                           status = excluded.status""",
                    (source_game_id, _base_id(source_game_id), classification,
                     home_team or "", away_team or "", game_status,
                     captured_at, captured_at),
                )
                # raw snapshot state (reproducibility)
                cur = conn.execute(
                    """INSERT INTO clean_snapshots (
                           source_game_id, captured_at, period_label, quarter,
                           clock, home_score, away_score, total_points,
                           game_status, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_game_id, captured_at, period_label, quarter, clock,
                     _i(home_score), _i(away_score), total_points,
                     game_status, source),
                )
                snapshot_id = int(cur.lastrowid)
                # market line identities — every distinct line at a capture,
                # never averaged
                if line is not None:
                    conn.execute(
                        """INSERT OR IGNORE INTO clean_market_observations (
                               source_game_id, captured_at, market_type,
                               line_value, over_price, under_price, source)
                           VALUES (?, ?, 'MatchTotal', ?, ?, ?, ?)""",
                        (source_game_id, market_captured_at, line,
                         over_price, under_price, market_source),
                    )
                for m in ws_batch:
                    mv = _f(m.get("line_value"))
                    if mv is None:
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO clean_market_observations (
                               source_game_id, captured_at, market_type,
                               line_value, over_price, under_price, source)
                           VALUES (?, ?, 'MatchTotal', ?, ?, ?, ?)""",
                        (source_game_id, m.get("captured_at") or captured_at, mv,
                         _f(m.get("over_price")), _f(m.get("under_price")), "ws"),
                    )
                # calculated metrics row
                conn.execute(
                    """INSERT INTO clean_observations (
                           source_game_id, snapshot_id, captured_at, classification,
                           home_score, away_score, total_points,
                           total_game_minutes, elapsed_game_minutes,
                           remaining_game_minutes, progress_pct,
                           actual_pts_per_min, required_pts_per_min,
                           required_remaining_points, pace_difference,
                           required_pace_target,
                           live_total_line, over_price, under_price,
                           market_captured_at, market_source,                           projected_final_total, fair_total, market_fair_difference,
                           z_score, status, reason,
                           terminal, predictive_eligible,
                           exclusion_reason, predictive_validation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?)""",
                    (source_game_id, snapshot_id, captured_at, classification,
                     _i(home_score), _i(away_score), total_points,
                     tm["total_game_minutes"], elapsed, remaining,
                     tm["progress_pct"],
                     pm["actual_pts_per_min"], pm["required_pts_per_min"],
                     pm["required_remaining_points"], pm["pace_difference"],
                     pm["required_pace_target"],
                     line, over_price, under_price, market_captured_at,
                     market_source,
                     fair_total, fair_total, mf_diff,
                     None, status, reason,
                     int(terminal), int(pred_eligible),
                     excl_reason,
                     predictive_validation_label(terminal)),
                )
                conn.commit()
            finally:
                conn.close()

        return {
            "source_game_id": source_game_id,
            "captured_at": captured_at,
            "classification": classification,
            "home_score": _i(home_score),
            "away_score": _i(away_score),
            "total_points": total_points,
            **tm, **pm,
            "live_total_line": line,
            "over_price": over_price,
            "under_price": under_price,
            "market_captured_at": market_captured_at,
            "market_source": market_source,
            "projected_final_total": fair_total,
            "fair_total": fair_total,
            "market_fair_difference": mf_diff,
            "z_score": None,
            "status": status,
            "reason": reason,
            "terminal": int(terminal),
            "predictive_eligible": int(pred_eligible),
            "exclusion_reason": excl_reason,
            "predictive_validation": predictive_validation_label(terminal),
        }

    def record_snapshot_obs(self, game: Any, obs: Any, main_store: Any) -> dict:
        """Convenience wrapper: record a MarketObservation + PokerBetGame."""
        return self.record_snapshot(
            source_game_id=game.source_game_id,
            classification=game.classification,
            captured_at=obs.captured_at,
            quarter=obs.quarter,
            period_label=obs.period_label,
            clock=obs.clock,
            home_score=obs.home_score,
            away_score=obs.away_score,
            game_status=getattr(obs, "game_status", "live") or "live",
            source=getattr(obs, "source", "PokerBet") or "PokerBet",
            snapshot_total_line=obs.total_line,
            snapshot_over_odds=obs.total_over_odds,
            snapshot_under_odds=obs.total_under_odds,
            main_store=main_store,
            home_team=game.home_team,
            away_team=game.away_team,
        )

    def finalize(
        self, source_game_id: str, *,
        final_home: Optional[int] = None, final_away: Optional[int] = None,
        final_status: str = "ended",
        classification: Optional[str] = None,
    ) -> None:
        """Record the final result on the clean game/instance record.

        Finals are stored ONCE and never overwrite prior live
        observations (they live on clean_games, not the observation
        rows).  A game that ended without a verified final is recorded
        with final_result_status 'UNKNOWN' and NULL finals.  The record
        is created on demand if no observation was ever written.
        """
        with self._lock:
            conn = self._connect()
            try:
                if final_home is not None and final_away is not None:
                    conn.execute(
                        """UPDATE clean_games
                           SET final_home = ?, final_away = ?,
                               final_total = ?, final_result_status = 'FINAL',
                               finalized_at = ?, status = ?
                           WHERE source_game_id = ?""",
                        (int(final_home), int(final_away),
                         int(final_home) + int(final_away), _now_iso(),
                         final_status, source_game_id),
                    )
                else:
                    conn.execute(
                        """UPDATE clean_games
                           SET final_result_status = 'UNKNOWN',
                               finalized_at = ?, status = ?
                           WHERE source_game_id = ?""",
                        (_now_iso(), final_status, source_game_id),
                    )
                if conn.total_changes == 0:
                    conn.execute(
                        """INSERT OR IGNORE INTO clean_games (
                               source_game_id, base_game_id, classification,
                               status, first_seen_at, last_seen_at,
                               final_result_status, finalized_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (source_game_id, _base_id(source_game_id),
                         classification, final_status, _now_iso(), _now_iso(),
                         "FINAL" if final_home is not None
                         else "UNKNOWN", _now_iso()),
                    )
                conn.commit()
            finally:
                conn.close()

    # ── reads (tests/audit) ─────────────────────────────────────────

    def _last_valid(self, source_game_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    """SELECT total_points, elapsed_game_minutes
                       FROM clean_observations
                       WHERE source_game_id = ? AND status = ?
                       ORDER BY id DESC LIMIT 1""",
                    (source_game_id, VALID),
                ).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    # ── terminal eligibility (directive enforcement) ────────────────

    def restamp_terminal_eligibility(self) -> dict[str, int]:
        """Re-derive terminal / predictive_eligible for EVERY stored clean
        observation and its trajectory row from the authoritative
        predicate (game-time evidence, never 100% alone).  Idempotent:
        repeated execution produces identical eligibility.

        Rows are never deleted — terminal rows keep their quality status
        and all metrics for settlement/audit; they only lose eligibility
        for the predictive research populations."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT o.id, o.classification, o.elapsed_game_minutes,
                              o.progress_pct, s.quarter, s.clock,
                              s.period_label, s.game_status
                       FROM clean_observations o
                       LEFT JOIN clean_snapshots s ON s.id = o.snapshot_id""").fetchall()
                n_terminal = 0
                for r in rows:
                    terminal, pred_eligible, _ = eligibility_state(
                        classification=r["classification"],
                        elapsed_minutes=r["elapsed_game_minutes"],
                        progress=(r["progress_pct"] / 100.0
                                  if r["progress_pct"] is not None else None),
                        quarter=r["quarter"],
                        clock=r["clock"],
                        period_label=r["period_label"],
                        game_status=r["game_status"],
                    )
                    conn.execute(
                        """UPDATE clean_observations
                           SET terminal = ?, predictive_eligible = ?,
                               exclusion_reason = ?, predictive_validation = ?
                           WHERE id = ?""",
                        (int(terminal), int(pred_eligible),
                         TERMINAL_EXCLUSION_REASON if terminal else None,
                         predictive_validation_label(terminal),
                         r["id"]),
                    )
                    conn.execute(
                        """UPDATE clean_projections
                           SET terminal = ?, predictive_eligible = ?
                           WHERE observation_id = ?""",
                        (int(terminal), int(pred_eligible), r["id"]),
                    )
                    n_terminal += int(terminal)
                conn.commit()
                return {
                    "observations": len(rows),
                    "terminal": n_terminal,
                    "predictive_eligible": len(rows) - n_terminal,
                }
            finally:
                conn.close()

    def count_observations(self, status: Optional[str] = None) -> int:
        with self._lock:
            conn = self._connect()
            try:
                q = "SELECT COUNT(*) AS c FROM clean_observations"
                params: tuple = ()
                if status:
                    q += " WHERE status = ?"
                    params = (status,)
                return int(conn.execute(q, params).fetchone()["c"])
            finally:
                conn.close()

    def count_snapshots(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                return int(conn.execute(
                    "SELECT COUNT(*) AS c FROM clean_snapshots").fetchone()["c"])
            finally:
                conn.close()

    def count_market_observations(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                return int(conn.execute(
                    "SELECT COUNT(*) AS c FROM clean_market_observations"
                ).fetchone()["c"])
            finally:
                conn.close()

    def observation(self, obs_id: int) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT * FROM clean_observations WHERE id = ?", (obs_id,),
                ).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def latest_observation(self, source_game_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT * FROM clean_observations WHERE source_game_id = ? "
                    "ORDER BY id DESC LIMIT 1", (source_game_id,),
                ).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def list_market_lines(self, source_game_id: str) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM clean_market_observations "
                    "WHERE source_game_id = ? ORDER BY captured_at, line_value",
                    (source_game_id,)).fetchall()]
            finally:
                conn.close()

    def game(self, source_game_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT * FROM clean_games WHERE source_game_id = ?",
                    (source_game_id,),
                ).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    # ── Pace projector support (deterministic trajectory layer) ────

    def valid_observations(self, source_game_id: str) -> list[dict]:
        """All VALID clean observations for a game, ascending, with the
        raw snapshot period/clock joined (projector provenance).

        Replayed/stale/INVALID observations are never part of the clean
        statistical population, so they cannot feed the trajectory layer.
        """
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(
                    """SELECT o.*, s.period_label, s.clock
                       FROM clean_observations o
                       LEFT JOIN clean_snapshots s ON s.id = o.snapshot_id
                       WHERE o.source_game_id = ? AND o.status = ?
                       ORDER BY o.id ASC""",
                    (source_game_id, VALID)).fetchall()]
            finally:
                conn.close()

    def game_final_total(self, source_game_id: str) -> Optional[int]:
        """Recorded final total for the game/instance (clean_games), or
        None while the game is still live."""
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT final_total FROM clean_games WHERE source_game_id = ?",
                    (source_game_id,),
                ).fetchone()
                return r["final_total"] if r else None
            finally:
                conn.close()

    def replace_projections(
        self, source_game_id: str, rows: list[dict],
    ) -> None:
        """Idempotently replace all trajectory rows for one game.

        One row per VALID clean observation; recomputed wholesale on each
        refresh so the subsequent-observation linkage stays current as new
        observations arrive.  All-or-nothing per game (single transaction).
        """
        cols = (
            "observation_id", "model_version", "source_game_id",
            "classification", "captured_at", "period_label", "clock",
            "elapsed_game_minutes", "remaining_game_minutes", "progress_pct",
            "current_total_points", "live_total_line", "market_captured_at",
            "market_age_seconds", "market_status",
            "actual_pts_per_min", "required_pts_per_min", "pace_gap",
            "required_to_actual_ratio", "projected_final_total",
            "projection_vs_live_line", "fair_total",
            "recent_pace_1m", "recent_span_1m",
            "recent_pace_2m", "recent_span_2m",
            "recent_pace_3m", "recent_span_3m",
            "recent_pace_5m", "recent_span_5m",
            "pace_acceleration", "acceleration_window", "trajectory_state",
            "subsequent_observation_id", "subsequent_actual_pace",
            "subsequent_pace_change", "subsequent_live_line",
            "subsequent_live_line_change", "final_settled_total",
            "terminal", "predictive_eligible",
            "status", "computed_at",
        )
        marks = ",".join("?" * len(cols))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM clean_projections WHERE source_game_id = ?",
                    (source_game_id,),
                )
                for r in rows:
                    conn.execute(
                        f"INSERT INTO clean_projections ({', '.join(cols)}) "
                        f"VALUES ({marks})",
                        [r.get(c) for c in cols],
                    )
                conn.commit()
            finally:
                conn.close()

    def latest_projection(self, source_game_id: str) -> Optional[dict]:
        """Most recent trajectory row for a game (the current live state)."""
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT * FROM clean_projections WHERE source_game_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (source_game_id,),
                ).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def count_projections(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                return int(conn.execute(
                    "SELECT COUNT(*) AS c FROM clean_projections").fetchone()["c"])
            finally:
                conn.close()


def _isfinite(x: float) -> bool:
    import math
    return math.isfinite(x)