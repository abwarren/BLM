"""REPLAY ENGINE (directive §7) — historical BLM signals through the
TEST adapter, deterministically.

Replay takes historical signal records (any iterable of dicts with at
least ``game_id``/``checkpoint``/``market``/``selection``/``line``/
``price``/``stake_amount``, optionally ``scenario``) and drives each one
through the SAME BetEngine pipeline the live path uses — same state
machine, same PRECHECK gate, same idempotency — against the
``TestBookmakerAdapter``.  Nothing in replay can ever touch a real
bookmaker: the engine is hard-wired to the TEST adapter here (§1: no
replay environment may accidentally submit a real wager).

Per-signal ``scenario`` scripting lets a replay reproduce a historical
failure mode exactly (odds_changed, market_suspended, timeout, ...) so
post-mortems are reproducible run-for-run.
"""
from __future__ import annotations

from typing import Iterable, Optional

from blm_v4.betting.adapters import (
    AdapterAmbiguousError,
    AdapterSubmitError,
    TestAdapterConfig,
    TestBookmakerAdapter,
)
from blm_v4.betting.engine import BetEngine, ClaimStore
from blm_v4.betting.precheck import PrecheckLimits
from blm_v4.betting.session import BookmakerSession


class ReplayEngine:
    """Deterministic signal replay against the TEST bookmaker."""

    def __init__(self, *, adapter: Optional[TestBookmakerAdapter] = None,
                 session: Optional[BookmakerSession] = None,
                 claims: Optional[ClaimStore] = None,
                 precheck_limits: Optional[PrecheckLimits] = None,
                 db_path: str = ":memory:",
                 audit=None):
        # HARD WIRING: replay constructs its own TEST adapter unless a
        # TEST adapter is explicitly handed in.  A live adapter can
        # never be attached here — the parameter type forbids it.
        self.adapter = adapter or TestBookmakerAdapter()
        if not isinstance(self.adapter, TestBookmakerAdapter):
            raise TypeError(
                "replay requires a TestBookmakerAdapter (fail-closed)")
        self.session = session or self._connected_session()
        self.claims = claims or ClaimStore(db_path)
        self.pre = precheck_limits or PrecheckLimits(
            max_stake_per_bet=10.0, max_total_exposure=1000.0,
            current_exposure=0.0)
        self.engine = BetEngine(adapter=self.adapter, session=self.session,
                                claims=self.claims, precheck_limits=self.pre,
                                audit=audit)

    @staticmethod
    def _connected_session() -> BookmakerSession:
        s = BookmakerSession()
        s.login(("replay-operator", "replay-credential-not-a-real-secret"))
        return s

    def replay(self, signals: Iterable[dict]) -> dict:
        """Run every signal; return the aggregate + per-signal outcomes
        in input order.  A signal-level defect is CAPTURED, not raised —
        replay is a forensic tool and must always finish."""
        results = []
        for i, sig in enumerate(signals):
            # scenario scripting: explicit per signal, clean "accepted"
            # otherwise — one signal's failure mode never leaks into
            # the next signal's replay (deterministic, order-safe)
            self.adapter.set_scenario(
                str(sig.get("scenario") or "accepted"))
            signal = {k: v for k, v in sig.items() if k != "scenario"}
            try:
                bet = self.engine.place(signal)
                results.append({"index": i, **bet.as_dict()})
            except Exception as e:  # noqa: BLE001 — capture, keep replaying
                results.append({"index": i, "state": "DEFECT",
                                "reason": f"{type(e).__name__}: {e}"})
        return self._summarize(results)

    # ── aggregation ────────────────────────────────────────────────────
    @staticmethod
    def _summarize(results: list[dict]) -> dict:
        by_state: dict[str, int] = {}
        for r in results:
            by_state[r.get("state", "?")] = \
                by_state.get(r.get("state", "?"), 0) + 1
        # "claims_won" counts records that actually ENTERED the machine
        # (won the idempotency claim); duplicate echoes of an original
        # bet are counted separately so a retried signal inflates
        # neither the placed-bet count nor the duplicate count twice.
        claims_won = sum(1 for r in results if r.get("placed"))
        duplicates = sum(1 for r in results
                         if not r.get("placed")
                         and r.get("state") not in ("DEFECT",))
        # outcome counts include ONLY records that entered the machine —
        # a duplicate echo of a CONFIRMED bet must not double-count it
        confirmed = sum(1 for r in results
                        if r.get("placed") and r.get("state") == "CONFIRMED")
        settled = sum(1 for r in results
                      if r.get("placed") and r.get("state") == "SETTLED")
        rejected = sum(1 for r in results
                       if r.get("placed") and r.get("state") == "REJECTED")
        failed = sum(1 for r in results
                     if r.get("placed")
                     and r.get("state") == "SUBMISSION_FAILED")
        terminal = confirmed + settled + rejected + failed
        return {
            "total": len(results),
            "by_state": by_state,
            "claims_won": claims_won,
            "duplicates": duplicates,
            "confirmed": confirmed,
            "settled": settled,
            "rejected": rejected,
            "submission_failed": failed,
            "non_terminal": len(results) - terminal,
            "results": results,
        }

    # ── convenience builders ───────────────────────────────────────────
    @staticmethod
    def signal(game_id: str = "TEST-GAME-9001", *, checkpoint=75,
               market: str = "TOTAL", selection: str = "UNDER",
               line: float = 180.5, price: float = 1.85,
               stake_amount: float = 1.0, alert_id: str = "alert-1",
               scenario: Optional[str] = None) -> dict:
        s = {"game_id": game_id, "checkpoint": checkpoint, "market": market,
             "selection": selection, "line": line, "price": price,
             "stake_amount": stake_amount, "alert_id": alert_id}
        if scenario:
            s["scenario"] = scenario
        return s
