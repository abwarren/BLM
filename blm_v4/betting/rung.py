"""Auto-Bet RUNG — the ONE canonical rung calculation + shared pre-execution
validator (operator directive 2026-10-03, Production Wiring Gates PW-01..16).

There is EXACTLY ONE production implementation of the rung arithmetic and
EXACTLY ONE UNDER decision, and it lives here:

    rungs_moved = (current_line - trigger_line) / rung_size
    rungs_moved >= -1  ->  ALLOW
    rungs_moved <  -1  ->  REJECT

Every execution producer (manual UI command, autonomous engine, and the final
pre-execution validation) calls THIS module.  No producer re-implements the
formula.

ARCHITECTURE (operator correction 2026-10-03): the BLM Alert Monitor is the
ONLY source of a betting opportunity.  Auto-Bet CONSUMES an existing alert; it
never discovers an opportunity, never establishes a new baseline, and never
creates a new trigger line.  The ``trigger_line`` is the FROZEN alert line
(``under_outcome.trigger_observation`` / the served under_alert block) and is
the reference for BOTH manual and autonomous betting.  There is no separate
manual reference line.

Pure functions only — no DB, no network, no config, no provider.  Fail CLOSED.
"""
from __future__ import annotations

import math
from typing import Any, Optional

#: The inclusive boundary, expressed in RUNGS (never a point distance).
RUNG_ALLOW_MIN = -1.0
#: Float tolerance so an intended exact -1 rung is not rejected by rounding.
EPS = 1e-9

#: Decision vocabulary.
ALLOW = "ALLOW"
REJECT = "REJECT"

#: Refusal reasons (fail-closed) — stable strings for the audit record.
R_MARKET_MISSING = "rung_market_missing"          # current/trigger line absent
R_RUNG_AMBIGUOUS = "rung_size_ambiguous"          # missing/0/neg/inf/inconsistent
R_MOVED_TOO_FAR = "more_than_one_rung_down"       # the real rung rejection
R_IDENTITY = "rung_identity_unverified"           # game/market/selection ambiguous


