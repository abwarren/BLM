"""League/state-relative descriptive historical context.

Eligibility is ``remaining_game_minutes >= 2.5``.  Historical pace means are
resolved by the existing benchmark.py population key:
PROVIDER | COMPETITION | PERIOD | 5-point PROGRESS bucket.

No global-average or cross-competition fallback is permitted.
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
    """Exactly 2.50 minutes is eligible; anything below it is excluded."""
    return remaining_game_minutes is not None and float(remaining_game_minutes) >= MIN_REMAINING_MINUTES


class HistoricalContextEngine:
    """Read-only historical context using the existing benchmark semantics."""

    def __init__(self, clean_path: Path, sidecar: sqlite3.Connection):
        self.clean_path = Path(clean_path)
        self.sidecar = sidecar
        ensure_league_schema(sidecar)
        from blm_v4.live_analytics.benchmark import ensure_schema
        ensure_schema(sidecar)

    def _attach_clean(self) -> None:
        if not self.sidecar.execute(
            "SELECT COUNT(*) FROM pragma_database_list WHERE name='clean'"
        ).fetchone()[0]:
            self.sidecar.execute(
                "ATTACH DATABASE 'file:%s?mode=ro' AS clean" % self.clean_path
            )

    def _attach_main(self, main_conn: sqlite3.Connection) -> None:
        """Attach the same production DB used by the API, read-only."""
        if self.sidecar.execute(
            "SELECT COUNT(*) FROM pragma_database_list WHERE name='prod'"
        ).fetchone()[0]:
            return
        path_row = main_conn.execute("PRAGMA database_list").fetchall()
        main_path = next((r[2] for r in path_row if r[1] == "main"), None)
        if not main_path:
            raise RuntimeError("cannot resolve production DB path")
        self.sidecar.execute(
            "ATTACH DATABASE 'file:%s?mode=ro' AS prod" % main_path
        )

    def _row(self, game_id: str) -> Optional[sqlite3.Row]:
        self._attach_clean()
        self.sidecar.row_factory = sqlite3.Row
        return self.sidecar.execute(
            """SELECT id, source_game_id, classification, captured_at,
                      period_label, progress_pct, remaining_game_minutes,
                      actual_pts_per_min, required_pts_per_min, pace_gap
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
                period=row["period_label"], progress_pct=row["progress_pct"],
                remaining_game_minutes=rem, actual_pace=row["actual_pts_per_min"],
                required_pace=row["required_pts_per_min"], pace_gap=row["pace_gap"],
            )

        reg = self._competition(main_conn, row)
        if reg.provider in (None, UNKNOWN) or reg.competition in (None, UNKNOWN):
            return HistoricalContext(
                status="no_validated_competition", period=row["period_label"],
                progress_pct=row["progress_pct"], remaining_game_minutes=rem,
                actual_pace=row["actual_pts_per_min"],
                required_pace=row["required_pts_per_min"], pace_gap=row["pace_gap"],
            )

        bench = benchmark_for(
            self.sidecar, reg.provider, reg.competition, float(row["progress_pct"]),
            str(row["captured_at"]), cutoff_observation_id=int(row["id"]),
            exclude_game_id=str(row["source_game_id"]), use_cache=True,
            period_label=row["period_label"], _qualifier="clean",
        )
        if bench.n < MIN_BENCHMARK_N or bench.mean is None:
            return HistoricalContext(
                status="no_mature_historical_context", benchmark_key=bench.key,
                provider=reg.provider, competition=reg.competition,
                period=row["period_label"], progress_pct=row["progress_pct"],
                remaining_game_minutes=rem, actual_pace=row["actual_pts_per_min"],
                required_pace=row["required_pts_per_min"], pace_gap=row["pace_gap"],
                mean_pace=bench.mean, benchmark_n=bench.n,
            )

        actual = row["actual_pts_per_min"]
        required = row["required_pts_per_min"]
        actual_vs = None if actual is None else float(actual) - float(bench.mean)
        required_vs = None if required is None else float(required) - float(bench.mean)
        under_state = actual_vs is not None and required_vs is not None and actual_vs < 0 and required_vs >= 0
        mirror = actual_vs is not None and required_vs is not None and actual_vs >= 0 and required_vs < 0
        under_pct, game_under_pct, obs_n, game_n = self._outcomes(
            main_conn, row, reg.provider, reg.competition, bench.key
        )
        return HistoricalContext(
            status="matched", benchmark_key=bench.key, provider=reg.provider,
            competition=reg.competition, period=row["period_label"],
            progress_pct=row["progress_pct"], remaining_game_minutes=rem,
            actual_pace=actual, required_pace=required, pace_gap=row["pace_gap"],
            mean_pace=bench.mean, benchmark_n=bench.n,
            actual_vs_avg=actual_vs, required_vs_avg=required_vs,
            under_state=under_state, mirror_over_state=mirror,
            historical_under_pct=under_pct,
            historical_game_under_pct=game_under_pct,
            historical_obs=obs_n, historical_games=game_n,
        )

    def _outcomes(self, main_conn: sqlite3.Connection, row: sqlite3.Row,
                  provider: str, competition: str, key: str):
        """Empirical UNDER rates for the exact historical state population."""
        self._attach_clean()
        self._attach_main(main_conn)
        qbucket = {"Q1": "1st Quarter", "Q2": "2nd Quarter",
                   "Q3": "3rd Quarter", "Q4": "4th Quarter"}[key.split("|")[2]]
        lo = int(key.split("|")[3][1:])
        hi = lo + 5
        family = {"BETUAL": "BETUAL_NBA", "CYBER": "CYBER_2K26"}.get(provider)
        if family is None:
            return None, None, 0, 0
        rows = self.sidecar.execute(
            """SELECT p.source_game_id, p.live_total_line, r.final_total
                 FROM clean.clean_projections p
                 JOIN competition_ledger l ON l.source_game_id=p.source_game_id
                 JOIN prod.game_results r ON r.source_game_id=p.source_game_id
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
        if not rows:
            return None, None, 0, 0
        under_flags = [float(final) < float(line) for _, line, final in rows]
        by_game: dict[str, list[bool]] = {}
        for (gid, _line, _final), flag in zip(rows, under_flags):
            by_game.setdefault(str(gid), []).append(flag)
        game_rates = [sum(v) / len(v) for v in by_game.values()]
        return (
            round(100.0 * sum(under_flags) / len(under_flags), 2),
            round(100.0 * sum(game_rates) / len(game_rates), 2),
            len(rows), len(by_game),
        )
