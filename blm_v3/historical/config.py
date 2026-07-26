"""
BLM V3 — Historical Engine Configuration.

Centralised configuration for the historical data collection and time-series
visualisation engine. All thresholds, paths, and tunables live here.
"""

from __future__ import annotations

from pathlib import Path


# ── Database ─────────────────────────────────────────────────────────

DEFAULT_DB_DIR: Path = Path(__file__).resolve().parent.parent.parent
"""Default directory for the historical SQLite database (project root)."""

DEFAULT_DB_FILENAME: str = "blm_historical.db"
"""Filename for the historical time-series database."""

DEFAULT_DB_PATH: Path = DEFAULT_DB_DIR / DEFAULT_DB_FILENAME
"""Full path: ``<project-root>/blm_historical.db``."""


# ── Collector ────────────────────────────────────────────────────────

DEFAULT_COLLECT_INTERVAL_MS: int = 250
"""Default snapshot collection interval in milliseconds."""

FALLBACK_COLLECT_INTERVAL_MS: int = 500
"""Fallback interval when rate limiting is detected (ms)."""

MIN_COLLECT_INTERVAL_MS: int = 100
"""Hard minimum — never poll faster than this (ms)."""


# ── Derived Metric Thresholds ────────────────────────────────────────

# Inflation index thresholds
INFLATION_LOW: float = 2.0
"""Inflation index above this is 'low' concern."""
INFLATION_MID: float = 4.0
"""Inflation index above this is 'mid' concern."""
INFLATION_HIGH: float = 6.0
"""Inflation index above this is 'high' concern."""

# Compression index thresholds
COMPRESSION_HIGH: float = 0.85
"""Compression index above = tight odds (high confidence market)."""
COMPRESSION_LOW: float = 0.40
"""Compression index below = wide odds (low confidence)."""

# Pace thresholds
PACE_EXPECTED_DEFAULT: float = 108.0
"""Default expected pace for Cyber 2K26 (possessions per 48 min)."""
PACE_COLLAPSE_THRESHOLD: float = 1.0
"""Pace (possessions/min) below this + game_minutes > 12 = pace collapse."""

# Line movement thresholds
LINE_JUMP_THRESHOLD: float = 2.0
"""Line delta above this (absolute) = line jump event."""
LINE_FREEZE_MIN_TICKS: int = 10
"""Consecutive snapshots with zero line delta = line freeze."""
SHARP_MOVEMENT_THRESHOLD: float = 1.5
"""Line moves opposite to pace direction above this threshold."""

# Odds thresholds
ODDS_DELTA_MIN: float = 0.02
"""Minimum odds delta to be considered meaningful."""

# Momentum
MOMENTUM_ALPHA: float = 0.3
"""Exponential moving average alpha for momentum calculation."""
MOMENTUM_SWING_THRESHOLD: float = 3.0
"""Absolute momentum change above this = momentum swing."""

# Variance / Volatility
VARIANCE_WINDOW: int = 10
"""Number of recent snapshots for rolling variance calculation."""

# Traps
TRAP_METER_FORMATION: float = 60.0
"""Trap meter >= this = 'trap formation' signal."""
TRAP_METER_ACTIVE: float = 80.0
"""Trap meter >= this = active trap (bull/bear)."""

# Regression
REGRESSION_DISTANCE_THRESHOLD: float = 5.0
"""Distance from fair total to line above this = regression candidate."""
REGRESSION_RETURN_THRESHOLD: float = 1.0
"""Distance threshold to consider regression 'complete'."""

# Confidence
CONFIDENCE_LOW: float = 0.30
"""Confidence below this triggers signal."""


# ── Signal Engine ────────────────────────────────────────────────────

SIGNAL_COOLDOWN_S: float = 5.0
"""Minimum seconds between identical signal firings."""

MAX_SIGNALS_PER_GAME: int = 10000
"""Safety cap on signals per game to prevent runaway detection."""

MAX_EVENTS_PER_GAME: int = 5000
"""Safety cap on events per game."""


# ── ML Export ────────────────────────────────────────────────────────

ML_DEFAULT_FEATURES: list[str] = [
    "total_line", "spread", "home_team_total", "away_team_total",
    "over_odds", "under_odds",
    "line_delta", "odds_delta", "spread_delta",
    "possessions_per_min", "projected_total",
    "trap_meter", "inflation_index", "compression_index",
    "momentum", "regression_prob", "fair_total", "expected_total",
    "variance", "volatility", "confidence",
]
"""Default feature set for ML dataset export."""

ML_DEFAULT_LABEL: str = "final_total"
"""Default target label for ML training rows."""
