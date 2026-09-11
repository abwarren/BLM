"""Historical context — league/state-relative UNDER statistics layer.

READ-ONLY descriptive infrastructure under the frozen architecture.  For
one game's CURRENT live observation this module retrieves the historical
outcome distribution of the matching (provider, competition, period,
progress-bucket) population from the CLEAN archive:

  population : clean_projections rows, status='VALID', with a live line,
               a resolvable clock and remaining_game_minutes >= 2.5
               (analytical eligibility — 2.50 is INCLUDED; this is NOT a
               terminal-state rule)
  matching   : same canonical benchmark key the live engine uses
               (provider | competition | period | 5-pt progress bucket),
               via the competition ledger — never a global average, never
               a cross-competition population
  served as  : the state-mean benchmark identity is exposed EXPLICITLY as
               state_mean_provider / _competition / _period / _state /
               _key / _n / _pace.  state_mean_n and state_mean_pace are the
               actual N and mean of THIS computation.  They are NOT the
               Z-score benchmark population: both share the benchmark_for
               definition, but each resolves at its OWN cutoff observation
               row (the Z path requires a non-null actual pace), so the two
               N's may legitimately differ and must never be conflated.
               benchmark_n / historical_avg_pace remain as documented
               aliases of state_mean_n / state_mean_pace.
  prior only : benchmark statistics for an observation at time T use
               STRICTLY prior observations (captured_at < T, own row
               excluded) for actual-vs-average classification; the
               OUTCOME statistics (UNDER rate) describe the settled
               population of that same key (hindsight, like the archive)
  mature     : n >= MIN_BENCHMARK_N (30); otherwise the explicit
               ``no_mature_historical_context`` state — no fallback to
               another competition, another bucket or a global average,
               and no fabrication

Definitions (authoritative, unchanged):
  actual_pace   = total_points / elapsed_game_minutes
  required_pace = (live_total_line - total_points) / remaining_game_minutes
  pace_gap      = required_pace - actual_pace        (NOT redefined)
  UNDER         = final_total < live_total_line

Primary historical state (frozen forensic audit, 2026-09-11 archive):
  actual_pace < own-state average  AND  required_pace >= own-state average
  → 69.75% observation UNDER / 73.02% equal-game UNDER (1,515 games).
  Served as DISPLAY constants (PRIMARY_*); the per-observation state is
  always evaluated live.  Key-level hindsight rates are served under
  explicit key_hindsight_* names and must never be conflated with the
  primary-cell qualifying rates (forensic AUDIT J finding).
Z remains secondary context; nothing here is a prediction, probability,
edge or fair value — observed historical frequencies only.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from blm_v4.live_analytics.benchmark import MIN_BENCHMARK_N
from blm_v4.live_analytics.service import attach_clean_ro
from blm_v4.live_analytics.league import canonical_competition, \
    register_game_competition

ANALYTICAL_MIN_REMAINING_MINUTES = 2.5   # inclusive; 2.50 IS eligible

FAMILY_OF_PROVIDER = {"BETUAL": "BETUAL_NBA", "CYBER": "CYBER_2K26"}

# ── FROZEN FORENSIC REFERENCE (2026-09-11 archive) ──────────────────────────
# The independent forensic audit (forensic_relative_pace_audit_2026-09-11,
# regression-frozen in tests/test_forensic_relative_pace_freeze.py) measured
# the PRIMARY relative-pace condition — actual pace < own-state prior mean
# AND required pace >= own-state prior mean — over the whole eligible
# archive.  These are DISPLAY constants for the live UI's context block;
# they are NOT recomputed here and never replace the per-observation
# state evaluation (which is always computed live from the authoritative
# payload).  The live UI must label them as frozen whole-archive values.
PRIMARY_UNDER_PCT = 69.75            # observation-weighted UNDER rate
PRIMARY_EQUAL_GAME_UNDER_PCT = 73.02 # equal-game-weighted UNDER rate
PRIMARY_GAMES = 1515                 # settled games in the primary cell
PRIMARY_OBSERVATIONS = 12444         # decisive observations in the cell
ARCHIVE_BASELINE_UNDER_PCT = 49.79   # all-observation baseline rate
# cutoff-keyed pace_benchmark_cache rows written before this instant belong
# to earlier archive vintages; the live mean for a game must be computed
# fresh at its own cutoff (use_cache=False) so the displayed state mean is
# never a stale snapshot.
FROZEN_AUDIT_UTC = "2026-09-11T13:05:48Z"

_CTX_SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_context_cache (
    benchmark_key   TEXT NOT NULL,   -- provider|competition|Qn|Pnnn
    n               INTEGER NOT NULL,
    under_n         INTEGER,
    equal_game_under_pct REAL,
    under_pct       REAL,
    over_pct        REAL,
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (benchmark_key)
);
"""

