"""AUTHENTICATED BROWSER BRIDGE — 2026-10-05 directive.

Proves the live-market-observation + authenticated-session gates added to
``blm_v4/execution/browser_bridge.py`` and the wiring of the ACTUAL executable
line + DECIMAL ODDS into the POSITION.

Covers, each as its own named test:
  * full path captures executable_line + executable_odds (line != odds) and
    requires the bookmaker confirmation BEFORE BET_PLACED (EXECUTED);
  * zero-stake rehearsal submits nothing (complete path);
  * stale line rejected; matching line accepted;
  * missing odds rejected; missing line rejected; market not resolved rejected;
  * unauthenticated / expired session rejected (no bet);
  * duplicate execution → ONE bet;
  * timeout / ambiguous confirmation → UNKNOWN (never success).

SAFETY: fakes only — no provider, no credentials, no network, no real bet.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import (  # noqa: E402
    ACCEPTED,
    SUBMITTED,
    FakeBrowserAdapter,
)

from blm_v4.betting import command as C  # noqa: E402
from blm_v4.betting import stake as S  # noqa: E402
from blm_v4.betting import position as P  # noqa: E402
from blm_v4.execution.adapter import (  # noqa: E402
    MarketObservation,
    SelectionResolver,
)
from blm_v4.execution import browser_bridge as B  # noqa: E402

OBS = (190.0, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0, 193.5)   # 0.5 tick
EVENT = "Team A vs Team B"
GID = "30990001"


def game(checkpoint=75, trigger=193.5, cur=193.5) -> dict:
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": GID, "live": True, "live_reason": None,
        "market": {"total_line": cur, "market_status": "LIVE",
                   "observed_lines": list(OBS)},
        "projector": {"progress_pct": 76.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": cur, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": checkpoint,
                        "trigger_line": trigger, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": f"{GID}|{checkpoint}"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }


def prod_command(*, key="ik", unit=10.0, g=None):
    cmd = C.build_command(g or game(), source=C.SOURCE_AUTONOMOUS,
                          mode=S.PRODUCTION_AUTO_BET, unit_size=unit,
                          authorized=True, authorized_by="test",
                          idempotency_key=key)
    cmd["event"] = EVENT
    return cmd


def zero_command(*, key="ik-zero"):
    cmd = C.build_command(game(), source=C.SOURCE_MANUAL,
                          mode=S.ZERO_STAKE, idempotency_key=key)
    cmd["event"] = EVENT
    return cmd


def adapter_ok(line=193.5, under=1.90, over=1.95, *, place_status=ACCEPTED,
               confirmation=None) -> FakeBrowserAdapter:
    a = FakeBrowserAdapter()
    a.set_market(EVENT, line, over, under)
    a.place_status = place_status
    a.confirmation = confirmation
    return a


def bridge(a, *, session_probe=None) -> B.ExecutionBridge:
    return B.ExecutionBridge(
        B.ResolverBrowserBridge(a, session_probe=session_probe))


class _ObsResolver(SelectionResolver):
    """Minimal resolver that returns a scripted observation (edge cases)."""

    name = "obs_stub"

    def __init__(self, *, line=193.5, price=1.90, market="TOTAL",
                 position="UNDER", ok=True):
        self.line, self.price = line, price
        self.market, self.position, self.ok = market, position, ok
        self.clicks: list = []
        self.placements: list = []

    def find_event(self, event): return self.ok
    def find_market(self, event, market): return self.ok

    def find_position(self, event, market, position):
        if not self.ok:
            return None
        return MarketObservation(event=event, market=self.market,
                                 position=self.position, line=self.line,
                                 price=self.price)

    def click_selection(self, obs): self.clicks.append(obs); return True
    def read_betslip(self): return []
    def place_parlay(self, stake):
        self.placements.append(stake)
        return {"status": ACCEPTED, "provider_ref": "stub-ref"}
    def read_order_confirmation(self): return {"reference": "stub-ref"}


# ══════════════════════════════════════════════════════════════════════════
# 1. the full path captures the ACTUAL line + DECIMAL ODDS and confirms
# ══════════════════════════════════════════════════════════════════════════

def test_full_path_captures_line_and_odds_and_requires_confirmation():
    a = adapter_ok(line=193.5, under=1.88, over=1.95,
                   confirmation={"reference": "PB-EXEC-9"})
    rec = bridge(a).execute(prod_command(key="cap"))
    assert rec.state == B.EXECUTED and rec.submitted is True
    assert rec.provider_ref == "fake-ref-1"          # bookmaker confirmation
    assert rec.observed_line == 193.5                # the current betting LINE
    assert rec.observed_odds == 1.88                 # the DECIMAL ODDS
    assert rec.observed_line != rec.observed_odds    # a line is NOT odds
    # wire the live observation into the POSITION
    pos = P.build_position(
        game_id=GID, checkpoint=75, trigger_line=193.5,
        executable_line=rec.observed_line, executable_odds=rec.observed_odds,
        stake=rec.stake_amount, provider_ref=rec.provider_ref,
        executed_at="2026-10-05T02:00:00Z")
    assert pos.executable_line == 193.5 and pos.executable_odds == 1.88
    assert pos.provider_ref == "fake-ref-1"


def test_confirmation_required_before_bet_placed():
    # ACCEPTED but the bookmaker's OWN receipt is absent → UNKNOWN, not success
    a = adapter_ok(confirmation=None)
    rec = bridge(a).execute(prod_command(key="noconf"))
    assert rec.state == B.BRIDGE_UNKNOWN and rec.submitted is False


# ══════════════════════════════════════════════════════════════════════════
# 2. zero-stake complete path submits nothing
# ══════════════════════════════════════════════════════════════════════════

def test_zero_stake_full_path_submits_nothing():
    a = adapter_ok()
    rec = bridge(a).execute(zero_command(key="zs"))
    assert rec.state == B.NO_OP and rec.reason == B.R_ZERO_STAKE
    assert a.clicks == [] and a.placements == [] and a.slip == []


# ══════════════════════════════════════════════════════════════════════════
# 3. line validation (stale / changed / matching)
# ══════════════════════════════════════════════════════════════════════════

def test_stale_position_line_is_rejected():
    a = adapter_ok(line=195.5)                       # the live line moved
    cmd = prod_command(key="stale")
    cmd["executable_line"] = 193.5                   # the position's line
    rec = bridge(a).execute(cmd)
    assert rec.state == B.BRIDGE_REJECTED and rec.reason == B.R_LINE_MOVED
    assert a.placements == []                        # nothing submitted


def test_matching_line_is_accepted():
    a = adapter_ok(line=193.5, confirmation={"reference": "x"})
    cmd = prod_command(key="match")
    cmd["executable_line"] = 193.5
    assert bridge(a).execute(cmd).state == B.EXECUTED


# ══════════════════════════════════════════════════════════════════════════
# 4. missing / unprovable market → NO BET
# ══════════════════════════════════════════════════════════════════════════

def _raw_bridge(obs):
    class _RawBridge(B.ResolverBrowserBridge):
        def __init__(self, o):
            super().__init__(_ObsResolver())
            self._raw = o

        def observe(self, command):
            return self._raw
    return _RawBridge(obs)


def test_missing_odds_rejected():
    # resolve_selection already fails closed on missing data; this also pins
    # the envelope's OWN guard so a resolver that returns a raw bad observation
    # can never be executed.
    obs = MarketObservation(event=EVENT, market="TOTAL", position="UNDER",
                            line=193.5, price=None)
    rec = B.ExecutionBridge(_raw_bridge(obs)).execute(prod_command(key="noodds"))
    assert rec.state == B.BRIDGE_REJECTED and rec.reason == B.R_ODDS_MISSING


def test_missing_line_rejected():
    obs = MarketObservation(event=EVENT, market="TOTAL", position="UNDER",
                            line=None, price=1.90)
    rec = B.ExecutionBridge(_raw_bridge(obs)).execute(prod_command(key="noline"))
    assert rec.state == B.BRIDGE_REJECTED and rec.reason == B.R_LINE_MISSING


def test_wrong_market_not_resolved_rejected():
    a = adapter_ok()
    a.missing_markets = {f"{EVENT}|TOTAL"}           # market unavailable
    rec = bridge(a).execute(prod_command(key="nomkt"))
    assert rec.state == B.BRIDGE_REJECTED and rec.reason == B.R_OBSERVE_FAILED
    assert a.placements == []


# ══════════════════════════════════════════════════════════════════════════
# 5. authentication is mandatory
# ══════════════════════════════════════════════════════════════════════════

def test_unauthenticated_session_no_bet():
    a = adapter_ok()
    rec = bridge(a, session_probe=lambda: False).execute(prod_command(key="nologin"))
    assert rec.state == B.BRIDGE_REJECTED and rec.reason == B.R_SESSION_EXPIRED
    assert a.placements == [] and a.clicks == []


def test_expired_session_probe_error_no_bet():
    def boom():
        raise RuntimeError("session probe failed")
    a = adapter_ok()
    rec = bridge(a, session_probe=boom).execute(prod_command(key="expired"))
    assert rec.state == B.BRIDGE_REJECTED and rec.reason == B.R_SESSION_EXPIRED


def test_authenticated_session_proceeds():
    a = adapter_ok(confirmation={"reference": "x"})
    rec = bridge(a, session_probe=lambda: True).execute(prod_command(key="login"))
    assert rec.state == B.EXECUTED


# ══════════════════════════════════════════════════════════════════════════
# 6. idempotency / timeout / ambiguity
# ══════════════════════════════════════════════════════════════════════════

def test_duplicate_execution_produces_one_bet():
    a = adapter_ok(confirmation={"reference": "x"})
    br = bridge(a)
    cmd = prod_command(key="dup")
    assert br.execute(cmd).state == B.EXECUTED
    second = br.execute(cmd)
    assert second.state == B.BRIDGE_REJECTED and second.reason == B.R_DUPLICATE
    assert len(a.placements) == 1


def test_timeout_unconfirmed_is_unknown():
    a = adapter_ok(place_status=SUBMITTED)           # never confirmed
    rec = bridge(a).execute(prod_command(key="to"))
    assert rec.state == B.BRIDGE_UNKNOWN and rec.submitted is False


def test_ambiguous_confirmation_is_unknown_not_success():
    a = adapter_ok(confirmation=None)                # ACCEPTED, no receipt
    rec = bridge(a).execute(prod_command(key="amb"))
    assert rec.state == B.BRIDGE_UNKNOWN
    assert rec.submitted is False
    assert rec.provider_ref == "fake-ref-1"          # ref seen, still not success
