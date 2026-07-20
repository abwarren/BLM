"""
BLM V2 — Centralised Configuration

All application settings in one place, read from .env or environment variables.
Uses pydantic-settings for automatic .env file discovery, validation, and
type coercion.

Usage:
    from blm_v2.config import get_settings

    settings = get_settings()
    settings.database_url        # str
    settings.log_level           # str
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Default paths ─────────────────────────────────────────────────


def _project_root() -> Path:
    """Return the project root directory (grandparent of this file)."""
    return Path(__file__).resolve().parent.parent


def _env_file_path() -> Path:
    """Default .env path alongside this config module."""
    return _project_root() / ".env"


# ── Settings ──────────────────────────────────────────────────────


class Settings(BaseSettings):
    """Typed application settings loaded from environment / .env file.

    Every configurable value lives here — no magic constants in modules.
    Nested model classes group related settings for clarity.
    """

    model_config = SettingsConfigDict(
        env_file=str(_env_file_path()),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── General ────────────────────────────────────────────────
    app_name: str = Field(
        default="BLM V2",
        description="Application name used in health checks and logging.",
    )
    app_version: str = Field(
        default="2.0.0",
        description="Semantic version of the BLM V2 platform.",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug-level logging and development behaviour.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Root logger level.",
    )
    log_format: Literal["json", "text"] = Field(
        default="text",
        description="Structured log output format (json or text).",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment label.",
    )

    # ── Server ─────────────────────────────────────────────────
    host: str = Field(
        default="0.0.0.0",
        description="Bind address for the FastAPI / Uvicorn server.",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Bind port for the FastAPI / Uvicorn server.",
    )
    workers: int = Field(
        default=1,
        ge=1,
        le=16,
        description="Number of Uvicorn worker processes.",
    )
    reload: bool = Field(
        default=False,
        description="Enable auto-reload on file changes (development only).",
    )
    cors_origins: list[str] = Field(
        default=["*"],
        description="Allowed CORS origin URLs.",
    )

    # ── Database ───────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite:///blm_v2.db",
        description="Database connection URL (SQLAlchemy format).",
    )
    database_echo: bool = Field(
        default=False,
        description="Log all SQL statements for debugging.",
    )
    database_pool_size: int = Field(
        default=5,
        ge=1,
        description="Maximum database connection pool size.",
    )
    database_max_overflow: int = Field(
        default=10,
        ge=0,
        description="Maximum overflow connections beyond pool size.",
    )

    # ── Scraper / Collector ────────────────────────────────────
    collector_enabled: bool = Field(
        default=False,
        description="Enable the background snapshot collector on start.",
    )
    collector_interval: float = Field(
        default=1.0,
        gt=0.0,
        le=60.0,
        description="Seconds between collector scrape attempts.",
    )
    collector_headless: bool = Field(
        default=True,
        description="Run Playwright browser in headless mode.",
    )
    collector_url: str = Field(
        default="https://www.pokerbet.co.za/sports/basketball/cyber-basketball",
        description="Target URL for the Playwright scraper.",
    )
    collector_nav_timeout: int = Field(
        default=30000,
        ge=5000,
        description="Navigation timeout in milliseconds.",
    )
    collector_selector_timeout: int = Field(
        default=5000,
        ge=1000,
        description="DOM selector timeout in milliseconds.",
    )

    # ── Event Bus ──────────────────────────────────────────────
    event_bus_max_handlers: int = Field(
        default=100,
        ge=1,
        description="Maximum registered event handlers per type.",
    )
    event_bus_handler_timeout: float = Field(
        default=10.0,
        gt=0.0,
        description="Maximum seconds a handler may run before warning.",
    )

    # ── BLM Engine ─────────────────────────────────────────────
    engine_enabled: bool = Field(
        default=True,
        description="Enable BLM analysis engine (trap meter, inflation, etc.).",
    )
    engine_run_interval: float = Field(
        default=1.0,
        gt=0.0,
        le=30.0,
        description="Seconds between BLM engine analysis cycles.",
    )
    default_league: str = Field(
        default="Cyber 2K26",
        description="Default league identifier when none is specified.",
    )
    default_season: str = Field(
        default="2026",
        description="Default season identifier.",
    )

    # ── Prediction Tuning ──────────────────────────────────────
    confidence_threshold: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        description="Minimum composite confidence for actionable predictions.",
    )
    trap_meter_threshold: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Trap meter score above which a trap alert fires.",
    )
    momentum_window: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of recent snapshots used for momentum calculation.",
    )

    # ── WebSocket ──────────────────────────────────────────────
    websocket_enabled: bool = Field(
        default=True,
        description="Enable WebSocket endpoint for real-time streaming.",
    )
    websocket_heartbeat_interval: int = Field(
        default=30,
        ge=5,
        description="WebSocket heartbeat ping interval in seconds.",
    )

    # ── External Services ──────────────────────────────────────
    influxdb_url: str | None = Field(
        default=None,
        description="InfluxDB 2.x server URL (optional).",
    )
    influxdb_token: str | None = Field(
        default=None,
        description="InfluxDB 2.x API token.",
    )
    influxdb_org: str | None = Field(
        default=None,
        description="InfluxDB 2.x organisation name.",
    )
    influxdb_bucket: str | None = Field(
        default=None,
        description="InfluxDB 2.x bucket name.",
    )

    # ── Validators ─────────────────────────────────────────────

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        """Parse CORS origins from a comma-separated env var."""
        if isinstance(v, str):
            origins = [o.strip() for o in v.split(",") if o.strip()]
            return origins if origins else ["*"]
        return v

    @field_validator("database_url", mode="before")
    @classmethod
    def resolve_database_path(cls, v: str) -> str:
        """Expand relative SQLite paths to absolute."""
        if v.startswith("sqlite:///") and not v.startswith("sqlite:////"):
            rel = v[len("sqlite:///"):]
            if rel and "/" not in rel and "\\" not in rel:
                abs_path = _project_root() / rel
                return f"sqlite:///{abs_path}"
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def coerce_log_level(cls, v: str) -> str:
        """Normalise log level casing."""
        return v.upper()

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        """Cross-field validation rules."""
        if self.environment == "production" and self.reload:
            raise ValueError("reload=True is not allowed in production environment")
        return self


# ── Singleton accessor ─────────────────────────────────────────────


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the application-wide Settings singleton.

    Lazy-loaded so it works at module import time without ordering issues.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Force-reload settings from the environment (useful in tests)."""
    global _settings
    _settings = Settings()
    return _settings
