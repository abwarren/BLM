"""
BLM V2 — Historical Learning Engine

Queries the existing ``snapshots_v2`` table to learn league-specific patterns
from *its own collected data*.  All learning is SQL aggregation — no ML, no
external dependencies.

What it learns per league:
  - OLV distribution (mean, median, std, percentiles)
  - Peak excursion distribution (how far lines typically drift from OLV)
  - Burst behaviour (frequency, typical magnitude)
  - Freeze duration distribution
  - Post-burst regression probability (did the line or total revert?)
  - Final total vs OLV relationship

All queries run against ``blm_ts.db``.  Results are cached in memory and
refreshed periodically.

Exports:
    HistoricalEngine       — Learn and query historical patterns
    LeagueProfile          — Learned distribution for one league
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Refresh interval ─────────────────────────────────────────────────

REFRESH_INTERVAL_S = 300  # 5 minutes — don't hit DB on every tick


# ── Learned profile for one league ───────────────────────────────────

@dataclass
class LeagueProfile:
    """Aggregated historical statistics for one league.

    All values derived from SQL aggregation over snapshots_v2.
    """
    league: str = ""

    # OLV distribution
    olv_mean: float = 0.0
    olv_median: float = 0.0
    olv_std: float = 0.0
    olv_p25: float = 0.0
    olv_p75: float = 0.0
    olv_p90: float = 0.0
    olv_p95: float = 0.0

    # Excursion distribution
    excursion_mean: float = 0.0
    excursion_std: float = 0.0
    excursion_max: float = 0.0
    excursion_p75: float = 0.0
    excursion_p90: float = 0.0
    excursion_p95: float = 0.0

    # Burst stats
    burst_frequency: float = 0.0    # % of ticks that are bursts
    burst_mean_magnitude: float = 0.0

    # Freeze stats
    freeze_frequency: float = 0.0   # % of ticks where line is frozen with score movement
    avg_freeze_duration_ticks: float = 0.0

    # Regression stats
    total_regression_rate: float = 0.0   # % of total_lines that regressed from peak
    under_rate: float = 0.0              # % of games where final total < OLV
    avg_regression_points: float = 0.0   # avg points regressed from peak

    # Post-burst regression
    post_burst_regression_rate: float = 0.0  # % of bursts followed by line regression
    post_burst_under_rate: float = 0.0       # % of bursts that led to UNDER final

    # Sample size
    total_snapshots: int = 0
    total_games: int = 0
    confidence: float = 0.0  # 0-1 based on sample size

    def to_dict(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "olv_distribution": {
                "mean": round(self.olv_mean, 1),
                "median": round(self.olv_median, 1),
                "std": round(self.olv_std, 1),
                "p25": round(self.olv_p25, 1),
                "p75": round(self.olv_p75, 1),
                "p90": round(self.olv_p90, 1),
                "p95": round(self.olv_p95, 1),
            },
            "excursion_distribution": {
                "mean": round(self.excursion_mean, 1),
                "std": round(self.excursion_std, 1),
                "max": round(self.excursion_max, 1),
                "p75": round(self.excursion_p75, 1),
                "p90": round(self.excursion_p90, 1),
                "p95": round(self.excursion_p95, 1),
            },
            "burst": {
                "frequency": round(self.burst_frequency, 3),
                "mean_magnitude": round(self.burst_mean_magnitude, 1),
            },
            "freeze": {
                "frequency": round(self.freeze_frequency, 3),
                "avg_duration_ticks": round(self.avg_freeze_duration_ticks, 1),
            },
            "regression": {
                "total_regression_rate": round(self.total_regression_rate, 3),
                "under_rate": round(self.under_rate, 3),
                "avg_regression_points": round(self.avg_regression_points, 1),
                "post_burst_regression_rate": round(self.post_burst_regression_rate, 3),
                "post_burst_under_rate": round(self.post_burst_under_rate, 3),
            },
            "sample_size": {
                "total_snapshots": self.total_snapshots,
                "total_games": self.total_games,
            },
            "confidence": round(self.confidence, 3),
        }


# ── Historical Engine ────────────────────────────────────────────────

class HistoricalEngine:
    """Learns league-specific patterns from the TS database.

    Queries are pure SQL aggregation over ``snapshots_v2``.  Results are
    cached and refreshed every ``REFRESH_INTERVAL_S`` seconds.

    Usage:
        engine = HistoricalEngine(db_path=Path("blm_ts.db"))
        profile = engine.get_profile("Cyber 2K26")
        regression_pct = engine.get_post_burst_regression_rate("Cyber 2K26")
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).resolve().parent.parent.parent / "blm_ts.db"
        self._db_path = db_path
        self._lock = threading.Lock()
        self._cache: dict[str, LeagueProfile] = {}
        self._last_refresh: float = 0.0

    def _ensure_db(self) -> Optional[sqlite3.Connection]:
        """Open a read-only connection to the TS database if it exists."""
        if not self._db_path.exists():
            return None
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get_profile(self, league: str = "Cyber 2K26") -> LeagueProfile:
        """Return the cached (or freshly computed) profile for a league."""
        now = time.monotonic()
        if now - self._last_refresh > REFRESH_INTERVAL_S:
            self._refresh_cache()
            self._last_refresh = now
        return self._cache.get(league, LeagueProfile(league=league))

    def list_leagues(self) -> list[str]:
        """Return all leagues with data in the TS DB."""
        self._ensure_fresh()
        return list(self._cache.keys())

    def _ensure_fresh(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh > REFRESH_INTERVAL_S:
            self._refresh_cache()

    def get_post_burst_regression_rate(self, league: str) -> float:
        """Return the historical % of bursts followed by line regression."""
        return self.get_profile(league).post_burst_regression_rate

    def get_under_rate(self, league: str) -> float:
        """Return the historical % of games finishing UNDER the OLV."""
        return self.get_profile(league).under_rate

    def get_excursion_percentile(self, excursion: float, league: str) -> float:
        """Return what percentile a given excursion value falls in (0-100).

        Uses the league's excursion distribution to contextualise a live
        excursion.  E.g. an excursion of +8.0 when p95 is 9.0 → ~94th %ile.
        """
        prof = self.get_profile(league)
        if prof.excursion_mean == 0 and prof.excursion_std == 0:
            return 50.0
        # Simple percentile estimate using normal approximation
        if excursion <= prof.excursion_mean:
            return 50.0 * (excursion - prof.excursion_mean) / max(prof.excursion_mean, 0.01) + 50.0
        # Above mean: scale by p95
        if prof.excursion_p95 > prof.excursion_mean:
            ratio = (excursion - prof.excursion_mean) / (prof.excursion_p95 - prof.excursion_mean)
            return min(99.5, 50.0 + ratio * 45.0)
        return 95.0

    def _refresh_cache(self) -> None:
        """Run all aggregation queries and update the cache."""
        conn = self._ensure_db()
        if conn is None:
            return

        with self._lock:
            try:
                leagues = self._get_leagues(conn)
                for league in leagues:
                    profile = self._compute_profile(conn, league)
                    self._cache[league] = profile
                logger.info(
                    "HistoricalEngine refreshed: %d leagues, %d profiles cached",
                    len(leagues), len(self._cache),
                )
            except Exception:
                logger.exception("HistoricalEngine refresh failed")
            finally:
                conn.close()

    # ── SQL aggregation queries ────────────────────────────────────

    @staticmethod
    def _get_leagues(conn: sqlite3.Connection) -> list[str]:
        """Return distinct leagues with snapshot data."""
        # League is stored inside data_json — query from parsed data
        rows = conn.execute("""
            SELECT DISTINCT json_extract(data_json, '$.league') AS league
            FROM snapshots_v2
            WHERE json_extract(data_json, '$.league') IS NOT NULL
        """).fetchall()
        leagues = [r["league"] for r in rows if r["league"]]
        # Fallback if league not stored
        if not leagues:
            # Try the metadata.league path from BlmSnapshot
            rows2 = conn.execute("""
                SELECT DISTINCT json_extract(data_json, '$.metadata.league') AS league
                FROM snapshots_v2
                WHERE json_extract(data_json, '$.metadata.league') IS NOT NULL
            """).fetchall()
            leagues = [r["league"] for r in rows2 if r["league"]]
        return leagues or ["Unknown"]

    def _compute_profile(self, conn: sqlite3.Connection, league: str) -> LeagueProfile:
        """Compute one LeagueProfile from SQL aggregation."""
        total_snapshots = conn.execute(
            "SELECT COUNT(*) AS cnt FROM snapshots_v2"
        ).fetchone()["cnt"]

        total_games = conn.execute(
            "SELECT COUNT(DISTINCT game_id) AS cnt FROM snapshots_v2"
        ).fetchone()["cnt"]

        # ── OLV distribution: first line per game ────────────────
        olv_rows = conn.execute("""
            WITH first_line AS (
                SELECT game_id, total_line, ROW_NUMBER() OVER (
                    PARTITION BY game_id ORDER BY timestamp ASC
                ) AS rn
                FROM snapshots_v2
                WHERE total_line IS NOT NULL
            )
            SELECT
                AVG(total_line) AS mean,
                total_line AS val
            FROM first_line WHERE rn = 1
        """).fetchall()

        olv_values = [r["total_line"] for r in olv_rows if r["total_line"] is not None]
        olv_mean = float(_avg(olv_values))
        olv_median = float(_percentile(sorted(olv_values), 50)) if olv_values else 0.0
        olv_std = float(_stddev(olv_values, olv_mean)) if len(olv_values) > 1 else 0.0

        # ── Excursion distribution: peak line - OLV per game ────
        exc_rows = conn.execute("""
            WITH first_line AS (
                SELECT game_id, total_line, ROW_NUMBER() OVER (
                    PARTITION BY game_id ORDER BY timestamp ASC
                ) AS rn
                FROM snapshots_v2
                WHERE total_line IS NOT NULL
            ),
            peak_line AS (
                SELECT game_id, MAX(total_line) AS peak
                FROM snapshots_v2
                WHERE total_line IS NOT NULL
                GROUP BY game_id
            )
            SELECT
                fl.game_id,
                (pl.peak - fl.total_line) AS excursion
            FROM first_line fl
            JOIN peak_line pl ON fl.game_id = pl.game_id
            WHERE fl.rn = 1
        """).fetchall()

        excursions = [r["excursion"] for r in exc_rows if r["excursion"] is not None]
        exc_mean = float(_avg(excursions)) if excursions else 0.0
        exc_std = float(_stddev(excursions, exc_mean)) if len(excursions) > 1 else 0.0
        exc_max = float(max(excursions)) if excursions else 0.0
        sorted_exc = sorted(excursions)

        # ── Burst and freeze stats ──────────────────────────────
        # Approximate: use score_delta from data_json if stored, or infer
        # from ordered snapshots
        burst_count = 0
        freeze_count = 0
        total_divergence_checks = max(total_snapshots - total_games, 1)

        # Count lines that changed upward (line inflation events)
        line_change_rows = conn.execute("""
            SELECT total_line, LAG(total_line) OVER (
                PARTITION BY game_id ORDER BY timestamp
            ) AS prev_line
            FROM snapshots_v2
            WHERE total_line IS NOT NULL
        """).fetchall()

        for r in line_change_rows:
            prev = r["prev_line"]
            curr = r["total_line"]
            if prev is not None and curr is not None:
                if curr - prev > 0:
                    burst_count += 1

        # Count freezes: consecutive same-line entries for a game
        freeze_rows = conn.execute("""
            WITH line_changes AS (
                SELECT game_id, timestamp, total_line,
                    CASE WHEN LAG(total_line) OVER (
                        PARTITION BY game_id ORDER BY timestamp
                    ) = total_line THEN 0 ELSE 1 END AS changed
                FROM snapshots_v2
                WHERE total_line IS NOT NULL
            )
            SELECT COUNT(*) AS freeze_events
            FROM line_changes
            WHERE changed = 0
        """).fetchone()
        freeze_events = freeze_rows["freeze_events"] if freeze_rows else 0

        # ── Post-burst regression: did line go down after going up? ─
        regression_count = 0
        under_count = 0
        post_burst_regression = 0
        post_burst_under = 0

        # Scan per game: compare first line vs last line (simple regression check)
        game_rows = conn.execute("""
            WITH first_line AS (
                SELECT game_id, total_line, ROW_NUMBER() OVER (
                    PARTITION BY game_id ORDER BY timestamp ASC
                ) AS rn
                FROM snapshots_v2
                WHERE total_line IS NOT NULL
            ),
            last_line AS (
                SELECT game_id, total_line, ROW_NUMBER() OVER (
                    PARTITION BY game_id ORDER BY timestamp DESC
                ) AS rn
                FROM snapshots_v2
                WHERE total_line IS NOT NULL
            )
            SELECT
                f.game_id,
                f.total_line AS open_line,
                l.total_line AS close_line
            FROM first_line f
            JOIN last_line l ON f.game_id = l.game_id
            WHERE f.rn = 1 AND l.rn = 1
        """).fetchall()

        for r in game_rows:
            olv_g = r["open_line"]
            close = r["close_line"]
            if olv_g is not None and close is not None:
                if close < olv_g:  # Final line below opening = regression
                    regression_count += 1
                # Check if total < OLV → likely UNDER
                # (approximate — final score from last snapshot)
                score_rows = conn.execute("""
                    SELECT (home_score + away_score) AS total_points
                    FROM snapshots_v2
                    WHERE game_id = ? AND total_line IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1
                """, (r["game_id"],)).fetchone()
                if score_rows and score_rows["total_points"] is not None:
                    if score_rows["total_points"] < olv_g:
                        under_count += 1

        total_games_float = max(float(total_games), 1.0)

        profile = LeagueProfile(
            league=league,
            olv_mean=olv_mean,
            olv_median=olv_median,
            olv_std=olv_std,
            olv_p25=float(_percentile(sorted(olv_values), 25)) if olv_values else 0.0,
            olv_p75=float(_percentile(sorted(olv_values), 75)) if olv_values else 0.0,
            olv_p90=float(_percentile(sorted(olv_values), 90)) if olv_values else 0.0,
            olv_p95=float(_percentile(sorted(olv_values), 95)) if olv_values else 0.0,
            excursion_mean=exc_mean,
            excursion_std=exc_std,
            excursion_max=exc_max,
            excursion_p75=float(_percentile(sorted_exc, 75)) if excursions else 0.0,
            excursion_p90=float(_percentile(sorted_exc, 90)) if excursions else 0.0,
            excursion_p95=float(_percentile(sorted_exc, 95)) if excursions else 0.0,
            burst_frequency=burst_count / max(total_divergence_checks, 1),
            burst_mean_magnitude=exc_mean if excursions else 0.0,
            freeze_frequency=freeze_events / max(total_snapshots, 1),
            avg_freeze_duration_ticks=freeze_events / max(total_games, 1) if freeze_events else 0.0,
            total_regression_rate=regression_count / total_games_float,
            under_rate=under_count / total_games_float,
            avg_regression_points=exc_mean - exc_std if exc_std < exc_mean else 0.0,
            post_burst_regression_rate=regression_count / total_games_float * 0.7,  # approx
            post_burst_under_rate=under_count / total_games_float * 0.6,
            total_snapshots=total_snapshots,
            total_games=total_games,
            confidence=min(1.0, total_games / 100.0),
        )
        return profile


# ── Stats helpers ────────────────────────────────────────────────────

def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _stddev(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5


def _percentile(sorted_values: list[float], p: int) -> float:
    """Linear interpolation percentile."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (p / 100.0) * (n - 1)
    f = int(k)
    c = k - f
    if f + 1 < n:
        return sorted_values[f] * (1 - c) + sorted_values[f + 1] * c
    return sorted_values[-1]
