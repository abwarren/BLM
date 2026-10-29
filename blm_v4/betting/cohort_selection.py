"""The cohort Auto-Bet is CONFIGURED to use — a TEST-ONLY, fail-closed selector.

Scope (workflow doc §6.5, directive 2026-10-27)
----------------------------------------------
This module owns exactly one persisted key, ``autobet_selected_cohort``.  It is
the *configuration* of which named trade cohort Auto-Bet is set up for; it is
NOT an execution control:

  * it never reads or writes the kill switch (``auto_betting_enabled``), the
    unit price, the daily/stake limits, the server-side ``dry_run`` flag or the
    browser-submit authorization;
  * it never creates, mutates or retries an execution, and never calls the
    executor or a provider;
  * nothing in the execution path consults it (see ``NOT_CONSULTED_BY`` below).
    Selecting a cohort therefore cannot arm, size, gate or submit a bet — the
    value is persisted configuration plus an audit line, nothing else.

Validation is a strict allow-list of the seven frozen codes from
:mod:`blm_v4.cohorts.spec` (the same registry the analytics use).  Anything
else — an unknown code, a lower-case alias, a display label, a blank, a
non-string, an oversized/injected value — is REJECTED (``CohortSelectionError``
→ HTTP 422 at the API boundary) and nothing is written: a selector that guessed
a default on bad input would silently change what Auto-Bet is configured for.

Fail-closed status
------------------
:func:`execution_status` reports the configuration-level reasons execution is
LOCKED.  It computes them from the existing authorities only (persisted switch,
unit price, the three server-side limits, ``dry_run``, browser-submit).  A
missing limit is reported as missing; no default is ever substituted.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from blm_v4.cohorts import spec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from blm_v4.betting.config import BettingConfig
    from blm_v4.betting.store import BettingStore

#: The ONE config key this module owns.
KEY_SELECTED_COHORT = "autobet_selected_cohort"

#: The operator-reported current selection (2026-10-27).  It is the value the UI
#: shows when nothing has been persisted yet; it is NOT written back to the store
#: on a read (a page load must never change configuration).
DEFAULT_COHORT = "TC-X"

#: Explicitly documents the execution surface this module does NOT touch.
NOT_CONSULTED_BY = ("blm_v4.betting.executor.evaluate", "blm_v4.betting.command")

MAX_CODE_LEN = 16


class CohortSelectionError(ValueError):
    """Raised for any value that is not one of the seven frozen cohort codes."""


def valid_codes() -> tuple[str, ...]:
    """The seven frozen codes, in the spec's stable qualifying order."""
    return tuple(spec.QUALIFYING_ORDER)


def normalize_cohort(value: object) -> str:
    """Return the canonical code for ``value`` or raise ``CohortSelectionError``.

    Deliberately strict: no case folding, no trimming-to-fit, no alias table and
    no default.  ``"tc-x"`` is not ``"TC-X"``; a display name is not a code.
    """
    if not isinstance(value, str):
        raise CohortSelectionError(f"cohort must be a string, got {type(value).__name__}")
    code = value
    if not code or len(code) > MAX_CODE_LEN:
        raise CohortSelectionError("cohort code is empty or too long")
    if code != code.strip():
        raise CohortSelectionError("cohort code must not carry surrounding whitespace")
    if code not in spec.COHORT_RULES:
        raise CohortSelectionError(
            f"unknown cohort {code!r}; expected one of {', '.join(valid_codes())}")
    return code


@dataclass(frozen=True)
class Selection:
    """The persisted (or defaulted) cohort selection, with its provenance."""

    cohort: str
    source: str            # "persisted" | "default"
    stored: bool
    updated_at: Optional[str] = None

    def as_dict(self) -> dict:
        rule = spec.COHORT_RULES[self.cohort]
        return {
            "cohort": self.cohort,
            "name": rule.name,
            "source": self.source,
            "stored": self.stored,
            "updated_at": self.updated_at,
            "rule_version": spec.RULE_VERSION,
        }


def get_selection(store: "BettingStore") -> Selection:
    """Read the configured cohort.  Never writes; a missing/garbage value → default.

    A persisted value that is no longer a valid code (e.g. written by an older
    build) is reported as the default with ``source='default'`` — it is never
    echoed back as if it were a valid selection.
    """
    raw = store.get_config(KEY_SELECTED_COHORT, "")
    if not raw:
        return Selection(DEFAULT_COHORT, "default", stored=False)
    try:
        code = normalize_cohort(raw)
    except CohortSelectionError:
        return Selection(DEFAULT_COHORT, "default", stored=False)
    return Selection(code, "persisted", stored=True)


