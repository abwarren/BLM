"""Pure KPI candlestick aggregation for the isolated product-owner dashboard.

This module has no database, BLM runtime, collector, model or betting imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor, isfinite
from typing import Iterable, Mapping, Any


@dataclass(frozen=True)
class KpiCandle:
    """OHLC candle over market-line-minus-final-score values."""

    time: int  # UTC epoch seconds at the start of the bucket
    open: float
    high: float
    low: float
    close: float
    color: str  # green=UNDER, red=OVER, neutral=PUSH
    observations: int


def _epoch_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            # Naive timestamps are rejected: timezone must be explicit.
            return None
        return dt.astimezone(timezone.utc).timestamp()
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc).timestamp()
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def aggregate_kpi_candles(
    observations: Iterable[Mapping[str, Any]],
    interval_seconds: int = 60,
) -> list[KpiCandle]:
    """Aggregate observations into OHLC candles.

    Each observation must provide `timestamp`, `market_line` and
    `final_score`. KPI value is market_line - final_score: positive is UNDER
    (green), negative is OVER (red), and zero is PUSH (neutral).

    Buckets are UTC epoch-aligned. Missing or non-finite values are skipped;
    they are never interpreted as zero. Input order is used to break equal
    timestamp ties deterministically.
    """
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise TypeError("interval_seconds must be an integer")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")

    prepared: list[tuple[float, int, float]] = []
    for index, row in enumerate(observations):
        timestamp = _epoch_seconds(row.get("timestamp"))
        try:
            market_line = float(row.get("market_line"))
            final_score = float(row.get("final_score"))
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp is None or not isfinite(market_line) or not isfinite(final_score):
            continue
        prepared.append((timestamp, index, market_line - final_score))

    prepared.sort(key=lambda item: (item[0], item[1]))

    grouped: dict[int, list[float]] = {}
    for timestamp, _index, value in prepared:
        bucket = floor(timestamp / interval_seconds) * interval_seconds
        grouped.setdefault(bucket, []).append(value)

    candles: list[KpiCandle] = []
    for bucket in sorted(grouped):
        values = grouped[bucket]
        close = values[-1]
        color = "green" if close > 0 else "red" if close < 0 else "neutral"
        candles.append(
            KpiCandle(
                time=bucket,
                open=values[0],
                high=max(values),
                low=min(values),
                close=close,
                color=color,
                observations=len(values),
            )
        )
    return candles
