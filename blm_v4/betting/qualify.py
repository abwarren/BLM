"""AUTO-BET QUALIFICATION — strength × fingerprint (directive 2026-11-15).

The 30-day backtest (2026-10-16 → 2026-11-15, 75–92 % band, shortfall ≥ 0,
pace > 1.04× league average, cyber-2k26 excluded) showed the edge is NOT flat
across required-pace strength: 1.20×+ runs 90.5–95.0 %, 1.10–1.20 77.6 %, and
below 1.10 only 71.4 % — and below 1.10, a game with Q3 data and NO fingerprint
firing has near-zero edge.

So this module classifies an alert that has already cleared the existing
execution gate into a TIER by ``req_ratio`` and, for the two weaker tiers,
requires a fingerprint to be TRUE.  It is an ADDITIVE qualification layer: it
never re-decides the alert, never changes stake sizing, the ladder, the rung or
any safety limit.  Every caller sees the same three-state semantics as
:mod:`blm_v4.live_analytics.under_fingerprints` — UNAVAILABLE is never a pass.

Tiers (directive §3.4)::

    A   req_ratio >= 1.20            ALWAYS qualifies (Q3 not required)
    B   1.10 <= req_ratio < 1.20     qualifies with C3/C5/R2, or without Q3
    C   1.04 <  req_ratio < 1.10     qualifies ONLY with C3/C5/R2
    –   req_ratio <= 1.04            never qualifies

The fingerprint authority is :func:`under_fingerprints.evaluate_fingerprints`
imported and called as-is (directive §6) — the C3/C5/R2 conditions, their
thresholds and the three-state HAIL logic live there and are not reimplemented
here.  Only the KEYS that may qualify are narrowed: the directive lists C3, C5
and R2, so C1 and C2 — recorded context elsewhere — cannot make a tier pass.

Shortfall: the directive states ``required > actual`` (shortfall ≥ 0) as an
existing, unchanged condition.  In code that condition lives behind
``trade_window.TRADE_SHORTFALL_ENABLED`` (production currently sets it to 0), so
this gate reads the SAME toggle instead of silently re-arming a leg the
operator switched off.  Pass ``shortfall=None`` to honour the toggle, or a bool
to override for analysis.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from blm_v4.live_analytics.under_alert import REQUIRED_MARGIN
from blm_v4.live_analytics.under_fingerprints import evaluate_fingerprints
# Read LIVE (module attribute) rather than binding the values: the progress
# ceiling and the shortfall switch are environment-driven at import time in
# trade_window, and this gate must agree with whatever the process is running.
from blm_v4 import trade_window as TW
from blm_v2.telemetry.logging import get_logger

logger = get_logger("autobet_gate")

LAYER_NAME = "AUTOBET_STRENGTH_FP_QUALIFICATION"

#: §3.1 — permanently excluded from auto-bet (a hard block, not a tier).
CYBER_2K26_SLUG = "cyber-basketball-2k26-matches"

#: §3.2 — the alert's own execution band (unchanged; the executor already
#: enforces the same window, so this can only ever agree with it).  Read from
#: ``trade_window`` at call time so one configuration governs both.
PROGRESS_MIN = 75.0
PROGRESS_MAX = 92.0

#: §3.4 — strength tiers.  ``TIER_A_MIN`` is INCLUSIVE, ``TIER_B_MIN`` is
#: INCLUSIVE, ``TIER_C_MIN`` is the production margin and STRICT.
TIER_A_MIN = 1.20
TIER_B_MIN = 1.10
TIER_C_MIN = REQUIRED_MARGIN                 # 1.04

#: §3.5 — the ONLY fingerprints that may qualify a tier.
QUALIFYING_FINGERPRINTS = ("C3", "C5", "R2")

#: reasons (stable, auditable strings)
MISSING_INPUT = "MISSING_INPUT"
COMPETITION_EXCLUDED = "COMPETITION_EXCLUDED"
PROGRESS_OUT_OF_BAND = "PROGRESS_OUT_OF_BAND"
SHORTFALL_ABSENT = "SHORTFALL_ABSENT"
BELOW_MIN_STRENGTH = "BELOW_MIN_STRENGTH"
NO_FINGERPRINT = "NO_FINGERPRINT"
TIER_C_Q3_UNAVAILABLE = "TIER_C_Q3_UNAVAILABLE"


def _numeric(value: Any) -> Optional[float]:
    """Finite float, or None.  Booleans are NOT numbers here (§5.3)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def tier_for(req_ratio: Optional[float]) -> Optional[str]:
    """The strength tier of a ratio — ``None`` below the minimum edge."""
    r = _numeric(req_ratio)
    if r is None or r <= TIER_C_MIN:
        return None
    if r >= TIER_A_MIN:
        return "A"
    if r >= TIER_B_MIN:
        return "B"
    return "C"