def set_selection(store: "BettingStore", value: object, *,
                  actor: Optional[str] = None) -> Selection:
    """Persist ``value`` as the configured cohort.  Returns the new selection.

    Writes exactly one config key plus one audit row.  Raises
    ``CohortSelectionError`` (without writing anything) for an invalid value.
    """
    code = normalize_cohort(value)
    previous = get_selection(store)
    store.set_config(KEY_SELECTED_COHORT, code)
    store.audit(
        "autobet_cohort_selection",
        reason=(actor or "operator"),
        details={
            "from": previous.cohort,
            "from_source": previous.source,
            "to": code,
            "rule_version": spec.RULE_VERSION,
        },
    )
    stored = store.get_config(KEY_SELECTED_COHORT, "")
    return Selection(code, "persisted", stored=(stored == code))


@dataclass(frozen=True)
class ExecutionStatus:
    """Why execution is (or is not) locked — configuration-level only."""

    locked: bool
    reasons: list[str] = field(default_factory=list)
    missing_limits: list[str] = field(default_factory=list)
    dry_run: bool = True
    enabled: bool = False
    unit_price: Optional[float] = None
    browser_submit_authorized: bool = False

    def as_dict(self) -> dict:
        return {
            "locked": self.locked,
            "reasons": list(self.reasons),
            "missing_limits": list(self.missing_limits),
            "dry_run": self.dry_run,
            "enabled": self.enabled,
            "unit_price": self.unit_price,
            "browser_submit_authorized": self.browser_submit_authorized,
        }


def execution_status(store: "BettingStore", cfg: "BettingConfig", *,
                     browser_submit: Optional[bool] = None) -> ExecutionStatus:
    """Describe the configuration-level execution lock.

    ``locked`` is True unless EVERY prerequisite is present: the persisted switch
    ON, not dry-run, a positive unit price, all three server-side limits, and the
    browser-submit authorization.  Nothing is defaulted — an absent limit is
    named in ``missing_limits`` and keeps execution locked.
    """
    enabled = store.is_enabled()
    unit_price = store.get_unit_price()
    missing = []
    if cfg.max_stake_per_bet is None:
        missing.append("max_stake_per_bet")
    if cfg.max_bets_per_day is None:
        missing.append("max_bets_per_day")
    if cfg.max_daily_exposure is None:
        missing.append("max_daily_exposure")
    if browser_submit is None:
        from blm_v4.betting.provider import browser_submit_authorized
        browser_submit = browser_submit_authorized()

    reasons: list[str] = []
    if not enabled:
        reasons.append("auto_betting_off")
    if cfg.dry_run:
        reasons.append("dry_run")
    if unit_price is None:
        reasons.append("unit_price_missing")
    for name in missing:
        reasons.append(f"{name}_missing")
    if not browser_submit:
        reasons.append("browser_submit_not_authorized")

    return ExecutionStatus(
        locked=bool(reasons),
        reasons=reasons,
        missing_limits=missing,
        dry_run=cfg.dry_run,
        enabled=enabled,
        unit_price=unit_price,
        browser_submit_authorized=bool(browser_submit),
    )


def selection_payload(store: "BettingStore", cfg: "BettingConfig", *,
                      browser_submit: Optional[bool] = None) -> dict:
    """The complete, secret-free payload the UI renders for the selector."""
    selection = get_selection(store)
    status = execution_status(store, cfg, browser_submit=browser_submit)
    return {
        "selected": selection.as_dict(),
        "options": [
            {"code": code, "name": spec.COHORT_RULES[code].name,
             "rule": {"pace_ratio_gt": spec.COHORT_RULES[code].pace_ratio,
                      "shortfall_gte": spec.COHORT_RULES[code].shortfall,
                      "min_under_price_gte": spec.COHORT_RULES[code].min_under_price}}
            for code in valid_codes()
        ],
        "execution": status.as_dict(),
        "rule_version": spec.RULE_VERSION,
        "environment": "test",
        "read_only_execution": True,
        "note": ("Cohort selection is configuration only: it never enables "
                 "Auto-Bet, changes stakes, limits, the kill switch, dry-run or "
                 "browser submission, and never places a bet."),
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
