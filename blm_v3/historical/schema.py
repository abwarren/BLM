"""
BLM V3 — Historical Database Schema.

All SQL DDL as string constants.  The schema is versioned via table-level
comments and can be re-applied idempotently (``CREATE TABLE IF NOT EXISTS``).

There are 7 core tables:
  - ``games``             — Game metadata and final results
  - ``snapshots``         — Core time-series observations (one per capture)
  - ``signals``           — Threshold-crossing detections
  - ``market_events``     — Higher-level events grouping multiple signals
  - ``predictions``       — BLM model predictions at snapshot points
  - ``comparative_queries`` — Saved analysis presets
  - ``ml_exports``        — ML dataset export tracking
"""

from __future__ import annotations


# ── Indexes ──────────────────────────────────────────────────────────

_IDX_GAMES_LEAGUE = """
    CREATE INDEX IF NOT EXISTS idx_games_league ON games(league);
"""

_IDX_GAMES_STATUS = """
    CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
"""

_IDX_GAMES_START = """
    CREATE INDEX IF NOT EXISTS idx_games_start ON games(start_time);
"""

_IDX_SNAPSHOTS_GAME_TIME = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_game_time
    ON snapshots(game_id, timestamp);
"""

_IDX_SNAPSHOTS_TS = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(timestamp);
"""

_IDX_SNAPSHOTS_QTR = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_qtr ON snapshots(game_id, quarter);
"""

_IDX_SNAPSHOTS_TRAP = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_trap
    ON snapshots(game_id, trap_meter);
"""

_IDX_SNAPSHOTS_INFL = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_infl
    ON snapshots(game_id, inflation_index);
"""

_IDX_SNAPSHOTS_MOM = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_mom
    ON snapshots(game_id, momentum);
"""

_IDX_SNAPSHOTS_CONF = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_conf
    ON snapshots(game_id, confidence);
"""

_IDX_SNAPSHOTS_PTOT = """
    CREATE INDEX IF NOT EXISTS idx_snapshots_ptot
    ON snapshots(game_id, projected_total);
"""

_IDX_SIGNALS_GAME = """
    CREATE INDEX IF NOT EXISTS idx_signals_game
    ON signals(game_id, timestamp);
"""

_IDX_SIGNALS_TYPE = """
    CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);
"""

_IDX_SIGNALS_SEVERITY = """
    CREATE INDEX IF NOT EXISTS idx_signals_severity ON signals(severity);
"""

_IDX_EVENTS_GAME = """
    CREATE INDEX IF NOT EXISTS idx_events_game
    ON market_events(game_id, timestamp);
"""

_IDX_EVENTS_TYPE = """
    CREATE INDEX IF NOT EXISTS idx_events_type ON market_events(event_type);
"""

_IDX_PREDICTIONS_GAME = """
    CREATE INDEX IF NOT EXISTS idx_predictions_game
    ON predictions(game_id, timestamp);
