import threading
from pathlib import Path

from blm_v4.clean_metrics import CleanMetricsStore

_lock = threading.Lock()
_store: CleanMetricsStore | None = None
_store_key: Path | None = None


def clean_store_for(db_path: Path) -> CleanMetricsStore:
    """Process-wide CleanMetricsStore singleton keyed by resolved path.

    The API previously constructed a store per /api/v4/live request; each
    construction runs schema init + meta queries on the clean DB, costing
    hundreds of milliseconds and piling up under poll load.  Construction
    is idempotent (initialize() is CREATE-if-absent + marker upsert), so
    one shared instance per path is safe.
    """
    global _store, _store_key
    key = Path(db_path).resolve()
    with _lock:
        if _store is None or _store_key != key:
            _store = CleanMetricsStore(key)
            _store_key = key
        return _store
