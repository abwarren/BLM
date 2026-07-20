"""
BLM V2 — Telemetry Package

Structured logging and performance monitoring infrastructure
for the BLM platform layer.

Subsystems:
  - logging:  Structured logging with structlog, correlation IDs, JSON output
  - metrics:  In-memory performance metrics collector with min/max/avg/count
"""

from blm_v2.telemetry.logging import (
    setup_logging,
    get_logger,
    correlation_id_ctx,
    CorrelationIdMiddleware,
)
from blm_v2.telemetry.metrics import (
    MetricsCollector,
    MetricsSnapshot,
    get_metrics_collector,
    reset_metrics_collector,
)

__all__ = [
    "setup_logging",
    "get_logger",
    "correlation_id_ctx",
    "CorrelationIdMiddleware",
    "MetricsCollector",
    "MetricsSnapshot",
    "get_metrics_collector",
    "reset_metrics_collector",
]
