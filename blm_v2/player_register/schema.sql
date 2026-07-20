-- Player Register — SQLite schema
-- Tracks observed player accounts across poker tables.

CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    first_seen  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_table  TEXT,
    last_game   TEXT,
    sessions    INTEGER NOT NULL DEFAULT 1,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS player_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    table_name  TEXT,
    game_type   TEXT,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at    TEXT,
    buy_in      REAL,
    stack       REAL
);

CREATE TABLE IF NOT EXISTS player_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    table_name  TEXT,
    seat        INTEGER,
    stack       REAL,
    vpip        REAL,
    pfr         REAL,
    hands       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_players_username ON players(username);
CREATE INDEX IF NOT EXISTS idx_players_last_seen ON players(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_observations_player ON player_observations(player_id, timestamp DESC);
