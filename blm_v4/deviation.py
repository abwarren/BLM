"""BLM V4 — Deviation / Benchmark / Z-score measurement layer.

Authorized slice (Phase 2): measure the EMPIRICAL relationship between
the observed live market and the observational trajectory.

For every eligible clean observation (a VALID clean observation whose
trajectory row carries BOTH a live total line and a pace-based projected
final total) this layer persists a raw deviation:

    market_trajectory_residual = live_total_line - projected_final_total

   positive → the market line sits ABOVE the pace-projected trajectory
   negative → the market line sits BELOW the pace-projected trajectory

The residual is a *different variable* from the pace metrics:

    pace_gap                  = required pace - actual pace
    market_trajectory_residual = live total line - projected final total
    z_score                   = residual standardized by its empirical
                                benchmark distribution

They are stored separately and never collapsed into one another.

Benchmark: an empirical distribution of residuals is accumulated per
benchmark bucket.  Buckets are conditioned on game state NOW —
classification (league) + period quarter (Q1..Q4) — so there is NO single
unconditional global benchmark.  Every residual row additionally stores
the full state context (classification, period, elapsed minutes, progress,
actual pace), so the benchmark can later be made arbitrarily finer
(period x time-bin x score-state x pace-state) WITHOUT a schema change.

Online semantics (no self-contamination, no future leakage):

    past observations (same bucket, strictly earlier)
        -> benchmark (n, mean, std)
        -> CURRENT residual
        -> CURRENT z-score            (stored once, immutable)
        -> add the current observation to the benchmark
        -> updated benchmark for later observations

A residual row's stored z-score and benchmark snapshot are written ONCE at
creation time and are NEVER rewritten: subsequent observations and settled
outcomes cannot alter a historical T-state z-score.  Only information
available at the observation's own time is used — no subsequent
observation, no settlement result, no final score, no future market state
leaks backward into the benchmark or z-score.

Maturity: every row stores benchmark_n / benchmark_mean / benchmark_std /
benchmark_status.  The statuses EXPLORATORY / PROVISIONAL / ESTABLISHED are
OPERATIONAL MATURITY LABELS for the bucket size N (the count of prior
residuals actually used to compute this z), NOT claims of statistical
significance — a large-N bucket can still be unstable if its conditioning
is poor.  z_score is NULL whenever the bucket std is unavailable (fewer
than 2 prior residuals) or zero/invalid (never an infinity).

The z-score is DESCRIPTIVE.  Nothing in this module maps z_score to Over,
Under, edge, probability, confidence, or a recommendation.  Feed sources
are the already-gated clean trajectory rows (VALID observations only), so
replayed / duplicated / stale / regressed frames can never enter the
benchmark.

No existing table or writer is modified; this layer adds its own tables to
blm_metrics_clean.db.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.terminal_eligibility import is_terminal_checkpoint

MODEL_VERSION = "v4-deviation-1"

# Operational maturity thresholds on the benchmark bucket size N.
EXPLORATORY_LIMIT = 100     # N <  100 -> EXPLORATORY
PROVISIONAL_LIMIT = 1000    # N < 1000 -> PROVISIONAL, else ESTABLISHED
MATURITY_LABELS = ("EXPLORATORY", "PROVISIONAL", "ESTABLISHED")

Z_EPS = 1e-9                # std at/below this is treated as undefined


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), n)


def benchmark_status(n: int) -> str:
    """Operational maturity label for a bucket of size ``n`` — a label for
    how much empirical data exists, NEVER a statistical-significance claim."""
    if n < EXPLORATORY_LIMIT:
        return "EXPLORATORY"
    if n < PROVISIONAL_LIMIT:
        return "PROVISIONAL"
    return "ESTABLISHED"


def normalize_period(period_label: Optional[str]) -> Optional[int]:
    """Quarter (1..4) parsed from a period label (\"Q3\", \"3rd Quarter\",
    ...).  None when no unambiguous regulation quarter is present."""
    m = re.search(r"(\d+)", period_label or "")
    if not m:
        return None
    q = int(m.group(1))
    return q if 1 <= q <= 4 else None


def benchmark_key(classification: Optional[str], period: Optional[int]) -> str:
    """Bucket identity: league + regulation quarter.  No unconditional
    global bucket — classification is always part of the key."""
    cls = str(classification or "UNKNOWN")
    return f"{cls}|Q{period}" if period else f"{cls}|UNK"


def market_trajectory_residual(
    live_total_line: Optional[float], projected_final_total: Optional[float],
) -> Optional[float]:
    """market expectation - observational trajectory (signed).

    positive → the live line sits ABOVE the pace-projected trajectory;
    negative → below.  NOT an Over/Under call and NOT a probability.
    """
    if live_total_line is None or projected_final_total is None:
        return None
    return round(float(live_total_line) - float(projected_final_total), 2)


def _mean_std(n: int, s: float, ss: float) -> tuple[int, Optional[float], Optional[float]]:
    """(n, mean, sample std) from count / sum / sum-of-squares.  Std is
    None when n < 2 (a variance is undefined)."""
    if n <= 0:
        return 0, None, None
    mean = s / n
    if n < 2:
        return n, mean, None
    var = (ss - (s * s) / n) / (n - 1)
    if var < 0.0:
        var = 0.0  # floating noise on a ~zero variance
    return n, mean, math.sqrt(var)


def z_score(
    residual: Optional[float],
    benchmark_mean: Optional[float],
    benchmark_std: Optional[float],
) -> Optional[float]:
    """Standardized deviation vs the empirical benchmark.  NULL (never an
    infinity) when the benchmark std is missing or zero/invalid."""
    if residual is None or benchmark_mean is None or benchmark_std is None:
        return None
    if benchmark_std <= Z_EPS:
        return None
    return round((residual - benchmark_mean) / benchmark_std, 4)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deviation_residuals (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id            INTEGER NOT NULL UNIQUE,
    model_version             TEXT NOT NULL,
    source_game_id            TEXT NOT NULL,
    -- state context (preserved for finer future conditioning)
    classification            TEXT,
    period                    INTEGER,        -- regulation quarter 1..4 | NULL
    benchmark_key             TEXT NOT NULL,  -- league|Qn
    captured_at               TEXT NOT NULL,
    elapsed_game_minutes      REAL,
    progress_pct              REAL,
    current_total_points      INTEGER,
    actual_pts_per_min        REAL,
    -- the three distinct variables (never collapsed)
    live_total_line           REAL,
    projected_final_total     REAL,
    market_trajectory_residual REAL,          -- live line - projected total
    -- empirical benchmark snapshot AT this observation (prior residuals only)
    benchmark_n               INTEGER,        -- prior residuals in bucket
    benchmark_mean            REAL,           -- prior mean  (sample)
    benchmark_std             REAL,           -- prior std   (sample)
    benchmark_status          TEXT,           -- EXPLORATORY | PROVISIONAL | ESTABLISHED
    z_score                   REAL,           -- descriptive; NULL when std invalid
    computed_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dev_res_key_time
    ON deviation_residuals(benchmark_key, captured_at, observation_id);
CREATE INDEX IF NOT EXISTS idx_dev_res_game
    ON deviation_residuals(source_game_id, captured_at);
"""