def qualifies_for_autobet(
    competition_slug: str,
    progress_pct: float,
    required_pts_per_min: float,
    actual_pts_per_min: float,
    competition_avg_pace: float,
    competition_avg_q3_pace: Optional[float],
    q3_total_points: Optional[int],
    q3_minutes: Optional[float],
    *,
    q3_ppm: Optional[float] = None,
    shortfall: Optional[bool] = None,
) -> tuple[bool, str]:
    """Does this alert qualify for auto-bet?  ``(True, tier)`` / ``(False, why)``.

    Pure — no I/O, no side effects, no logging.  Any missing, non-finite or
    non-positive input that the verdict depends on yields
    ``(False, "MISSING_INPUT")``; a value is never fabricated (§5.3).

    ``q3_ppm`` is an optional pre-computed Q3 pace for callers that already hold
    it (the served payload's fingerprint block); when both it and the raw
    ``q3_total_points``/``q3_minutes`` are absent, Q3 is UNAVAILABLE.  Passing
    both is harmless — the pre-computed value wins because it is the one the
    alert itself consumed.

    ``shortfall`` None means "use the existing TRADE_SHORTFALL_ENABLED rule".
    """
    # ── §3.1 competition exclusion (hard block, before anything else) ──
    if not isinstance(competition_slug, str) or not competition_slug.strip():
        return False, MISSING_INPUT
    if competition_slug.strip() == CYBER_2K26_SLUG:
        return False, COMPETITION_EXCLUDED

    # ── §5.3 fail closed on anything unprovable ───────────────────────
    pct = _numeric(progress_pct)
    required = _numeric(required_pts_per_min)
    actual = _numeric(actual_pts_per_min)
    avg = _numeric(competition_avg_pace)
    if pct is None or required is None or actual is None:
        return False, MISSING_INPUT
    if avg is None or avg <= 0:
        return False, MISSING_INPUT

    # ── §3.2 progress band (the alert's own execution window) ─────────
    progress_min = float(getattr(TW, "EXEC_MIN_PROGRESS_PCT", PROGRESS_MIN))
    progress_max = float(getattr(TW, "TRADE_MAX_PROGRESS_PCT", PROGRESS_MAX))
    if pct < progress_min or pct > progress_max:
        return False, PROGRESS_OUT_OF_BAND

    # ── §3.3 shortfall present (existing rule, existing toggle) ───────
    require_shortfall = (bool(getattr(TW, "TRADE_SHORTFALL_ENABLED", True))
                         if shortfall is None else bool(shortfall))
    if require_shortfall and not required > actual:
        return False, SHORTFALL_ABSENT

    # ── §3.4 minimum strength + tier ──────────────────────────────────
    req_ratio = required / avg
    tier = tier_for(req_ratio)
    if tier is None:
        return False, BELOW_MIN_STRENGTH
    if tier == "A":
        return True, "A"

    # ── §3.5 fingerprints (tiers B and C) ─────────────────────────────
    q3 = _numeric(q3_ppm)
    if q3 is None:
        total = _numeric(q3_total_points)
        minutes = _numeric(q3_minutes)
        q3 = (total / minutes) if (total is not None and minutes is not None
                                   and minutes > 0) else None
    block = evaluate_fingerprints(
        required, avg, q3,
        _numeric(competition_avg_q3_pace) if q3 is not None else None)
    # ``*_triggered`` is TRUE only for a proven TRUE (UNAVAILABLE is never a
    # pass), so this list is exactly the qualifying fingerprints that fired.
    fired = [k for k in QUALIFYING_FINGERPRINTS
             if block.get(f"fingerprint_{k.lower()}_triggered") is True]
    if fired:
        return True, tier
    if block.get("q3_ratio") is None:
        # Q3 unprovable: a Tier-B base rate (77.6 %) is still acceptable, a
        # Tier-C edge (71.4 %) is not — it only exists with confirmation.
        if tier == "B":
            return True, "B"
        return False, TIER_C_Q3_UNAVAILABLE
    return False, NO_FINGERPRINT


