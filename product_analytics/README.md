# Product-owner KPI analytics (isolated)

This package is a read-only analytics feature. It is deliberately outside
`blm_v4`, the production API, collector, engine, alerting and Auto-Bet code.

## Scope

- Historical SQL observations only in the first phase.
- Japanese candlesticks with a default 1-minute bucket and selectable intervals.
- A later real-time source can feed the same candle contract without changing
  production decision-making code.
- No writes, migrations, triggers, jobs, collector control or production API
  integration.

## KPI sign convention

For every market observation:

`kpi_value = market_line - final_score`

- Positive: final score finished UNDER the observed market line; render green.
- Negative: final score finished OVER the observed market line; render red.
- Zero: exact equality / PUSH; render neutral.

Candles aggregate `kpi_value`, not the raw market line:

- Open: first KPI value in the interval.
- High: maximum KPI value in the interval.
- Low: minimum KPI value in the interval.
- Close: last KPI value in the interval.

Candle colour is determined by the sign of the candle close, not by whether
close is above or below open. This prevents conventional bullish/bearish
candlestick semantics from being misinterpreted.

## Isolation contract

1. Run as a separate local process / Compose project and separate port.
2. Connect to the source database with a read-only account. For SQLite, use
   URI `mode=ro` and `PRAGMA query_only=ON`.
3. Never import BLM engine, collector, alert, model or betting modules.
4. Never write to source databases or create tables/indexes there.
5. No output from this package may be consumed by production or Auto-Bet.
6. No production service restart or deployment is part of this feature.

## Current implementation

`candles.py` contains the pure, database-independent aggregation contract.
It skips observations with missing timestamps, market lines or final scores;
missing values are never coerced to zero.

## Integration gate

The actual SQL adapter and chart page must not guess source schema. Before
connecting the database, identify and verify the exact observation table,
timestamp column, event identifier, market-line column, final-score source,
and event/result join. Use representative read-only queries and verify
timestamp units/timezone and duplicate handling.

Once that mapping is confirmed, the local UI can expose the interval selector
(default 1m) and render the returned OHLC values with a candlestick library.
The API must remain read-only and isolated from all production decision paths.
