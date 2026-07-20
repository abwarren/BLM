"""
BLM V2 — Betting Logic Model Platform Layer

A production-grade platform for live basketball betting market analysis.
Built on FastAPI, Pydantic V2, and an async event-driven architecture.

Core subsystems:
  - config:  Centralised typed configuration via pydantic-settings
  - models:  Pydantic schemas for snapshots, games, events, predictions, API
  - events:  Async typed event bus with pub/sub and filtering
  - engine:  Analysis engines (Trap Meter, Inflation, Similarity, Regression)
  - api:     FastAPI application with V2 endpoints
"""

__version__ = "2.0.0"
__author__ = "Nous Research"

from blm_v2.config import Settings, get_settings

settings = get_settings()

__all__ = [
    "__version__",
    "settings",
    "Settings",
    "get_settings",
]
