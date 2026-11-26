"""Z-SCORE COHORT for auto-bet — ADDITIVE and DISARMED BY DEFAULT.

This module implements the operator-requested "z > 1" tradeable cohort as a
SECOND, independent qualification layer on top of the incumbent
strength/fingerprint gate in :mod:`blm_v4.betting.qualify`.

Design invariants
-----------------
* DISARMED BY DEFAULT.  ``AUTOBET_Z_COHORT_ENABLED`` must be explicitly true;
  when false the layer is a no-op and performs NO database I/O at all.
* ADDITIVE.  It can only REMOVE a game the incumbent gate would have passed.
  It never widens the gate, never changes stake, ladder, rung or safety.
* FAIL CLOSED.  Missing / non-finite z, an unreadable DB, a stale or
  unprovable observation time, or z <= floor all refuse.  UNAVAILABLE is
  never a pass.
* READ ONLY.  Every connection is ``file:...?mode=ro`` with ``query_only=1``.
  No table is written; the z value is read from the already-persisted
  ``blm_metrics_clean.db.deviation_residuals`` rows.
* NO DEPLOY / NO ACTIVATION.  Enabling the env alone changes nothing in a
  running process; a restart plus the operator's own arm step is required.

The z stored by ``blm_v4.deviation`` is standardized against prior residuals
of the same league x quarter bucket (line - fair).  It is descriptive: this
module only compares it to a numeric floor; it does not claim edge.
"""
from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

ENABLE_ENV = "AUTOBET_Z_COHORT_ENABLED"
FLOOR_ENV = "AUTOBET_Z_COHORT_MIN_Z"
MAX_AGE_ENV = "AUTOBET_Z_COHORT_MAX_AGE_S"
DB_ENV = "BLM_METRICS_DB"

DEFAULT_FLOOR = 1.0
DEFAULT_MAX_AGE_S = 120.0

LAYER_NAME = "AUTOBET_Z_COHORT"
DISTANCE = "deviation_residuals"

# verdict reasons (stable strings for the audit record)
DISARMED = "DISARMED"
MISSING_Z = "MISSING_Z"
STALE_Z = "STALE_Z"
BELOW_Z_FLOOR = "BELOW_Z_FLOOR"
QUALIFIED = "Z_QUALIFIED"
DB_UNAVAILABLE = "DB_UNAVAILABLE"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def enabled() -> bool:
    """True only with an explicit opt-in.  Never true from a code default."""
    return _env_bool(ENABLE_ENV, False)


def z_floor() -> float:
    return _env_float(FLOOR_ENV, DEFAULT_FLOOR)


def max_age_s() -> float:
    return max(0.0, _env_float(MAX_AGE_ENV, DEFAULT_MAX_AGE_S))


def _numeric(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metrics_db_path() -> Path:
    configured = os.environ.get(DB_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "blm_metrics_clean.db"


def _parse_iso(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def latest_z(source_game_id: Any, clean_db_path: Optional[Path] = None,
             as_of: Any = None, max_age: Optional[float] = None
             ) -> dict[str, Any]:
    """Latest persisted z for a game, read-only, fail-closed.

    Accepts a base or ``#iN`` instance id; searches the indexed base-id range
    so no suffix-vs-base mismatch can silently drop the read.  No row, an
    unreadable DB, a non-finite z or a stale row yields ``z=None`` and a
    reason — never a fabricated value and never an exception.
    """
    raw = "" if source_game_id is None else str(source_game_id)
    base = raw.split("#", 1)[0]
    if not base:
        return {"z": None, "captured_at": None, "source_game_id": None,
                "reason": MISSING_Z}
    db = Path(clean_db_path).expanduser() if clean_db_path else _metrics_db_path()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=1")
        try:
            row = conn.execute(
                f"SELECT source_game_id, captured_at, z_score FROM {DISTANCE} "
                "WHERE source_game_id BETWEEN ? AND ? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (base, base + "\uffff")).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {"z": None, "captured_at": None, "source_game_id": base,
                "reason": DB_UNAVAILABLE}
    if not row:
        return {"z": None, "captured_at": None, "source_game_id": base,
                "reason": MISSING_Z}
    z = _numeric(row[2])
    if z is None:
        return {"z": None, "captured_at": row[1], "source_game_id": row[0],
                "reason": MISSING_Z}
    result = {"z": z, "captured_at": row[1], "source_game_id": row[0],
              "reason": None}
    age_limit = max_age_s() if max_age is None else max(0.0, float(max_age))
    as_of_ts = _parse_iso(as_of)
    row_ts = _parse_iso(row[1])
    # FAIL CLOSED: freshness must be PROVABLE.  Without both timestamps the
    # layer cannot show the z belongs to this observation, so it refuses
    # instead of assuming the row is current.
    if as_of_ts is None or row_ts is None:
        result["z"] = None
        result["reason"] = STALE_Z
        return result
    age = as_of_ts - row_ts
    if age < -1.0 or age > age_limit:
        result["z"] = None
        result["reason"] = STALE_Z
    return result


def evaluate(game: dict, clean_db_path: Optional[Path] = None) -> dict[str, Any]:
    """Additive verdict for one game dict.

    Disabled → ``{"applicable": False, "qualified": True, ...}`` (a no-op).
    Enabled → ``qualified`` is True only for a finite z strictly above the
    configured floor and inside the freshness window.
    """
    record: dict[str, Any] = {
        "layer": LAYER_NAME, "applicable": False, "qualified": True,
        "z": None, "floor": z_floor(), "reason": DISARMED,
    }
    if not enabled():
        return record
    record["applicable"] = True
    gid = (game.get("game_id") if isinstance(game, dict) else None)
    projector = (game.get("projector") or {}) if isinstance(game, dict) else {}
    lookup = latest_z(gid, clean_db_path, as_of=projector.get("captured_at"))
    record["z"] = lookup["z"]
    record["z_at"] = lookup["captured_at"]
    record["z_game"] = lookup["source_game_id"]
    if lookup["reason"] is not None:
        record["qualified"] = False
        record["reason"] = lookup["reason"]
        return record
    floor = z_floor()
    if lookup["z"] <= floor:                      # STRICTLY greater than floor
        record["qualified"] = False
        record["reason"] = BELOW_Z_FLOOR
        return record
    record["qualified"] = True
    record["reason"] = QUALIFIED
    return record
