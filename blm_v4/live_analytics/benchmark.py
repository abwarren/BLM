"""Pace benchmark + T-state Z — the historical reference layer.

Population: PRIOR COMPLETED observations only, keyed by league + 5-point
progress bucket (`league|Pnnn`).  An observation can never define its own
benchmark: the lookup is strictly `captured_at < T` AND `id != T's id`,
the benchmark stats are computed at read time from immutable stored rows,
and the results are cached ONLY under the full population key
(key + cutoff time + cutoff id) so a later observation can never read a
benchmark computed with a longer cutoff than its own T.

Leakage rules enforced here:
  1. prior completed observations only  → `captured_at < T` (strict)
  2. the current observation is never its own population member
  3. same-game exclusion is available (prior obs of the SAME game carry
     the game's own information — excluded by default for a clean
     cross-game population, configurable for small-cohort leagues)
  4. benchmark stats are functions of (key, cutoff) — monotone in data,
     immutable in history; nothing is ever written back into historical
     rows.

Descriptive statistics only: no probability, no edge, no betting
semantics anywhere in this module.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Optional

from blm_v4.live_analytics.league import PROVIDERS
from blm_v4.live_analytics.pace import benchmark_key

# DB rows carry the source PROVIDER FAMILY in `classification`
# (BETUAL_NBA / CYBER_2K26); the benchmark key carries the canonical
# provider (BETUAL / CYBER).  Map provider → family for the SQL filter.
_FAMILY_OF_PROVIDER = {v: k for k, v in PROVIDERS.items()}

# Population source: clean_projections carries classification (provider
# family), period_label and actual_pts_per_min together; the canonical
# COMPETITION dimension arrives per-game from the competition ledger.
# The ledger and cache may live in a SIDECAR database (the production
# clean DB is opened read-only by consumers and can host no tables):
# a qualifier of None keeps the historical single-connection mode.
_POPULATION_SOURCE = "clean_projections"


def _qualified(table: str, qualifier: Optional[str]) -> str:
    return table if qualifier is None else "%s.%s" % (qualifier, table)

# Minimum population size before a Z is meaningful.  Below this the
# honest answer is "insufficient history" (None + n reported), never a
# fragile Z from 3 rows.
MIN_BENCHMARK_N = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pace_benchmark_cache (
    benchmark_key   TEXT NOT NULL,   -- 'provider|competition|Qn|Pnnn'
    cutoff_captured_at TEXT NOT NULL,-- strict upper bound T (exclusive)
    cutoff_observation_id INTEGER,   -- T's own observation id (NULL ok)
    n               INTEGER NOT NULL,
    mean_pace       REAL,
    std_pace        REAL,
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (benchmark_key, cutoff_captured_at, cutoff_observation_id)
);
"""


@dataclass(frozen=True)
class PaceBenchmark:
    key: str
    n: int
    mean: Optional[float]
    std: Optional[float]


@dataclass(frozen=True)
class PaceZ:
    z: Optional[float]
    benchmark_key: str
    n: int
    mean: Optional[float]
    std: Optional[float]
    actual_pace: Optional[float]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _stats(conn: sqlite3.Connection, key: str, cutoff_iso: str,
           cutoff_id: Optional[int], exclude_game: Optional[str],
           _qualifier: Optional[str] = None) -> PaceBenchmark:
    """Population stats from immutable stored rows, strictly prior to T.

    The 4-part key decomposes as provider|competition|Qn|Pnnn; the SQL
    filters on provider (classification family), PERIOD and progress
    bucket, and the COMPETITION dimension is enforced by joining the
    per-game competition ledger (status='classified') — so NBA and TSBL
    never share a population even though both come through BETUAL, and
    unregistered/conflicting/unknown games are excluded by the join
    itself.  ``_qualifier`` (internal, via benchmark_for) lets the
    population rows and the ledger live in an attached sidecar DB."""
    parts = key.split("|")
    if len(parts) != 4:
        raise ValueError("benchmark key must be provider|competition|Qn|Pnnn, "
                         "got %r" % key)
    provider, competition, qbucket, pbucket = parts
    if provider not in PROVIDERS.values():
        return PaceBenchmark(key, 0, None, None)   # non-canonical provider
    family = _FAMILY_OF_PROVIDER[provider]
    # Population rows may live in an attached read-only DB (qualified);
    # the ledger and cache ALWAYS live in the connection's writable main
    # DB (the sidecar) — unqualified names resolve there.
    pop = _qualified(_POPULATION_SOURCE, _qualifier)
    q = ("SELECT p.actual_pts_per_min FROM %s p "
         "JOIN competition_ledger l ON l.source_game_id = p.source_game_id "
         "WHERE p.status='VALID' AND p.actual_pts_per_min IS NOT NULL "
         "AND p.classification = ? "                  # ← provider partition
         "AND l.provider = ? "                        # ← ledger agreement
         "AND l.competition = ? "                     # ← competition partition
         "AND l.status = 'classified' "               # conflicts/unknown excluded
         "AND p.period_label = ? "                    # ← period partition
         "AND p.progress_pct IS NOT NULL "
         "AND p.progress_pct >= 0 AND p.progress_pct <= 100 "
         "AND p.captured_at < ? ") % (pop,)
    params: list = [family, provider, competition,
                    {"Q1": "1st Quarter", "Q2": "2nd Quarter",
                     "Q3": "3rd Quarter", "Q4": "4th Quarter"}[qbucket],
                    cutoff_iso]
    if cutoff_id is not None:
        q += "AND p.id != ? "
        params.append(cutoff_id)
    if exclude_game:
        q += "AND p.source_game_id != ? "
        params.append(exclude_game)
    # rebuild the exact bucket filter from the key (single source of truth)
    lo = int(pbucket[1:])
    q += "AND p.progress_pct >= ? AND p.progress_pct < ? "
    params += [float(lo), float(lo + 5)]
    rows = [r[0] for r in conn.execute(q, params).fetchall()]
    n = len(rows)
    if n == 0:
        return PaceBenchmark(key, 0, None, None)
    mean = sum(rows) / n
    if n < 2:
        return PaceBenchmark(key, n, round(mean, 6), None)
    var = sum((x - mean) ** 2 for x in rows) / (n - 1)  # sample σ (n−1)
    return PaceBenchmark(key, n, round(mean, 6), round(math.sqrt(var), 6))


