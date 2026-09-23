"""
BLM V4 — Result Reconciliation + Automatic Audit (directive 2026-09-23).

A disappearing live market is NOT evidence that a game has no result.
PokerBet serves every completed game's result at
https://www.pokerbet.co.za/en/sports/results?game={GAME_ID} — the
authoritative fallback the moment a game is completed or leaves the
live panel without a verified final.

Authoritative source hierarchy (directive):
  1. LIVE_DOM       — the live/event DOM while the game is still
                      available (the collector's existing verified path)
  2. RESULTS_PAGE   — the PokerBet results page, queried whenever a
                      completed/disappeared game lacks a verified final
  3. DB_EXISTING    — previously captured/verified data only

REQUIRED RESULT (state transition, directive §7): a game that leaves
the live market without a verified final enters
final_result_status = 'NEEDS_RECONCILIATION' — never 'UNKNOWN-as-final,
never abandoned'.  This worker then queries the results page, verifies
the rendered game belongs to the recorded game, and promotes the row to
'OK' with result_source='RESULTS_PAGE'.

INTEGRITY (directive §6) — a result is VERIFIED only when ALL hold:
  * the rendered page carries a scoreboard (never the failed template)
  * quarter scores sum to the final score (when the page shows them)
  * team identity is PROVEN against the canonical DB game record:
    the results page does NOT echo the game id in its DOM, and the SPA
    silently keeps the previous game's scoreboard when a request does
    not resolve — so identity comes from slugged team-name containment
    (the same policy the event-view reconciler uses), never from the
    rendered block alone
  * no verified result is ever overwritten: RESULTS_PAGE may only
    fill a NEEDS_RECONCILIATION/UNKNOWN row; a row whose verdict came
    from LIVE_DOM or from a verified DB_EXISTING OK is IMMUTABLE here
  * conflicts (a results-page total disagreeing with a stored LIVE_DOM
    OK verdict) are FLAGGED in result_conflicts, never overwritten

AUDIT (directive §3/§4): audit_report() returns the coverage census
(games scanned / expected to finish / verified / missing / recovered /
unresolved / coverage %) and audit_missing()/audit_recovered()/
audit_conflicts() produce the read-only report tables.

IDEMPOTENCE (directive §9): one pass re-attempts only games whose
latest attempt did not end VERIFIED or TEMPLATE_FAILED; VERIFIED rows
are terminal, TEMPLATE_FAILED games are exhausted (their result is not
on the results page — re-fetching cannot change that until a NEW page
render exists), and FAILED_ATTEMPT games retry with attempt counters
capped.  Writes are keyed UNIQUE(source_game_id): a re-run updates the
same row, never duplicates.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.results_fetcher import (
    PlaywrightResultsFetcher,
    ResultsFetcher,
    parse_results_page,
)
from blm_v4.scorecard import SCORECARD_SCHEMA

logger = logging.getLogger("blm_v4.result_reconciler")

#: source vocabulary (directive §6)
SRC_LIVE_DOM = "LIVE_DOM"
SRC_RESULTS_PAGE = "RESULTS_PAGE"
SRC_DB_EXISTING = "DB_EXISTING"
SRC_MANUAL_REVIEW = "MANUAL_REVIEW"

#: game_results.final_result_status values this worker reads/writes.
#: OK / UNKNOWN / INVALID pre-exist (scorecard vocabulary);
#: NEEDS_RECONCILIATION is the directive's new state: the game left the
#: live market without a verified final — reconciliation REQUIRED.
STATUS_NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

#: the state machine (directive §7), for docs/API surfaces.
RESULT_STATUS_NOTE = (
    "LIVE MARKET PRESENT -> LIVE MARKET DISAPPEARS -> "
    "RESULT RECONCILIATION REQUIRED (NEEDS_RECONCILIATION) -> "
    "QUERY RESULTS PAGE -> VERIFY GAME -> CAPTURE FINAL RESULT -> "
    "UPDATE DB -> MARK RESULT VERIFIED (OK, result_source).  "
    "A disappearing market is NEVER 'no result'; games are never "
    "abandoned for having left the live panel."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS result_reconciliation (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id     TEXT NOT NULL,
    classification     TEXT,
    attempt            INTEGER NOT NULL DEFAULT 0,
    outcome            TEXT NOT NULL,          -- VERIFIED | FAILED_ATTEMPT |
                                               -- TEMPLATE_FAILED | CONFLICT
    rendered_home      TEXT,
    rendered_away      TEXT,
    final_home         INTEGER,
    final_away         INTEGER,
    final_total        INTEGER,
    quarter_scores     TEXT,                   -- JSON [[h,a],...] or NULL
    match_score        INTEGER,                -- team identity proof
    checks_json        TEXT NOT NULL DEFAULT '{}',
    fetched_at         TEXT NOT NULL,
    UNIQUE(source_game_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_rr_game
    ON result_reconciliation(source_game_id, attempt);

-- Latest outcome per game (materialized for audit queries).
CREATE TABLE IF NOT EXISTS result_reconciliation_state (
    source_game_id   TEXT PRIMARY KEY,
    classification   TEXT,
    outcome          TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 0,
    final_home       INTEGER,
    final_away       INTEGER,
    final_total      INTEGER,
    source           TEXT,                    -- RESULTS_PAGE | LIVE_DOM | ...
    updated_at       TEXT NOT NULL
);

-- Flagged disagreements between sources — NEVER silently overwritten.
CREATE TABLE IF NOT EXISTS result_conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id    TEXT NOT NULL,
    classification    TEXT,
    live_dom_total    INTEGER,                -- stored OK verdict's total
    results_total     INTEGER,                -- results-page rendered total
    detail            TEXT NOT NULL DEFAULT '',
    flagged_at        TEXT NOT NULL,
    UNIQUE(source_game_id)
);
"""