def inputs_from_game(game: dict) -> dict:
    """Extract the gate's inputs from a live-payload / executor game dict.

    The competition pace and the Q3 evidence come from the served
    ``under_alert_fingerprint`` block — the SAME point-in-time values (and the
    same authority, :func:`evaluate_fingerprints`) the alert consumed.  A
    producer that does not carry the block (the fast lane's synthetic payload)
    supplies ``competition_pace.avg_pace`` instead and has no Q3 evidence, so
    Q3 reads UNAVAILABLE rather than being guessed.
    """
    proj = game.get("projector") or {}
    fp = game.get("under_alert_fingerprint") or {}
    pace = game.get("competition_pace") or {}
    ua = game.get("under_alert") or {}
    # The same competition identity the alert itself was resolved against —
    # never the classification code (directive §6).
    slug = (game.get("competition_slug")
            or (game.get("under_alert_competition") or {}).get("slug"))
    # One pace authority, read where the payload carries it: the alert's own
    # fingerprint block first, then the explicit competition block, then the
    # alert block.  All three are populated from the SAME reference at the
    # trigger instant; a missing value is still MISSING_INPUT, never guessed.
    avg_pace = fp.get("league_average_pace")
    if avg_pace is None:
        avg_pace = pace.get("avg_pace")
    if avg_pace is None:
        avg_pace = ua.get("league_average_pace")
    return {
        "competition_slug": slug,
        "progress_pct": proj.get("progress_pct"),
        "required_pts_per_min": proj.get("required_pts_per_min"),
        "actual_pts_per_min": proj.get("actual_pts_per_min"),
        "competition_avg_pace": avg_pace,
        "competition_avg_q3_pace": fp.get("league_q3_avg"),
        "q3_total_points": None,
        "q3_minutes": None,
        "q3_ppm": fp.get("q3_ppm"),
    }


def audit_block(game: dict, result: Optional[tuple[bool, str]] = None,
                inputs: Optional[dict] = None) -> dict:
    """The §5.4 audit record for ONE evaluation (gid + every deciding value)."""
    inputs = inputs if inputs is not None else inputs_from_game(game)
    if result is None:
        result = qualifies_for_autobet(**inputs)
    qualified, verdict = result
    fp = game.get("under_alert_fingerprint") or {}
    ratio = _numeric(inputs.get("competition_avg_pace"))
    req = _numeric(inputs.get("required_pts_per_min"))
    record = {
        "gid": game.get("game_id"),
        "slug": inputs.get("competition_slug"),
        "req_ratio": (round(req / ratio, 6)
                      if req is not None and ratio else None),
        "q3_ratio": fp.get("q3_ratio"),
        "tier": tier_for(req / ratio) if (req is not None and ratio) else None,
        "avg_pace": ratio,
        "fingerprints_fired": [
            k for k in QUALIFYING_FINGERPRINTS
            if fp.get(f"fingerprint_{k.lower()}_triggered") is True],
        "qualified": bool(qualified),
        "reason": verdict if not qualified else f"TIER_{verdict}",
        "layer": LAYER_NAME,
    }
    return record


