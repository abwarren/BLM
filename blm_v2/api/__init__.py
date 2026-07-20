"""
BLM V2 — API Package

FastAPI application with V2 REST endpoints, WebSocket handler,
and dependency injection wiring.

Subsystems:
  - v2_fastapi:    FastAPI application with all V2 REST endpoints
  - websocket:     WebSocket handler for push-based live snapshots
  - dependencies:  FastAPI dependency injection providers
"""

from blm_v2.api.v2_fastapi import create_v2_app
from blm_v2.api.dependencies import (
    TSInterface,
    StorageInterface,
    EventBusInterface,
    BLMEngineInterface,
    get_ts_interface,
    get_storage_interface,
    get_event_bus,
    get_blm_engine,
    get_metrics_collector_dep,
)

__all__ = [
    "create_v2_app",
    "TSInterface",
    "StorageInterface",
    "EventBusInterface",
    "BLMEngineInterface",
    "get_ts_interface",
    "get_storage_interface",
    "get_event_bus",
    "get_blm_engine",
    "get_metrics_collector_dep",
]