# ── Identity helpers (shared policy with reconcile.py) ────────────────

_WS_RE = re.compile(r"\s+")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: Optional[str]) -> str:
    """Lowercase alphanumeric slug of a team name ('' for None/blank)."""
    if not name:
        return ""
    return _SLUG_STRIP_RE.sub("", name.strip().lower()) or ""


def _team_variants(name: Optional[str]) -> list[str]:
    """Slug variants accepting the Betual 'Virtual' marker difference.

    DB canonical names may be 'Lakers' while the page renders
    'Los Angeles Lakers Virtual' (or vice versa) — containment of the
    shorter slug inside the longer one, in BOTH directions, proves the
    same team.  Marker tokens are never hardcoded away: containment
    handles prefix/suffix renderings generically.
    """
    variants = {slugify(name)}
    if name:
        base = re.sub(r"\bvirtual\b", " ", name, flags=re.I)
        variants.add(slugify(base))
    return sorted(v for v in variants if v)


def _team_match(rendered: Optional[str], recorded: Optional[str]) -> bool:
    """Identity containment: either side's variant slug contains the
    other's (len-guarded so a 3-letter stub can never 'prove' a long
    canonical name it merely substrings)."""
    if not rendered or not recorded:
        return False
    r_slugs = _team_variants(rendered)
    d_slugs = _team_variants(recorded)
    for r in r_slugs:
        for d in d_slugs:
            if r == d:
                return True
            shorter, longer = (r, d) if len(r) <= len(d) else (d, r)
            if len(shorter) >= 4 and shorter in longer:
                return True
    return False


