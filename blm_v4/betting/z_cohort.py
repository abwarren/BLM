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
  ``blm_metrics_clean.db.deviation_residuals`` rows — or, when that row is
  missing/stale, computed from the current ``clean_projections`` row against
  the same prior-only benchmark (``AUTOBET_Z_COHORT_ON_DEMAND``, default on).
  The stored row always wins when it is fresh, and nothing is written.
* NO DEPLOY / NO ACTIVATION.  Enabling the env alone changes nothing in a
  running process; a restart plus the operator's own arm step is required.

The z stored by ``blm_v4.deviation`` is standardized against prior residuals
of the same league x quarter bucket (line - fair).  It is descriptive: this
module only compares it to a numeric floor; it does not claim edge.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

ENABLE_ENV = "AUTOBET_Z_COHORT_ENABLED"
FLOOR_ENV = "AUTOBET_Z_COHORT_MIN_Z"
MAX_AGE_ENV = "AUTOBET_Z_COHORT_MAX_AGE_S"
DB_ENV = "BLM_METRICS_DB"
ONDEMAND_ENV = "AUTOBET_Z_COHORT_ON_DEMAND"
WINDOW_ENV = "AUTOBET_Z_COHORT_WINDOW_N"

DEFAULT_FLOOR = 1.0
DEFAULT_MAX_AGE_S = 120.0
DEFAULT_ONDEMAND = True
#: The computed path standardises against the MOST RECENT prior residuals in
#: the bucket, not all history.  All history is ~1M rows per bucket (1-2 s per
#: call, 80 s cold) and the market's residual distribution drifts, so a recent
#: window is both affordable and more representative.  The stored row — which
#: IS full-history — still wins whenever it is fresh.
DEFAULT_WINDOW_N = 20000

LAYER_NAME = "AUTOBET_Z_COHORT"
DISTANCE = "deviation_residuals"
PROJECTIONS = "clean_projections"

#: Mirror of blm_v4.deviation's constants.  The maths below must stay BYTE-
#: identical to the stored-z producer, or a computed z and a stored z for the
#: same observation would disagree.  Parity is pinned by a test.
Z_EPS = 1e-9
EXPLORATORY_LIMIT = 100
PROVISIONAL_LIMIT = 1000

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


def on_demand_enabled() -> bool:
    """May a missing/stale persisted row be computed from the live observation?

    Read-only either way, and the persisted row always wins when it is fresh —
    this only stops a stalled writer from starving the cohort layer.
    """
    return _env_bool(ONDEMAND_ENV, DEFAULT_ONDEMAND)


def window_n() -> int:
    """How many recent prior residuals the computed benchmark uses."""
    return max(2, int(_env_float(WINDOW_ENV, DEFAULT_WINDOW_N)))


# ── the stored-z maths, mirrored from blm_v4.deviation ──────────────────
# Same formulas, same rounding, same Z_EPS: a computed z and the stored z for
# the same observation must be identical (pinned by a parity test).

def _benchmark_key(classification: Any, period: Optional[int]) -> str:
    cls = str(classification or "UNKNOWN")
    return f"{cls}|Q{period}" if period else f"{cls}|UNK"


def _normalize_period(label: Any) -> Optional[int]:
    m = re.search(r"(\d+)", str(label or ""))
    if not m:
        return None
    quarter = int(m.group(1))
    return quarter if 1 <= quarter <= 4 else None


def _residual(line: Any, projected: Any) -> Optional[float]:
    if line is None or projected is None:
        return None
    return round(float(line) - float(projected), 2)


def _mean_std(n: int, s: float, ss: float):
    if n <= 0:
        return 0, None, None
    mean = s / n
    if n < 2:
        return n, mean, None
    var = (ss - (s * s) / n) / (n - 1)
    if var < 0.0:
        var = 0.0
    return n, mean, math.sqrt(var)


def _standardize(residual: Optional[float], mean: Optional[float],
                 std: Optional[float]) -> Optional[float]:
    if residual is None or mean is None or std is None:
        return None
    if std <= Z_EPS:
        return None
    return round((residual - mean) / std, 4)


def _maturity(n: int) -> str:
    if n < EXPLORATORY_LIMIT:
        return "EXPLORATORY"
    if n < PROVISIONAL_LIMIT:
        return "PROVISIONAL"
    return "ESTABLISHED"


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


def _stored_z(conn: sqlite3.Connection, base: str) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT source_game_id, captured_at, z_score FROM {DISTANCE} "
        "WHERE source_game_id BETWEEN ? AND ? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (base, base + "\uffff")).fetchone()
    if not row:
        return {"z": None, "captured_at": None, "source_game_id": base,
                "reason": MISSING_Z, "source": "stored"}
    z = _numeric(row[2])
    if z is None:
        return {"z": None, "captured_at": row[1], "source_game_id": row[0],
                "reason": MISSING_Z, "source": "stored"}
    return {"z": z, "captured_at": row[1], "source_game_id": row[0],
            "reason": None, "source": "stored"}


def _apply_age(result: dict[str, Any], as_of_ts: Optional[float],
               age_limit: float) -> dict[str, Any]:
    """Freshness must be PROVABLE: a missing timestamp is STALE_Z."""
    if result.get("reason") is not None:
        return result
    row_ts = _parse_iso(result.get("captured_at"))
    if as_of_ts is None or row_ts is None:
        result["z"] = None
        result["reason"] = STALE_Z
        return result
    age = as_of_ts - row_ts
    if age < -1.0 or age > age_limit:
        result["z"] = None
        result["reason"] = STALE_Z
    return result


