"""IDENTITY WATCH — autobet must never go quiet about an unprovable account.

Observed live 2026-11-25: at ~11:24Z the PokerBet page stopped yielding DOM
reads (the fast lane's own balance read went ``null`` in the same pass and
stayed null).  The account-identity guard did exactly the right thing — it
failed closed — so 53 claims were refused over roughly nine hours and not one
bet was placed.  Nothing logged a word, no counter moved visibly, and the lane
reported itself armed the whole time.  A fail-closed guard plus a silent
failure is indistinguishable from a switch nobody turned on.

This module watches that guard.  It does NOT gate, retry, override or weaken
anything: ``verify_account`` remains the only authority and still refuses.

* the first refusal of an episode returns ``True`` exactly once, so the
  executor can write ONE ``identity_stall`` audit row per episode;
* a WARNING is logged on the transition and then at most every
  ``ALERT_EVERY_S`` seconds while the episode lasts;
* recovery is logged when a readable identity returns;
* ``snapshot()`` exposes the state to ``/api/v4/betting/status``.

State is per process, in memory.  A restart starts a fresh episode, which is
correct: the stall is a property of the live browser session, not of history.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("blm_v4.betting.identity_watch")

#: How long refusals must persist before the state counts as a STALL.  One
#: empty read is a mid-render page (``ACCOUNT_READ_ATTEMPTS`` already retries
#: those); two minutes of them is an unreadable session.
STALL_AFTER_S = 120.0

#: While stalled, repeat the warning at most this often.  The dashboard shows
#: the live state; this is for whoever is reading logs.
ALERT_EVERY_S = 300.0

#: state names surfaced to the API
OK = "OK"
BLIND = "BLIND"          # identity-requiring provider, read returned nothing
MISMATCH = "MISMATCH"    # readable id, but not a permitted account
EXEMPT = "EXEMPT"        # provider carries no identity (dry run) — not watched

_lock = threading.Lock()
_state = OK
_blocks = 0
_blocked_since: Optional[datetime] = None
_last_ok: Optional[datetime] = None
_last_reason: Optional[str] = None
_last_alert_at: Optional[float] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _classify(reason: str) -> str:
    return BLIND if "unverifiable" in reason.lower() else MISMATCH


def _alert(state: str, blocks: int, since_s: float, reason: str) -> None:
    """One loud line: what broke, how long, and that it is costing money."""
    logger.warning(
        "identity_stall state=%s blocks=%d stalled_for_s=%.0f reason=%r "
        "autobet is ARMED but cannot prove the signed-in account — every "
        "candidate will be refused until the browser identity read recovers",
        state, blocks, since_s, reason[:200])


def observe(*, required: bool, reason: Optional[str]) -> bool:
    """Record one guard decision.  ``True`` only on the episode's first block.

    ``required`` is the provider's own ``requires_account_identity``: when it
    is False the guard is exempt (dry run, no real submission) and nothing is
    watched.  ``reason`` is ``verify_account``'s return — ``None`` means the
    account was proven and permitted.
    """
    global _state, _blocks, _blocked_since, _last_ok, _last_reason
    global _last_alert_at

    if not required:
        return False

    with _lock:
        if not reason:
            recovered = _state in (BLIND, MISMATCH) and _blocks > 0
            _state, _blocks = OK, 0
            _blocked_since = None
            _last_reason = None
            _last_ok = _now()
            _last_alert_at = None
            if recovered:
                logger.warning(
                    "identity_recovered — the signed-in account is readable "
                    "again and matches; autobet can place bets")
            return False

        previous = _state
        _blocks += 1
        _last_reason = reason
        _state = _classify(reason)
        if _blocked_since is None:
            _blocked_since = _now()
        since_s = (_now() - _blocked_since).total_seconds()
        monotonic = time.monotonic()

        first = _blocks == 1 or previous not in (BLIND, MISMATCH)
        if first:
            _last_alert_at = monotonic
            _alert(_state, _blocks, since_s, reason)
            return True
        if _last_alert_at is None or monotonic - _last_alert_at >= ALERT_EVERY_S:
            _last_alert_at = monotonic
            _alert(_state, _blocks, since_s, reason)
        return False


def snapshot() -> dict:
    """The current watch state.  Never raises; safe for a status endpoint."""
    with _lock:
        since_s = None
        if _blocked_since is not None:
            since_s = round((_now() - _blocked_since).total_seconds(), 1)
        return {
            "state": _state,
            "stalled": bool(_blocks and since_s is not None
                            and since_s >= STALL_AFTER_S),
            "consecutive_blocks": _blocks,
            "blocked_since_utc": _iso(_blocked_since),
            "blocked_for_s": since_s,
            "last_ok_utc": _iso(_last_ok),
            "last_reason": _last_reason,
        }


def reset() -> None:
    """Clear the watch (tests only — the live process never calls this)."""
    global _state, _blocks, _blocked_since, _last_ok, _last_reason
    global _last_alert_at
    with _lock:
        _state, _blocks = OK, 0
        _blocked_since = _last_ok = _last_reason = _last_alert_at = None
