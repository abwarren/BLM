"""AUTO-BET POSITION + BETTABLE ROI — 2026-10-05 directive.

Proves the SIGNAL / POSITION / BET separation (``blm_v4/betting/position.py``):

  * a position exists ONLY when the eligibility gate returned ALLOW;
  * the canonical idempotency key is the OPPORTUNITY (source-agnostic), so a
    repeated observation / alert / retry / refresh / restart maps to the SAME
    position and a duplicate can never become a second bet;
  * the lifecycle matrix (UNKNOWN is NEVER BET_PLACED);
  * ROI is computed from PLACED + SETTLED positions only (never from raw
    alerts, exclusions honoured), and is UNAVAILABLE — never estimated — when
    odds/stake are missing;
  * a full ZERO-STAKE rehearsal runs the pipeline end to end with no real bet.

SAFETY: no provider, no credentials, no network, no real bet anywhere.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import FakeBrowserAdapter  # noqa: E402

from blm_v4.betting import command as C  # noqa: E402
from blm_v4.betting import stake as S  # noqa: E402
from blm_v4.betting import position as P  # noqa: E402
from blm_v4.execution import browser_bridge as B  # noqa: E402

OBS = (190.0, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0, 193.5)
EVENT = "Team A vs Team B"
GID = "30990001"


def game(game_id=GID, checkpoint=75, age_s=5.0, trigger=193.5, cur=193.5,
         obs=OBS, active=True) -> dict:
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id, "live": True, "live_reason": None,
        "market": {"total_line": cur, "market_status": "LIVE",
                   "observed_lines": list(obs) if obs is not None else None},
        "projector": {"progress_pct": 76.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": cur, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": active, "checkpoint": checkpoint,
                        "trigger_line": trigger, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": f"{game_id}|{checkpoint}"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }


def allow_command(*, key="ik-pos", source=C.SOURCE_AUTONOMOUS, g=None):
    cmd = C.build_command(g or game(), source=source, mode=S.PRODUCTION_AUTO_BET,
                          unit_size=10.0, authorized=True, authorized_by="test",
                          idempotency_key=key)
    cmd["event"] = EVENT
    return cmd


def zero_command(*, key="ik-zero", g=None, source=C.SOURCE_MANUAL):
    cmd = C.build_command(g or game(), source=source, mode=S.ZERO_STAKE,
                          idempotency_key=key)
    cmd["event"] = EVENT
    return cmd


def adapter_ok(line=193.5, under=1.90, over=1.95) -> FakeBrowserAdapter:
    a = FakeBrowserAdapter()
    a.set_market(EVENT, line, over, under)
    return a


def bridge(a: FakeBrowserAdapter) -> B.ExecutionBridge:
    return B.ExecutionBridge(B.ResolverBrowserBridge(a))


# ══════════════════════════════════════════════════════════════════════════
# 1. the position is created ONLY at the eligibility ALLOW point
# ══════════════════════════════════════════════════════════════════════════

def test_position_created_only_on_allow():
    ok = allow_command(key="p-allow")
    assert ok["decision"] == "ALLOW"
    pos = P.position_from_command(ok, executable_odds=1.88)
    assert pos is not None and pos.state == P.POSITION_INTENT
    assert pos.game_id == GID and pos.checkpoint == 75
    assert pos.direction == "UNDER" and pos.market_id == "TOTAL"
    assert pos.trigger_line == 193.5
    assert pos.executable_line == 193.5          # the live market LINE
    assert pos.executable_odds == 1.88           # the DECIMAL ODDS (separate)
    assert pos.executable_line != pos.executable_odds   # a line is NOT odds
    assert pos.signal_id == f"{GID}|75"


def test_rejected_command_is_never_a_position():
    # rungs_moved = (190.0 - 193.5)/0.5 = -7 => REJECT
    bad = C.build_command(game(trigger=193.5, cur=190.0),
                          source=C.SOURCE_AUTONOMOUS,
                          mode=S.PRODUCTION_AUTO_BET, unit_size=10.0,
                          authorized=True, authorized_by="test",
                          idempotency_key="p-reject")
    assert bad["decision"] == "REJECT"
    assert P.position_from_command(bad) is None


def test_build_position_fails_closed_without_identity():
    with pytest.raises(ValueError):
        P.build_position(game_id="", checkpoint=75)
    with pytest.raises(ValueError):
        P.build_position(game_id=GID, checkpoint=None)


# ══════════════════════════════════════════════════════════════════════════
# 2. canonical idempotency key is the OPPORTUNITY (source-agnostic)
# ══════════════════════════════════════════════════════════════════════════

def test_canonical_key_is_source_agnostic_manual_equals_autonomous():
    m = C.manual_command(game(), mode=S.PRODUCTION_AUTO_BET, unit_size=10.0,
                         authorized=True, authorized_by="op")
    a = C.autonomous_command(game(), mode=S.PRODUCTION_AUTO_BET, unit_size=10.0,
                             authorized=True, authorized_by="worker")
    pm, pa = P.position_from_command(m), P.position_from_command(a)
    assert pm.position_id == pa.position_id          # ONE opportunity → ONE position
    assert pm.key == pa.key
    # the key is exactly game|market|direction|checkpoint|alert — no source,
    # no timestamp, no price.
    assert pm.key == f"{GID}|TOTAL|UNDER|75|{GID}|75"


def test_key_never_derives_from_price_or_time():
    a = P.build_position(game_id=GID, checkpoint=75, trigger_line=193.5,
                         executable_line=193.5, executable_odds=1.80,
                         trigger_time="2026-10-05T00:00:00Z")
    b = P.build_position(game_id=GID, checkpoint=75, trigger_line=199.5,
                         executable_line=199.5, executable_odds=2.50,
                         trigger_time="2026-10-05T09:99:99Z")
    assert a.position_id == b.position_id            # price/time irrelevant


# ══════════════════════════════════════════════════════════════════════════
# 3. duplicate protection: repeated observations → SAME position
# ══════════════════════════════════════════════════════════════════════════

def test_repeated_observation_returns_same_position():
    book = P.PositionBook()
    p1 = P.position_from_command(allow_command(key="d1"))
    p2 = P.position_from_command(allow_command(key="d1"))   # re-observed
    ok1, k1 = book.claim(p1)
    ok2, k2 = book.claim(p2)
    assert ok1 is True and k1 is p1
    assert ok2 is False and k2 is p1                  # the EXISTING position
    assert book.duplicates_blocked == 1
    assert len(book.positions()) == 1


def test_retry_refresh_restart_map_to_same_position():
    key = P.position_key(game_id=GID, checkpoint=75,
                         alert_id=f"{GID}|75")
    # a fresh worker (new book) rebuilding after a timeout/refresh/restart
    # recomputes the SAME id — nothing about the environment enters the key.
    assert P.position_id_for(key) == P.position_id_for(key)
    book_a, book_b = P.PositionBook(), P.PositionBook()
    book_a.claim(P.position_from_command(allow_command(key="r")))
    ok, existing = book_b.claim(P.position_from_command(allow_command(key="r")))
    assert ok is True and existing.position_id == P.position_id_for(key)


def test_only_one_execution_can_be_placed():
    a = adapter_ok()
    a.confirmation = {"reference": "PB-EXEC-1"}   # the bookmaker's own receipt
    br = bridge(a)
    cmd = allow_command(key="one-place")
    first = br.execute(cmd)
    second = br.execute(cmd)
    assert first.state == B.EXECUTED
    assert second.state == B.BRIDGE_REJECTED and second.reason == "duplicate_command"
    assert len(a.placements) == 1                      # exactly one bet, ever


# ══════════════════════════════════════════════════════════════════════════
# 4. lifecycle (UNKNOWN is never BET_PLACED)
# ══════════════════════════════════════════════════════════════════════════

def test_lifecycle_matrix_and_unknown_is_not_placed():
    for a_, b_ in [(P.SIGNAL, P.ELIGIBLE), (P.ELIGIBLE, P.POSITION_INTENT),
                   (P.POSITION_INTENT, P.EXECUTION_ATTEMPT),
                   (P.EXECUTION_ATTEMPT, P.BET_PLACED),
                   (P.EXECUTION_ATTEMPT, P.UNKNOWN),
                   (P.UNKNOWN, P.RECONCILIATION),
                   (P.RECONCILIATION, P.BET_PLACED),
                   (P.BET_PLACED, P.SETTLED)]:
        assert P.can_transition(a_, b_), (a_, b_)
    assert not P.can_transition(P.UNKNOWN, P.BET_PLACED)
    assert not P.can_transition(P.SIGNAL, P.BET_PLACED)
    with pytest.raises(P.InvalidPositionTransition):
        P.validate_transition(P.SIGNAL, P.SETTLED)


def test_reconciliation_resolves_unknown():
    book = P.PositionBook()
    pos = P.position_from_command(allow_command(key="recon"))
    book.claim(pos)
    book.transition(pos.position_id, P.EXECUTION_ATTEMPT)
    book.transition(pos.position_id, P.UNKNOWN)
    assert not pos.is_placed                            # UNKNOWN ≠ placed
    book.transition(pos.position_id, P.RECONCILIATION)
    book.transition(pos.position_id, P.BET_PLACED)
    assert pos.is_placed
    book.transition(pos.position_id, P.SETTLED)
    assert pos.is_settled


# ══════════════════════════════════════════════════════════════════════════
# 5. bettable ROI only
# ══════════════════════════════════════════════════════════════════════════

def _pos(stake, odds, result, state=P.SETTLED, gid=None, provider_ref="ref"):
    return P.Position(
        position_id=f"pos-{gid}-{result}", game_id=gid or GID, market_id="TOTAL",
        checkpoint=75, direction="UNDER", stake=stake,
        executable_odds=odds, executable_line=None,
        result=result, state=state, provider_ref=provider_ref)


def test_roi_excludes_rejected_pending_and_duplicates():
    win = _pos(10.0, 1.90, P.RESULT_WIN, gid="g1")
    loss = _pos(10.0, 2.00, P.RESULT_LOSS, gid="g2")
    rej = P.Position(position_id="pos-rej", game_id="g3", market_id="TOTAL",
                     checkpoint=75, direction="UNDER", state=P.REJECTED)
    pend = P.Position(position_id="pos-pend", game_id="g4", market_id="TOTAL",
                      checkpoint=75, direction="UNDER", state=P.POSITION_INTENT)
    r = P.roi([win, loss, rej, pend])
    assert r["positions_attempted"] == 4
    assert r["positions_placed"] == 2
    assert r["settled_positions"] == 2
    assert r["rejected_positions"] == 1
    assert r["pending_positions"] == 1
    assert r["total_stake"] == 20.0
    assert r["net_pl"] == pytest.approx(10.0 * 0.90 - 10.0)   # +9 - 10 = -1
    assert r["gross_returns"] == pytest.approx(19.0)
    assert r["roi_pct"] == pytest.approx(-5.0)               # -1 / 20
    assert r["win_rate"] == pytest.approx(50.0)


def test_roi_unavailable_without_odds_never_estimated():
    win = _pos(10.0, 1.90, P.RESULT_WIN, gid="g1")
    unknown_odds = _pos(10.0, None, P.RESULT_LOSS, gid="g2")
    r = P.roi([win, unknown_odds])
    assert r["roi_available"] is False
    assert r["roi_pct"] is None and r["net_pl"] is None


def test_roi_ignores_raw_alerts_by_construction():
    # the module's ROI takes POSITIONS; an alert-shaped dict has no position id
    # and contributes nothing (structural separation).
    r = P.roi([])
    assert r["positions_attempted"] == 0 and r["roi_pct"] is None


def test_candidate_price_is_a_line_never_odds():
    # the executor's candidate["price"] is the market LINE; it must map to
    # executable_line and NEVER to executable_odds.
    cand = {"execution_id": "bet-x", "idempotency_key": "k", "game_id": GID,
            "alert_id": f"{GID}|75", "checkpoint": "75", "market": "TOTAL",
            "selection": "UNDER", "triggered_line": 193.5,
            "price": 193.5, "stake_amount": 10.0}
    pos = P.position_from_candidate(cand)
    assert pos.executable_line == 193.5
    assert pos.executable_odds is None            # a LINE is not odds
    pos.state, pos.result = P.SETTLED, P.RESULT_WIN
    r = P.roi([pos])
    assert r["roi_available"] is False and r["roi_pct"] is None
    assert pos.position_id in r["missing_odds_positions"]


def test_odds_timestamp_and_bookmaker_ref_captured_at_position_time():
    ts = "2026-10-05T02:00:00Z"
    cand = {"execution_id": "bet-9", "game_id": GID, "alert_id": f"{GID}|75",
            "checkpoint": "75", "market": "TOTAL", "selection": "UNDER",
            "triggered_line": 193.5, "executable_line": 193.5,
            "executable_odds": 1.91, "stake_amount": 10.0,
            "provider_ref": "PB-EXEC-7", "executed_at": ts}
    pos = P.position_from_candidate(cand)
    assert pos.executable_line == 193.5 and pos.executable_odds == 1.91
    assert pos.executed_at == ts and pos.provider_ref == "PB-EXEC-7"
    assert pos.trigger_line == 193.5


def test_failed_and_unknown_executions_excluded_from_roi():
    win = _pos(10.0, 1.90, P.RESULT_WIN, gid="g1")
    failed = P.Position(position_id="pos-f", game_id="g2", market_id="TOTAL",
                        checkpoint=75, direction="UNDER", stake=10.0,
                        executable_odds=1.90, state=P.FAILED)
    unknown = P.Position(position_id="pos-u", game_id="g3", market_id="TOTAL",
                         checkpoint=75, direction="UNDER", stake=10.0,
                         executable_odds=1.90, state=P.UNKNOWN)
    r = P.roi([win, failed, unknown])
    assert r["settled_positions"] == 1
    assert r["positions_placed"] == 1
    assert r["failed_or_unknown_executions"] == 2
    assert r["total_stake"] == 10.0
    assert r["roi_pct"] == pytest.approx(90.0)     # +9 / 10


def test_settled_position_with_valid_odds_correct_pl_and_roi():
    win = _pos(10.0, 1.95, P.RESULT_WIN, gid="w")     # +9.50
    loss = _pos(20.0, 1.80, P.RESULT_LOSS, gid="l")   # -20.00
    r = P.roi([win, loss])
    assert r["total_stake"] == 30.0
    assert r["gross_returns"] == pytest.approx(19.50)
    assert r["net_pl"] == pytest.approx(-10.50)
    assert r["roi_pct"] == pytest.approx(-35.0)       # -10.5 / 30
    assert r["win_rate"] == pytest.approx(50.0)


# ══════════════════════════════════════════════════════════════════════════
# 6. ZERO-STAKE REHEARSAL — full pipeline, no real bet
# ══════════════════════════════════════════════════════════════════════════

def test_zero_stake_rehearsal_full_pipeline_no_real_bet():
    a = adapter_ok()
    br = bridge(a)
    # SIGNAL → ELIGIBILITY (the shared command validator) → POSITION
    cmd = zero_command(key="rehearsal")
    assert cmd["decision"] == "ALLOW"
    pos = P.position_from_command(cmd)
    assert pos is not None and pos.state == P.POSITION_INTENT
    # DUPLICATE PROTECTION
    book = P.PositionBook()
    ok1, _ = book.claim(pos)
    ok2, _ = book.claim(P.position_from_command(zero_command(key="rehearsal")))
    assert ok1 is True and ok2 is False and book.duplicates_blocked == 1
    # EXECUTION REQUEST → SIMULATED EXECUTION (the safety envelope)
    book.transition(pos.position_id, P.EXECUTION_ATTEMPT)
    receipt = br.execute(cmd)
    # LEDGER: a ZERO_STAKE command is a NO_OP — it submits nothing.
    assert receipt.state == B.NO_OP and receipt.reason == B.R_ZERO_STAKE
    assert a.clicks == [] and a.placements == [] and a.slip == []
    # RECONCILIATION / SETTLEMENT: nothing was placed → the position stays
    # pending (never promoted to BET_PLACED), so nothing settles and no money
    # moved.  UNKNOWN is NEVER treated as BET_PLACED.
    assert not pos.is_placed and not pos.is_settled
    assert pos.state == P.EXECUTION_ATTEMPT and pos.is_pending
    r = P.roi(book.positions())
    assert r["positions_placed"] == 0 and r["roi_pct"] is None
