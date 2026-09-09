"""Provider / competition classification — the mandatory first dimension
of the benchmark key.

AUDITED PRODUCTION FACTS (2026-09-09, blm_pokerbet.db `games`):
  - `classification` (BETUAL_NBA / CYBER_2K26) is the PROVIDER FAMILY,
    not the league: BETUAL_NBA bundles ≥5 distinct competitions
    (betual-nba, betual-tbsl, betual-euroleague, betual-cba, betual-kbl).
  - The authoritative competition identifiers are `games.competition_id`
    (numeric source id) and `games.competition_slug`, verified 1:1.
  - The DISPLAY name (`games.competition`) is unreliable: TSBL games
    display as "Betual NBA".  It is never used here.

Rules enforced:
  1. provider/competition come ONLY from source/game metadata.
  2. unknown/missing competition → UNKNOWN (benchmark-ineligible),
     never silently assigned to a provider default.
  3. the raw source classification is preserved alongside for audit.
  4. a game cannot change competition mid-game: first assignment wins,
     later contradictions FLAG the game instead of silently switching.
  5. benchmark populations key on the canonical competition.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

# Provider families (from the collector's classification layer).
PROVIDERS = {"BETUAL_NBA": "BETUAL", "CYBER_2K26": "CYBER"}
UNKNOWN = "UNKNOWN"

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS competition_ledger (
    source_game_id   TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,      -- BETUAL | CYBER | UNKNOWN
    competition      TEXT NOT NULL,      -- canonical slug (or 'unknown')
    competition_id   TEXT,               -- authoritative numeric id (audit)
    source_classification TEXT,          -- raw, for audit
    status           TEXT NOT NULL,      -- classified|unknown|conflict
    reason           TEXT,
    resolved_at      TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CompetitionResolution:
    provider: Optional[str]       # BETUAL | CYBER | None
    competition: Optional[str]    # canonical slug, or None if ineligible
    competition_id: Optional[str]
    source: Optional[str]         # raw source classification (audit)
    status: str                   # classified | unknown | conflict
    reason: str = ""


def canonical_competition(source_classification: Optional[str],
                          competition_slug: Optional[str],
                          competition_id: Optional[str] = None
                          ) -> CompetitionResolution:
    """Resolve source metadata to (provider, competition).

    Unknown/missing values are benchmark-ineligible (never defaulted).
    The slug is authoritative; competition_id is carried for audit."""
    src = (source_classification or "").strip()
    provider = PROVIDERS.get(src)
    slug = (competition_slug or "").strip()
    if provider is None:
        return CompetitionResolution(None, None, competition_id,
                                     src or None, "unknown",
                                     "source classification %r is not a "
                                     "known provider family" % (src or ""))
    if not slug or slug.upper() == UNKNOWN:
        return CompetitionResolution(None, None, competition_id, src,
                                     "unknown",
                                     "no authoritative competition metadata")
    return CompetitionResolution(provider, slug, competition_id, src,
                                 "classified")


def ensure_league_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_LEDGER_SCHEMA)
    conn.commit()


def register_game_competition(conn: sqlite3.Connection, source_game_id: str,
                              source_classification: Optional[str],
                              competition_slug: Optional[str],
                              competition_id: Optional[str],
                              resolved_at: str) -> CompetitionResolution:
    """Assign (or re-verify) a game's provider+competition, immutably.

    First assignment wins; a later contradictory claim flags the game
    'conflict' (kept on the first assignment, benchmark-ineligible until
    resolved) — never a silent switch.  An unknown start may be upgraded
    exactly once when real metadata arrives."""
    res = canonical_competition(source_classification, competition_slug,
                                competition_id)
    row = conn.execute(
        "SELECT provider, competition, source_classification, status "
        "FROM competition_ledger WHERE source_game_id=?",
        (source_game_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR REPLACE INTO competition_ledger VALUES (?,?,?,?,?,?,?,?)",
            (source_game_id, res.provider or UNKNOWN,
             res.competition or UNKNOWN, res.competition_id, res.source,
             res.status, res.reason, resolved_at))
        conn.commit()
        return res
    p0, c0, s0, st0 = row
    if res.status == "classified" and st0 == "unknown":
        conn.execute(
            "UPDATE competition_ledger SET provider=?, competition=?, "
            "competition_id=?, source_classification=?, status='classified', "
            "reason='', resolved_at=? WHERE source_game_id=?",
            (res.provider, res.competition, res.competition_id, res.source,
             resolved_at, source_game_id))
        conn.commit()
        return res
    if res.status == "classified" and st0 == "classified" \
            and (res.provider != p0 or res.competition != c0):
        conn.execute(
            "UPDATE competition_ledger SET status='conflict', reason=?, "
            "resolved_at=? WHERE source_game_id=?",
            ("competition conflict: %s|%s then %s|%s" % (p0, c0, res.provider,
                                                         res.competition),
             resolved_at, source_game_id))
        conn.commit()
        return CompetitionResolution(p0, c0, None, s0, "conflict",
                                     "game claimed %s|%s after %s|%s" % (
                                         res.provider, res.competition, p0, c0))
    return CompetitionResolution(
        None if p0 == UNKNOWN else p0,
        None if c0 == UNKNOWN else c0, None, s0, st0)


def game_competition(conn: sqlite3.Connection,
                     source_game_id: str) -> CompetitionResolution:
    """Read back a game's competition status (None = ineligible)."""
    row = conn.execute(
        "SELECT provider, competition, competition_id, source_classification, "
        "status, reason FROM competition_ledger WHERE source_game_id=?",
        (source_game_id,)).fetchone()
    if row is None:
        return CompetitionResolution(None, None, None, None, "unknown",
                                     "not registered")
    p, c, cid, s, st, reason = row
    return CompetitionResolution(
        None if p == UNKNOWN else p,
        None if c == UNKNOWN else c, cid, s, st, reason or "")


def benchmark_eligible(res: CompetitionResolution) -> bool:
    """Only cleanly classified, non-conflicting games may enter or read a
    benchmark population."""
    return (res.status == "classified" and res.provider in PROVIDERS.values()
            and bool(res.competition))