class DeviationStore:
    """Persistence for the deviation benchmark layer (own tables inside
    blm_metrics_clean.db).  Read-write; append-style: a residual row is
    created once per clean observation and never modified afterward."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else \
            Path(__file__).resolve().parent.parent / "blm_metrics_clean.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=rwc", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # ── projection-row reads (the ONLY feed) ────────────────────────

    def game_projection_rows(self, source_game_id: str) -> list[dict]:
        """Trajectory rows for one game (VALID clean observations only —
        replayed/stale/regressed frames never appear here)."""
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM clean_projections WHERE source_game_id=? "
                    "ORDER BY captured_at, observation_id",
                    (source_game_id,)).fetchall()]
            finally:
                conn.close()

    def game_ids_with_projections(self) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                return [r["source_game_id"] for r in conn.execute(
                    "SELECT DISTINCT source_game_id FROM clean_projections "
                    "ORDER BY source_game_id")]
            finally:
                conn.close()

    # ── residual persistence ────────────────────────────────────────

    def residual_observation_ids(self, source_game_id: str) -> set[int]:
        with self._lock:
            conn = self._connect()
            try:
                return {int(r["observation_id"]) for r in conn.execute(
                    "SELECT observation_id FROM deviation_residuals "
                    "WHERE source_game_id=?", (source_game_id,))}
            finally:
                conn.close()

    def prior_bucket_stats(
        self, benchmark_key: str, captured_at: str, observation_id: int,
    ) -> tuple[int, Optional[float], Optional[float]]:
        """(n, mean, std) over residuals STRICTLY EARLIER than the given
        observation (same bucket).  Later residuals — and the observation
        itself — are excluded, so no future information can leak in."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT COUNT(*) AS n,
                              COALESCE(SUM(market_trajectory_residual), 0) AS s,
                              COALESCE(SUM(market_trajectory_residual
                                           * market_trajectory_residual), 0) AS ss
                       FROM deviation_residuals
                       WHERE benchmark_key = ?
                         AND (captured_at < ?
                              OR (captured_at = ? AND observation_id < ?))""",
                    (benchmark_key, captured_at, captured_at, observation_id),
                ).fetchone()
                return _mean_std(int(row["n"]), float(row["s"]), float(row["ss"]))
            finally:
                conn.close()

    def insert_residual(self, row: dict[str, Any]) -> int:
        cols = (
            "observation_id", "model_version", "source_game_id",
            "classification", "period", "benchmark_key", "captured_at",
            "elapsed_game_minutes", "progress_pct", "current_total_points",
            "actual_pts_per_min",
            "live_total_line", "projected_final_total",
            "market_trajectory_residual",
            "benchmark_n", "benchmark_mean", "benchmark_std",
            "benchmark_status", "z_score", "computed_at",
        )
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO deviation_residuals "
                    f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [row.get(c) for c in cols],
                )
                conn.commit()
                return int(cur.lastrowid) if cur.lastrowid else 0
            finally:
                conn.close()

    # ── reads (dashboard / audit) ──────────────────────────────────

    def residuals_for_game(self, source_game_id: str) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM deviation_residuals "
                    "WHERE source_game_id=? ORDER BY captured_at, id",
                    (source_game_id,)).fetchall()]
            finally:
                conn.close()

    def count_residuals(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                return int(conn.execute(
                    "SELECT COUNT(*) AS c FROM deviation_residuals"
                ).fetchone()["c"])
            finally:
                conn.close()

    def bucket_summaries(
        self, classification: Optional[str] = None,
    ) -> list[dict]:
        """Current per-bucket empirical benchmark state (all residuals so
        far), for the maturity table / chips.  ``n`` here is the bucket's
        TOTAL size; each stored z used the bucket's PRIOR size."""
        rows: list[dict] = []
        with self._lock:
            conn = self._connect()
            try:
                q = "SELECT * FROM deviation_residuals"
                params: tuple = ()
                if classification:
                    q += " WHERE classification=?"
                    params = (classification,)
                rows = [dict(r) for r in conn.execute(q, params).fetchall()]
            finally:
                conn.close()
        by_key: dict[str, list[float]] = {}
        meta: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = r["benchmark_key"]
            by_key.setdefault(key, []).append(r["market_trajectory_residual"])
            m = meta.setdefault(key, {
                "benchmark_key": key,
                "classification": r.get("classification"),
                "period": r.get("period"),
                "latest_captured_at": None,
            })
            ts = r.get("captured_at") or ""
            if m["latest_captured_at"] is None or ts > m["latest_captured_at"]:
                m["latest_captured_at"] = ts
        out = []
        for key in sorted(by_key):
            vals = [v for v in by_key[key] if v is not None]
            n = len(vals)
            mean = _r(sum(vals) / n, 6) if n else None
            std = None
            if n >= 2:
                var = (sum((v - mean) ** 2 for v in vals) / (n - 1)
                       if mean is not None else 0.0)
                std = _r(math.sqrt(max(0.0, var)), 6)
            out.append({
                **meta[key],
                "n": n,
                "mean": mean,
                "std": std,
                "min": _r(min(vals), 6) if vals else None,
                "max": _r(max(vals), 6) if vals else None,
                "status": benchmark_status(n),
            })
        return out


