"""Result-reconciliation CANDIDATE STARVATION — regression coverage (2026-10-05).

INCIDENT (read-only forensic 2026-10-04): six ended BETUAL_NBA games
(31114497, 31114758, 31114763, 31114764, 31114765, 31117867) sat
NEEDS_RECONCILIATION with NULL finals.  Their three reconciliation attempts
were all consumed WHILE THE GAME WAS STILL LIVE (1–3-quarter ``partial``
renders, correctly rejected), then ``RETRY_COOLDOWN_S`` correctly re-armed
them — but ``candidate_games`` is ``ORDER BY last_seen_at DESC LIMIT
batch_limit``, so with ~3 528 eligible candidates those OLDER re-armed games
were pushed behind the newest candidates on every pass and never reached.

FIX (candidate-selection only): ``candidate_games`` now UNIONs
(deduplicates) the UNCHANGED newest-first pass with a BOUNDED, deterministic
slice of the exhausted ``REJECTED``/``FAILED_ATTEMPT`` cohort whose retry
cooldown has elapsed, selected OLDEST-attempt-first.  Nothing else changes:
the rejection rule, ``RETRY_COOLDOWN_S``, the writers and the batch bound
are untouched; normal/newest candidacy is preserved exactly.

These tests pin the starvation-proof slice and, equally, that it does NOT
broaden candidacy (cooldown still enforced; only exhausted games; bounded).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from blm_v4.result_reconciler import (
    RETRY_COOLDOWN_S,
    SCHEMA as RECONCILER_SCHEMA,
    ResultReconciler,
)
from blm_v4.scorecard import SCORECARD_SCHEMA
from blm_v4.storage import PokerBetStore

DB_NAME = "blm_pokerbet.db"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


class _Env:
    """Temp pipeline DB: games + a NON-terminal snapshot + reconciliation state."""

    def __init__(self, tmp_path):
        self.db = tmp_path / DB_NAME
        PokerBetStore(self.db)
        conn = sqlite3.connect(str(self.db))
        conn.executescript(SCORECARD_SCHEMA)
        conn.executescript(RECONCILER_SCHEMA)
        conn.commit()
        conn.close()

    def connect(self):
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        return conn

    def game(self, gid, last_seen, *, classification="BETUAL_NBA",
             status="ended"):
        conn = self.connect()
        conn.execute(
            """INSERT INTO games (source, source_game_id, classification,
                   competition_id, competition_slug, competition, region,
                   game_family, sport, home_team, away_team, game_slug,
                   source_url, status, first_seen_at, last_seen_at)
               VALUES ('PokerBet', ?, ?, '1', 'betual-nba', ?, 'World', 'x',
                       'basketball', ?, ?, ?, ?, ?, ?, ?)""",
            (gid, classification, classification, f"H-{gid}", f"A-{gid}",
             f"{gid}-g", f"https://x/{gid}", status, last_seen, last_seen))
        game_id = conn.execute(
            "SELECT id FROM games WHERE source_game_id=?", (gid,)).fetchone()[0]
        # NON-terminal (quarter 3, clock 00:00) — keeps the game a candidate
        conn.execute(
            """INSERT INTO snapshots (game_id, source_game_id, classification,
                   captured_at, home_score, away_score, quarter, clock,
                   game_status, total_line)
               VALUES (?, ?, ?, ?, 60, 55, 3, '00:00', 'live', 205.5)""",
            (game_id, gid, classification, last_seen))
        conn.commit()
        conn.close()

    def state(self, gid, outcome, attempt, updated_at, classification="BETUAL_NBA"):
        conn = self.connect()
        conn.execute(
            """INSERT INTO result_reconciliation_state (source_game_id,
                   classification, outcome, attempt, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source_game_id) DO UPDATE SET
                   outcome=excluded.outcome, attempt=excluded.attempt,
                   updated_at=excluded.updated_at""",
            (gid, classification, outcome, attempt, updated_at))
        conn.commit()
        conn.close()

    def reconciler(self, **kw):
        return ResultReconciler(self.db, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_POKERBET_DB", str(tmp_path / DB_NAME))
    return _Env(tmp_path)


def _ids(r, env):
    conn = env.connect()
    try:
        return [c["source_game_id"] for c in r.candidate_games(conn)]
    finally:
        conn.close()


# ── 1 + 3. the regression: a re-armed game is not starved; newest still picked ──

def test_re_armed_rejected_game_is_not_starved_behind_newer(env):
    """[criterion 9 — CENTRAL] 25 newer candidates + 1 eligible old REJECTED
    game.  The 25 newer completely fill the normal batch_limit; the old
    re-armed candidate must still be returned, and must NOT displace any
    existing candidate in the newest-first pass (criterion 3)."""
    for i in range(25):
        env.game(f"NEW{i:02d}", last_seen=f"2026-10-04T21:{i:02d}:00.000000Z")
    env.game("OLD_REARM", last_seen="2026-10-04T09:00:00.000000Z")
    env.state("OLD_REARM", "REJECTED", 3, _ago(RETRY_COOLDOWN_S + 60))

    r = env.reconciler(batch_limit=25, max_attempts=3)     # slice = 5
    ids = _ids(r, env)
    assert ids[:25] == [f"NEW{i:02d}" for i in range(24, -1, -1)], \
        "newest-first pass unchanged; no existing candidate displaced"
    assert "OLD_REARM" in ids, "CENTRAL: the old re-armed game is not starved"
    assert len(ids) == 26, "25 normal + 1 re-arm"


# ── 2. the re-arm slice is selected oldest-attempt-first ────────────────────

def test_re_arm_slice_is_oldest_attempt_first(env):
    """[req 2] With the newest pass saturated, the re-arm slice is served
    OLDEST-attempt-first (deterministic age ordering)."""
    for i in range(12):                       # saturate the newest pass (limit 10)
        env.game(f"NEW{i:02d}", last_seen=f"2026-10-04T21:{i:02d}:00.000000Z")
    env.game("R_OLD", last_seen="2026-10-04T08:00:00.000000Z")
    env.game("R_NEW", last_seen="2026-10-04T08:30:00.000000Z")
    env.state("R_OLD", "REJECTED", 3, _ago(RETRY_COOLDOWN_S + 7200))  # oldest
    env.state("R_NEW", "REJECTED", 3, _ago(RETRY_COOLDOWN_S + 60))    # newer

    r = env.reconciler(batch_limit=10, max_attempts=3)     # slice = 2
    ids = _ids(r, env)
    assert ids[:10] == [f"NEW{i:02d}" for i in range(11, 1, -1)]
    assert ids[10:] == ["R_OLD", "R_NEW"], "oldest attempt first in the slice"


# ── 4. deduplication when a game qualifies for both sets ────────────────────

def test_candidate_deduplication_when_in_both_sets(env):
    """[req 4] A re-armed game that is ALSO the newest candidate appears once."""
    env.game("DUAL", last_seen="2026-10-04T22:00:00.000000Z")   # newest…
    env.state("DUAL", "REJECTED", 3, _ago(RETRY_COOLDOWN_S + 60))  # …and re-armed
    env.game("OTHER", last_seen="2026-10-04T21:00:00.000000Z")

    r = env.reconciler(batch_limit=5, max_attempts=3)      # slice = 1
    ids = _ids(r, env)
    assert ids.count("DUAL") == 1, "union must deduplicate by source_game_id"


# ── 5. the cooldown is still enforced (no request storm) ────────────────────

def test_cooldown_still_enforced(env):
    """[criterion 5] A game whose cooldown has NOT elapsed (updated_at >
    retry_cutoff) is excluded from BOTH passes — for REJECTED and for
    FAILED_ATTEMPT alike."""
    for i in range(25):
        env.game(f"NEW{i:02d}", last_seen=f"2026-10-04T21:{i:02d}:00.000000Z")
    env.game("COOL_REJ", last_seen="2026-10-04T09:00:00.000000Z")
    env.state("COOL_REJ", "REJECTED", 3, _ago(RETRY_COOLDOWN_S / 2))       # in cooldown
    env.game("COOL_FA", last_seen="2026-10-04T08:00:00.000000Z")
    env.state("COOL_FA", "FAILED_ATTEMPT", 3, _ago(RETRY_COOLDOWN_S / 2))  # in cooldown

    r = env.reconciler(batch_limit=25, max_attempts=3)
    ids = _ids(r, env)
    assert "COOL_REJ" not in ids
    assert "COOL_FA" not in ids


# ── 6. FAILED_ATTEMPT treated consistently with REJECTED ────────────────────

def test_failed_attempt_is_re_armed_like_rejected(env):
    """[req 6] The slice covers FAILED_ATTEMPT exactly like REJECTED."""
    for i in range(3):
        env.game(f"NEW{i}", last_seen=f"2026-10-04T21:0{i}:00.000000Z")
    env.game("FA", last_seen="2026-10-04T09:00:00.000000Z")
    env.state("FA", "FAILED_ATTEMPT", 3, _ago(RETRY_COOLDOWN_S + 60))

    r = env.reconciler(batch_limit=2, max_attempts=3)
    assert "FA" in _ids(r, env)


# ── 7. the pass stays BOUNDED ───────────────────────────────────────────────

def test_candidate_selection_remains_bounded(env):
    """[req 7] newest (batch_limit) + slice (batch_limit//5) — a hard bound."""
    for i in range(30):
        env.game(f"R{i:02d}", last_seen=f"2026-10-04T09:{i:02d}:00.000000Z")
        env.state(f"R{i:02d}", "REJECTED", 3, _ago(RETRY_COOLDOWN_S + 60 + i))
    for i in range(30):
        env.game(f"N{i:02d}", last_seen=f"2026-10-04T21:{i:02d}:00.000000Z")

    bl = 25
    r = env.reconciler(batch_limit=bl, max_attempts=3)
    ids = _ids(r, env)
    bound = bl + max(1, bl // ResultReconciler._REARM_SLICE_DIVISOR)
    assert len(ids) <= bound, "bounded: batch_limit + slice"
    assert len(ids) > bl, "the slice actually adds starved games"
    # updated_at offset grows with i ⇒ R29 is the OLDEST attempt; the
    # slice serves OLDEST-attempt-first.
    assert ids[25:] == ["R29", "R28", "R27", "R26", "R25"]


# ── tightening: the slice does NOT broaden candidacy ────────────────────────

def test_slice_does_not_resurrect_a_settled_or_still_in_window_game(env):
    """A game with an OK row is never a candidate; a game still inside the
    cooldown is never a candidate — the slice must not change either."""
    env.game("SETTLED", last_seen="2026-10-04T09:00:00.000000Z")
    conn = env.connect()
    conn.execute(
        """INSERT INTO game_results (source_game_id, classification,
               final_home, final_away, final_total, result_at,
               final_result_status)
           VALUES ('SETTLED', 'BETUAL_NBA', 105, 100, 205, ?, 'OK')""",
        (_ago(7200),))
    conn.commit()
    conn.close()
    env.state("SETTLED", "REJECTED", 3, _ago(RETRY_COOLDOWN_S + 60))
    env.game("IN_WINDOW", last_seen="2026-10-04T08:00:00.000000Z")
    env.state("IN_WINDOW", "REJECTED", 3, _ago(RETRY_COOLDOWN_S / 4))

    r = env.reconciler(batch_limit=5, max_attempts=3)
    ids = _ids(r, env)
    assert "SETTLED" not in ids
    assert "IN_WINDOW" not in ids
