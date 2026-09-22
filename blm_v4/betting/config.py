"""Betting-execution configuration — environment-sourced, fail-closed.

EVERY value comes from the environment (or the gitignored ``.env`` file
the server process loads).  Nothing here is hard-coded: not the monetary
values, not the credentials, not the provider endpoints.

Credential policy (directive 2026-09-21 §SECURITY): the username and
password NEVER appear in source, Git, logs, API responses, frontend
JavaScript or the database.  They are read from
``POKERBET_USERNAME`` / ``POKERBET_PASSWORD`` at runtime and exist only
inside the provider's process memory.  This module deliberately exposes
no credential value: it only reports whether they are PRESENT.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def _env_bool(name: str, default: str) -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: str) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default) if default != "" else None
    try:
        f = float(raw.strip())
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _env_int(name: str, default: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return int(default) if default != "" else None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from ``path`` into the environment WITHOUT
    overriding variables that are already set.  Missing file → no-op;
    parse problems → skip the line; never raises.  Values are quoted-
    aware (strips matching single/double quotes)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class BettingConfig:
    """The immutable server-side betting configuration.

    ``dry_run`` defaults TRUE and is a separate, higher authority than
    the frontend kill switch: even with AUTO BETTING ON (frontend) and
    ``auto_betting_enabled`` persisted, no real submission happens unless
    the environment explicitly sets ``BETTING_DRY_RUN=false``.
    """

    # ── the provider execution mode (directive: DO NOT enable live yet) ──
    dry_run: bool = True

    # ── risk limits (server-side authority; the frontend can never raise
    #    these — it may only request settings within them) ──────────────
    max_stake_per_bet: Optional[float] = None
    max_bets_per_day: Optional[int] = None
    max_daily_exposure: Optional[float] = None

    # ── staking ────────────────────────────────────────────────────────
    stake_units: float = 1.0
    min_unit_price: float = 1.0
    max_unit_price: float = 1000.0

    # ── alert freshness: an alert older than this is STALE — no bet ────
    alert_max_age_s: float = 90.0

    # ── provider ───────────────────────────────────────────────────────
    provider_base_url: str = "https://pokerbet.co.za"
    provider_timeout_s: float = 20.0

    # ── paths ──────────────────────────────────────────────────────────
    db_path: str = ""

    @staticmethod
    def from_env(root: Optional[Path] = None) -> "BettingConfig":
        """Build the configuration from the environment.

        The gitignored ``.env`` file at the project root is loaded first
        (existing environment variables always WIN — the file only fills
        gaps), so credentials and limits staged there reach the provider
        without ever entering source control.

        Missing values mean "no limit configured" (None) and the executor
        treats a missing limit as UNVERIFIABLE → NO BET (fail-safe §9).
        ``DRY_RUN`` defaults true: live submission requires an explicit
        ``BETTING_DRY_RUN=false`` in the environment.
        """
        if root is None:
            root = Path(__file__).resolve().parent.parent.parent
        _load_env_file(root / ".env")
        return BettingConfig(
            dry_run=_env_bool("BETTING_DRY_RUN", "true"),
            max_stake_per_bet=_env_float("BETTING_MAX_STAKE_PER_BET", ""),
            max_bets_per_day=_env_int("BETTING_MAX_BETS_PER_DAY", ""),
            max_daily_exposure=_env_float("BETTING_MAX_DAILY_EXPOSURE", ""),
            stake_units=_env_float("BETTING_STAKE_UNITS", "1.0") or 1.0,
            min_unit_price=_env_float("BETTING_MIN_UNIT_PRICE", "1.0") or 1.0,
            max_unit_price=_env_float("BETTING_MAX_UNIT_PRICE", "1000.0") or 1000.0,
            alert_max_age_s=_env_float("BETTING_ALERT_MAX_AGE_S", "90") or 90.0,
            provider_base_url=_env(
                "BETTING_PROVIDER_BASE_URL", "https://pokerbet.co.za"),
            provider_timeout_s=_env_float(
                "BETTING_PROVIDER_TIMEOUT_S", "20") or 20.0,
            db_path=_env("BETTING_DB_PATH",
                         str(root / "blm_betting.db")),
        )

    @property
    def live_money_enabled(self) -> bool:
        """True only when dry-run is explicitly switched off.  This is
        the LAST line of defence — the executor checks it immediately
        before any provider submission, and the frontend can never
        influence it."""
        return not self.dry_run


def credentials_present() -> dict:
    """Whether the provider credentials are PRESENT in the environment —
    never their values.  Safe to log and to serve (booleans only)."""
    user = os.environ.get("POKERBET_USERNAME", "").strip()
    pw = os.environ.get("POKERBET_PASSWORD", "")
    return {"username_present": bool(user),
            "password_present": bool(pw)}
