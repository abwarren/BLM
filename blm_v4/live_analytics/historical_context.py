"""League/state-relative descriptive historical context.

This module is deliberately descriptive.  It answers whether the current
observed pace state resembles historical observations that subsequently
settled UNDER, using the same benchmark population rules as benchmark.py.

Eligibility boundary:
    remaining_game_minutes >= 2.5

Historical population key:
    PROVIDER | COMPETITION | PERIOD | 5-point PROGRESS bucket

There is no global-average fallback and no cross-competition fallback.
A benchmark requires at least MIN_BENCHMARK_N prior observations.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from blm_v4.live_analytics.benchmark import MIN_BENCHMARK_N, benchmark_for
from blm_v4.live_analytics.league import UNKNOWN, canonical_competition, ensure_league_schema

MIN_REMAINING_MINUTES = 2.5


@dataclass(frozen=True)
class HistoricalContext:
    status: str
    benchmark_key: Optional[str] = None
    provider: Optional[str] = None
    competition: Optional[str] = None
    period: Optional[str] = None
    progress_pct: Optional[float] = None
    remaining_game_minutes: Optional[float] = None
    actual_pace: Optional[float] = None
    required_pace: Optional[float] = None
    pace_gap: Optional[float] = None
    mean_pace: Optional[float] = None
    benchmark_n: int = 0
    actual_vs_avg: Optional[float] = None
    required_vs_avg: Optional[float] = None
    under_state: bool = False
    mirror_over_state: bool = False
    historical_under_pct: Optional[float] = None
    historical_game_under_pct: Optional[float] = None
    historical_obs: int = 0
    historical_games: int = 0


def eligible_remaining(remaining_game_minutes: Optional[float]) -> bool:
    """Return whether an observation is inside the analytical time window."""
    return (
        remaining_game_minutes is not None
        and float(remaining_game_minutes) >= MIN_REMAINING_MINUTES
    )


class HistoricalContextEngine:
    """Cached descriptive historical context over clean projections.

    ``clean_path`` is read-only.  The supplied writable ``sidecar`` owns the
    competition ledger and benchmark cache, matching the existing pace-Z
    service architecture.
    """

    def __init__(self, clean_path: Path, sidecar: sqlite3.Connection):
        self.clean_path = Path(clean_path)
        self.sidecar = sidecar
        ensure_league_schema(sidecar)
        from blm_v4.live_analytics.benchmark import ensure_schema
        ensure_schema(sidecar)
        self._cache: dict[tuple, HistoricalContext] = {}

    def _attach_clean(self) -> None:
        exists = self.sidecar.execute(
            "SELECT COUNT(*) FROM pragma_database_list WHERE name='clean'"
        ).fetchone()[0]
        if not exists:
            self.sidecar.execute(
                "ATTACH DATABASE 'file:%s?mode=ro' AS clean" % self.clean_path
            )

    def _row(self, game_id: str) -> Optional[sqlite3.Row]:
        self._attach_clean()
        conn = self.sidecar
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """SELECT id, source_game_id, classification, captured_at,
                      period_label, progress_pct, remaining_game_minutes,
                      actual_pts_per_min, required_pts_per_min, pace_gap,
                      live_total_line, current_total_points
               FROM clean.clean_projections
               WHERE source_game_id=? AND status='VALID'
               ORDER BY captured_at DESC, id DESC LIMIT 1""",
            (str(game_id),),
        ).fetchone()

    def _competition(self, main_conn: sqlite3.Connection, row: sqlite3.Row):
        g = main_conn.execute(
            "SELECT competition_slug, competition_id FROM games WHERE source_game_id=?",
            (str(row["source_game_id"]),),
        ).fetchone()
        slug, cid = (g[0], g[1]) if g else (None, None)
        return canonical_competition(row["classification"], slug, cid)

    def context_for(self, main_conn: sqlite3.Connection, game_id: str) -> HistoricalContext:
        row = self._row(game_id)
        if row is None:
            return HistoricalContext(status="no_observations")
        rem = row["remaining_game_minutes"]
        if not eligible_remaining(rem):
            return HistoricalContext(
                status="excluded_remaining_lt_2_5",
                remaining_game_minutes=rem,
                actual_pace=row["actual_pts_per_min"],
                required_pace=row["required_pts_per_min"],
                pace_gap=row["pace_gap"],
                period=row["period_label"],
                progress_pct=row["progress_pct"],
            )

        reg = self._competition(main_conn, row)
        if reg.provider in (None, UNKNOWN) or reg.competition in (None, UNKNOWN):
            return HistoricalContext(
                status="no_validated_competition",
                period=row["period_label"],
                progress_pct=row["progress_pct"],
                remaining_game_minutes=rem,
                actual_pace=row["actual_pts_per_min"],
                required_pace=row["required_pts_per_min"],
                pace_gap=row["pace_gap"],
            )

        # benchmark_for is the single source of truth for the historical
        # mean and its strict prior/current-game exclusion semantics.
        bench = benchmark_for(
            self.sidecar,
            reg.provider,
            reg.competition,
            float(row["progress_pct"]),
            str(row["captured_at"]),
            cutoff_observation_id=int(row["id"]),
            exclude_game_id=str(row["source_game_id"]),
            use_cache=True,
            period_label=row["period_label"],
            _qualifier="clean",
        )
        if bench.n < MIN_BENCHMARK_N or bench.mean is None:
            return HistoricalContext(
                status="no_mature_historical_context",
                benchmark_key=bench.key,
                provider=reg.provider,
                competition=reg.competition,
                period=row["period_label"],
                progress_pct=row["progress_pct"],
                remaining_game_minutes=rem,
                actual_pace=row["actual_pts_per_min"],
                required_pace=row["required_pts_per_min"],
                pace_gap=row["pace_gap"],
                mean_pace=bench.mean,
                benchmark_n=bench.n,
            )

        actual = row["actual_pts_per_min"]
        required = row["required_pts_per_min"]
        actual_vs = None if actual is None else float(actual) - float(bench.mean)
        required_vs = None if required is None else float(required) - float(bench.mean)
        under_state = actual_vs is not None and required_vs is not None and actual_vs < 0 and required_vs >= 0
        mirror = actual_vs is not None and required_vs is not None and actual_vs >= 0 and required_vs < 0

        under_pct, game_under_pct, obs_n, game_n = self._outcomes(
            row, reg.provider, reg.competition, bench.key,
        )
        return HistoricalContext(
            status="matched",
            benchmark_key=bench.key,
            provider=reg.provider,
            competition=reg.competition,
            period=row["period_label"],
            progress_pct=row["progress_pct"],
            remaining_game_minutes=rem,
            actual_pace=actual,
            required_pace=required,
            pace_gap=row["pace_gap"],
            mean_pace=bench.mean,
            benchmark_n=bench.n,
            actual_vs_avg=actual_vs,
            required_vs_avg=required_vs,
            under_state=under_state,
            mirror_over_state=mirror,
            historical_under_pct=under_pct,
            historical_game_under_pct=game_under_pct,
            historical_obs=obs_n,
            historical_games=game_n,
        )

    def _outcomes(self, row: sqlite3.Row, provider: str, competition: str, key: str):
        """Return empirical UNDER rates for the exact benchmark population.

        The outcome join is read-only and uses the same provider/competition/
        period/progress filters as benchmark.py.  The current game and all
        observations from it are excluded.  The final total is compared with
        each historical observation's stored live total line.
        """
        qbucket = {"Q1": "1st Quarter", "Q2": "2nd Quarter",
                   "Q3": "3rd Quarter", "Q4": "4th Quarter"}[key.split("|")[2]]
        lo = int(key.split("|")[3][1:])
        hi = lo + 5
        family = {"BETUAL": "BETUAL_NBA", "CYBER": "CYBER_2K26"}[provider]
        rows = self.sidecar.execute(
            """SELECT p.source_game_id, p.live_total_line, r.final_total
               FROM clean.clean_projections p
               JOIN competition_ledger l ON l.source_game_id=p.source_game_id
               JOIN main_game_results r ON r.source_game_id=p.source_game_id
               WHERE p.status='VALID' AND p.actual_pts_per_min IS NOT NULL
                 AND p.live_total_line IS NOT NULL AND r.final_total IS NOT NULL
                 AND p.classification=? AND l.provider=? AND l.competition=?
                 AND l.status='classified' AND p.period_label=?
                 AND p.progress_pct>=? AND p.progress_pct<?
                 AND p.captured_at<? AND p.source_game_id!=?
                 AND p.remaining_game_minutes>=2.5""",
            (family, provider, competition, qbucket, float(lo), float(hi),
             str(row["captured_at"]), str(row["source_game_id"])),
        ).fetchall()
        # The production clean DB is attached as `clean`; outcomes live in
        # the main DB.  A temporary attached alias is created by the service
        # caller when this method is used outside tests.
        if not rows:
            return None, None, 0, 0
        obs_under = sum(1 for _, line, final in rows if float(final) < float(line))
        by_game: dict[str, list[bool]] = {}
        for gid, line, final in rows:
            by_game.setdefault(str(gid), []).append(float(final) < float(line))
        game_rates = [sum(vals) / len(vals) for vals in by_game.values()]
        return (
            round(100.0 * obs_under / len(rows), 2),
            round(100.0 * sum(game_rates) / len(game_rates), 2),
            len(rows),
            len(by_game),
        )