"""


# ── Schema DDL ───────────────────────────────────────────────────────

CREATE_GAMES_TABLE = """
CREATE TABLE IF NOT EXISTS games (
    id              TEXT PRIMARY KEY,                 -- game_id
    league          TEXT NOT NULL DEFAULT 'Cyber 2K26',
    season          TEXT,
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'live'
                    CHECK(status IN ('pre', 'live', 'halftime', 'ended')),
    start_time      TEXT,                             -- ISO 8601
    end_time        TEXT,                             -- ISO 8601
    final_home      INTEGER,
    final_away      INTEGER,
    final_total     INTEGER,                          -- home + away
    final_margin    INTEGER,                          -- home - away
    total_snapshots INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                  TEXT PRIMARY KEY,             -- UUID v7 (time-sortable)
    game_id             TEXT NOT NULL REFERENCES games(id),
    timestamp           TEXT NOT NULL,                 -- ISO 8601 with microseconds
    quarter             INTEGER NOT NULL DEFAULT 1,
    clock               TEXT,                          -- "MM:SS"
    possession          TEXT,

    -- Scoreboard
    home_score          INTEGER NOT NULL DEFAULT 0,
    away_score          INTEGER NOT NULL DEFAULT 0,
    score_difference    INTEGER NOT NULL DEFAULT 0,
    total_score         INTEGER NOT NULL DEFAULT 0,

    -- Market lines
    total_line          REAL,
    spread              REAL,
    home_team_total     REAL,
    away_team_total     REAL,
    total_line_raw      REAL,
    spread_raw          REAL,

    -- Odds (decimal)
    over_odds           REAL,
    under_odds          REAL,
    spread_odds_home    REAL,
    spread_odds_away    REAL,

    -- Movement deltas (computed)
    line_delta          REAL,
    odds_delta          REAL,
    spread_delta        REAL,

    -- Pace (computed)
    possessions         INTEGER,
    possessions_per_min REAL,
    projected_possessions REAL,
    projected_total     REAL,

    -- Derived BLM metrics (computed)
    trap_meter          REAL,
    tt_modifier         REAL,
    inflation_index     REAL,
    compression_index   REAL,
    momentum            REAL,
    regression_prob     REAL,
    fair_total          REAL,
    expected_total      REAL,
    variance            REAL,
    volatility          REAL,
    confidence          REAL,

    -- Full snapshot as JSON (forward compat)
    raw_json            TEXT,

    -- Metadata
    ingested_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id              TEXT PRIMARY KEY,                 -- UUID v7
    game_id         TEXT NOT NULL REFERENCES games(id),
    snapshot_id     TEXT REFERENCES snapshots(id),    -- Which observation triggered it
    timestamp       TEXT NOT NULL,
    signal_type     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'mid'
                    CHECK(severity IN ('low', 'mid', 'high', 'critical')),
    value           REAL,                             -- The metric value that triggered
    threshold       REAL,                             -- The threshold that was crossed
    description     TEXT,
    related_json    TEXT,                             -- JSON of surrounding context
    confirmed       INTEGER DEFAULT 0,                -- Post-hoc validation
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_MARKET_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS market_events (
    id                TEXT PRIMARY KEY,
    game_id           TEXT NOT NULL REFERENCES games(id),
    snapshot_id       TEXT REFERENCES snapshots(id),
    timestamp         TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    duration_seconds  REAL,                            -- How long the event lasted
    magnitude         REAL,                            -- How significant
    description       TEXT,
    data_json         TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_PREDICTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS predictions (
    id                TEXT PRIMARY KEY,
    game_id           TEXT NOT NULL REFERENCES games(id),
    snapshot_id       TEXT REFERENCES snapshots(id),
    timestamp         TEXT NOT NULL,
    predicted_total   REAL,
    predicted_margin  REAL,
    predicted_winner  TEXT,
    win_probability   REAL,
    confidence        REAL,
    fair_total        REAL,
    expected_pace     REAL,
    model_version     TEXT,
    data_json         TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_COMPARATIVE_QUERIES_TABLE = """
CREATE TABLE IF NOT EXISTS comparative_queries (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    description     TEXT,
    filters_json    TEXT NOT NULL,                    -- Full filter spec as JSON
    game_ids_json   TEXT,                             -- Explicit game selection
    result_count    INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_run_at     TEXT
);
"""

CREATE_ML_EXPORTS_TABLE = """
CREATE TABLE IF NOT EXISTS ml_exports (
    id              TEXT PRIMARY KEY,
    export_type     TEXT NOT NULL,                    -- 'csv', 'parquet', 'json'
    game_ids_json   TEXT,                             -- Which games were included
    row_count       INTEGER NOT NULL DEFAULT 0,
    file_path       TEXT,
    file_size_bytes INTEGER,
    feature_list    TEXT,                             -- Which features were exported
    label_column    TEXT,
    model_version   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


# ── Combined DDL ─────────────────────────────────────────────────────

SCHEMA_DDL: str = ";".join([
    CREATE_GAMES_TABLE,
    CREATE_SNAPSHOTS_TABLE,
    CREATE_SIGNALS_TABLE,
    CREATE_MARKET_EVENTS_TABLE,
    CREATE_PREDICTIONS_TABLE,
    CREATE_COMPARATIVE_QUERIES_TABLE,
    CREATE_ML_EXPORTS_TABLE,
])

INDEX_DDL: str = ";".join([
    _IDX_GAMES_LEAGUE,
    _IDX_GAMES_STATUS,
    _IDX_GAMES_START,
    _IDX_SNAPSHOTS_GAME_TIME,
    _IDX_SNAPSHOTS_TS,
    _IDX_SNAPSHOTS_QTR,
    _IDX_SNAPSHOTS_TRAP,
    _IDX_SNAPSHOTS_INFL,
    _IDX_SNAPSHOTS_MOM,
    _IDX_SNAPSHOTS_CONF,
    _IDX_SNAPSHOTS_PTOT,
    _IDX_SIGNALS_GAME,
    _IDX_SIGNALS_TYPE,
    _IDX_SIGNALS_SEVERITY,
    _IDX_EVENTS_GAME,
    _IDX_EVENTS_TYPE,
    _IDX_PREDICTIONS_GAME,
])

FULL_DDL: str = f"{SCHEMA_DDL};{INDEX_DDL}"
"""Complete DDL: all CREATE TABLE IF NOT EXISTS followed by all indexes."""


def get_table_names() -> list[str]:
    """Return the list of table names managed by this schema."""
    return [
        "games",
        "snapshots",
        "signals",
        "market_events",
        "predictions",
        "comparative_queries",
        "ml_exports",
    ]
