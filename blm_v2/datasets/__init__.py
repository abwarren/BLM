"""BLM V2 — Dataset Builder.

Flattens the nested BLM engine snapshots into ML-ready flat rows:
every snapshot becomes one sample; per-game outcome targets are attached
from the game's final snapshot.

Feature/target separation is strict: FEATURES are inputs, TARGETS are
outcomes.  They are disjoint by construction (asserted in tests).
"""

from __future__ import annotations

from blm_v2.datasets.builder import DatasetBuilder, FEATURES, TARGETS

__all__ = ["DatasetBuilder", "FEATURES", "TARGETS"]
