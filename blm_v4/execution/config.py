"""BLM EXECUTION — CONFIGURATION (env-sourced, fail-safe).

The hard mode ladder (directive addition): the system never silently
transitions from DRY_RUN to live.  ``EXECUTION_DRY_RUN`` defaults TRUE;
LIVE mode exists only when the env explicitly sets it false AND the
caller requests MODE_LIVE — the engine then refuses every click and
every placement in any other mode.  The UI must show 🔴 LIVE EXECUTION
before the final action is allowed in that state.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _env_int(name: str, default: str) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return int(default)
    try:
        return int(raw.strip())
    except ValueError:
        return int(default)


def _env_float(name: str, default: str) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        f = float(raw.strip())
    except ValueError:
        return float(default)
    return f if math.isfinite(f) else float(default)


def _env_bool(name: str, default: str) -> bool:
    return (os.environ.get(name) or default).strip().lower() in (
        "1", "true", "yes", "on")


@dataclass(frozen=True)
class ExecutionConfig:
    """Immutable execution-engine configuration."""

    dry_run: bool = True                 # EXECUTION_DRY_RUN (default true)
    max_selection_retries: int = 3       # EXECUTION_MAX_SELECTION_RETRIES
    retry_delay_ms: int = 750            # pause before a re-resolve
    settle_ms: int = 400                 # DOM settle pause after nav
    slip_wait_ms: int = 900              # wait for the slip to reflect a click
    verify_attempts: int = 3             # slip re-reads before declaring failure
    leg_timeout_s: float = 45.0          # watchdog: one leg's wall clock
    job_timeout_s: float = 600.0         # watchdog: one parlay's wall clock
    max_parlays_per_run: int = 100       # hard run cap
    max_stake_per_parlay: Optional[float] = None
    max_total_exposure: Optional[float] = None
    cdp_url: str = "http://127.0.0.1:9222"
    db_path: str = ""

    @staticmethod
    def from_env(root: Optional[Path] = None) -> "ExecutionConfig":
        if root is None:
            root = Path(__file__).resolve().parent.parent.parent
        # the root .env is loaded by blm_v4.betting.config on the server
        # path; standalone use loads it here too (never overrides set vars)
        try:
            from blm_v4.betting.config import _load_env_file
            _load_env_file(root / ".env")
        except Exception:
            pass
        return ExecutionConfig(
            dry_run=_env_bool("EXECUTION_DRY_RUN", "true"),
            max_selection_retries=_env_int(
                "EXECUTION_MAX_SELECTION_RETRIES", "3"),
            retry_delay_ms=_env_int("EXECUTION_RETRY_DELAY_MS", "750"),
            settle_ms=_env_int("EXECUTION_SETTLE_MS", "400"),
            slip_wait_ms=_env_int("EXECUTION_SLIP_WAIT_MS", "900"),
            verify_attempts=_env_int("EXECUTION_VERIFY_ATTEMPTS", "3"),
            leg_timeout_s=_env_float("EXECUTION_LEG_TIMEOUT_S", "45"),
            job_timeout_s=_env_float("EXECUTION_JOB_TIMEOUT_S", "600"),
            max_parlays_per_run=_env_int(
                "EXECUTION_MAX_PARLAYS_PER_RUN", "100"),
            max_stake_per_parlay=_env_float(
                "EXECUTION_MAX_STAKE_PER_PARLAY", "0") or None,
            max_total_exposure=_env_float(
                "EXECUTION_MAX_TOTAL_EXPOSURE", "0") or None,
            cdp_url=os.environ.get("EXECUTION_CDP_URL",
                                   "http://127.0.0.1:9222").strip(),
            db_path=os.environ.get(
                "EXECUTION_DB_PATH", str(root / "blm_execution.db")),
        )

    @property
    def live_permitted(self) -> bool:
        """True only when the environment explicitly allows live money.
        The engine checks this immediately before any real placement —
        the LAST line of defence above the mode argument."""
        return not self.dry_run