_CURRENT_ROW_SQL = """
SELECT id, source_game_id, classification, captured_at, period_label,
       clock, progress_pct, elapsed_game_minutes,
       remaining_game_minutes, current_total_points, live_total_line,
       actual_pts_per_min, required_pts_per_min, pace_gap
  FROM clean_projections
 WHERE source_game_id = ? AND status = 'VALID'
 ORDER BY captured_at DESC, id DESC
 LIMIT 1
"""


def _sidecar_path(clean_path: Path) -> Path:
    return Path(str(clean_path) + ".live_analytics.db")


def ensure_ctx_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_CTX_SCHEMA)
    conn.commit()


class HistoricalContextEngine:
    """Cached historical-context lookups keyed by the canonical benchmark
    key.  The cache is a (key → stats) map refreshed lazily; per-game
    work is a cheap key lookup + two small indexed queries — never a
    per-poll historical aggregation."""

    def __init__(self, clean_path: Path):
        self.clean_path = Path(clean_path)
        self._lock = threading.Lock()
        self._stats: dict[str, Optional[dict]] = {}
        self._stats_loaded = False

    # ── helpers ─────────────────────────────────────────────────────────
    def _ro(self) -> sqlite3.Connection:
        conn = sqlite3.connect("file:%s?mode=ro" % self.clean_path, uri=True,
                               timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _side(self) -> sqlite3.Connection:
        side = sqlite3.connect(str(_sidecar_path(self.clean_path)),
                               timeout=30)
        side.row_factory = sqlite3.Row
        return side

    # ── population stats per canonical key (cached) ──────────────────
    def refresh_stats(self, main_conn: sqlite3.Connection) -> dict[str, Any]:
        """Rebuild the key → outcome-stats cache.

        ONE query for the whole archive: the per-key totals AND the
        equal-game rate are computed in a single grouped scan.  (The
        previous shape ran a second aggregation PER KEY — ~123 extra
        queries over the clean archive — which cost ~100s of cold-start
        latency on the live endpoint for no extra information.)  Called
        at most once per process; the result is cached in memory and the
        state evaluation itself never depends on it.
        """
        side = self._side()
        try:
            ensure_ctx_schema(side)
            side.execute(
                "ATTACH DATABASE 'file:%s?mode=ro' AS prod"
                % (self.clean_path.parent / "blm_pokerbet.db"))
            side.execute(
                "ATTACH DATABASE 'file:%s?mode=ro' AS clean"
                % self.clean_path)
            # Per (key, game) aggregate first, then roll up per key: the
            # outer AVG of per-game UNDER rates IS the equal-game rate,
            # and the summed counts give the observation-level totals.
            q = """
            SELECT bkey,
                   SUM(gn)       AS n,
                   SUM(under_n)  AS under_n,
                   SUM(push_n)   AS push_n,
                   AVG(grate) * 100 AS eg
            FROM (
                SELECT l.provider || '|' || l.competition || '|' ||
                       CASE substr(p.period_label,1,1)
                         WHEN '1' THEN 'Q1' WHEN '2' THEN 'Q2'
                         WHEN '3' THEN 'Q3' WHEN '4' THEN 'Q4' END || '|' ||
                       'P' || printf('%03d', CAST(p.progress_pct/5 AS INTEGER)*5)
                         AS bkey,
                       p.source_game_id AS gid,
                       COUNT(*) AS gn,
                       SUM(CASE WHEN g.final_total < p.live_total_line
                                THEN 1 ELSE 0 END) AS under_n,
                       SUM(CASE WHEN g.final_total = p.live_total_line
                                THEN 1 ELSE 0 END) AS push_n,
                       AVG(CASE WHEN g.final_total < p.live_total_line
                                THEN 1.0 ELSE 0.0 END) AS grate
                FROM clean_projections p
                JOIN competition_ledger l
                     ON l.source_game_id = p.source_game_id
                    AND l.status = 'classified'
                JOIN prod.game_results g ON g.source_game_id = p.source_game_id
                WHERE p.status='VALID' AND p.live_total_line IS NOT NULL
                  AND p.elapsed_game_minutes IS NOT NULL
                  AND p.remaining_game_minutes IS NOT NULL
                  AND p.remaining_game_minutes >= 2.5
                  AND p.progress_pct IS NOT NULL AND p.period_label IS NOT NULL
                  AND g.final_total IS NOT NULL
                  AND l.provider = CASE p.classification
                        WHEN 'BETUAL_NBA' THEN 'BETUAL'
                        WHEN 'CYBER_2K26' THEN 'CYBER' END
                GROUP BY bkey, gid
            )
            GROUP BY bkey
            """
            stats: dict[str, dict] = {}
            for r in side.execute(q):
                n = r["n"]
                if not n or not r["bkey"]:
                    continue
                eg = r["eg"]
                stats[r["bkey"]] = {
                    "n": n,
                    "under_n": r["under_n"],
                    "under_pct": round(r["under_n"] / n * 100, 2) if n else None,
                    "equal_game_under_pct": round(eg, 2) if eg is not None else None,
                }
            with self._lock:
                self._stats = stats
                self._stats_loaded = True
            # persist a read-only snapshot for ops inspection (sidecar only)
            side.execute("DELETE FROM historical_context_cache")
            side.executemany(
                "INSERT OR REPLACE INTO historical_context_cache "
                "(benchmark_key, n, under_n, equal_game_under_pct, "
                " under_pct, over_pct, computed_at) VALUES (?,?,?,?,?,?,?)",
                [(k, v["n"], v["under_n"], v["equal_game_under_pct"],
                  v["under_pct"],
                  round(100 - v["under_pct"], 2) if v["under_pct"] is not None else None,
                  _now_iso())
                 for k, v in stats.items() if k])
            side.commit()
            side.execute("DETACH DATABASE prod")
            side.execute("DETACH DATABASE clean")
            return {"keys": len(stats)}
        finally:
            side.close()

    def benchmark_for(self, side: sqlite3.Connection, provider: str,
                      competition: str, progress_pct: float,
                      captured_at: str, observation_id: int,
                      exclude_game_id: str,
                      period_label: str,
                      use_cache: bool = True) -> tuple[Optional[float], int]:
        """Own-state historical average pace for THIS observation via the
        authoritative benchmark (strictly prior, same-game excluded).

        ``use_cache=False`` forces the population to be computed fresh at
        this observation's own cutoff: cache rows are keyed by (key,
        cutoff, id), so a hit is always semantically correct, but rows
        written BEFORE the frozen forensic audit describe an earlier
        archive vintage — recomputing guarantees the displayed state mean
        reflects the current archive, never a stale snapshot."""
        from blm_v4.live_analytics.benchmark import benchmark_for
        bench = benchmark_for(
            side, provider, competition, progress_pct, captured_at,
            cutoff_observation_id=observation_id,
            exclude_game_id=exclude_game_id, use_cache=use_cache,
            period_label=period_label, _qualifier="clean")
        if bench.mean is None or bench.n < MIN_BENCHMARK_N:
            return None, bench.n
        return bench.mean, bench.n

    def context_for(self, main_conn: sqlite3.Connection,
                    source_game_id: str) -> dict:
        """Historical-context payload for one game's current live state.
        Returns status='matched' or 'no_mature_historical_context' —
        never a fallback, never a fabricated value.

        The key-level hindsight stats are aggregated lazily on the first
        payload request (once per process, cheap: two indexed queries,
        cached in memory + sidecar snapshot).  A failed aggregation
        leaves ``_stats_loaded`` False and is retried on the next
        request; the state evaluation itself never depends on it."""
        side = self._side()
        ro = self._ro()
        try:
            if not self._stats_loaded:
                try:
                    self.refresh_stats(main_conn)
                except Exception:
                    pass        # rates served as None; state unaffected
            row = ro.execute(_CURRENT_ROW_SQL, (str(source_game_id),)).fetchone()
            if row is None:
                return {"status": "no_mature_historical_context",
                        "reason": "no_clean_observation"}
            g = main_conn.execute(
                "SELECT competition_slug, competition_id FROM games "
                "WHERE source_game_id=?", (str(source_game_id),)).fetchone()
            slug, comp_id = (g[0], g[1]) if g else (None, None)
            reg = register_game_competition(
                side, str(row["source_game_id"]), row["classification"],
                slug, comp_id, str(row["captured_at"]))
            if reg.provider is None or reg.competition is None:
                return {"status": "no_mature_historical_context",
                        "reason": "competition_unresolved",
                        "provider": reg.provider,
                        "competition": reg.competition}
            attach_clean_ro(side, self.clean_path)
            # Fresh at the observation's own cutoff — cache rows written
            # before the frozen audit describe an older archive vintage;
            # the displayed state mean must reflect the current archive.
            avg, n = self.benchmark_for(
                side, reg.provider, reg.competition,
                row["progress_pct"], row["captured_at"],
                observation_id=int(row["id"]),
                exclude_game_id=str(row["source_game_id"]),
                period_label=row["period_label"], use_cache=False)
            with self._lock:
                stats = self._stats
            bkey = "%s|%s|%s|%s" % (
                reg.provider, reg.competition,
                _qbucket(row["period_label"]),
                _pbucket(row["progress_pct"]))
            base = {
                "provider": reg.provider, "competition": reg.competition,
                "period": row["period_label"],
                "progress_pct": row["progress_pct"],
                "state": _pbucket(row["progress_pct"]),
                "benchmark_key": bkey, "benchmark_n": n,
                # ── EXPLICIT relative-pace / historical STATE-MEAN benchmark ──
                # The benchmark that supplies the state mean the relative-pace
                # comparison reads.  It is a SEPARATE calculation from the
                # Z-score benchmark population: both are defined by
                # benchmark.benchmark_for (same key, strictly prior, own row
                # and same game excluded) but each resolves at its OWN cutoff
                # observation row, so the two N's MUST NOT be assumed equal.
                # (Measured divergence: a game whose latest VALID observation
                # carries no actual pace — the Z path skips to an older row or
                # finds none, this path uses the latest row.)  ``state_mean_n``
                # is always the real N of THIS computation — never copied from
                # the Z payload, never fabricated when the population is
                # immature.
                "state_mean_provider": reg.provider,
                "state_mean_competition": reg.competition,
                "state_mean_period": _qbucket(row["period_label"]),
                "state_mean_state": _pbucket(row["progress_pct"]),
                "state_mean_key": bkey,
                "state_mean_n": n,
                "state_mean_pace": (round(avg, 4) if avg is not None else None),
            }
            if avg is None:
                return {"status": "no_mature_historical_context", **base,
                        "reason": "insufficient_mature_population"}
            # analytical eligibility gate
            remaining = row["remaining_game_minutes"]
            if remaining is None or remaining < ANALYTICAL_MIN_REMAINING_MINUTES:
                return {"status": "no_mature_historical_context", **base,
                        "reason": "below_analytical_eligibility",
                        "remaining_game_minutes": remaining}
            actual = row["actual_pts_per_min"]
            required = row["required_pts_per_min"]
            kstats = stats.get(bkey)
            # the two comparisons are served SEPARATELY evaluated (the UI
            # must show each relationship explicitly, not only the combined
            # flag).  None-safe: a clean row can carry a live line and a
            # resolvable clock yet a MISSING actual pace (archival gap, e.g.
            # the 11,790 no-actual rows of the audit) — such a row must
            # classify as an explicit non-TRUE state, never raise.
            actual_below = bool(actual is not None and actual < avg)
            required_ge = bool(required is not None and required >= avg)
            under_state = bool(actual_below and required_ge)
            mirror_state = bool(actual is not None and required is not None
                                and actual >= avg and required < avg)
            return {
                "status": "matched",
                **base,
                "remaining_game_minutes": remaining,
                "eligible": True,
                "actual_pace": actual,
                "historical_avg_pace": round(avg, 4),
                "actual_vs_avg": (round(actual - avg, 4)
                                  if actual is not None else None),
                "required_pace": required,
                "required_vs_avg": (round(required - avg, 4)
                                    if required is not None else None),
                "pace_gap": row["pace_gap"],
                "under_state": under_state,
                "mirror_over_state": mirror_state,
                # explicit per-comparison evaluation (TRUE/FALSE for the UI)
                "actual_below_state_mean": actual_below,
                "required_ge_state_mean": required_ge,
                "both_conditions_true": bool(actual_below and required_ge),
                # server-evaluated direction of each pace against the state
                # mean — the browser renders these labels verbatim and never
                # compares the values itself.  Each label is exactly
                # consistent with the boolean below it: ACTUAL is "BELOW"
                # iff actual_below_state_mean, REQUIRED is "AT/ABOVE" iff
                # required_ge_state_mean (note the asymmetric tie-handling —
                # strictly below for ACTUAL, at-or-above for REQUIRED).
                "actual_vs_state_mean": (
                    None if actual is None
                    else ("BELOW" if actual < avg else "ABOVE")),
                "required_vs_state_mean": (
                    None if required is None
                    else ("BELOW" if required < avg else "AT/ABOVE")),
                # matched-key settled population (hindsight, key-level):
                # labelled as such — NEVER presented as the primary-cell
                # qualifying rate (two different statistics, see AUDIT J).
                "key_hindsight_under_pct": (kstats or {}).get("under_pct"),
                "key_hindsight_equal_game_under_pct":
                    (kstats or {}).get("equal_game_under_pct"),
                "key_hindsight_obs": (kstats or {}).get("n"),
                "key_hindsight_obs_under": (kstats or {}).get("under_n"),
                # frozen whole-archive PRIMARY-cell reference (display):
                # the qualifying-population rates the forensic audit froze.
                "qualifying_under_pct": PRIMARY_UNDER_PCT,
                "qualifying_equal_game_under_pct": PRIMARY_EQUAL_GAME_UNDER_PCT,
                "qualifying_games": PRIMARY_GAMES,
                "qualifying_observations": PRIMARY_OBSERVATIONS,
                "archive_baseline_under_pct": ARCHIVE_BASELINE_UNDER_PCT,
                "frozen_audit_utc": FROZEN_AUDIT_UTC,
            }
        finally:
            ro.close()
            side.close()


def _qbucket(period_label: Optional[str]) -> Optional[str]:
    s = (period_label or "").strip()
    for i in (1, 2, 3, 4):
        if s.startswith(f"{i}"):
            return f"Q{i}"
    return None


def _pbucket(progress_pct: Optional[float]) -> Optional[str]:
    if progress_pct is None:
        return None
    return "P%03d" % (int(progress_pct // 5) * 5)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def statistics_mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0
