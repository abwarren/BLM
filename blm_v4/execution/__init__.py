"""BLM EXECUTION — parlay execution subsystem (phase ①).

Scope lock: TOTAL → OVER | UNDER only.  Selection identity is
event + market + position; line/price are resolved fresh at execution
time.  DRY_RUN is the shipped default; LIVE requires an explicit env
change and never happens silently.
"""
from blm_v4.execution.adapter import (
    AdapterUnavailable,
    MarketObservation,
    SelectionResolver,
)
from blm_v4.execution.config import ExecutionConfig
from blm_v4.execution.parlay_matrix import build_matrix, describe_combo
from blm_v4.execution.selection_model import (
    AbortEvent,
    MODE_LIVE,
    MODES,
    ParlayJob,
    Selection,
    validate_selection,
)
from blm_v4.execution.total_executor import TotalExecutor
from blm_v4.execution.browser_bridge import (
    BridgeError,
    BridgeLedger,
    BridgeReceipt,
    BridgeTimeout,
    BridgeUnavailable,
    BrowserBridge,
    ExecutionBridge,
    ResolverBrowserBridge,
)

__all__ = [
    "AdapterUnavailable", "MarketObservation", "SelectionResolver",
    "ExecutionConfig", "build_matrix", "describe_combo", "AbortEvent",
    "MODE_LIVE", "MODES", "ParlayJob", "Selection", "validate_selection",
    "TotalExecutor",
    # Auto-Bet browser/extension execution bridge
    "BrowserBridge", "ResolverBrowserBridge", "ExecutionBridge",
    "BridgeLedger", "BridgeReceipt", "BridgeError", "BridgeTimeout",
    "BridgeUnavailable",
]
