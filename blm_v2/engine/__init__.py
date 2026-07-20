"""
BLM V2 — Engine Package

All sub-engines in the BLM analysis pipeline. Each engine is an independent,
deterministic computation unit with a single calculate() or analyze() method.

Engines:
  - BLMEngine:     Top-level orchestrator. Injects sub-engines, runs all
                   calculations, returns the enriched snapshot.
  - Confidence:    Computes composite confidence from 5 component scores
                   (PACE, LINE, INJURY, BLOWOUT, TEAM_TOTAL) plus drift tracking.
  - Momentum:      Computes momentum scores, direction, velocity, acceleration,
                   and strength using exponential moving average (EMA).
  - TrapMeter:     Detects 7 trap types (bull, bear, reverse-bull, dead-market,
                   false-momentum, late, sharp) and computes a composite trap meter.
  - Market:        Analyzes market state: steam movement, reverse line movement,
                   market efficiency, fouls/line correlation, market momentum.

Conventions:
  - Every engine accepts typed inputs and returns typed outputs (TypedDict or
    Pydantic model).
  - Dependency injection: engines receive their dependencies via __init__.
  - All calculations are deterministic — same inputs → same outputs.
  - No engine depends on blm_v2.models; each defines its own payload types.
"""

from blm_v2.engine.confidence import (
    ConfidenceEngine,
    ConfidenceInput,
    ConfidenceOutput,
    ConfidenceDrift,
)
from blm_v2.engine.momentum import (
    MomentumEngine,
    MomentumInput,
    MomentumOutput,
)
from blm_v2.engine.trap_meter import (
    TrapMeterEngine,
    TrapMeterInput,
    TrapOutput,
    TrapType,
)
from blm_v2.engine.market import (
    MarketAnalyzer,
    MarketInput,
    MarketOutput,
)
from blm_v2.engine.blm_engine import (
    BLMEngine,
    BLMConfig,
    BLMResult,
)

__all__ = [
    # Sub-engines
    "ConfidenceEngine",
    "ConfidenceInput",
    "ConfidenceOutput",
    "ConfidenceDrift",
    "MomentumEngine",
    "MomentumInput",
    "MomentumOutput",
    "TrapMeterEngine",
    "TrapMeterInput",
    "TrapOutput",
    "TrapType",
    "MarketAnalyzer",
    "MarketInput",
    "MarketOutput",
    # Top-level orchestrator
    "BLMEngine",
    "BLMConfig",
    "BLMResult",
]
