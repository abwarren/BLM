"""Auto-Bet COMMAND API — the ONE shared execution-command path (gap G-07).

Before this module the two Auto-Bet producers validated DIFFERENTLY:

  * the MANUAL path — ``POST /api/v4/betting/manual`` (the active-alert
    card's PLACE BET button) — ran its own freshness / line / stake checks
    inside ``blm_v4/betting/api.py``; and
  * the AUTONOMOUS path — ``BettingWorker`` → ``executor.evaluate`` — ran
    the ten-condition ladder inside ``blm_v4/betting/executor.py``

and NEITHER consulted the canonical rung rule (``blm_v4/betting/rung.py``)
or the three stake modes (``blm_v4/betting/stake.py``).  This module is the
single seam that removes that divergence: both producers build ONE command
and run the SAME validator, so manual and autonomous can never disagree.

    BLM UI ─┐
            ├─► command.build_command ─► rung.validate_execution  (identity +
    engine ─┘                              frozen trigger + current line + rung)
                                        + stake.resolve_stake     (mode + unit / R2)
                                        ─► decision ALLOW / REJECT
                                        ─► execution state machine
                                        ─► (provider) ─► ledger

ARCHITECTURE (operator correction, unchanged): the BLM Alert Monitor is the
ONLY opportunity source.  Auto-Bet CONSUMES an existing alert; it never
discovers an opportunity, never establishes a new baseline and never creates
a new trigger line.  The frozen alert ``trigger_line`` is the reference for
BOTH manual and autonomous — there is no separate manual reference line.

ARMING
------
Per the operator's own Production Wiring Gate (``docs/autobet/
RUNG_PRODUCTION_WIRING_GATE.md``), PW-02 / PW-03 / PW-14 are unprovable
without a controllable, authenticated PokerBet browser — which does not
exist in this environment.  The canonical command gate is therefore
IMPLEMENTED here but NOT ARMED in production::

    WIRED_INTO_PRODUCTION = False

While ``False`` the legacy ladders stay authoritative and this module is
exercised only by its own tests.  Flipping it is a bet-behaviour change that
needs its own explicit authorization (it must not happen before every PW
gate passes).  Every branch fails CLOSED.

Pure decision layer — no DB, no network, no provider, no config import.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from blm_v4.betting.rung import (
    ALLOW,
    REJECT,
    validate_execution,
)
from blm_v4.betting import stake as _stake

#: The arm gate (see module docstring + the PW gate).  Stays False until
#: every Production Wiring Gate passes AND the operator authorizes the flip.
WIRED_INTO_PRODUCTION = False

#: command producers — the ONLY two sources a command may have.
SOURCE_MANUAL = "manual"
SOURCE_AUTONOMOUS = "autonomous"
SOURCES = (SOURCE_MANUAL, SOURCE_AUTONOMOUS)

# ── the directive's execution-state vocabulary (D) ─────────────────────────
CREATED = "CREATED"
VALIDATED = "VALIDATED"
SENT = "SENT"
ACKNOWLEDGED = "ACKNOWLEDGED"
EXECUTED = "EXECUTED"
REJECTED = "REJECTED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
STATES = (CREATED, VALIDATED, SENT, ACKNOWLEDGED, EXECUTED, REJECTED,
          FAILED, UNKNOWN)

#: command state → the status vocabulary the production ledger persists
#: (``blm_v4/betting/store.py`` ``_STATUSES``).  A timeout / ambiguous
#: provider answer is UNKNOWN and is NEVER success (D).
LEDGER_STATUS = {
    CREATED: "PENDING",
    VALIDATED: "PENDING",
    SENT: "SUBMITTING",
    ACKNOWLEDGED: "SUBMITTED",
    EXECUTED: "ACCEPTED",
    REJECTED: "REJECTED",
    FAILED: "FAILED",
    UNKNOWN: "UNKNOWN",
}

#: Stable refusal reasons raised by THIS layer (rung/stake reasons come
#: verbatim from the canonical modules).
R_SOURCE = "command_source_invalid"
R_IDENTITY_KEY = "command_identity_missing"


def command_identity(game: dict, source: str) -> Optional[str]:
    """The immutable command identity (D) — the idempotency basis.

    Derived only from the bet's IMMUTABLE identity: canonical game id +
    market + selection + the alert checkpoint.  Never from timestamps,
    prices or display text, so a re-delivered / retried command hashes
    identically and can never create a second bet.
    """
    ua = game.get("under_alert") or {}
    game_id = str(game.get("game_id") or "").strip()
    cp = ua.get("checkpoint")
    if not game_id or cp is None:
        return None
    alert_id = ua.get("alert_id") or f"{game_id}|{cp}"
    parts = [game_id, "TOTAL", "UNDER", str(cp), str(alert_id)]
    return f"{source}:" + "|".join(parts)


def _command_id(idempotency_key: str) -> str:
    return "cmd-" + hashlib.sha256(
        idempotency_key.encode("utf-8")).hexdigest()[:20]


def build_command(game: dict, *, source: str, mode: Any,
                  unit_size: Any = None, currency: Any = _stake.CURRENCY,
                  authorized: bool = False, authorized_by: Optional[str] = None,
                  idempotency_key: Optional[str] = None,
                  market: str = "TOTAL", selection: str = "UNDER") -> dict:
    """Build and validate ONE Auto-Bet execution command — THE shared path.

    Manual and autonomous call this with the SAME ``game`` payload; the only
    difference is ``source``.  The decision is ALLOW only when BOTH the
    canonical rung validator (identity + frozen trigger + current line + rung)
    AND the canonical stake authority (mode + unit size / R2.00) agree.

    Returns a dict carrying everything the audit record needs plus
    ``decision`` (ALLOW/REJECT), ``state`` and ``reason``.  No command may
    execute unless ``decision == ALLOW`` AND ``state == VALIDATED``.
    """
    ua = game.get("under_alert") or {}
    ik = idempotency_key or command_identity(game, source)
    base = {
        "command_id": _command_id(ik) if ik else None,
        "idempotency_key": ik,
        "source": source,
        "game_id": game.get("game_id"),
        "alert_id": ua.get("alert_id") or (
            f"{game.get('game_id')}|{ua.get('checkpoint')}"
            if ua.get("checkpoint") is not None else None),
        # the ALERT checkpoint (the immutable opportunity identity) — NOT the
        # actual trigger percent; downstream position keying uses THIS.
        "checkpoint": ua.get("checkpoint"),
        "market": market,
        "selection": selection,
        "mode": mode,
        "state": CREATED,
        "decision": REJECT,
        "reason": None,
        "rung": None,
        "stake": None,
        "authorization": None,
    }
    if source not in SOURCES:
        base["reason"] = R_SOURCE
        return base
    if not ik:
        base["reason"] = R_IDENTITY_KEY
        return base

    # ── the ONE rung validator (identity / frozen trigger / current / rung) ─
    rung = validate_execution(game, market=market, selection=selection)
    # ── the ONE stake authority (three modes; unit size / R2.00) ────────────
    stk = _stake.resolve_stake(mode, unit_size=unit_size,
                               currency=currency, authorized=authorized)
    auth = _stake.authorization_proof(mode, authorized=authorized,
                                      authorized_by=authorized_by)

    # a real-money mode additionally requires an explicit actor proof
    real_money = mode != _stake.ZERO_STAKE
    auth_ok = (auth["satisfied"] if real_money else True)

    base["rung"] = rung
    base["stake"] = stk
    base["authorization"] = auth

    if rung["decision"] != ALLOW:
        base["reason"] = rung["reason"]
        base["state"] = REJECTED
        return base
    if not stk["ok"]:
        base["reason"] = stk["reason"]
        base["state"] = REJECTED
        return base
    if not auth_ok:
        base["reason"] = auth["reason"] or _stake.R_AUTH
        base["state"] = REJECTED
        return base
    base["decision"] = ALLOW
    base["state"] = VALIDATED
    base["reason"] = None
    return base


def manual_command(game: dict, **kwargs) -> dict:
    """The MANUAL producer (BLM UI UNDER command against an existing alert)."""
    return build_command(game, source=SOURCE_MANUAL, **kwargs)


def autonomous_command(game: dict, **kwargs) -> dict:
    """The AUTONOMOUS producer (Auto-Bet engine against the same alert)."""
    return build_command(game, source=SOURCE_AUTONOMOUS, **kwargs)


def validate_for_execution(game: dict, **kwargs) -> dict:
    """The pre-execution gate both producers consult before any submission.

    A thin alias of :func:`build_command` so callers read as a validation
    step; it is the SAME call (never a second implementation).
    """
    return build_command(game, **kwargs)


def audit_record(command: dict) -> dict:
    """The credential-free audit record for one command (safety contract §4)."""
    rung = command.get("rung") or {}
    stk = command.get("stake") or {}
    return {
        "command_id": command.get("command_id"),
        "idempotency_key": command.get("idempotency_key"),
        "source": command.get("source"),
        "game_id": command.get("game_id"),
        "alert_id": command.get("alert_id"),
        "market": command.get("market"),
        "selection": command.get("selection"),
        "mode": command.get("mode"),
        "trigger_checkpoint_percent": rung.get("trigger_checkpoint_percent"),
        "trigger_line": rung.get("trigger_line"),
        "current_line": rung.get("current_line"),
        "rung_size": rung.get("rung_size"),
        "rungs_moved": rung.get("rungs_moved"),
        "unit_size": stk.get("stake"),
        "currency": stk.get("currency"),
        "execution_decision": command.get("decision"),
        "state": command.get("state"),
        "reason": command.get("reason"),
        "provider_ref": None,
        "final_status": None,
    }
