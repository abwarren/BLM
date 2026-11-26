"""AUTOBET COHORT SELECTOR — three explicit, mutually exclusive modes.

Two qualification cohorts can gate a bet:

  strength   the incumbent strength/fingerprint gate (always evaluated)
  z          the z-score layer (AUTOBET_Z_COHORT_*), which reads the persisted
             deviation_residuals table and requires z strictly > its floor

Selector modes (persisted as ``betting_config.autobet_cohort_mode``):

  STRENGTH   only the strength gate decides; z is ignored
  Z_ONLY     only the z gate decides; the strength gate's verdict is ignored
  BOTH       both gates must pass

The selector is a POLICY control: it never places, resizes or reprices a bet,
never raises a limit, never changes DRY_RUN, and is fail-closed.  A mode is
read from the DB once at startup and can be flipped live through the audited
settings endpoint; the z layer itself remains disarmed unless its own
AUTOBET_Z_COHORT_ENABLED opt-in is set (two independent locks).
"""
from __future__ import annotations

import os
import threading

MODE_KEY = "autobet_cohort_mode"
STRENGTH = "STRENGTH"
Z_ONLY = "Z_ONLY"
BOTH = "BOTH"
VALID = (STRENGTH, Z_ONLY, BOTH)

DEFAULT_MODE = STRENGTH           # incumbent behaviour; never auto-widens
_lock = threading.Lock()
_mode: str = DEFAULT_MODE


def normalize(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return text if text in VALID else None


def get() -> str:
    with _lock:
        return _mode


def load_persisted_override(db_path: str | os.PathLike | None) -> None:
    """Read the DB row once at startup.  Bad/unreadable row keeps default."""
    if not db_path:
        return
    try:
        conn = __import__("sqlite3").connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM betting_config WHERE key=?", (MODE_KEY,)
            ).fetchone()
        finally:
            conn.close()
        value = normalize(row[0]) if row else None
    except Exception:
        value = None
    with _lock:
        global _mode
        _mode = value or DEFAULT_MODE


def apply_override(raw: object) -> str | None:
    """Publish a change immediately after an audited API write."""
    value = normalize(raw)
    if value is None:
        return None
    with _lock:
        global _mode
        _mode = value
    return value


def wants_strength(mode: str | None = None) -> bool:
    return (mode or get()) in (STRENGTH, BOTH)


def wants_z(mode: str | None = None) -> bool:
    return (mode or get()) in (Z_ONLY, BOTH)


# ── INDEPENDENT LAYER TOGGLES (three optional overlays) ────────────────
# STRENGTH remains the always-on incumbent gate.  Each overlay is an
# independent persisted ON/OFF with its own unit size.  All toggles default
# OFF, so an unconfigured deployment behaves exactly as before.
# Layer strength order (STRONGEST first) decides the label and unit when
# several layers match: Z2+TC-X > Z2+ > Z1+ > TC-X.
LAYER_Z1 = "Z1"
LAYER_Z2 = "Z2"
LAYER_Z2TCX = "Z2TCX"
LAYER_TCX = "TCX"
LAYERS = (LAYER_Z2TCX, LAYER_Z2, LAYER_Z1, LAYER_TCX)   # strongest first
LAYER_LABELS = {
    LAYER_Z1: "Z1+", LAYER_Z2: "Z2+",
    LAYER_Z2TCX: "Z2+TC-X", LAYER_TCX: "TC-X",
}
DEFAULT_UNITS = {LAYER_Z1: 1.0, LAYER_Z2: 2.0,
                 LAYER_Z2TCX: 3.0, LAYER_TCX: 1.0}
DEFAULT_ENABLED = {layer: False for layer in LAYERS}


def layer_enabled_key(layer: str) -> str:
    return f"cohort_{layer.lower()}_enabled"


def layer_units_key(layer: str) -> str:
    return f"cohort_{layer.lower()}_units"


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
    return value if value > 0 else default


def layer_config(layer: str) -> dict:
    """Enabled flag + unit size for one layer.  Env = default, DB = authority.

    Read from the persisted ``betting_config`` when a store connection is
    supplied; the three callers (fast lane, worker, API) pass their store's
    path so a UI flip takes effect immediately on the next pass.
    """
    if layer not in LAYERS:
        raise ValueError(f"unknown cohort layer {layer}")
    cfg = {"layer": layer, "label": LAYER_LABELS[layer],
           "enabled": _env_bool("AUTOBET_" + layer + "_ENABLED",
                                DEFAULT_ENABLED[layer]),
           "units": _env_float("AUTOBET_" + layer + "_UNITS",
                               DEFAULT_UNITS[layer])}
    return cfg


def resolve_layers(match: dict, store=None) -> dict:
    """Given which layers a game MATCHES, pick the strongest enabled one.

    ``match`` is ``{layer: bool}``.  Disabled layers never win.  Returns
    ``{"label": str|None, "layer": str|None, "units": float|None,
       "matched": [...], "config": {...}}``.  The incumbent STRENGTH gate is
    always evaluated separately; a no-match result leaves the default unit.
    """
    match = dict(match)
    # COMBINED tier: z>2 AND TC-X pace>1.16 is stronger than either alone.
    if match.get(LAYER_Z2) and match.get(LAYER_TCX):
        match[LAYER_Z2TCX] = True
    matched = [layer for layer in LAYERS if match.get(layer)]
    winner = None
    for layer in LAYERS:                 # strongest first
        if not match.get(layer):
            continue
        cfg = layer_config(layer)
        if store is not None:
            try:
                e = store.get_config(layer_enabled_key(layer), None)
                u = store.get_config(layer_units_key(layer), None)
                if e is not None:
                    cfg["enabled"] = str(e).strip().lower() in (
                        "1", "true", "yes", "on")
                if u is not None:
                    parsed = float(u)
                    if parsed > 0:
                        cfg["units"] = parsed
            except Exception:
                pass                     # env/code defaults stay authoritative
        if cfg["enabled"]:
            winner = (layer, cfg)
            break
    if winner is None:
        return {"label": None, "layer": None, "units": None,
                "matched": matched, "config": None}
    layer, cfg = winner
    return {"label": cfg["label"], "layer": layer, "units": cfg["units"],
            "matched": matched, "config": cfg}