def _row_terminal(p: dict[str, Any]) -> bool:
    """Terminal-eligibility gate for one trajectory row (directive).

    Belt and braces: the stamped terminal flag is authoritative when
    present, and the single authoritative predicate is applied to the
    row's own game-time evidence as well — so a row whose elapsed game
    time / progress / period-over clock proves it is the END of the game
    can never enter the benchmark, whatever its stamp vintage.  A mid-game
    row of a since-ended game is NOT terminal (its game time contradicts
    the end state).
    """
    if int(p.get("terminal") or 0):
        return True
    progress = p.get("progress_pct")
    return is_terminal_checkpoint(
        classification=p.get("classification"),
        elapsed_minutes=p.get("elapsed_game_minutes"),
        progress=(progress / 100.0 if progress is not None else None),
        quarter=normalize_period(p.get("period_label")),
        clock=p.get("clock"),
        period_label=p.get("period_label"),
    )


class DeviationEngine:
    """Computes residuals + empirical-benchmark z-scores from clean
    trajectory rows (the collector calls this as observations arrive, and
    once at startup so pre-existing clean observations join immediately).

    Deterministic and idempotent: rows are created once per observation
    and never rewritten; a repeated refresh changes nothing.
    """

    def __init__(self, clean_db_path: Optional[Path] = None):
        self.store = DeviationStore(clean_db_path)

    def refresh_game(self, source_game_id: str) -> dict[str, Any]:
        projections = self.store.game_projection_rows(source_game_id)
        existing = self.store.residual_observation_ids(source_game_id)
        # TERMINAL EXCLUSION (directive): the final/terminal frame is
        # settlement/audit only — it can never enter the residual
        # benchmark, so no terminal tautology (fair == actual by
        # construction) can contaminate any z-score or benchmark stat.
        terminal_flags = [_row_terminal(p) for p in projections]
        n_terminal = sum(terminal_flags)
        eligible = [
            p for p, t in zip(projections, terminal_flags)
            if not t
            and p.get("live_total_line") is not None
            and p.get("projected_final_total") is not None
            and p.get("observation_id") is not None
        ]
        eligible.sort(key=lambda p: (p["captured_at"], p["observation_id"]))
        added = 0
        existing_hits = 0
        ineligible = len(projections) - len(eligible)
        for p in eligible:
            obs_id = int(p["observation_id"])
            if obs_id in existing:
                existing_hits += 1
                continue
            cls = p.get("classification")
            period = normalize_period(p.get("period_label"))
            key = benchmark_key(cls, period)
            residual = market_trajectory_residual(
                p.get("live_total_line"), p.get("projected_final_total"))
            if residual is None:  # defensive — eligibility already checked
                continue
            n, mean, std = self.store.prior_bucket_stats(
                key, p["captured_at"], obs_id)
            # Snapshot stats are rounded to their stored precision BEFORE
            # the z-score, so the stored row is exactly reproducible.
            mean_r = _r(mean, 6)
            std_r = _r(std, 6)
            z = z_score(residual, mean_r, std_r)
            self.store.insert_residual({
                "observation_id": obs_id,
                "model_version": MODEL_VERSION,
                "source_game_id": p["source_game_id"],
                "classification": cls,
                "period": period,
                "benchmark_key": key,
                "captured_at": p["captured_at"],
                "elapsed_game_minutes": p.get("elapsed_game_minutes"),
                "progress_pct": p.get("progress_pct"),
                "current_total_points": p.get("current_total_points"),
                "actual_pts_per_min": p.get("actual_pts_per_min"),
                "live_total_line": _r(p.get("live_total_line"), 2),
                "projected_final_total": _r(p.get("projected_final_total"), 2),
                "market_trajectory_residual": residual,
                "benchmark_n": n,
                "benchmark_mean": mean_r,
                "benchmark_std": std_r,
                "benchmark_status": benchmark_status(n),
                "z_score": z,
                "computed_at": _now_iso(),
            })
            existing.add(obs_id)
            added += 1
        return {
            "source_game_id": source_game_id,
            "projections": len(projections),
            "eligible": len(eligible),
            # non-overlapping partition: total = eligible + terminal_excluded
            # + ineligible (non-terminal rows missing a line/projection/obs)
            "ineligible": len(projections) - len(eligible) - n_terminal,
            "terminal_excluded": n_terminal,
            "added": added,
            "existing": existing_hits,
        }

    def refresh_all(self) -> dict[str, Any]:
        """Backfill / reconcile every game that has trajectory rows
        (idempotent).  Called once at collector start so residuals for
        already-stored clean observations enter the benchmark
        immediately; per-game refreshes keep it current afterwards."""
        total_added = 0
        total_existing = 0
        games = 0
        for gid in self.store.game_ids_with_projections():
            stats = self.refresh_game(gid)
            games += 1
            total_added += stats["added"]
            total_existing += stats["existing"]
        return {
            "games": games,
            "added": total_added,
            "existing": total_existing,
        }
