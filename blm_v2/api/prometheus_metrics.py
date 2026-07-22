"""BLM V2 — Prometheus Metrics Exporter

Exposes BLM under-timing, game-state, and engine metrics at /metrics for
Prometheus scraping.  Metrics are rebuilt from live dependencies on each
scrape, so no separate update loop is needed.

Usage:
    from blm_v2.api.prometheus_metrics import add_prometheus_route
    add_prometheus_route(app)
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from prometheus_client import (
    CollectorRegistry,
    Gauge,
    Info,
    generate_latest,
    write_to_textfile,
)

# ── Custom registry so we don't collide with uvicorn/prometheus_client defaults ──
REGISTRY = CollectorRegistry(auto_describe=False)

# ── UNDER Timing metrics ──────────────────────────────────────────────────────
under_score = Gauge(
    "blm_under_timing_score",
    "Composite UNDER timing score (0–100)",
    registry=REGISTRY,
)
under_confidence = Gauge(
    "blm_under_timing_confidence",
    "Confidence in the current timing assessment (0–1)",
    registry=REGISTRY,
)
under_status = Info(
    "blm_under_timing_status",
    "Current UNDER timing assessment status (PASS/WAIT/WATCH/UNDER_READY)",
    registry=REGISTRY,
)
under_component = Gauge(
    "blm_under_component",
    "Individual component score contributing to the UNDER timing composite",
    ["component"],
    registry=REGISTRY,
)

# ── Game-state metrics ─────────────────────────────────────────────────────────
current_total = Gauge(
    "blm_current_total",
    "Current total score (home + away) for the live game",
    registry=REGISTRY,
)
current_line = Gauge(
    "blm_current_line",
    "Current bookmaker total line for the live game",
    registry=REGISTRY,
)
olv = Gauge(
    "blm_olv",
    "Opening Line Value for the live game",
    registry=REGISTRY,
)
excursion = Gauge(
    "blm_excursion",
    "Excursion from OLV (current_line - olv), positive = line inflated",
    registry=REGISTRY,
)
freeze_ticks = Gauge(
    "blm_freeze_ticks",
    "Consecutive ticks without line movement while score changed",
    registry=REGISTRY,
)
score_delta = Gauge(
    "blm_score_delta",
    "Score points scored in the last tick interval",
    registry=REGISTRY,
)
line_delta = Gauge(
    "blm_line_delta",
    "Line movement in the last tick interval",
    registry=REGISTRY,
)
is_burst = Gauge(
    "blm_is_burst",
    "1 if a scoring burst was detected this tick, 0 otherwise",
    registry=REGISTRY,
)

# ── Pipeline health ────────────────────────────────────────────────────────────
snapshots_processed = Gauge(
    "blm_snapshots_processed",
    "Total number of line-analysis entries recorded for the live game",
    registry=REGISTRY,
)
pipeline_uptime = Gauge(
    "blm_pipeline_uptime_seconds",
    "Seconds since the BLM pipeline last restarted",
    registry=REGISTRY,
)
pipeline_info = Info(
    "blm_pipeline",
    "Static pipeline metadata (version, league)",
    registry=REGISTRY,
)

# ── Historical profile ─────────────────────────────────────────────────────────
hist_games = Gauge(
    "blm_historical_games_total",
    "Total games in the historical profile for the current league",
    registry=REGISTRY,
)
hist_snapshots = Gauge(
    "blm_historical_snapshots_total",
    "Total snapshots in the historical profile",
    registry=REGISTRY,
)
hist_regression_rate = Gauge(
    "blm_historical_regression_rate",
    "Historical rate at which excursions regress toward OLV",
    registry=REGISTRY,
)
hist_under_rate = Gauge(
    "blm_historical_under_rate",
    "Historical UNDER hit rate for the current league",
    registry=REGISTRY,
)
hist_excursion_p95 = Gauge(
    "blm_historical_excursion_p95",
    "95th percentile of historical excursion values",
    registry=REGISTRY,
)


# ── Metrics builder ────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    """Coerce to float, returning *default* on None / NaN / error."""
    if v is None:
        return default
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (ValueError, TypeError):
        return default


_START_TIME: float = 0.0
_LEAGUE: str = "Cyber 2K26"


async def refresh_metrics(ts_iface, storage_iface) -> None:
    """Query live dependencies and update all Prometheus gauges."""
    global _START_TIME
    if _START_TIME == 0.0:
        _START_TIME = time.time()

    pipeline_info.info({"version": "2.0.0", "league": _LEAGUE})
    pipeline_uptime.set(max(0.0, time.time() - _START_TIME))

    # ── UNDER signals ───────────────────────────────────────────
    under_raw = None
    try:
        under_raw = await ts_iface.get_live_line_analysis()
        if under_raw:
            under_score.set(_safe_float(under_raw.get("under_timing_score")))
            under_confidence.set(_safe_float(under_raw.get("under_timing_confidence")))
            under_status.info({"status": under_raw.get("under_timing_status", "PASS")})

            comps = under_raw.get("under_components", {})
            if isinstance(comps, dict):
                for comp_name, comp_val in comps.items():
                    under_component.labels(component=comp_name).set(_safe_float(comp_val))
            elif isinstance(comps, str):
                try:
                    comps_d = json.loads(comps)
                    for comp_name, comp_val in comps_d.items():
                        under_component.labels(component=comp_name).set(_safe_float(comp_val))
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        pass

    # ── Line analysis (latest entry for game state) ─────────────
    try:
        game_id = "East Cyber-vs-West Cyber-2026-07-21"  # stable fallback
        if under_raw and under_raw.get("game_id"):
            game_id = under_raw["game_id"]
        entries = await ts_iface.query_line_analysis(game_id=game_id, limit=1)
        if entries:
            latest = entries[-1]
            current_total.set(_safe_float(latest.get("current_total", latest.get("current_score", 0))))
            current_line.set(_safe_float(latest.get("current_line")))
            olv.set(_safe_float(latest.get("olv")))
            excursion.set(_safe_float(latest.get("excursion")))
            freeze_ticks.set(_safe_float(latest.get("freeze_ticks")))
            score_delta.set(_safe_float(latest.get("score_delta")))
            line_delta.set(_safe_float(latest.get("line_delta")))
            is_burst.set(1.0 if latest.get("is_burst") else 0.0)

        # Total entries count
        all_entries = await ts_iface.query_line_analysis(game_id=game_id, limit=0)
        if isinstance(all_entries, list):
            snapshots_processed.set(len(all_entries))
    except Exception:
        pass

    # ── Historical profile ──────────────────────────────────────
    try:
        # Use the HistoricalEngine from under_timing
        from blm_v2.analytics.historical import HistoricalEngine
        he = HistoricalEngine()
        profile = he.get_profile(_LEAGUE)
        hist_games.set(profile.total_games)
        hist_snapshots.set(profile.total_snapshots)
        hist_regression_rate.set(profile.total_regression_rate)
        hist_under_rate.set(profile.under_rate)
        hist_excursion_p95.set(profile.excursion_p95)
    except Exception:
        pass


def render_metrics() -> bytes:
    """Render all registered Prometheus metrics in text format."""
    return generate_latest(REGISTRY)
