# BLM (Betting Logic Model) — Architectural Constitution

## Purpose

BLM is a research and decision-support platform for evaluating live basketball betting
opportunities. It does not predict games directly. Instead, it measures whether the
sportsbook's live market has deviated from historically expected behaviour and
identifies high-probability entry timing for UNDER opportunities.

The system is evidence-driven, league-specific, modular, and continuously improved
through historical data.

## Core Philosophy

1. **Historical data is the primary source of truth.**
2. **Every recommendation must be statistically explainable.**
3. **Every league is independent.**
4. **Market behaviour is as important as game behaviour.**
5. **Timing is more valuable than prediction.**

## Core Concepts

### League Registry
Defines the statistical identity of every supported league. Contains pace
characteristics, scoring distributions, volatility (σ), inflation ranges,
regression behaviour, quarter characteristics, historical percentiles, timing
characteristics. Every calculation must reference the current league. No values
may be shared between leagues.

### Snapshot
The smallest unit of information. One complete market state at one point in time:
timestamp, score, quarter, clock, total, spread, team totals, odds, alternate
markets. Immutable and append-only — never edited after storage.

### Historical Database
Stores every snapshot, every completed game, derived metrics, and calculated
statistics. Nothing relies on memory. Everything is reproducible from stored data.

### Market State
What the bookmaker is currently pricing. Totals, spreads, team totals, odds,
movement, ladder position, bookmaker margin. Distinct from Game State.

### Game State
What is actually happening on the virtual court. Score, pace, quarter, possessions,
scoring rate, efficiency, time remaining.

### Derived Metrics
Calculated from historical and live data. Never manually entered. Examples:
Current Pace, Expected Pace, Inflation Score, Historical Percentile, Regression
Index, Compression Score, Freeze Score, Similarity Score, Under Timing Score.

### Trap Meter
Confidence indicator estimating whether the market is behaving normally or
exhibiting characteristics associated with bookmaker traps. Not a prediction engine.

### Historical Inflation Engine
Determines whether the current total is statistically inflated relative to
historical opening lines, similar games, expected pace, league distributions,
and historical excursions. Outputs: Inflation Score, Inflation Confidence,
Historical Fair Line.

### Similarity Engine
Finds historically similar games by opening total, current total, score, quarter,
pace, spread, game clock, volatility. Outputs comparable games, historical
outcomes, expected regression.

### Freeze Detector
Detects periods where score changes but the line does not move — temporary
pricing inefficiency.

### Compression Detector
Detects situations where odds move but the line remains unchanged — often
precedes ladder movement.

### Regression Engine
Estimates whether pace, scoring, efficiency, or line are returning toward
historical expectation. Outputs: Regression Probability, Expected Final Total.

### Under Timing Engine
The final decision layer. Combines outputs from Trap Meter, Inflation Engine,
Similarity Engine, Regression Engine, Freeze, Compression. Outputs:
PASS | WATCH | WAIT | UNDER READY.

### Confidence Engine
Every prediction includes confidence. Confidence depends on sample size,
similarity quality, historical coverage, data completeness, model agreement.
Confidence decreases when evidence is weak.

### Research Console
A visualization layer. Performs no calculations. Displays: live market,
historical context, model outputs, recommendations, supporting evidence.

## Architectural Layers

```
Presentation Layer
    Research Console
        ↓
API Layer
    Flask
        ↓
Decision Layer
    Under Timing Engine
        ↓
Analysis Layer
    Trap Meter
    Inflation Engine
    Similarity Engine
    Regression Engine
    Freeze Detector
    Compression Detector
        ↓
Historical Layer
    Historical Database
        ↓
Collection Layer
    Snapshot Collector
    Playwright
        ↓
External Source
    PokerBet / BetConstruct
```

## Design Rules

- Every module must have a single responsibility.
- Modules communicate through well-defined interfaces (database tables, API endpoints).
- No module should duplicate another module's logic.
- Historical data is immutable.
- Derived values must be reproducible.
- League-specific statistics are isolated.
- Presentation never contains business logic.
- Business logic never contains scraping logic.
- Scraping never performs analytics.

## Decision Hierarchy

```
Raw Data → Snapshots → Historical Storage → Derived Metrics →
Historical Comparison → Statistical Models → Confidence Assessment →
Timing Engine → Recommendation → Research Console
```

## Non-Negotiable Principles

- BLM is not a betting bot; it is a statistical decision-support system.
- Recommendations must be evidence-based, not heuristic alone.
- Every output must be traceable back to historical and live data.
- Architecture must remain modular, explainable, and extensible.
- The primary objective is not to predict the highest score, but to identify
  statistically favourable UNDER entry timing by detecting when the live market
  diverges from historical expectations and begins to regress.
