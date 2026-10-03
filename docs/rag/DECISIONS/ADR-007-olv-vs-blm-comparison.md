# ADR-007 — OLV vs BLM: always name the comparison line

Status: ACCEPTED
Retrieval keywords: OLV vs BLM, market vs fair, BLM vs CLV, benchmark, line type,
market_line_type, three benchmarks, ADR.

## What we decided

Every model-vs-market comparison names its line type. There are THREE benchmarks —
`BLM_vs_OLV` (fair vs opening line), `BLM_vs_CLV` (fair vs closing line) and
`market_vs_fair` (fair vs the live line) — and they are never conflated. BLM
compares its prediction against the MARKET line, never against its own prediction.

## Why

Comparing BLM against the wrong line (or against itself) yields a meaningless edge.
This has been a recurring issue in BLM work.

## What problem it solved

The M008-SCORE-M2 rework replaced a single unnamed "model vs market" block with
three named benchmark sections (olv / clv / checkpoint), each carrying
`market_line_type`, MAE/bias/beat with visible denominators, and retained signed
disparity.

## Alternatives rejected

- A single unnamed "model vs market" comparison.
- Comparing BLM to BLM.
- Using `PUSH` as a signal label (it is reserved for the settlement outcome;
  no-edge is `NO_EDGE`).

## Never change without explicit review

- The three named benchmarks.
- `market_line_type` on every comparison row.
- `PUSH` reserved for settlement only; `NO_EDGE` for a no-value position.