def _computed_z(conn: sqlite3.Connection, base: str, as_of_ts: Optional[float],
                age_limit: float) -> Optional[dict[str, Any]]:
    """Standardise the CURRENT clean projection against the prior benchmark.

    Read-only.  The projection row is the same one the pace pipeline just
    wrote for this observation, so z is as fresh as capture itself — no
    dependence on the deviation writer's cadence.  The benchmark is built from
    residual rows STRICTLY EARLIER than this observation (same bucket), so no
    future information leaks in.

    Returns ``None`` when the DB has no usable projection table at all, so the
    caller can fall back to the persisted-row verdict (an older DB without
    ``clean_projections`` must not turn MISSING_Z into DB_UNAVAILABLE).
    """
    try:
        row = conn.execute(
            f"SELECT source_game_id, captured_at, classification, period_label, "
            f"       progress_pct, live_total_line, projected_final_total, "
            f"       COALESCE(terminal, 0), observation_id "
            f"FROM {PROJECTIONS} WHERE source_game_id BETWEEN ? AND ? "
            f"ORDER BY captured_at DESC, id DESC LIMIT 1",
            (base, base + "\uffff")).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return {"z": None, "captured_at": None, "source_game_id": base,
                "reason": MISSING_Z, "source": "computed"}
    (gid, captured, cls, period_label, progress, line, projected,
     terminal, obs_id) = row
    base_result = {"z": None, "captured_at": captured, "source_game_id": gid,
                   "reason": MISSING_Z, "source": "computed"}
    if int(terminal or 0) or (progress is not None
                              and float(progress) >= 100.0):
        return base_result                    # no live observation to price
    residual = _residual(line, projected)
    if residual is None:
        return base_result
    row_ts = _parse_iso(captured)
    if row_ts is None or as_of_ts is None:
        base_result["reason"] = STALE_Z
        return base_result
    age = as_of_ts - row_ts
    if age < -1.0 or age > age_limit:
        base_result["reason"] = STALE_Z
        return base_result
    key = _benchmark_key(cls, _normalize_period(period_label))
    try:
        n, s, ss = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(r), 0), "
            f"       COALESCE(SUM(r * r), 0) FROM ("
            f"  SELECT market_trajectory_residual AS r FROM {DISTANCE}"
            f"  WHERE benchmark_key = ?"
            "    AND (captured_at < ? OR (captured_at = ? AND observation_id < ?))"
            "  ORDER BY captured_at DESC, id DESC LIMIT ?)",
            (key, captured, captured, int(obs_id or 0),
             window_n())).fetchone()
    except sqlite3.Error:
        return None
    n, mean, std = _mean_std(int(n), float(s), float(ss))
    z = _standardize(residual, mean, std)
    if z is None:
        base_result.update({"benchmark_key": key, "benchmark_n": n,
                            "benchmark_mean": mean, "benchmark_std": std})
        return base_result
    return {"z": z, "captured_at": captured, "source_game_id": gid,
            "reason": None, "source": "computed", "benchmark_key": key,
            "benchmark_n": n, "benchmark_mean": mean,
            "benchmark_std": std, "benchmark_status": _maturity(n),
            "residual": residual, "observation_id": obs_id,
            "progress_pct": progress}


def latest_z(source_game_id: Any, clean_db_path: Optional[Path] = None,
             as_of: Any = None, max_age: Optional[float] = None
             ) -> dict[str, Any]:
    """Latest available z for a game, read-only, fail-closed.

    Two sources, in order:

    1. the persisted ``deviation_residuals`` row — authoritative and immutable;
    2. when that row is absent, unprovable or stale, the CURRENT
       ``clean_projections`` row standardised against the same prior-only
       benchmark (``AUTOBET_Z_COHORT_ON_DEMAND``, default on).  Nothing is
       written; this exists so a stalled deviation writer cannot starve the
       cohort layer of a value the data already supports.

    Accepts a base or ``#iN`` instance id; the indexed base-id range is
    searched so no suffix-vs-base mismatch can drop the read.  No row, an
    unreadable DB, a non-finite z or an unprovable age yields ``z=None`` and a
    reason — never a fabricated value and never an exception.
    """
    raw = "" if source_game_id is None else str(source_game_id)
    base = raw.split("#", 1)[0]
    if not base:
        return {"z": None, "captured_at": None, "source_game_id": None,
                "reason": MISSING_Z}
    db = Path(clean_db_path).expanduser() if clean_db_path else _metrics_db_path()
    age_limit = max_age_s() if max_age is None else max(0.0, float(max_age))
    as_of_ts = _parse_iso(as_of)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=1")
        try:
            result = _apply_age(_stored_z(conn, base), as_of_ts, age_limit)
            if result["reason"] is None or not on_demand_enabled():
                return result
            computed = _computed_z(conn, base, as_of_ts, age_limit)
            return result if computed is None else computed
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {"z": None, "captured_at": None, "source_game_id": base,
                "reason": DB_UNAVAILABLE}


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
    record["z_source"] = lookup.get("source")
    if lookup.get("benchmark_status"):
        record["z_benchmark"] = {
            "key": lookup.get("benchmark_key"),
            "n": lookup.get("benchmark_n"),
            "mean": lookup.get("benchmark_mean"),
            "std": lookup.get("benchmark_std"),
            "status": lookup.get("benchmark_status"),
            "residual": lookup.get("residual"),
        }
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
