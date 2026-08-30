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
CREATE INDEX IF NOT EXISTS idx_snapshots_game_ts
    ON snapshots(game_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_games_class
    ON games(classification);

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

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self._db_path}?mode=ro" if False else str(self._db_path)
        conn = sqlite3.connect(uri, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
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
                    game.source_url, game.status, now, now,
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
                        home_total_line, away_total_line, source_url,
                        markets_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    game_id, obs.source, obs.source_game_id, obs.classification,
                    obs.captured_at, obs.home_team, obs.away_team,
                    obs.home_score, obs.away_score, obs.period_label,
                    obs.quarter, obs.clock, obs.game_status,
                    obs.w1_odds, obs.w2_odds, obs.spread_indicator,
                    obs.total_line, obs.total_over_odds, obs.total_under_odds,
                    obs.spread, obs.spread_home_odds, obs.spread_away_odds,
                    obs.home_total_line, obs.away_total_line, obs.source_url,
                    obs.markets_json, obs.raw_json,
                ))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def get_snapshots(
        self, source_game_id: str, limit: int = 500, ascending: bool = False,
    ) -> list[dict]:
        order = "ASC" if ascending else "DESC"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(f"""
                    SELECT s.* FROM snapshots s
                    JOIN games g ON g.id = s.game_id
                    WHERE g.source_game_id = ?
                    ORDER BY s.captured_at {order} LIMIT ?
                """, (source_game_id, limit)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def count_snapshots(self, classification: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) AS c FROM snapshots"
        params: tuple = ()
        if classification:
            q += " WHERE classification=?"
            params = (classification,)
        with self._lock:
            conn = self._connect()
            try:
                return int(conn.execute(q, params).fetchone()["c"])
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
                for cls in Classification:
                    g = conn.execute(
                        "SELECT COUNT(*) AS c FROM games WHERE classification=?",
                        (cls.value,),
                    ).fetchone()["c"]
                    s = conn.execute(
                        "SELECT COUNT(*) AS c FROM snapshots WHERE classification=?",
                        (cls.value,),
                    ).fetchone()["c"]
                    out["games"][cls.value] = int(g)
                    out["snapshots"][cls.value] = int(s)
                out["total_games"] = int(conn.execute(
                    "SELECT COUNT(*) AS c FROM games").fetchone()["c"] or 0)
                out["total_snapshots"] = int(conn.execute(
                    "SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"] or 0)
                out["reconciliations"] = int(conn.execute(
                    "SELECT COUNT(*) AS c FROM reconciliation").fetchone()["c"] or 0)
                out["reconciled_ok"] = int(conn.execute(
                    "SELECT COUNT(*) AS c FROM reconciliation WHERE result='matched'"
                ).fetchone()["c"] or 0)
                return out
            finally:
                conn.close()
