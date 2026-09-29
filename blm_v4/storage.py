"""
BLM V4 — PokerBet Data Pipeline: SQLite Storage.

Append-only snapshot store with full source provenance:
  - source, source_game_id, source_url, captured_at, competition,
    region, game_family, classification on every record
  - games deduped on (source, source_game_id)
  - snapshots immutable; exact-duplicate fingerprints suppressed
  - reconciliation records keep the PokerBet ↔ BetConstruct check trail
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.classifications import Classification
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.performance import PERFORMANCE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL DEFAULT 'PokerBet',
    source_game_id   TEXT NOT NULL,
    competition_id   TEXT,
    competition_slug TEXT,
    competition      TEXT NOT NULL,
    region           TEXT,
    game_family      TEXT NOT NULL,
    classification   TEXT NOT NULL,
    sport            TEXT NOT NULL DEFAULT 'basketball',
    home_team        TEXT NOT NULL,
    away_team        TEXT NOT NULL,
    game_slug        TEXT,
    source_url       TEXT,
    status           TEXT NOT NULL DEFAULT 'live',
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    UNIQUE(source, source_game_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id           INTEGER NOT NULL REFERENCES games(id),
    source            TEXT NOT NULL DEFAULT 'PokerBet',
    source_game_id    TEXT NOT NULL,
    classification    TEXT NOT NULL,
    captured_at       TEXT NOT NULL,
    home_team         TEXT,
    away_team         TEXT,
    home_score        INTEGER,
    away_score        INTEGER,
    period_label      TEXT,
    quarter           INTEGER,
    clock             TEXT,
    game_status       TEXT NOT NULL DEFAULT 'live',
    w1_odds           REAL,
    w2_odds           REAL,
    spread_indicator  TEXT,
    total_line        REAL,
    total_over_odds   REAL,
    total_under_odds  REAL,
    spread            REAL,
    spread_home_odds  REAL,
    spread_away_odds  REAL,
    home_total_line   REAL,
    away_total_line   REAL,
    -- Quarter-specific scores (directive 2026-09-22, DATA COLLECTION
    -- ONLY): exactly what the source presented at this instant — never
    -- backfilled, never reconstructed.  NULL = source did not expose it.
    q1_home_score     INTEGER,
    q1_away_score     INTEGER,
    q2_home_score     INTEGER,
    q2_away_score     INTEGER,
    q3_home_score     INTEGER,
    q3_away_score     INTEGER,
    q4_home_score     INTEGER,
    q4_away_score     INTEGER,
    source_url        TEXT,
    markets_json      TEXT NOT NULL DEFAULT '{}',
    raw_json          TEXT NOT NULL DEFAULT '{}',
    UNIQUE(game_id, captured_at)
);

CREATE TABLE IF NOT EXISTS reconciliation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL DEFAULT 'PokerBet',
    source_game_id  TEXT NOT NULL,
    classification  TEXT NOT NULL,
    bc_event_id     TEXT,
    bc_event_name   TEXT,
    bc_competition_id TEXT,
    bc_url          TEXT,
    checked_at      TEXT NOT NULL,
    checks_json     TEXT NOT NULL DEFAULT '{}',
    result          TEXT NOT NULL DEFAULT 'matched',
    UNIQUE(source, source_game_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_class_captured
    ON snapshots(classification, captured_at);
-- The scorecard's stage 5 (record_market_history) reads ONE game's snapshots
-- by source_game_id, ordered by captured_at, once per game over ~11.6k games
-- a pass.  Nothing led with source_game_id, so each of those reads was a full
-- scan plus a temp B-tree sort — 2.05s/game idle, 18.0s under memory pressure
-- — which is why stage 6 was never reached and checkpoint_market went hours
-- stale.  (source_game_id, captured_at) serves the equality AND the ORDER BY
-- from the index.  Index-only: no column, constraint or query semantics.
CREATE INDEX IF NOT EXISTS idx_snapshots_source_ts
    ON snapshots(source_game_id, captured_at);
-- Phase 3 P1 (observation cache): the UNIQUE(game_id, captured_at) constraint
-- provides an autoindex that covers all production queries on this table.
-- Queries by source_game_id use idx_snapshots_source_ts above.
-- No additional index is required — the two existing indexes cover every
-- production query pattern without a TEMP B-TREE.
--
-- NOTE: A previous schema version referenced idx_snapshots_game_created
-- (game_id, created_at) but that was based on a misunderstanding of the
-- blm.db server schema (which has a separate snapshots table with a
-- created_at column).  The collector's clean-metrics snapshots table uses
-- captured_at and is fully covered by the UNIQUE constraint + source index.
-- consumers (scorecard stages 3/4/6, settle_worker) use the autoindex.
-- See tests/test_storage_schema_indexes.py.
CREATE INDEX IF NOT EXISTS idx_games_class
    ON games(classification);
-- API joins resolve games by source identity on every /api/v4/* request
-- (_load_snapshots: snapshots.game_id -> games.id via games.source_game_id);
-- without this index SQLite scans the whole games table per game.
CREATE INDEX IF NOT EXISTS idx_games_source
    ON games(source_game_id);

-- Virtual-replay split audit: positive evidence for EVERY instance split.
-- Each row records the exact observation that triggered it (path + signal)
-- plus the tracked instance's last state vs the observed state, so churn
-- can be forensically reconstructed (one game -> one #iN history rule).
CREATE TABLE IF NOT EXISTS instance_splits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    base_id      TEXT NOT NULL,
    old_id       TEXT NOT NULL,
    new_id       TEXT NOT NULL,
    path         TEXT NOT NULL,          -- list | event | restart
    signal       TEXT NOT NULL,          -- score_drop | clock_regression
    prev_home    INTEGER,
    prev_away    INTEGER,
    prev_period  TEXT,
    prev_clock   TEXT,
    prev_at      TEXT,
    new_home     INTEGER,
    new_away     INTEGER,
    new_period   TEXT,
    new_clock    TEXT,
    new_at       TEXT
);

-- Live market observations captured from the PokerBet eu-swarm WebSocket
-- feed (independent of the event-view DOM).  The bookmaker O/U line for
-- every live game is pushed here with its Over/Under prices; each row is
-- one market observation at one moment — the historical series that
-- market momentum / closing-line / model-vs-market analysis consumes.
-- The event-view snapshot path (snapshots.total_line) remains the OTHER
-- market source; both are observed PokerBet data, never model output.
CREATE TABLE IF NOT EXISTS market_observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id        INTEGER REFERENCES games(id),
    source_game_id TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    market_type    TEXT NOT NULL,        -- MatchTotal | MatchHomeTeamTotal2 | ...
    market_name    TEXT NOT NULL,        -- Total Points | Team 1 Total Points | ...
    line_value     REAL,                 -- the O/U line (base)
    over_price     REAL,
    under_price    REAL,
    home_score     INTEGER,
    away_score     INTEGER,
    period_label   TEXT,
    clock          TEXT,
    raw_json       TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_game_id, market_type, line_value, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_market_obs_game_time
    ON market_observations(source_game_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_market_obs_type
    ON market_observations(source_game_id, market_type, captured_at);

-- ── QUARTER-SPECIFIC COLLECTION (directive 2026-09-22, DATA COLLECTION
-- ONLY) ────────────────────────────────────────────────────────────────
-- Purpose: reliably track QUARTER-SPECIFIC scores and QUARTER-SPECIFIC
-- lines whenever the source provides them, preserving the RAW upstream
-- observation so analysis can reconstruct exactly what was known at each
-- timestamp.  These tables never feed alerts, fingerprints, betting or
-- thresholds — collection and research only.

-- Raw eu-swarm WS frames, retained verbatim (§5 DO NOT DESTROY RAW DATA):
-- if parsing fails or the source changes format, the frame survives here
-- and the failure is recorded — never silently dropped.  Throttled (see
-- collector) to bound growth; parse failures are ALWAYS recorded.
CREATE TABLE IF NOT EXISTS ws_raw_frames (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT NOT NULL,
    ws_url        TEXT,
    byte_size     INTEGER,
    parse_status  TEXT NOT NULL,          -- parsed | parse_failed
    game_count    INTEGER,                -- games carrying a market tree
    raw_json      TEXT NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ws_raw_frames_ts
    ON ws_raw_frames(captured_at);

-- One row per quarter-specific (or other non-game-total) O/U market
-- observation pushed by the feed.  line_value from the market's own base
-- (event base as fallback); raw_json preserves the market tree entry.
CREATE TABLE IF NOT EXISTS quarter_market_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER REFERENCES games(id),
    source_game_id  TEXT NOT NULL,
    classification  TEXT NOT NULL DEFAULT '',
    captured_at     TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    market_name     TEXT,
    market_period   TEXT,                 -- Q1..Q4 | full_game | quarter_unknown
    period_number   INTEGER,              -- 1..4 when determinable
    line_value      REAL,
    over_price      REAL,
    under_price     REAL,
    home_score      INTEGER,
    away_score      INTEGER,
    period_label    TEXT,
    clock           TEXT,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_game_id, market_id, line_value, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_qmo_game_ts
    ON quarter_market_observations(source_game_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_qmo_type
    ON quarter_market_observations(source_game_id, market_type, captured_at);

-- Historical bookmaker markets read from the hydrated PokerBet event DOM.
-- Kept separate from websocket/model data; raw_text is the displayed DOM
-- text and normalized values are nullable when the DOM is ambiguous.
CREATE TABLE IF NOT EXISTS pokerbet_dom_market_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER REFERENCES games(id),
    source_game_id  TEXT NOT NULL,
    event_url       TEXT,
    league          TEXT,
    home_team       TEXT,
    away_team       TEXT,
    game_started_at TEXT,
    observed_at     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'pokerbet_dom',
    period          TEXT NOT NULL,         -- Q1..Q4 | 1H | 2H | other
    market_type     TEXT,
    selection       TEXT,
    line_value      REAL,
    odds            REAL,
    quarter_home_score INTEGER,
    quarter_away_score INTEGER,
    cumulative_home_score INTEGER,
    cumulative_away_score INTEGER,
    market_timestamp TEXT,
    raw_text        TEXT NOT NULL DEFAULT '',
    raw_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pbdom_game_observed
    ON pokerbet_dom_market_observations(source_game_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_pbdom_period
    ON pokerbet_dom_market_observations(period, market_type, observed_at);

-- One row per verified game-state capture carrying quarter scores (from
-- the event-view scoreboard's per-quarter breakdown, or the WS state).
-- All-NULL rows are kept: they are the denominator for coverage metrics.
CREATE TABLE IF NOT EXISTS quarter_score_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         INTEGER REFERENCES games(id),
    source_game_id  TEXT NOT NULL,
    classification  TEXT NOT NULL DEFAULT '',
    captured_at     TEXT NOT NULL,
    period_label    TEXT,
    quarter         INTEGER,
    clock           TEXT,
    q1_home_score   INTEGER,
    q1_away_score   INTEGER,
    q2_home_score   INTEGER,
    q2_away_score   INTEGER,
    q3_home_score   INTEGER,
    q3_away_score   INTEGER,
    q4_home_score   INTEGER,
    q4_away_score   INTEGER,
    home_score      INTEGER,
    away_score      INTEGER,
    source_path     TEXT NOT NULL DEFAULT '',  -- event_view | ws
    raw_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_qso_game_ts
    ON quarter_score_observations(source_game_id, captured_at);

-- Chronological-consistency anomalies (§4): FLAGGED, never silently
-- corrected.  No code path rewrites a historical observation because of
-- an anomaly — the observation stands and the anomaly is recorded.
CREATE TABLE IF NOT EXISTS quarter_validation_anomalies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,
    source_game_id  TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    check_name      TEXT NOT NULL,
    detail          TEXT NOT NULL,
    anomaly_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_qva_game
    ON quarter_validation_anomalies(source_game_id, detected_at);

-- ── BETUAL-ONLY DATASET (directive 2026-09-22, DATA COLLECTION ONLY)
-- ─────────────────────────────────────────────────────────────────────
-- A prospective, Betual-only longitudinal dataset for studying how the
-- market line responds to SCORE and TIME (TIME → SCORE → LINE →
-- LINE MOVEMENT).  The internal game timer is authoritative (Betual has
-- no timeouts); the bookmaker's displayed clock is stored as a
-- comparison field ONLY (clock_difference).  These tables never feed
-- alerts, fingerprints, betting or thresholds.

-- One row per Betual score observation with internal-timer fields and
-- cumulative→quarter derivation performed ONLY from point-in-time-valid
-- inputs (§5); a missing input stores NULL, never a manufactured value.
CREATE TABLE IF NOT EXISTS betual_time_observations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id           TEXT NOT NULL,
    classification           TEXT NOT NULL DEFAULT 'BETUAL_NBA',
    captured_at              TEXT NOT NULL,
    source_start_time        REAL,      -- epoch s (WS start_ts), §2
    internal_game_time       TEXT,      -- MM:SS elapsed, internal model
    internal_elapsed_seconds REAL,      -- monotonic-model elapsed, §3
    quarter                  INTEGER,   -- internal model quarter 1..4
    quarter_remaining_seconds REAL,
    q1_home_score            INTEGER,
    q1_away_score            INTEGER,
    q2_home_score            INTEGER,
    q2_away_score            INTEGER,
    q3_home_score            INTEGER,
    q3_away_score            INTEGER,
    q4_home_score            INTEGER,
    q4_away_score            INTEGER,
    home_score               INTEGER,
    away_score               INTEGER,
    total_score              INTEGER,
    betual_displayed_clock   TEXT,      -- observation/debug ONLY (§2)
    clock_difference         REAL,      -- internal minus displayed remaining
    source_quarter           INTEGER,   -- the source's own view
    period_label             TEXT,
    raw_json                 TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_game_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_bto_game_ts
    ON betual_time_observations(source_game_id, captured_at);

-- One row per Betual O/U line observation (full game + Q1..Q4) with
-- line-movement fields vs the game's previous line observation (§6/§7).
-- A line that changed and returned is RETAINED (§10): dedup collapses
-- only same game+market+line+timestamp.
CREATE TABLE IF NOT EXISTS betual_line_observations (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id            TEXT NOT NULL,
    classification            TEXT NOT NULL DEFAULT 'BETUAL_NBA',
    captured_at               TEXT NOT NULL,
    market_id                 TEXT NOT NULL,
    market_name               TEXT,
    period                    TEXT,      -- full_game | Q1..Q4 | other
    line                      REAL,
    line_previous             REAL,
    line_change               REAL,
    seconds_since_previous_line REAL,
    line_velocity             REAL,      -- line_change / elapsed s
    internal_game_time        TEXT,
    internal_elapsed_seconds  REAL,
    quarter                   INTEGER,   -- internal model quarter
    quarter_remaining_seconds REAL,
    home_score                INTEGER,
    away_score                INTEGER,
    total_score               INTEGER,
    score_at_observation      INTEGER,   -- total at the PREVIOUS obs
    betual_displayed_clock    TEXT,
    over_price                REAL,
    under_price               REAL,
    raw_json                  TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_game_id, market_id, line, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_blo_game_ts
    ON betual_line_observations(source_game_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_blo_market
    ON betual_line_observations(source_game_id, market_id, captured_at);

-- High-quality snapshot at each observed quarter transition (§11) —
-- Q1→Q2, Q2→Q3, Q3→Q4; Q3→Q4 is the BLM-research-critical one.
CREATE TABLE IF NOT EXISTS betual_transitions (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id            TEXT NOT NULL,
    classification            TEXT NOT NULL DEFAULT 'BETUAL_NBA',
    captured_at               TEXT NOT NULL,
    transition                TEXT NOT NULL,  -- 'Q3->Q4' etc.
    prev_quarter              INTEGER,
    new_quarter               INTEGER,
    internal_game_time        TEXT,
    internal_elapsed_seconds  REAL,
    quarter_remaining_seconds REAL,
    betual_displayed_clock    TEXT,
    clock_difference          REAL,
    prev_quarter_home         INTEGER,   -- final score of the ended quarter
    prev_quarter_away         INTEGER,
    home_score                INTEGER,   -- new cumulative
    away_score                INTEGER,
    total_score               INTEGER,
    full_game_line            REAL,
    quarter_line              REAL,
    line_previous             REAL,
    line_change               REAL,
    raw_json                  TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_game_id, transition)
);
CREATE INDEX IF NOT EXISTS idx_bt_game
    ON betual_transitions(source_game_id, captured_at);

-- §12 game-end capture.  Final status comes from SOURCE/finalization
-- evidence ONLY — the internal timer expiring is never recorded as
-- final; end_evidence distinguishes observed_final vs disappeared rows
-- (the NO-FINAL diagnosis data).  §19: collection only, never betting.
CREATE TABLE IF NOT EXISTS betual_game_ends (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id           TEXT NOT NULL,
    classification           TEXT NOT NULL DEFAULT 'BETUAL_NBA',
    captured_at              TEXT NOT NULL,
    internal_game_time       TEXT,
    internal_elapsed_seconds REAL,
    quarter                  INTEGER,
    final_home_score         INTEGER,
    final_away_score         INTEGER,
    final_total              INTEGER,
    q1_home_score            INTEGER,
    q1_away_score            INTEGER,
    q2_home_score            INTEGER,
    q2_away_score            INTEGER,
    q3_home_score            INTEGER,
    q3_away_score            INTEGER,
    q4_home_score            INTEGER,
    q4_away_score            INTEGER,
    full_game_line           REAL,
    betual_displayed_clock   TEXT,
    settlement_state         TEXT,      -- source finalization evidence
    end_evidence             TEXT NOT NULL DEFAULT 'observed_final',
    source_quarter           INTEGER,
    raw_json                 TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_game_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_bge_game
    ON betual_game_ends(source_game_id, captured_at);

-- §14 clock diagnostics: internal timer vs Betual displayed clock —
-- FLAGGED, never used to steer the internal timer.
CREATE TABLE IF NOT EXISTS betual_clock_diagnostics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,
    source_game_id  TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- backward_clock_movement |
                                     -- impossible_elapsed_time |
                                     -- quarter_transition_anomaly |
                                     -- large_clock_discrepancy |
                                     -- duplicate_timestamp |
                                     -- phase_quarter_mismatch
    detail          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bcd_game
    ON betual_clock_diagnostics(source_game_id, detected_at);

-- §9 parse failures: raw retained, parsed value NULL, error recorded.
CREATE TABLE IF NOT EXISTS betual_parse_failures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,
    source_game_id  TEXT NOT NULL DEFAULT '',
    raw_json        TEXT NOT NULL DEFAULT '',
    error           TEXT
);

-- §13 restart anchors: one row per Betual game carrying the persisted
-- wall-clock anchor (from the WS feed's start_ts) + the timer model, so
-- a restart reconstructs the internal clock from authoritative start
-- evidence and NEVER resets the game timer to zero.
CREATE TABLE IF NOT EXISTS betual_game_timers (
    source_game_id      TEXT PRIMARY KEY,
    game_start_wall     REAL,
    observed_at_wall    REAL,
    quarter_seconds     REAL,
    break_seconds       REAL,
    timer_model         TEXT NOT NULL DEFAULT 'default',
    last_home           INTEGER,
    last_away           INTEGER,
    last_line           REAL,
    last_line_at        REAL,
    last_capture_at     TEXT,
    updated_at          TEXT NOT NULL
);

"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PokerBetStore:
    """SQLite store for the PokerBet pipeline (thread-safe)."""

    def __init__(self, db_path: Path | str, read_only: bool = False):
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        if not read_only:
            self._init()

    @property
    def db_path(self) -> Path:
        """Location of the main operational database file."""
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self._db_path}?mode=ro" if False else str(self._db_path)
        # timeout = SQLite busy_timeout (seconds).  The server's scorecard
        # holds per-section write transactions on this same DB for ~30-90s
        # windows; 30s was shorter than a window, so live snapshot/market
        # writes failed with "database is locked" and the collector
        # relaunched healthy browsers.  90s waits out a section window —
        # transient cross-process contention must never drop an
        # observation or kill a live session.
        conn = sqlite3.connect(uri, timeout=90)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                # Quarter-score columns on snapshots (directive 2026-09-22)
                # — backward-compatible ALTERs for DBs created before the
                # directive; fresh DBs get them from _SCHEMA directly.
                cols = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(snapshots)")}
                for c in ("q1_home_score", "q1_away_score",
                          "q2_home_score", "q2_away_score",
                          "q3_home_score", "q3_away_score",
                          "q4_home_score", "q4_away_score"):
                    if c not in cols:
                        conn.execute(
                            f"ALTER TABLE snapshots ADD COLUMN {c} INTEGER")
                conn.commit()
            finally:
                conn.close()

    # ── Games ────────────────────────────────────────────────────

    def upsert_game(self, game: PokerBetGame) -> int:
        """Insert or update a game; returns its row id."""
        now = _utcnow()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("""
                    INSERT INTO games (
                        source, source_game_id, competition_id, competition_slug,
                        competition, region, game_family, classification, sport,
                        home_team, away_team, game_slug, source_url, status,
                        first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, source_game_id) DO UPDATE SET
                        home_team   = excluded.home_team,
                        away_team   = excluded.away_team,
                        competition = excluded.competition,
                        region      = excluded.region,
                        game_family = excluded.game_family,
                        classification = excluded.classification,
                        source_url  = excluded.source_url,
                        status      = excluded.status,
                        last_seen_at = excluded.last_seen_at
                """, (
                    game.source, game.source_game_id, game.competition_id,
                    game.competition_slug, game.competition, game.region,
                    game.game_family, game.classification, game.sport,
                    game.home_team, game.away_team, game.game_slug,
                    game.source_url, game.status,
                    # first_seen: the RECORD's own discovery time when the
                    # caller provides one (the reconciler's start-time
                    # cross-check reads it); 'now' only as the fallback.
                    # Existing rows never touch it (see ON CONFLICT).
                    game.first_seen_at or now, now,
                ))
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM games WHERE source=? AND source_game_id=?",
                    (game.source, game.source_game_id),
                ).fetchone()
                return int(row["id"])
            finally:
                conn.close()

    def get_game(self, source_game_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM games WHERE source_game_id = ?",
                    (source_game_id,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_recorded_final(self, source_game_id: str) -> Optional[dict]:
        """Read-only lookup of a recorded final result (game_results), if any.

        game_results is created/written by the scorecard (server-side), so
        the table may not exist on a fresh DB — a missing table reads as
        "no recorded final".  Used by the collector's replay guard to prove
        a fixture has already finished before classifying an earlier
        observed frame as a replay.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT final_home, final_away, final_total, "
                    "final_result_status, result_at FROM game_results "
                    "WHERE source_game_id = ?",
                    (source_game_id,),
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.OperationalError:
                # no game_results table yet (scorecard has never run)
                return None
            finally:
                conn.close()

    def insert_instance_split(self, **fields: Any) -> int:
        """Persist one virtual-replay split audit row (see instance_splits)."""
        cols = [c for c in (
            "created_at", "base_id", "old_id", "new_id", "path", "signal",
            "prev_home", "prev_away", "prev_period", "prev_clock", "prev_at",
            "new_home", "new_away", "new_period", "new_clock", "new_at",
        ) if fields.get(c) is not None]
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"INSERT INTO instance_splits ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    [fields[c] for c in cols],
                )
                conn.commit()
                return cur.lastrowid or 0
            finally:
                conn.close()

    def list_instance_ids(self, base_id: str) -> list[str]:
        """All virtual-instance ids (base#iN) recorded for a fixture."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT source_game_id FROM games WHERE source_game_id LIKE ?",
                    (f"{base_id}#i%",),
                ).fetchall()
                return [r["source_game_id"] for r in rows]
            finally:
                conn.close()

    def list_games(
        self, classification: Optional[str] = None, limit: int = 200,
    ) -> list[dict]:
        q = "SELECT * FROM games"
        params: tuple = ()
        if classification:
            q += " WHERE classification=?"
            params = (classification,)
        q += " ORDER BY last_seen_at DESC LIMIT ?"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(q, params + (limit,)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ── Snapshots ────────────────────────────────────────────────

    def insert_snapshot(
        self, game_id: int, obs: MarketObservation, *, force: bool = False,
    ) -> Optional[int]:
        with PERFORMANCE.measure("sql.main.insert_snapshot") as timing:
            timing.add("sql_calls", 1 if force else 2)
            row_id = self._insert_snapshot_impl(game_id, obs, force=force)
            timing.add("rows_inserted", 1 if row_id is not None else 0)
            return row_id

    def _insert_snapshot_impl(
        self, game_id: int, obs: MarketObservation, *, force: bool = False,
    ) -> Optional[int]:
        """Insert one observation.  Returns new row id or None if a
        duplicate (same game, same captured_at) already exists."""
        with self._lock:
            conn = self._connect()
            try:
                if not force:
                    dup = conn.execute(
                        "SELECT id FROM snapshots WHERE game_id=? AND captured_at=?",
                        (game_id, obs.captured_at),
                    ).fetchone()
                    if dup:
                        return None
                cur = conn.execute("""
                    INSERT INTO snapshots (
                        game_id, source, source_game_id, classification,
                        captured_at, home_team, away_team,
                        home_score, away_score, period_label, quarter, clock,
                        game_status, w1_odds, w2_odds, spread_indicator,
                        total_line, total_over_odds, total_under_odds,
                        spread, spread_home_odds, spread_away_odds,
                        home_total_line, away_total_line,
                        q1_home_score, q1_away_score,
                        q2_home_score, q2_away_score,
                        q3_home_score, q3_away_score,
                        q4_home_score, q4_away_score,
                        source_url, markets_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?)
                """, (
                    game_id, obs.source, obs.source_game_id, obs.classification,
                    obs.captured_at, obs.home_team, obs.away_team,
                    obs.home_score, obs.away_score, obs.period_label,
                    obs.quarter, obs.clock, obs.game_status,
                    obs.w1_odds, obs.w2_odds, obs.spread_indicator,
                    obs.total_line, obs.total_over_odds, obs.total_under_odds,
                    obs.spread, obs.spread_home_odds, obs.spread_away_odds,
                    obs.home_total_line, obs.away_total_line,
                    obs.q1_home_score, obs.q1_away_score,
                    obs.q2_home_score, obs.q2_away_score,
                    obs.q3_home_score, obs.q3_away_score,
                    obs.q4_home_score, obs.q4_away_score,
                    obs.source_url, obs.markets_json, obs.raw_json,
                ))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def get_snapshots(
        self, source_game_id: str, limit: int = 500, ascending: bool = False,
    ) -> list[dict]:
        order = "ASC" if ascending else "DESC"
        with PERFORMANCE.measure("sql.main.get_snapshots") as timing:
          with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(f"""
                    SELECT s.* FROM snapshots s
                    JOIN games g ON g.id = s.game_id
                    WHERE g.source_game_id = ?
                    ORDER BY s.captured_at {order} LIMIT ?
                """, (source_game_id, limit)).fetchall()
                timing.add("rows_returned", len(rows))
                timing.add("sql_calls")
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def count_snapshots(self, classification: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) AS c FROM snapshots"
        params: tuple = ()
        if classification:
            q += " WHERE classification=?"
            params = (classification,)
        label = "sql.main.count_snapshots_filtered" if classification else "sql.main.count_snapshots"
        with PERFORMANCE.measure(label) as timing:
            with self._lock:
                conn = self._connect()
                try:
                    result = int(conn.execute(q, params).fetchone()["c"])
                    timing.add("sql_calls")
                    return result
                finally:
                    conn.close()

    # ── Reconciliation ───────────────────────────────────────────

    def record_reconciliation(
        self,
        source_game_id: str,
        classification: str,
        bc_event_id: str,
        bc_event_name: str,
        bc_competition_id: Optional[str],
        bc_url: str,
        checks: dict[str, Any],
        result: str,
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO reconciliation (
                        source, source_game_id, classification, bc_event_id,
                        bc_event_name, bc_competition_id, bc_url, checked_at,
                        checks_json, result)
                    VALUES ('PokerBet', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, source_game_id) DO UPDATE SET
                        classification = excluded.classification,
                        bc_event_id = excluded.bc_event_id,
                        bc_event_name = excluded.bc_event_name,
                        bc_competition_id = excluded.bc_competition_id,
                        bc_url = excluded.bc_url,
                        checked_at = excluded.checked_at,
                        checks_json = excluded.checks_json,
                        result = excluded.result
                """, (
                    source_game_id, classification, bc_event_id, bc_event_name,
                    bc_competition_id, bc_url, _utcnow(),
                    json.dumps(checks, default=str), result,
                ))
                conn.commit()
            finally:
                conn.close()

    def list_reconciliation(self, limit: int = 200) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM reconciliation ORDER BY checked_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ── Market observations (eu-swarm WS feed) ──────────────────

    def upsert_market_observation(self, obs: dict) -> None:
        """Persist one WS market observation (deduped on game+type+line+ts)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO market_observations (
                        game_id, source_game_id, captured_at, market_type,
                        market_name, line_value, over_price, under_price,
                        home_score, away_score, period_label, clock, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_game_id, market_type, line_value, captured_at)
                    DO NOTHING
                """, (
                    obs.get("game_id"), obs["source_game_id"], obs["captured_at"],
                    obs["market_type"], obs["market_name"], obs.get("line_value"),
                    obs.get("over_price"), obs.get("under_price"),
                    obs.get("home_score"), obs.get("away_score"),
                    obs.get("period_label"), obs.get("clock"),
                    json.dumps(obs.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    # ── Quarter-specific collection (directive 2026-09-22) ──────

    def insert_ws_raw_frame(
        self, captured_at: str, ws_url: Optional[str], byte_size: int,
        parse_status: str, game_count: Optional[int], raw_json: str,
        error: Optional[str] = None,
    ) -> None:
        """Retain one raw WS frame verbatim (§5: never destroy raw data).
        parse_status='parse_failed' rows carry the parser error."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO ws_raw_frames (
                        captured_at, ws_url, byte_size, parse_status,
                        game_count, raw_json, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (captured_at, ws_url, byte_size, parse_status,
                      game_count, raw_json, error))
                conn.commit()
            finally:
                conn.close()

    def insert_quarter_market_observation(self, obs: dict) -> None:
        """One non-game-total O/U market observation (e.g. a quarter
        total).  Raw market tree entry preserved; a NULL line is stored
        as NULL — never fabricated.  Same game+market+line+timestamp is
        a duplicate by definition and is ignored."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO quarter_market_observations (
                        game_id, source_game_id, classification, captured_at,
                        market_id, market_type, market_name, market_period,
                        period_number, line_value, over_price, under_price,
                        home_score, away_score, period_label, clock, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_game_id, market_id, line_value,
                                captured_at) DO NOTHING
                """, (
                    obs.get("game_id"), obs["source_game_id"],
                    obs.get("classification", ""), obs["captured_at"],
                    obs["market_id"], obs["market_type"],
                    obs.get("market_name"), obs.get("market_period"),
                    obs.get("period_number"), obs.get("line_value"),
                    obs.get("over_price"), obs.get("under_price"),
                    obs.get("home_score"), obs.get("away_score"),
                    obs.get("period_label"), obs.get("clock"),
                    json.dumps(obs.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    def insert_pokerbet_dom_market_observation(self, obs: dict) -> None:
        """Append one verbatim observation from a hydrated PokerBet DOM.

        Values are deliberately not upserted: repeated captures are distinct
        observations, and a missing line stays SQL NULL.  The internal game
        primary key and canonical source_game_id must name the same row; a
        valid foreign key alone is not enough to prevent cross-game linkage.
        """
        source_game_id = str(obs.get("source_game_id") or "")
        game_id = obs.get("game_id")
        if not source_game_id or game_id is None:
            raise ValueError("DOM market observation requires game_id and source_game_id")
        with self._lock:
            conn = self._connect()
            try:
                game = conn.execute(
                    "SELECT source_game_id FROM games WHERE id=? AND source='PokerBet'",
                    (game_id,),
                ).fetchone()
                if game is None or str(game["source_game_id"]) != source_game_id:
                    raise ValueError(
                        "DOM market observation game_id/source_game_id mismatch")
                conn.execute("""
                    INSERT INTO pokerbet_dom_market_observations (
                        game_id, source_game_id, event_url, league, home_team,
                        away_team, game_started_at, observed_at, source, period,
                        market_type, selection, line_value, odds,
                        quarter_home_score, quarter_away_score,
                        cumulative_home_score, cumulative_away_score,
                        market_timestamp, raw_text, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?)
                """, (
                    game_id, source_game_id,
                    obs.get("event_url"), obs.get("league"),
                    obs.get("home_team"), obs.get("away_team"),
                    obs.get("game_started_at"), obs["observed_at"],
                    obs.get("source", "pokerbet_dom"), obs["period"],
                    obs.get("market_type"), obs.get("selection"),
                    obs.get("line_value"), obs.get("odds"),
                    obs.get("quarter_home_score"),
                    obs.get("quarter_away_score"),
                    obs.get("cumulative_home_score"),
                    obs.get("cumulative_away_score"),
                    obs.get("market_timestamp"), obs.get("raw_text", ""),
                    json.dumps(obs.get("raw", {}), ensure_ascii=False,
                               default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    def insert_quarter_score_observation(self, obs: dict) -> None:
        """One quarter-score observation (verified captures only).
        All-quarter-NULL rows are the no-coverage denominator and are
        still recorded — coverage metrics must not hide them."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO quarter_score_observations (
                        game_id, source_game_id, classification, captured_at,
                        period_label, quarter, clock,
                        q1_home_score, q1_away_score,
                        q2_home_score, q2_away_score,
                        q3_home_score, q3_away_score,
                        q4_home_score, q4_away_score,
                        home_score, away_score, source_path, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?)
                """, (
                    obs.get("game_id"), obs["source_game_id"],
                    obs.get("classification", ""), obs["captured_at"],
                    obs.get("period_label"), obs.get("quarter"),
                    obs.get("clock"),
                    obs.get("q1_home_score"), obs.get("q1_away_score"),
                    obs.get("q2_home_score"), obs.get("q2_away_score"),
                    obs.get("q3_home_score"), obs.get("q3_away_score"),
                    obs.get("q4_home_score"), obs.get("q4_away_score"),
                    obs.get("home_score"), obs.get("away_score"),
                    obs.get("source_path", ""),
                    json.dumps(obs.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    def last_quarter_score_observation(
        self, source_game_id: str,
    ) -> Optional[dict]:
        """The game's most recent quarter-score observation — the ``prev``
        for chronological validation (a restart simply passes None)."""
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute("""
                    SELECT * FROM quarter_score_observations
                    WHERE source_game_id=?
                    ORDER BY captured_at DESC, id DESC LIMIT 1
                """, (source_game_id,)).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def record_quarter_anomaly(
        self, source_game_id: str, captured_at: str, check_name: str,
        detail: str, anomaly: Optional[dict] = None,
    ) -> None:
        """Flag a chronological-consistency anomaly (§4: FLAG, never
        silently correct — the original observation stands)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO quarter_validation_anomalies (
                        detected_at, source_game_id, captured_at, check_name,
                        detail, anomaly_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (_utcnow(), source_game_id, captured_at, check_name,
                      detail, json.dumps(anomaly or {}, default=str)))
                conn.commit()
            finally:
                conn.close()

    def quarter_collection_metrics(self) -> dict:
        """Collection observability (§9) — one read-only aggregate.
        Coverage = observations where the source actually exposed the
        field; missing data is never hidden."""
        with self._lock:
            conn = self._connect()
            try:
                query_number = 0
                query_labels = (
                    "quarter_score_aggregate", "quarter_market_aggregate",
                    "raw_frame_aggregate", "quarter_anomaly_count",
                )
                def one(sql):
                    nonlocal query_number
                    query_number += 1
                    with PERFORMANCE.measure(
                            f"sql.storage.quarter_metrics.{query_labels[query_number - 1]}") as timing:
                        r = conn.execute(sql).fetchone()
                        timing.add("sql_calls")
                    return dict(r) if r else {}

                qso = one("""
                    SELECT COUNT(*) AS n,
                           SUM(q1_home_score IS NOT NULL) AS q1,
                           SUM(q2_home_score IS NOT NULL) AS q2,
                           SUM(q3_home_score IS NOT NULL) AS q3,
                           SUM(q4_home_score IS NOT NULL) AS q4
                    FROM quarter_score_observations""")
                qmo = one("""
                    SELECT COUNT(*) AS n,
                           SUM(market_period IN ('Q1','Q2','Q3','Q4'))
                               AS q_lines
                    FROM quarter_market_observations""")
                wf = one("""
                    SELECT COUNT(*) AS n,
                           SUM(parse_status='parse_failed') AS parse_failures
                    FROM ws_raw_frames""")
                an = one(
                    "SELECT COUNT(*) AS n FROM quarter_validation_anomalies")
                n = qso.get("n") or 0

                def pct(x):
                    return round((x or 0) / n * 100, 2) if n else 0.0

                return {
                    "quarter_score_observations": n,
                    "q1_coverage_pct": pct(qso.get("q1")),
                    "q2_coverage_pct": pct(qso.get("q2")),
                    "q3_coverage_pct": pct(qso.get("q3")),
                    "q4_coverage_pct": pct(qso.get("q4")),
                    "quarter_market_observations": qmo.get("n") or 0,
                    "quarter_line_observations": qmo.get("q_lines") or 0,
                    "ws_raw_frames": wf.get("n") or 0,
                    "ws_parse_failures": wf.get("parse_failures") or 0,
                    "quarter_validation_anomalies": an.get("n") or 0,
                }
            finally:
                conn.close()

    # ── Betual-only dataset (directive 2026-09-22) ─────────────

    def insert_betual_time_observation(self, obs: dict) -> None:
        """One Betual score observation.  Same game+timestamp is a
        duplicate by definition and is ignored (§10: only exact logical
        duplicates are collapsed — genuine later observations land)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_time_observations (
                        source_game_id, classification, captured_at,
                        source_start_time, internal_game_time,
                        internal_elapsed_seconds, quarter,
                        quarter_remaining_seconds,
                        q1_home_score, q1_away_score,
                        q2_home_score, q2_away_score,
                        q3_home_score, q3_away_score,
                        q4_home_score, q4_away_score,
                        home_score, away_score, total_score,
                        betual_displayed_clock, clock_difference,
                        source_quarter, period_label, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_game_id, captured_at) DO NOTHING
                """, (
                    obs["source_game_id"],
                    obs.get("classification", "BETUAL_NBA"),
                    obs["captured_at"], obs.get("source_start_time"),
                    obs.get("internal_game_time"),
                    obs.get("internal_elapsed_seconds"),
                    obs.get("quarter"),
                    obs.get("quarter_remaining_seconds"),
                    obs.get("q1_home_score"), obs.get("q1_away_score"),
                    obs.get("q2_home_score"), obs.get("q2_away_score"),
                    obs.get("q3_home_score"), obs.get("q3_away_score"),
                    obs.get("q4_home_score"), obs.get("q4_away_score"),
                    obs.get("home_score"), obs.get("away_score"),
                    obs.get("total_score"),
                    obs.get("betual_displayed_clock"),
                    obs.get("clock_difference"),
                    obs.get("source_quarter"), obs.get("period_label"),
                    json.dumps(obs.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    def insert_betual_line_observation(self, obs: dict) -> None:
        """One Betual line observation with movement fields.  Dedup
        collapses only same game+market+line+timestamp; a line that
        changed and returned at distinct timestamps is RETAINED (§10)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_line_observations (
                        source_game_id, classification, captured_at,
                        market_id, market_name, period, line,
                        line_previous, line_change,
                        seconds_since_previous_line, line_velocity,
                        internal_game_time, internal_elapsed_seconds,
                        quarter, quarter_remaining_seconds,
                        home_score, away_score, total_score,
                        score_at_observation, betual_displayed_clock,
                        over_price, under_price, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_game_id, market_id, line,
                                captured_at) DO NOTHING
                """, (
                    obs["source_game_id"],
                    obs.get("classification", "BETUAL_NBA"),
                    obs["captured_at"], obs["market_id"],
                    obs.get("market_name"), obs.get("period"),
                    obs.get("line"), obs.get("line_previous"), obs.get("line_change"),
                    obs.get("seconds_since_previous_line"), obs.get("line_velocity"),
                    obs.get("internal_game_time"), obs.get("internal_elapsed_seconds"),
                    obs.get("quarter"),
                    obs.get("quarter_remaining_seconds"),
                    obs.get("home_score"), obs.get("away_score"), obs.get("total_score"),
                    obs.get("score_at_observation"), obs.get("betual_displayed_clock"),
                    obs.get("over_price"), obs.get("under_price"),
                    json.dumps(obs.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    # Phase 5a: incremental betual_line distinct-game counter.
    # Replaces the full-table COUNT DISTINCT that dominated tick latency
    # (24.9s/tick p50 in Phase 3 P2).  A singleton counter table + trigger
    # maintains the count incrementally as observations are inserted.

    def _ensure_betual_line_counter(self) -> None:
        """Create the incremental distinct-game counter if it doesn't exist.

        Design: a single-row counter table tracks distinct source_game_id
        count.  On first creation we compute the initial count via COUNT
        DISTINCT (one-time cost).  Thereafter each new observation
        increment: we only increment when the NEW.source_game_id has not
        appeared in any earlier row (rowid < NEW.rowid check).  This is
        O(1) per insert after warmup.

        Concurrent inserts under self._lock keep the counter consistent.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS betual_line_distinct_games (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        distinct_count INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO betual_line_distinct_games (id, distinct_count)
                    VALUES (1, 0)
                """)
                # On first setup: compute initial count once.
                # Seed the counter ONCE: only when the row doesn't exist yet.
                # Subsequent calls skip the COUNT DISTINCT and rely on the
                # incremental trigger (betual_line_distinct_update) to keep
                # the counter accurate.
                existing = conn.execute(
                    "SELECT 1 FROM betual_line_distinct_games WHERE id = 1"
                ).fetchone()
                if not existing:
                    conn.execute("""
                        UPDATE betual_line_distinct_games
                        SET distinct_count = (
                            SELECT COUNT(DISTINCT source_game_id)
                            FROM betual_line_observations
                        )
                        WHERE id = 1
                    """)
                # Incremental trigger: for each new row, increment only if
                # the source_game_id has not appeared in any earlier row.
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS
                        betual_line_distinct_update
                    AFTER INSERT ON betual_line_observations
                    FOR EACH ROW
                    WHEN NEW.source_game_id IS NOT NULL
                    BEGIN
                        UPDATE betual_line_distinct_games
                        SET distinct_count = distinct_count + 1
                        WHERE id = 1
                          AND NOT EXISTS (
                            SELECT 1 FROM betual_line_observations
                            WHERE source_game_id = NEW.source_game_id
                              AND rowid < NEW.rowid
                        );
                    END
                """)
                conn.commit()
            finally:
                conn.close()

    def betual_line_distinct_count(self) -> int:
        """Return the incremental distinct-game count for betual_line_observations."""
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT distinct_count FROM betual_line_distinct_games WHERE id = 1"
                ).fetchone()
                return r[0] if r else 0
            finally:
                conn.close()

    def insert_betual_transition(self, row: dict) -> None:
        """One quarter-transition snapshot (§11); same game+transition is
        a duplicate (re-observed boundary) and is ignored."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_transitions (
                        source_game_id, classification, captured_at,
                        transition, prev_quarter, new_quarter,
                        internal_game_time, internal_elapsed_seconds,
                        quarter_remaining_seconds, betual_displayed_clock,
                        clock_difference, prev_quarter_home,
                        prev_quarter_away, home_score, away_score,
                        total_score, full_game_line, quarter_line,
                        line_previous, line_change, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?)  -- 21 cols / 21 params
                    ON CONFLICT(source_game_id, transition) DO NOTHING
                """, (
                    row["source_game_id"],
                    row.get("classification", "BETUAL_NBA"),
                    row["captured_at"], row["transition"],
                    row.get("prev_quarter"), row.get("new_quarter"),
                    row.get("internal_game_time"),
                    row.get("internal_elapsed_seconds"),
                    row.get("quarter_remaining_seconds"),
                    row.get("betual_displayed_clock"),
                    row.get("clock_difference"),
                    row.get("prev_quarter_home"),
                    row.get("prev_quarter_away"),
                    row.get("home_score"), row.get("away_score"),
                    row.get("total_score"),
                    row.get("full_game_line"),
                    row.get("quarter_line"),
                    row.get("line_previous"),
                    row.get("line_change"),
                    json.dumps(row.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    def insert_betual_game_end(self, row: dict) -> None:
        """One §12 game-end capture (final status from source evidence
        only — never from internal-timer expiry)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_game_ends (
                        source_game_id, classification, captured_at,
                        internal_game_time, internal_elapsed_seconds,
                        quarter, final_home_score, final_away_score,
                        final_total, q1_home_score, q1_away_score,
                        q2_home_score, q2_away_score, q3_home_score,
                        q3_away_score, q4_home_score, q4_away_score,
                        full_game_line, betual_displayed_clock,
                        settlement_state, end_evidence, source_quarter,
                        raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_game_id, captured_at) DO NOTHING
                """, (
                    row["source_game_id"],
                    row.get("classification", "BETUAL_NBA"),
                    row["captured_at"], row.get("internal_game_time"),
                    row.get("internal_elapsed_seconds"),
                    row.get("quarter"),
                    row.get("final_home_score"),
                    row.get("final_away_score"),
                    row.get("final_total"),
                    row.get("q1_home_score"), row.get("q1_away_score"),
                    row.get("q2_home_score"), row.get("q2_away_score"),
                    row.get("q3_home_score"), row.get("q3_away_score"),
                    row.get("q4_home_score"), row.get("q4_away_score"),
                    row.get("full_game_line"),
                    row.get("betual_displayed_clock"),
                    row.get("settlement_state"),
                    row.get("end_evidence", "observed_final"),
                    row.get("source_quarter"),
                    json.dumps(row.get("raw", {}), default=str),
                ))
                conn.commit()
            finally:
                conn.close()

    def record_betual_clock_diagnostic(
        self, source_game_id: str, captured_at: str, kind: str,
        detail: str,
    ) -> None:
        """Flag one §14 clock diagnostic (FLAG ONLY — the internal timer
        is never steered by diagnostics)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_clock_diagnostics (
                        detected_at, source_game_id, captured_at, kind,
                        detail)
                    VALUES (?, ?, ?, ?, ?)
                """, (_utcnow(), source_game_id, captured_at, kind,
                      detail))
                conn.commit()
            finally:
                conn.close()

    def record_betual_parse_failure(
        self, source_game_id: str, raw_json: str, error: str,
    ) -> None:
        """§9: raw retained, parsed value NULL, error recorded."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_parse_failures (
                        detected_at, source_game_id, raw_json, error)
                    VALUES (?, ?, ?, ?)
                """, (_utcnow(), source_game_id or "", raw_json or "",
                      error))
                conn.commit()
            finally:
                conn.close()

    def upsert_betual_timer(self, source_game_id: str,
                            game_start_wall: Optional[float],
                            observed_at_wall: Optional[float],
                            quarter_seconds: Optional[float],
                            break_seconds: Optional[float],
                            timer_model: str,
                            last_home: Optional[int],
                            last_away: Optional[int],
                            last_line: Optional[float],
                            last_line_at: Optional[float],
                            last_capture_at: str) -> None:
        """Persist the §13 restart anchor + caches for one Betual game."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT INTO betual_game_timers (
                        source_game_id, game_start_wall, observed_at_wall,
                        quarter_seconds, break_seconds, timer_model,
                        last_home, last_away, last_line, last_line_at,
                        last_capture_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_game_id) DO UPDATE SET
                        game_start_wall = COALESCE(excluded.game_start_wall,
                                                   game_start_wall),
                        observed_at_wall = COALESCE(
                            excluded.observed_at_wall, observed_at_wall),
                        quarter_seconds = COALESCE(
                            excluded.quarter_seconds, quarter_seconds),
                        break_seconds = COALESCE(excluded.break_seconds,
                                                 break_seconds),
                        timer_model = excluded.timer_model,
                        last_home = COALESCE(excluded.last_home, last_home),
                        last_away = COALESCE(excluded.last_away, last_away),
                        last_line = COALESCE(excluded.last_line, last_line),
                        last_line_at = COALESCE(excluded.last_line_at,
                                                last_line_at),
                        last_capture_at = excluded.last_capture_at,
                        updated_at = excluded.updated_at
                """, (source_game_id, game_start_wall, observed_at_wall,
                      quarter_seconds, break_seconds, timer_model,
                      last_home, last_away, last_line, last_line_at,
                      last_capture_at, _utcnow()))
                conn.commit()
            finally:
                conn.close()

    def get_betual_timer(self, source_game_id: str) -> Optional[dict]:
        """The persisted §13 anchor row for one game, if any."""
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT * FROM betual_game_timers WHERE source_game_id=?",
                    (source_game_id,)).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def list_betual_timers(self) -> list[dict]:
        """All persisted §13 anchor rows (restart recovery source)."""
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM betual_game_timers")]
            finally:
                conn.close()

    def betual_collection_metrics(self) -> dict:
        """§18 collection statistics for the Betual-only dataset.
        Coverage is per GAME (a game has Qk data when any of its
        observations exposed that quarter); missing data is never hidden.
        Phase 5a: betual_line_distinct_count is served from the incremental
        counter instead of a full-table COUNT DISTINCT.
        """
        with self._lock:
            conn = self._connect()
            try:
                query_number = 0
                query_labels = (
                    "betual_games_count", "betual_time_count_distinct",
                    "betual_transitions_count",
                    "betual_game_ends_count", "betual_parse_failures_count",
                    "betual_clock_diagnostics_count",
                )
                def one(sql):
                    nonlocal query_number
                    query_number += 1
                    with PERFORMANCE.measure(
                            f"sql.storage.{query_labels[query_number - 1]}") as timing:
                        r = conn.execute(sql).fetchone()
                        timing.add("sql_calls")
                    return dict(r) if r else {}

                # Phase 5a: incremental counter — O(1) instead of a full-table
                # COUNT DISTINCT.  Read it INLINE on the connection this
                # method already holds.  Do NOT call
                # self.betual_line_distinct_count() here: threading.Lock is
                # not reentrant, so the nested acquire self-deadlocks the
                # fast path (observed 2026-09-27: main thread parked in
                # futex_wait inside betual_line_distinct_count → no
                # completed fast cycle → watchdog SIGABRT restart loop).
                try:
                    blo_r = conn.execute(
                        "SELECT distinct_count FROM betual_line_distinct_games "
                        "WHERE id = 1"
                    ).fetchone()
                    blo = blo_r[0] if blo_r else 0
                except sqlite3.OperationalError:
                    # Counter table not initialized (tests, pre-migration
                    # stores): fall back to the committed-HEAD full-table
                    # COUNT DISTINCT so metrics never fail.
                    blo_r = conn.execute(
                        "SELECT COUNT(DISTINCT source_game_id) AS n "
                        "FROM betual_line_observations"
                    ).fetchone()
                    blo = blo_r["n"] if blo_r else 0
                games = one("""
                    SELECT COUNT(*) AS n FROM games
                    WHERE classification='BETUAL_NBA'""")
                bto = one("""
                    SELECT COUNT(DISTINCT source_game_id) AS n,
                           COUNT(*) AS obs,
                           SUM(q1_home_score IS NOT NULL) AS q1,
                           SUM(q2_home_score IS NOT NULL) AS q2,
                           SUM(q3_home_score IS NOT NULL) AS q3,
                           SUM(q4_home_score IS NOT NULL) AS q4,
                           SUM(clock_difference IS NOT NULL) AS diffs,
                           SUM(ABS(COALESCE(clock_difference,0)) >= 90)
                               AS large_diffs,
                           SUM(source_start_time IS NOT NULL) AS anchored
                    FROM betual_time_observations""")
                tr = one("""
                    SELECT COUNT(*) AS n,
                           SUM(transition='Q3->Q4') AS q34
                    FROM betual_transitions""")
                ge = one("""
                    SELECT COUNT(*) AS n,
                           SUM(end_evidence='observed_final') AS ok_final,
                           SUM(end_evidence='disappeared') AS disappeared
                    FROM betual_game_ends""")
                pf = one(
                    "SELECT COUNT(*) AS n FROM betual_parse_failures")
                cd = one(
                    "SELECT COUNT(*) AS n FROM betual_clock_diagnostics")
                g = games.get("n") or 0
                return {
                    "betual_games": g,
                    "games_with_score_data": bto.get("n") or 0,
                    "games_with_q1": bto.get("q1") or 0,
                    "games_with_q2": bto.get("q2") or 0,
                    "games_with_q3": bto.get("q3") or 0,
                    "games_with_q4": bto.get("q4") or 0,
                    "score_observations": bto.get("obs") or 0,
                    "games_anchored_to_start": bto.get("anchored") or 0,
                    "games_with_line_data": blo or 0,
                    "line_observations": blo or 0,
                    "line_observations_with_line": 0,
                    "line_movements": 0,
                    "games_with_full_game_lines": 0,
                    "games_with_quarter_lines": 0,
                    "transitions": tr.get("n") or 0,
                    "q3_q4_transitions": tr.get("q34") or 0,
                    "game_ends": ge.get("n") or 0,
                    "finalization_success": ge.get("ok_final") or 0,
                    "no_final_disappeared": ge.get("disappeared") or 0,
                    "parse_failures": pf.get("n") or 0,
                    "clock_diagnostics": cd.get("n") or 0,
                    "clock_diagnostics_large_diff": bto.get(
                        "large_diffs") or 0,
                }
            finally:
                conn.close()

    def latest_market_observation(
        self, source_game_id: str, market_type: str = "MatchTotal",
    ) -> Optional[dict]:
        """Most recent WS market observation for a game (line + prices).

        The book offers a RANGE of O/U lines per game (204.5/206.5/208.5
        at one timestamp); the event-view parser takes the FIRST (lowest)
        line — parity here: lowest line of the latest observation batch.
        """
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute("""
                    SELECT * FROM market_observations
                    WHERE source_game_id=? AND market_type=?
                      AND captured_at = (
                          SELECT MAX(captured_at) FROM market_observations
                          WHERE source_game_id=? AND market_type=?)
                    ORDER BY line_value ASC LIMIT 1
                """, (source_game_id, market_type, source_game_id, market_type)).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def latest_market_batch(
        self, source_game_id: str, market_type: str = "MatchTotal",
    ) -> list[dict]:
        """ALL rows of the most recent WS market observation batch (every
        distinct line at the latest captured_at, ascending by line).

        The book offers a RANGE of O/U lines per game at one capture
        (204.5/206.5/208.5) — the clean-metrics writer preserves every
        line identity instead of collapsing to one value.
        """
        with PERFORMANCE.measure("sql.main.latest_market_batch") as timing:
          with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("""
                    SELECT * FROM market_observations
                    WHERE source_game_id=? AND market_type=?
                      AND captured_at = (
                          SELECT MAX(captured_at) FROM market_observations
                          WHERE source_game_id=? AND market_type=?)
                    ORDER BY line_value ASC
                """, (source_game_id, market_type, source_game_id, market_type)).fetchall()
                timing.add("sql_calls")
                timing.add("rows_returned", len(rows))
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def game_has_market_line(self, source_game_id: str) -> bool:
        """True when a verified MatchTotal market line exists for the game
        (a market_observations MatchTotal row OR a snapshot carrying
        total_line).  Used by the collector's early-checkpoint priority to
        detect games that have never had a line captured."""
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute(
                    "SELECT 1 FROM market_observations "
                    "WHERE source_game_id=? AND market_type='MatchTotal' "
                    "AND line_value IS NOT NULL LIMIT 1",
                    (source_game_id,),
                ).fetchone()
                if r:
                    return True
                r = conn.execute(
                    "SELECT 1 FROM snapshots WHERE source_game_id=? "
                    "AND total_line IS NOT NULL LIMIT 1",
                    (source_game_id,),
                ).fetchone()
                return r is not None
            finally:
                conn.close()

    def market_observations_before(
        self, source_game_id: str, at_ts: str, market_type: str = "MatchTotal",
    ) -> Optional[dict]:
        """Most recent WS market observation at-or-before a timestamp —
        used to FREEZE the market total into predictions (never a later
        line).  Lowest line of the latest batch <= cutoff (event-view parity).
        """
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute("""
                    SELECT * FROM market_observations
                    WHERE source_game_id=? AND market_type=?
                      AND captured_at = (
                          SELECT MAX(captured_at) FROM market_observations
                          WHERE source_game_id=? AND market_type=?
                            AND captured_at <= ?)
                    ORDER BY line_value ASC LIMIT 1
                """, (source_game_id, market_type, source_game_id, market_type, at_ts)).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    # ── Stats ────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                out: dict[str, Any] = {"games": {}, "snapshots": {}}
                def count(label: str, sql: str, params: tuple = ()) -> int:
                    with PERFORMANCE.measure(f"sql.main.stats.{label}") as timing:
                        value = int(conn.execute(sql, params).fetchone()["c"] or 0)
                        timing.add("sql_calls")
                        return value
                for cls in Classification:
                    out["games"][cls.value] = count("games_by_classification", "SELECT COUNT(*) AS c FROM games WHERE classification=?", (cls.value,))
                    out["snapshots"][cls.value] = count("snapshots_by_classification", "SELECT COUNT(*) AS c FROM snapshots WHERE classification=?", (cls.value,))
                out["total_games"] = count("total_games", "SELECT COUNT(*) AS c FROM games")
                out["total_snapshots"] = count("total_snapshots", "SELECT COUNT(*) AS c FROM snapshots")
                out["reconciliations"] = count("reconciliations", "SELECT COUNT(*) AS c FROM reconciliation")
                out["reconciled_ok"] = count("reconciled_ok", "SELECT COUNT(*) AS c FROM reconciliation WHERE result='matched'")
                return out
            finally:
                conn.close()