def check_game(game: dict, store=None) -> dict:
    """Evaluate ONE game dict, LOG the audit record, return the block.

    This is the wiring entry point for the two autonomous producers —
    ``worker._dispatch_game`` and ``fast_lane.pass_once`` — each of which calls
    it immediately BEFORE ``executor.evaluate(..., claim=True)``, so the
    qualification decision and its evidence are recorded for every candidate
    whether it then passes or not.  The executor's own contract is untouched,
    so the manual/read-only paths are not re-decided by this gate.

    ``store`` is the producer's BettingStore.  Supplying it makes the
    persisted per-layer toggles (dashboard switches) authoritative on the
    decision path; without it the env defaults apply, which is what the
    read-only and unit-test paths want.
    """
    inputs = inputs_from_game(game)
    result = qualifies_for_autobet(**inputs)
    block = audit_block(game, result, inputs)
    strength_ok = bool(result[0])
    # COHORT OVERLAYS + SELECTOR (2026-11-11, modes wired 2026-11-26).
    # STRENGTH is the incumbent gate and the default policy.  The selector
    # is a persisted, audited policy choice:
    #   STRENGTH — the incumbent gate alone decides (z is informational)
    #   BOTH     — STRENGTH must pass AND the armed z layer must qualify
    #   Z_ONLY   — the armed z layer alone decides; this is the ONE mode that
    #              can admit a game STRENGTH refused, so it is an explicit
    #              operator choice, and while z is disarmed it refuses
    #              everything (fail closed).
    # Overlays only LABEL and PRICE a game that will actually run.
    try:
        from blm_v4.betting import cohort_select as _sel
        from blm_v4.betting import z_cohort
        mode = _sel.get()
        block["cohort_mode"] = mode
        z_block = z_cohort.evaluate(game)       # no-op while z layer disarmed
        block["z_cohort"] = z_block
        z_ok = (bool(z_block.get("applicable"))
                and bool(z_block.get("qualified")))
        z_reason = "Z_COHORT_" + str(z_block.get("reason") or "REFUSED")
        if mode == _sel.Z_ONLY:
            admitted = z_ok
            if not admitted:
                block["qualified"] = False
                block["reason"] = z_reason
                block["mode_reason"] = z_reason
            elif not strength_ok:
                # Audited: the selector, not the incumbent gate, is why this
                # game runs.  The strength verdict stays in the tier block.
                block["qualified"] = True
                block["reason"] = "MODE_Z_ONLY_ADMITTED"
                block["mode_reason"] = "MODE_Z_ONLY_ADMITTED"
        elif mode == _sel.BOTH:
            admitted = strength_ok and z_ok
            if strength_ok and not z_ok:
                block["qualified"] = False
                block["reason"] = z_reason
                block["mode_reason"] = z_reason
        else:
            admitted = strength_ok
        if admitted:
            inputs = inputs_from_game(game)
            req = inputs.get("required_pts_per_min")
            avg = inputs.get("competition_avg_pace")
            ratio = (req / avg) if (req is not None and avg) else None
            tcx = ratio is not None and ratio > 1.16  # TC-X elite pace rung
            z = z_block.get("z")
            match = {
                _sel.LAYER_Z2: z is not None and z > 2.0,
                _sel.LAYER_Z1: z is not None and z > 1.0,
                _sel.LAYER_TCX: tcx,
            }
            resolved = _sel.resolve_layers(match, store=store)
            block["cohort_layer"] = resolved["label"]
            block["cohort_layer_key"] = resolved["layer"]
            block["cohort_units"] = resolved["units"]
            block["cohort_matched"] = resolved["matched"]
        else:
            block["cohort_layer"] = None
            block["cohort_layer_key"] = None
            block["cohort_units"] = None
            block["cohort_matched"] = []
    except Exception:            # an overlay failure NEVER changes admission
        block["cohort_layer"] = None
        block["cohort_layer_key"] = None
        block["cohort_units"] = None
        block["cohort_matched"] = []
        block["mode_reason"] = "COHORT_LAYER_UNAVAILABLE"
    try:
        logger.info("autobet_qualification", **block)
    except Exception:            # logging must never decide a bet
        pass
    return block