def benchmark_for(conn: sqlite3.Connection, provider: str,
                  competition: str, progress_pct: float,
                  cutoff_captured_at: str,
                  cutoff_observation_id: Optional[int] = None,
                  exclude_game_id: Optional[str] = None,
                  use_cache: bool = True,
                  period_label: Optional[str] = None,
                  _qualifier: Optional[str] = None) -> PaceBenchmark:
    """Benchmark of the population STRICTLY BEFORE (cutoff_captured_at,
    cutoff_observation_id), partitioned by (provider, competition,
    period, progress).  Cache rows are keyed by the full cutoff, so a
    cache hit can never leak a later observation into an earlier T."""
    key = benchmark_key(provider, competition, progress_pct, period_label)
    if key is None:
        return PaceBenchmark("%s|%s|INELIGIBLE" % (provider or "UNKNOWN",
                                                   competition or "UNKNOWN"),
                             0, None, None)
    if use_cache:
        cache = "pace_benchmark_cache"   # always the writable main DB
        row = conn.execute(
            "SELECT n, mean_pace, std_pace FROM %s " % cache
            + "WHERE benchmark_key=? AND cutoff_captured_at=? AND "
            "(cutoff_observation_id IS ? OR cutoff_observation_id=?)",
            (key, cutoff_captured_at, cutoff_observation_id, cutoff_observation_id),
        ).fetchone()
        if row:
            return PaceBenchmark(key, row[0], row[1], row[2])
    bench = _stats(conn, key, cutoff_captured_at, cutoff_observation_id,
                   exclude_game_id, _qualifier)
    if use_cache:
        conn.execute(
            "INSERT OR REPLACE INTO %s VALUES (?,?,?,?,?,?,?)" % cache,
            (key, cutoff_captured_at, cutoff_observation_id, bench.n,
             bench.mean, bench.std,
             cutoff_captured_at))
        conn.commit()
    return bench


def pace_z(conn: sqlite3.Connection, actual_pace_value: Optional[float],
           provider: str, competition: str, progress_pct: float,
           cutoff_captured_at: str,
           cutoff_observation_id: Optional[int] = None,
           exclude_game_id: Optional[str] = None,
           use_cache: bool = True,
           period_label: Optional[str] = None,
           _qualifier: Optional[str] = None) -> Optional[PaceZ]:
    """z = (x − μ) / σ against the strictly-prior (provider, competition,
    period, progress) population.  Returns PaceZ(z=None, ...) when pace,
    μ, σ or n are insufficient — an honest gap, never a fabricated
    number."""
    key = benchmark_key(provider, competition, progress_pct, period_label) or \
        "%s|%s|INELIGIBLE" % (provider or "UNKNOWN", competition or "UNKNOWN")
    bench = benchmark_for(conn, provider, competition, progress_pct,
                          cutoff_captured_at, cutoff_observation_id,
                          exclude_game_id, use_cache, period_label,
                          _qualifier)
    if (actual_pace_value is None or bench.mean is None
            or bench.std is None or bench.std <= 0
            or bench.n < MIN_BENCHMARK_N):
        return PaceZ(None, key, bench.n, bench.mean, bench.std,
                     actual_pace_value)
    z = round((float(actual_pace_value) - bench.mean) / bench.std, 4)
    return PaceZ(z, key, bench.n, bench.mean, bench.std, actual_pace_value)