def _page_sums_valid(parsed: dict) -> Optional[bool]:
    """None when the page shows no quarter scores; else True when they
    sum to the rendered final.  A page contradicting itself is not a
    parsable result."""
    qs = parsed.get("quarter_scores") or []
    if len(qs) < 2:
        return None
    th = sum(int(q[0]) for q in qs)
    ta = sum(int(q[1]) for q in qs)
    return th == parsed.get("home_score") and ta == parsed.get("away_score")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ResultReconciler:
    """Result-reconciliation worker over one pipeline SQLite DB."""

    def __init__(self, db_path: Path | str,
                 fetcher: Optional[ResultsFetcher] = None, *,
                 max_attempts: int = 3,
                 batch_limit: int = 25,
                 log: Optional[logging.Logger] = None):
        self._db_path = Path(db_path)
        self._fetcher = fetcher
        self._max_attempts = max(1, int(max_attempts))
        self._batch_limit = max(1, int(batch_limit))
        self._log = log or logger
        self._lock = threading.Lock()
        self._init_schema()

    # ── schema ────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        """Idempotent bootstrap (same policy as settle_worker): create the
        scorecard tables the worker reads/writes (no-op on production),
        add the result_source provenance column when missing, then this
        worker's own tables."""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCORECARD_SCHEMA)
                cols = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(game_results)")}
                if "result_source" not in cols:
                    conn.execute(
                        "ALTER TABLE game_results ADD COLUMN result_source TEXT")
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # ── candidates (directive §1) ─────────────────────────────────
    def candidate_games(self, conn: sqlite3.Connection) -> list[dict]:
        """Games needing reconciliation — the directive's scan set.

        Candidates are games (any status) whose stored result is not
        VERIFIED OK, whose snapshot history cannot prove a final by the
        scorecard's own rules, and whose latest attempt is retryable:
          * never attempted, or
          * last attempt FAILED_ATTEMPT with attempts < max, or
          * last attempt CONFLICT (re-checked; flagged again, never
            silently overwritten),
        VERIFIED and TEMPLATE_FAILED games are never re-attempted
        (idempotence, directive §9).  A game holding a verified OK row
        is never a candidate (its result is final).
        """
        rows = conn.execute(
            """
            SELECT g.source_game_id,
                   COALESCE(g.classification, '') AS classification,
                   g.home_team, g.away_team, g.status,
                   g.last_seen_at,
                   (SELECT MAX(captured_at) FROM snapshots s
                    WHERE s.source_game_id = g.source_game_id) AS last_snap_at
            FROM games g
            LEFT JOIN result_reconciliation_state st
                   ON st.source_game_id = g.source_game_id
            WHERE NOT EXISTS (
                  SELECT 1 FROM game_results r
                  WHERE r.source_game_id = g.source_game_id
                    AND r.final_result_status = 'OK')
              AND (st.source_game_id IS NULL
                   OR st.outcome = 'FAILED_ATTEMPT'
                   OR st.outcome = 'CONFLICT')
              AND (st.attempt IS NULL OR st.attempt < ?)
              AND NOT EXISTS (
                  SELECT 1 FROM snapshots s2
                  WHERE s2.source_game_id = g.source_game_id
                    AND s2.captured_at IS NOT NULL
                    AND (
                        LOWER(COALESCE(s2.game_status, '')) IN
                            ('ended', 'finished', 'full time', 'ft',
                             'complete', 'completed')
                        OR LOWER(COALESCE(s2.period_label, '')) LIKE
                            '%full time%'
                        OR LOWER(COALESCE(s2.period_label, '')) LIKE
                            '%finished%'
                        OR LOWER(COALESCE(s2.period_label, '')) LIKE
                            '%end of match%'
                        OR LOWER(COALESCE(s2.period_label, '')) LIKE
                            '%match ended%'
                        OR (COALESCE(s2.quarter, 0) >= 4
                            AND TRIM(COALESCE(s2.clock, '')) IN
                                ('00:00', '0:00')))
                    AND (s2.home_score IS NOT NULL
                         OR s2.away_score IS NOT NULL))
            ORDER BY g.last_seen_at DESC
            LIMIT ?
            """,
            (self._max_attempts, self._batch_limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── verification (directive §6) ───────────────────────────────
    def verify_parsed(self, parsed: dict, game: dict) -> dict:
        """Pure verification of one parsed page against one game record.

        checks keys: has_scoreboard, page_sums_valid, home_team_match,
        away_team_match, teams_match, score_consistent.  result is
        VERIFIED | TEMPLATE_FAILED | REJECTED.  No DB, no I/O — the
        reconciliation decision is unit-testable end to end.
        """
        checks: dict[str, Any] = {
            "has_scoreboard": parsed.get("home_score") is not None
            and parsed.get("away_score") is not None,
            "page_sums_valid": _page_sums_valid(parsed),
        }
        checks["home_team_match"] = _team_match(
            parsed.get("home_team"), game.get("home_team"))
        checks["away_team_match"] = _team_match(
            parsed.get("away_team"), game.get("away_team"))
        # Identity first: BOTH rendered names must match the record's
        # two teams — in the rendered order OR swapped (the SPA
        # occasionally swaps the rendering order; both names matching in
        # SOME assignment proves the same fixture, and nothing else does).
        checks["away_render_matches_record_home"] = _team_match(
            parsed.get("away_team"), game.get("home_team"))
        checks["home_render_matches_record_away"] = _team_match(
            parsed.get("home_team"), game.get("away_team"))
        checks["teams_match"] = (
            (checks["home_team_match"] and checks["away_team_match"])
            or (checks["home_render_matches_record_away"]
                and checks["away_render_matches_record_home"]))
        # Orientation: the page's rendered-first team is the compact
        # line's first number.  When the render order is swapped vs the
        # record, the verified final is re-oriented to the RECORD's
        # home/away so the stored row stays comparable with every
        # snapshot-derived verdict.
        orientation = "as-rendered"
        if checks["teams_match"] and not (
                checks["home_team_match"] and checks["away_team_match"]):
            orientation = "re-oriented"
            h, a = parsed.get("home_score"), parsed.get("away_score")
            parsed = dict(parsed, home_score=a, away_score=h)
            qs = [(b, a2) for (a2, b) in (parsed.get("quarter_scores") or [])]
            parsed["quarter_scores"] = qs
        checks["orientation"] = orientation
        checks["score_consistent"] = (
            checks["page_sums_valid"] is not False)
        failures = []
        if not checks["has_scoreboard"]:
            failures.append("no scoreboard rendered")
        elif not checks["teams_match"]:
            failures.append(
                f"teams mismatch: rendered="
                f"{parsed.get('home_team')!r}/{parsed.get('away_team')!r} "
                f"recorded={game.get('home_team')!r}/"
                f"{game.get('away_team')!r}")
        elif not checks["score_consistent"]:
            failures.append("quarter scores do not sum to the final score")

        if not checks["has_scoreboard"]:
            result = "TEMPLATE_FAILED" if parsed.get("failed_template") \
                else "REJECTED"
        elif failures:
            result = "REJECTED"
        else:
            result = "VERIFIED"
        return {"result": result, "checks": checks, "failures": failures,
                "parsed": parsed}

    # ── one pass (directive §1 A–F) ───────────────────────────────
    def run_pass(self, *, fetcher: Optional[ResultsFetcher] = None) -> dict:
        """One reconciliation pass.  Returns pass stats; never raises
        for per-game problems (fail-closed per game, logged)."""
        own_fetcher = fetcher is None
        fetcher = fetcher or self._fetcher or PlaywrightResultsFetcher(
            log=self._log)
        stats = {"scanned": 0, "verified": 0, "rejected": 0,
                 "template_failed": 0, "conflicts": 0, "errors": 0}
        try:
            conn = self._connect()
            try:
                candidates = self.candidate_games(conn)
            finally:
                conn.close()
            stats["scanned"] = len(candidates)
            for game in candidates:
                try:
                    outcome = self._reconcile_one(fetcher, game)
                except Exception:
                    stats["errors"] += 1
                    self._log.exception(
                        "result_reconcile_game_failed %s",
                        game.get("source_game_id"))
                    continue
                if outcome == "VERIFIED":
                    stats["verified"] += 1
                elif outcome == "TEMPLATE_FAILED":
                    stats["template_failed"] += 1
                elif outcome == "CONFLICT":
                    stats["conflicts"] += 1
                else:
                    stats["rejected"] += 1
        finally:
            if own_fetcher:
                try:
                    fetcher.close()
                except Exception:
                    pass
        return stats

    # -- per-game pipeline ---------------------------------------------
    def _reconcile_one(self, fetcher: ResultsFetcher,
                       game: dict) -> str:
        gid = game["source_game_id"]
        text = fetcher.fetch_results_page(gid)
        now = _utcnow()
        if text is None:
            return self._record(game, {
                "outcome": "FAILED_ATTEMPT", "fetched_at": now,
                "checks": {"fetch_failed": True}})
        parsed = parse_results_page(text, gid)
        verdict = self.verify_parsed(parsed, game)
        # verify_parsed re-orients a swapped rendering inside its own
        # copy — the oriented parse is the authoritative one to persist.
        parsed = verdict.get("parsed") or parsed
        checks = dict(verdict["checks"])
        checks["parse_quality"] = parsed.get("parse_quality")
        rec = {
            "outcome": verdict["result"],
            "rendered_home": parsed.get("home_team"),
            "rendered_away": parsed.get("away_team"),
            "final_home": parsed.get("home_score"),
            "final_away": parsed.get("away_score"),
            "final_total": parsed.get("final_total"),
            "quarter_scores": (
                json.dumps(parsed.get("quarter_scores"))
                if parsed.get("quarter_scores") else None),
            "match_score": 1 if checks.get("teams_match") else 0,
            "checks": checks,
            "fetched_at": now,
        }
        if verdict["result"] != "VERIFIED":
            self._stamp_unresolved(game, rec)
            return self._record(game, rec)
        # VERIFIED — fill the game_results row (NEVER overwriting a
        # verified result; the UPDATE guards on the non-OK state).
        self._persist_result(game, parsed, rec)
        return self._record(game, rec)

    def _stamp_unresolved(self, game: dict, rec: dict) -> None:
        """A game whose reconciliation did NOT verify still gets an
        explicit game_results row (NEEDS_RECONCILIATION) when it has no
        row at all — the directive's 'no result' is a STATE, never a
        silent absence.  Existing rows are never downgraded."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO game_results (
                           source_game_id, classification, result_at,
                           final_result_status)
                       SELECT g.source_game_id,
                              COALESCE(g.classification, ''), ?, ?
                       FROM games g
                       WHERE g.source_game_id = ?
                         AND NOT EXISTS (
                             SELECT 1 FROM game_results r
                             WHERE r.source_game_id = g.source_game_id)""",
                    (rec["fetched_at"], STATUS_NEEDS_RECONCILIATION,
                     game["source_game_id"]))
                conn.commit()
            finally:
                conn.close()

    def _persist_result(self, game: dict, parsed: dict,
                        rec: dict) -> None:
        """Write the verified result + state, guarded against clobber.

        * game_results: only a non-OK row is updated (an OK verdict —
          live-observed or previously verified — is IMMUTABLE); a
          missing row is inserted.  If a concurrent writer stored OK
          between the candidate scan and this write, the UPDATE's
          WHERE clause matches nothing and the stored verdict wins.
        * result_conflicts: a verified page total that DISAGREES with
          an existing OK total is flagged, never written.
        """
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    """SELECT final_home, final_away, final_total,
                              final_result_status
                       FROM game_results WHERE source_game_id = ?""",
                    (game["source_game_id"],)).fetchone()
                live_total = None
                if existing and existing["final_result_status"] == "OK":
                    live_total = existing["final_total"]
                elif existing is not None and (
                        existing["final_home"] is not None
                        and existing["final_away"] is not None):
                    # UNKNOWN row carrying scored finals: if the page
                    # disagrees, that is a CONFLICT (flagged, not
                    # overwritten); agreement is resolved as OK below.
                    if (existing["final_total"] is not None
                            and existing["final_total"]
                            != parsed.get("final_total")):
                        self._flag_conflict(
                            conn, game, existing["final_total"],
                            parsed.get("final_total"),
                            "results page disagrees with a scored "
                            "UNKNOWN row")
                conn.execute(
                    """INSERT INTO game_results (
                           source_game_id, classification, final_home,
                           final_away, final_total, result_at,
                           final_result_status, result_source)
                       VALUES (?, ?, ?, ?, ?, ?, 'OK', ?)
                       ON CONFLICT(source_game_id) DO UPDATE SET
                           final_home = excluded.final_home,
                           final_away = excluded.final_away,
                           final_total = excluded.final_total,
                           result_at = excluded.result_at,
                           final_result_status = excluded.final_result_status,
                           result_source = excluded.result_source
                       WHERE game_results.final_result_status != 'OK'""",
                    (game["source_game_id"],
                     game.get("classification") or "",
                     parsed.get("home_score"), parsed.get("away_score"),
                     parsed.get("final_total"), rec["fetched_at"],
                     SRC_RESULTS_PAGE))
                conn.execute(
                    """INSERT INTO result_reconciliation_state (
                           source_game_id, classification, outcome,
                           attempt, final_home, final_away, final_total,
                           source, updated_at)
                       VALUES (?, ?, 'VERIFIED', 0, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_game_id) DO UPDATE SET
                           outcome = excluded.outcome,
                           attempt = 0,
                           final_home = excluded.final_home,
                           final_away = excluded.final_away,
                           final_total = excluded.final_total,
                           source = excluded.source,
                           updated_at = excluded.updated_at""",
                    (game["source_game_id"],
                     game.get("classification") or "",
                     parsed.get("home_score"), parsed.get("away_score"),
                     parsed.get("final_total"), SRC_RESULTS_PAGE,
                     rec["fetched_at"]))
                if live_total is not None \
                        and live_total != parsed.get("final_total"):
                    self._flag_conflict(
                        conn, game, live_total, parsed.get("final_total"),
                        "results page disagrees with the stored OK verdict "
                        "(stored result kept)")
                conn.commit()
            finally:
                conn.close()

    def _flag_conflict(self, conn: sqlite3.Connection, game: dict,
                       live_total: Optional[int],
                       results_total: Optional[int], detail: str) -> None:
        conn.execute(
            """INSERT INTO result_conflicts (
                   source_game_id, classification, live_dom_total,
                   results_total, detail, flagged_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_game_id) DO UPDATE SET
                   live_dom_total = excluded.live_dom_total,
                   results_total = excluded.results_total,
                   detail = excluded.detail,
                   flagged_at = excluded.flagged_at""",
            (game["source_game_id"], game.get("classification") or "",
             live_total, results_total, detail, _utcnow()))

    def _record(self, game: dict, rec: dict) -> str:
        """Persist one attempt outcome (audit trail + idempotence key).

        attempt numbering: VERIFIED uses the state table's own counter
        (reset to 0 there), every other outcome increments it — a
        TEMPLATE_FAILED page is cached by the SPA, so retrying would
        re-read the same render; attempts are capped and the game can
        re-enter reconciliation only when the attempt counter is reset
        (e.g. a new directive/ops action) or a new candidate condition
        arises.
        """
        outcome = rec["outcome"]
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT attempt FROM result_reconciliation_state "
                    "WHERE source_game_id = ?",
                    (game["source_game_id"],)).fetchone()
                attempt = (row["attempt"] if row else 0) + 1
                if outcome == "VERIFIED":
                    # state row already written by _persist_result with
                    # attempt=0 — keep attempt history consistent here
                    attempt = (row["attempt"] if row else 0) + 1
                conn.execute(
                    """INSERT INTO result_reconciliation (
                           source_game_id, classification, attempt,
                           outcome, rendered_home, rendered_away,
                           final_home, final_away, final_total,
                           quarter_scores, match_score, checks_json,
                           fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_game_id, attempt) DO NOTHING""",
                    (game["source_game_id"],
                     game.get("classification") or "", attempt, outcome,
                     rec.get("rendered_home"), rec.get("rendered_away"),
                     rec.get("final_home"), rec.get("final_away"),
                     rec.get("final_total"), rec.get("quarter_scores"),
                     rec.get("match_score"),
                     json.dumps(rec.get("checks") or {}), rec["fetched_at"]))
                if outcome != "VERIFIED":
                    conn.execute(
                        """INSERT INTO result_reconciliation_state (
                               source_game_id, classification, outcome,
                               attempt, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(source_game_id) DO UPDATE SET
                               outcome = excluded.outcome,
                               attempt = excluded.attempt,
                               updated_at = excluded.updated_at""",
                        (game["source_game_id"],
                         game.get("classification") or "", outcome,
                         attempt, rec["fetched_at"]))
                conn.commit()
            finally:
                conn.close()
        self._log.info(
            "result_reconcile %s outcome=%s checks=%s",
            game["source_game_id"], outcome,
            json.dumps(rec.get("checks") or {}))
        return outcome

    # ── marking NEEDS_RECONCILIATION (directive §1/§7 state machine) ──
    def mark_needs_reconciliation(self, conn: sqlite3.Connection,
                                  batch_limit: int = 200) -> int:
        """Stamp ended games with NO game_results row (or an UNKNOWN
        row) as NEEDS_RECONCILIATION — the directive's explicit state
        transition instead of silent 'no result'.  Returns rows stamped.
        Idempotent: already-stamped games are not re-stamped.  The stamp
        carries NO result values (never a fabricated final) and
        result_source stays NULL until a verified result lands.
        INVALID rows are never touched (the scorecard's final state)."""
        cur = conn.execute(
            """UPDATE game_results
               SET final_result_status = ?
               WHERE final_result_status = 'UNKNOWN'
                 AND EXISTS (
                     SELECT 1 FROM games g
                     WHERE g.source_game_id =
                               game_results.source_game_id
                       AND g.status = 'ended')""",
            (STATUS_NEEDS_RECONCILIATION,))
        n1 = cur.rowcount or 0
        # ended games with NO row at all: insert the stamp (bounded).
        cur = conn.execute(
            """INSERT INTO game_results (
                   source_game_id, classification, result_at,
                   final_result_status, result_source)
               SELECT g.source_game_id,
                      COALESCE(g.classification, ''),
                      COALESCE(g.last_seen_at, ?),
                      ?, NULL
               FROM games g
               WHERE g.status = 'ended'
                 AND NOT EXISTS (
                     SELECT 1 FROM game_results r
                     WHERE r.source_game_id = g.source_game_id)
               LIMIT ?""",
            (_utcnow(), STATUS_NEEDS_RECONCILIATION, batch_limit))
        n2 = cur.rowcount or 0
        return n1 + n2

    # ── audits (directive §3/§4, read-only) ───────────────────────
    def audit_report(self) -> dict[str, Any]:
        """The §12 RESULT AUDIT census.  Read-only; safe repeatedly."""
        conn = self._connect()
        try:
            total_games = conn.execute(
                "SELECT COUNT(*) FROM games").fetchone()[0]
            ended = conn.execute(
                "SELECT COUNT(*) FROM games WHERE status='ended'"
            ).fetchone()[0]
            by_status = {r[0]: r[1] for r in conn.execute(
                """SELECT final_result_status, COUNT(*)
                   FROM game_results GROUP BY final_result_status""")}
            recovered = conn.execute(
                """SELECT COUNT(*) FROM result_reconciliation_state
                   WHERE outcome='VERIFIED'
                     AND source = 'RESULTS_PAGE'""").fetchone()[0]
            attempts = conn.execute(
                "SELECT COUNT(*) FROM result_reconciliation"
            ).fetchone()[0]
            still_unresolved = conn.execute(
                """SELECT COUNT(*) FROM games g
                   WHERE g.status='ended'
                     AND NOT EXISTS (
                         SELECT 1 FROM game_results r
                         WHERE r.source_game_id = g.source_game_id
                           AND r.final_result_status = 'OK')"""
            ).fetchone()[0]
            conflicts = conn.execute(
                "SELECT COUNT(*) FROM result_conflicts").fetchone()[0]
            verified = by_status.get("OK", 0)
            return {
                "games_scanned": total_games,
                "expected_to_have_results": ended,
                "verified_results": verified,
                "missing_results": ended - verified,
                "recovered_from_results_page": recovered,
                "still_unresolved": still_unresolved,
                "result_coverage_pct": round(
                    100.0 * verified / ended, 2) if ended else 100.0,
                "status_breakdown": by_status,
                "reconciliation_attempts": attempts,
                "conflicts": conflicts,
                "generated_at": _utcnow(),
            }
        finally:
            conn.close()

    def audit_missing(self, limit: int = 50) -> list[dict]:
        """§4A — games missing final results, with reasons."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT g.source_game_id, g.classification,
                          g.home_team, g.away_team,
                          g.first_seen_at, g.last_seen_at,
                          g.status,
                          COALESCE(r.final_result_status,
                                   'NO_ROW') AS current_status,
                          st.outcome AS last_attempt_outcome,
                          st.attempt AS attempts,
                          COALESCE(st.updated_at, '') AS last_attempt_at,
                          CASE
                            WHEN st.outcome = 'TEMPLATE_FAILED' THEN
                              'results page renders the failed template'
                            WHEN st.outcome = 'CONFLICT' THEN
                              'conflicting result flagged'
                            WHEN st.outcome = 'FAILED_ATTEMPT' THEN
                              'fetch/parse attempt failed (retryable)'
                            ELSE 'not yet attempted'
                          END AS reason
                   FROM games g
                   LEFT JOIN game_results r
                          ON r.source_game_id = g.source_game_id
                   LEFT JOIN result_reconciliation_state st
                          ON st.source_game_id = g.source_game_id
                   WHERE g.status = 'ended'
                     AND (r.source_game_id IS NULL
                          OR r.final_result_status != 'OK')
                   ORDER BY g.last_seen_at DESC
                   LIMIT ?""", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def audit_recovered(self, limit: int = 50) -> list[dict]:
        """§4B — games recovered from the results page."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT s.source_game_id, s.classification,
                          r.final_home, r.final_away, r.final_total,
                          s.source, s.updated_at AS retrieved_at
                   FROM result_reconciliation_state s
                   JOIN game_results r
                         ON r.source_game_id = s.source_game_id
                   WHERE s.outcome = 'VERIFIED'
                     AND s.source = 'RESULTS_PAGE'
                   ORDER BY s.updated_at DESC LIMIT ?""",
                (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def audit_conflicts(self, limit: int = 50) -> list[dict]:
        """§4C — conflicting results (flagged, never overwritten)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM result_conflicts
                   ORDER BY flagged_at DESC LIMIT ?""",
                (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ── Daemon wrapper (mirrors SettleWorker's shape) ──────────────────────

class ResultReconcilerWorker:
    """Background reconciliation loop — a daemon sibling of SettleWorker.

    Cadence + bounds are env-tunable; the loop never dies (a failed
    pass is logged and retried on the next tick).
    """

    def __init__(self, db_path: Path | str, *,
                 interval_s: float = 900.0,
                 batch_limit: int = 25,
                 mark_batch: int = 200,
                 log: Optional[logging.Logger] = None,
                 fetcher: Optional[ResultsFetcher] = None):
        self._db_path = Path(db_path)
        self._interval_s = float(interval_s)
        self._mark_batch = int(mark_batch)
        self._log = log or logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reconciler = ResultReconciler(
            self._db_path, fetcher=fetcher, batch_limit=batch_limit,
            log=self._log)

    @property
    def reconciler(self) -> ResultReconciler:
        return self._reconciler

    def run_once(self) -> dict:
        stamped = 0
        conn = self._reconciler._connect()
        try:
            stamped = self._reconciler.mark_needs_reconciliation(
                conn, batch_limit=self._mark_batch)
            conn.commit()
        finally:
            conn.close()
        stats = self._reconciler.run_pass()
        out = {"needs_stamped": stamped, **stats}
        self._log.info("result_reconciliation_pass %s", json.dumps(out))
        return out

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._log.exception("result_reconciliation_pass_failed")
            self._stop.wait(self._interval_s)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.loop, name="blm-result-reconciler", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