def _f(value: Any) -> Optional[float]:
    """Finite float, else None (booleans excluded) — fail closed."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def infer_rung_size(observed_lines) -> Optional[float]:
    """The market's rung (tick) size DERIVED from observed line values.

    Never a global constant — a live-market quantity (PW-06).  Fail closed
    (None) whenever the size is not provable:

      * fewer than 3 distinct values (< 2 increments)        -> None (AMBIGUOUS)
      * fewer than 2 positive increments                     -> None (AMBIGUOUS)
      * increments not exact integer multiples of the tick   -> None (INCONSISTENT)
      * tick <= 0                                            -> None
    """
    vals = sorted({round(float(v), 6) for v in (observed_lines or ())
                   if _f(v) is not None})
    if len(vals) < 3:
        return None
    steps = [round(vals[i + 1] - vals[i], 6) for i in range(len(vals) - 1)]
    steps = [s for s in steps if s > 0]
    if len(steps) < 2:
        return None
    tick = min(steps)
    if tick <= 0:
        return None
    for s in steps:
        k = round(s / tick)
        if k < 1 or abs(s - k * tick) > 1e-6:
            return None
    return tick


def rungs_moved(current_line, trigger_line, rung_size) -> Optional[float]:
    """Signed count of rungs the live line has moved from the FROZEN trigger.

        rungs_moved = (current_line - trigger_line) / rung_size

    None whenever any operand is unprovable (fail closed) — never a guess.
    """
    cur, trig, rung = _f(current_line), _f(trigger_line), _f(rung_size)
    if cur is None or trig is None or rung is None or rung <= 0:
        return None
    moved = (cur - trig) / rung
    return moved if math.isfinite(moved) else None


def under_rung_decision(current_line, trigger_line, rung_size) -> dict:
    """THE canonical UNDER execution decision, expressed in RUNGS.

        rungs_moved >= -1 -> ALLOW
        rungs_moved <  -1 -> REJECT

    Returns ``{"decision", "reason", "rungs_moved"}``.  Ambiguous / unprovable
    inputs fail CLOSED (REJECT).
    """
    if _f(current_line) is None or _f(trigger_line) is None:
        return {"decision": REJECT, "reason": R_MARKET_MISSING,
                "rungs_moved": None}
    moved = rungs_moved(current_line, trigger_line, rung_size)
    if moved is None:
        return {"decision": REJECT, "reason": R_RUNG_AMBIGUOUS,
                "rungs_moved": None}
    if moved >= RUNG_ALLOW_MIN - EPS:
        return {"decision": ALLOW, "reason": "within_one_rung_down",
                "rungs_moved": round(moved, 6)}
    return {"decision": REJECT, "reason": R_MOVED_TOO_FAR,
            "rungs_moved": round(moved, 6)}


def _validate_identity(game: dict, market: str, selection: str) -> Optional[str]:
    """Game / market / selection identity must be present and unambiguous.

    Returns a refusal reason, or None when identity is verified.  A game with
    no UNDER alert block has no opportunity (HARD ARCHITECTURAL RULE: no alert
    -> no Auto-Bet opportunity) and is rejected as unverified identity.
    """
    if not str(game.get("game_id") or "").strip():
        return R_IDENTITY
    if not game.get("under_alert"):
        return R_IDENTITY
    if market not in ("TOTAL", "MONEYLINE_TOTAL_UNDER"):
        return R_IDENTITY
    if selection != "UNDER":
        return R_IDENTITY
    return None


def validate_execution(game: dict, *, market: str = "TOTAL",
                       selection: str = "UNDER") -> dict:
    """THE shared pre-execution validation (PW-04).

    Independently verifies, immediately before execution:
      * game identity  (canonical game_id present)
      * market identity (TOTAL)
      * selection       (UNDER)
      * the FROZEN trigger line (from the alert; immutable — PW-05)
      * the CURRENT line (fresh live market; the caller must have reacquired it)
      * the OBSERVED rung size (from the live market; PW-06)
      * the rung movement (the ONE decision; PW-09)

    Returns a dict carrying every value the audit record needs, plus
    ``decision`` (ALLOW/REJECT) and ``reason``.  No command may execute unless
    ``decision == ALLOW``.
    """
    reason = _validate_identity(game, market, selection)
    ua = game.get("under_alert") or {}
    mkt = game.get("market") or {}
    trigger_line = _f(ua.get("trigger_line"))
    current_line = _f(mkt.get("total_line"))
    observed_lines = mkt.get("observed_lines")
    rung_size = infer_rung_size(observed_lines)
    out = {
        "game_id": game.get("game_id"),
        "market": market,
        "selection": selection,
        "alert_id": ua.get("alert_id"),
        "trigger_line": trigger_line,
        "trigger_checkpoint_percent": (ua.get("trigger_progress") or
                                       ua.get("checkpoint")),
        "trigger_timestamp": ua.get("trigger_captured_at"),
        "current_line": current_line,
        "rung_size": rung_size,
        "rungs_moved": None,
        "decision": REJECT,
        "reason": reason,
    }
    if reason is not None:
        return out
    if trigger_line is None or current_line is None:
        out["reason"] = R_MARKET_MISSING
        return out
    decision = under_rung_decision(current_line, trigger_line, rung_size)
    out["rungs_moved"] = decision["rungs_moved"]
    out["decision"] = decision["decision"]
    out["reason"] = decision["reason"]
    return out


# ── the two producers converge here (PW-02 / PW-03 / PW-13) ────────────────
# Manual and autonomous are NOT separate implementations: each builds the game
# payload (manual from the selected alert + a fresh market read; autonomous from
# the live payload) and calls ``validate_execution`` — the ONE validator.
def manual_execution_command(game: dict, *, market: str = "TOTAL",
                             selection: str = "UNDER") -> dict:
    """The manual producer (BLM UI UNDER command against an existing alert)."""
    return validate_execution(game, market=market, selection=selection)


def autonomous_execution_command(game: dict, *, market: str = "TOTAL",
                                 selection: str = "UNDER") -> dict:
    """The autonomous producer (Auto-Bet engine against the same alert)."""
    return validate_execution(game, market=market, selection=selection)
