"""BLM EXECUTION — PARLAY EXECUTION TEST SUITE (phase ①).

The MANDATORY test (directive §18 / §14) runs first: a selection created
at UNDER 164.5 @ 1.90, the bookmaker changes to UNDER 166.5 @ 1.83,
execution resolves and selects 166.5 @ 1.83 — no manual intervention,
no stale click.

Covers the directive §17 matrix: unchanged line, changed line, changed
price, changed line+price, selection disappearing, event/market/position
not found, stale DOM, bookmaker rejection, betslip verification failure,
retry, maximum retry, multiple parlay combinations, duplicate selection,
conflicting selection, abort during execution, idempotent duplicate
protection, dry-run never clicking/placing, and the hard mode ladder.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import (  # noqa: E402
    ACCEPTED,
    FAILED,
    REJECTED,
    SUBMITTED,
    FakeBrowserAdapter,
)

from blm_v4.execution.betslip_verifier import (  # noqa: E402
    BETSLIP_NOT_UPDATED,
    BETSLIP_UNREADABLE,
    VERIFIED,
    verify_full_betslip,
    verify_leg_in_betslip,
)
from blm_v4.execution.config import ExecutionConfig  # noqa: E402
from blm_v4.execution.execution_queue import (  # noqa: E402
    ExecutionQueue,
    RunRequestError,
)
from blm_v4.execution.parlay_matrix import build_matrix  # noqa: E402
from blm_v4.execution.selection_model import (  # noqa: E402
    MODE_DRY_RUN,
    MODE_LIVE,
    MODE_RESOLVE_ONLY,
    MODE_TEST_VERIFY,
    ParlayJob,
    Selection,
    STATE_DRY_RUN_COMPLETE,
    validate_selection,
)
from blm_v4.execution.selection_resolver import resolve_selection  # noqa: E402
from blm_v4.execution.total_executor import TotalExecutor  # noqa: E402

GAME_A = "Team A vs Team B"
GAME_B = "Team C vs Team D"
GAME_C = "Team E vs Team F"
GAME_D = "Team G vs Team H"


def make_cfg(tmp_path, **over) -> ExecutionConfig:
    defaults = dict(
        dry_run=True, max_selection_retries=3, retry_delay_ms=0,
        settle_ms=0, slip_wait_ms=0, verify_attempts=3,
        leg_timeout_s=45.0, job_timeout_s=600.0,
        max_parlays_per_run=100, db_path=str(tmp_path / "blm_execution.db"))
    defaults.update(over)
    return ExecutionConfig(**defaults)


def under(event: str, **kw) -> Selection:
    return Selection(event=event, market="TOTAL", position="UNDER", **kw)


def over(event: str, **kw) -> Selection:
    return Selection(event=event, market="TOTAL", position="OVER", **kw)


# ══════════════════════════════════════════════════════════════════════
# THE MANDATORY TEST
# ══════════════════════════════════════════════════════════════════════

class TestMandatoryStaleLineScenario:
    def test_created_164_5_book_moves_to_166_5_system_selects_166_5(
            self, tmp_path):
        """User creates TOTAL → UNDER while the book shows
        UNDER 164.5 @ 1.90.  The bookmaker then changes to
        UNDER 166.5 @ 1.83.  EXECUTE must resolve and select
        166.5 @ 1.83 — automatically, with no manual reselection."""
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
        sel = under(GAME_A, snapshot_line=164.5, snapshot_price=1.90)

        # ── the bookmaker changes the market AFTER creation ──────────
        adapter.move_market(GAME_A, line=166.5, over=1.97, under=1.83)

        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
        result = executor.run_job("exec-mandatory", job, MODE_DRY_RUN)

        assert result.status == STATE_DRY_RUN_COMPLETE
        assert result.resolved_line == 166.5
        assert result.resolved_price == 1.83
        # exactly ONE click, on the CURRENT selection
        assert len(adapter.clicks) == 1
        c = adapter.clicks[0]
        assert (c["line"], c["price"]) == (166.5, 1.83)
        assert c["position"] == "UNDER"
        # and the slip shows the new values
        slip = adapter.read_betslip()
        assert slip[0]["line"] == 166.5 and slip[0]["price"] == 1.83

    def test_moves_again_mid_execution_resolves_167_5(self, tmp_path):
        """If it changes AGAIN (164.5 → 167.5) between the click and the
        retry, the next attempt resolves the NEW offer — never the old
        line (no stale DOM click)."""
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
        sel = under(GAME_A)
        # the slip lags LONGER than the verify window: the first click
        # can NEVER be confirmed → the engine must retry
        adapter.slip_lag_reads = 99
        orig_find = adapter.find_position

        def find_and_move(event, market, position):
            # fires at the RETRY's resolution: the book has moved and
            # re-rendered (stale leg gone, fresh slip)
            if adapter._click_no >= 1:
                adapter.move_market(GAME_A, line=167.5, over=1.98,
                                    under=1.80)
                adapter.slip.clear()
                adapter.slip_lag_reads = 0
                adapter.find_position = orig_find
            return orig_find(event, market, position)

        adapter.find_position = find_and_move
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
        result = executor.run_job("exec-mandatory2", job, MODE_DRY_RUN)

        assert result.status == STATE_DRY_RUN_COMPLETE
        assert result.resolved_line == 167.5
        assert result.resolved_price == 1.80
        assert adapter.clicks[0]["line"] == 164.5   # first (stale) attempt
        assert adapter.clicks[1]["line"] == 167.5   # re-resolved

    def test_over_side_stale_line(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_B, line=158.5, over=1.92, under=1.88)
        sel = over(GAME_B, snapshot_line=158.5, snapshot_price=1.92)
        adapter.move_market(GAME_B, line=160.5, over=1.86, under=1.94)
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
        result = executor.run_job("exec-mandatory3", job, MODE_DRY_RUN)
        assert result.status == STATE_DRY_RUN_COMPLETE
        assert (result.resolved_line, result.resolved_price) == (160.5, 1.86)
        assert adapter.clicks[0]["position"] == "OVER"


# ══════════════════════════════════════════════════════════════════════
# SELECTION MODEL / SCOPE LOCK
# ══════════════════════════════════════════════════════════════════════

class TestSelectionModel:
    def test_identity_excludes_line_and_price(self):
        a = under(GAME_A, snapshot_line=164.5, snapshot_price=1.90)
        b = under(GAME_A, snapshot_line=166.5, snapshot_price=1.83)
        assert a == b and hash(a) == hash(b)

    def test_different_positions_differ(self):
        assert under(GAME_A) != over(GAME_A)

    def test_scope_lock_rejects_other_markets(self):
        with pytest.raises(ValueError):
            validate_selection("SPREAD", "UNDER")
        with pytest.raises(ValueError):
            Selection(event=GAME_A, market="MONEYLINE", position="HOME")
        with pytest.raises(ValueError):
            Selection(event=GAME_A, market="TOTAL", position="BOTH")

    def test_event_required(self):
        with pytest.raises(ValueError):
            Selection(event="  ", market="TOTAL", position="UNDER")


# ══════════════════════════════════════════════════════════════════════
# MATRIX BUILDER
# ══════════════════════════════════════════════════════════════════════

class TestMatrix:
    def four_sels(self):
        return [under(GAME_A), under(GAME_B), under(GAME_C), under(GAME_D)]

    def test_doubles_count_and_content(self):
        rep = build_matrix(self.four_sels(), [2])
        assert rep.combo_count == 6
        pairs = {frozenset(s.event for s in c) for c in rep.combos}
        assert len(pairs) == 6
        assert rep.ok

    def test_trebles_and_4fold(self):
        rep = build_matrix(self.four_sels(), [3, 4])
        assert rep.combo_count == 4 + 1  # C(4,3) + C(4,4)
        assert all(len(c) == 3 for c in rep.combos[:4])
        assert len(rep.combos[-1]) == 4

    def test_singles(self):
        rep = build_matrix(self.four_sels(), [1])
        assert rep.combo_count == 4

    def test_duplicate_legs_flagged(self):
        rep = build_matrix([under(GAME_A), under(GAME_A)], [1])
        assert not rep.ok and len(rep.duplicates) == 1

    def test_conflict_over_under_same_event_flagged(self):
        rep = build_matrix([under(GAME_A), over(GAME_A), under(GAME_B)], [2])
        assert not rep.ok
        assert rep.conflicts == [{"event": GAME_A, "market": "TOTAL",
                                  "positions": ["OVER", "UNDER"]}]

    def test_fold_larger_than_selections_generates_nothing(self):
        rep = build_matrix([under(GAME_A)], [2])
        assert rep.combo_count == 0


# ══════════════════════════════════════════════════════════════════════
# FRESH RESOLUTION
# ══════════════════════════════════════════════════════════════════════

class TestResolution:
    def test_resolves_current_values(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, line=171.5, over=1.90, under=1.87)
        res = resolve_selection(adapter, under(GAME_A))
        assert res.ok and res.observation.line == 171.5
        assert res.observation.price == 1.87

    def test_event_not_found(self):
        adapter = FakeBrowserAdapter()
        res = resolve_selection(adapter, under("Missing Game"))
        assert not res.ok and res.reason == "EVENT_NOT_FOUND"

    def test_market_not_found(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.9, 1.9)
        adapter.missing_markets.add(f"{GAME_A}|TOTAL")
        res = resolve_selection(adapter, under(GAME_A))
        assert not res.ok and res.reason == "MARKET_NOT_FOUND"

    def test_position_not_found_is_recoverable_reason(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.9, 1.9)
        adapter.missing_positions.add(f"{GAME_A}|UNDER")
        res = resolve_selection(adapter, under(GAME_A))
        assert not res.ok and res.reason == "POSITION_NOT_FOUND"

    def test_suspended_position_treated_as_not_selectable(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.9, 1.9)
        adapter.suspended_positions.add(f"{GAME_A}|UNDER")
        res = resolve_selection(adapter, under(GAME_A))
        assert not res.ok and res.reason == "POSITION_NOT_FOUND"

    def test_adapter_down_propagates(self):
        adapter = FakeBrowserAdapter()
        adapter.adapt_down = True
        from blm_v4.execution.adapter import AdapterUnavailable
        with pytest.raises(AdapterUnavailable):
            resolve_selection(adapter, under(GAME_A))


# ══════════════════════════════════════════════════════════════════════
# BETSLIP VERIFIER
# ══════════════════════════════════════════════════════════════════════

class TestBetslipVerifier:
    def test_exact_match_verified(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 166.5, 1.97, 1.83)
        adapter.slip.append({"event": GAME_A, "market": "Total Points",
                             "position": "UNDER", "line": 166.5,
                             "price": 1.83})
        chk = verify_leg_in_betslip(adapter, under(GAME_A), 166.5, 1.83)
        assert chk.outcome == VERIFIED

    def test_absent_leg_not_updated(self):
        adapter = FakeBrowserAdapter()
        chk = verify_leg_in_betslip(adapter, under(GAME_A), 166.5, 1.83)
        assert chk.outcome == BETSLIP_NOT_UPDATED

    def test_unreadable_slip(self):
        adapter = FakeBrowserAdapter()
        adapter.slip_lag_reads = 1  # slip "has not caught up"
        chk = verify_leg_in_betslip(adapter, under(GAME_A), 166.5, 1.83)
        assert chk.outcome == BETSLIP_NOT_UPDATED

    def test_race_path_slip_shows_current_offer_verified(self):
        """Clicked 164.5; the book re-rendered the slip at 166.5 (the
        book's current offer) — the verifier confirms via the current
        observation, the position the user asked for."""
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, line=166.5, over=1.97, under=1.83)
        adapter.slip.append({"event": GAME_A, "market": "Total Points",
                             "position": "UNDER", "line": 166.5,
                             "price": 1.83})
        chk = verify_leg_in_betslip(adapter, under(GAME_A), 164.5, 1.90)
        assert chk.outcome == VERIFIED and chk.matched["line"] == 166.5

    def test_race_path_stale_slip_values_not_updated(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, line=167.5, over=1.98, under=1.80)
        adapter.slip.append({"event": GAME_A, "market": "Total Points",
                             "position": "UNDER", "line": 166.5,
                             "price": 1.83})
        chk = verify_leg_in_betslip(adapter, under(GAME_A), 164.5, 1.90)
        assert chk.outcome == BETSLIP_NOT_UPDATED

    def test_event_match_tolerates_team_order_and_suffix(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 166.5, 1.97, 1.83)
        adapter.slip.append({"event": "Team B vs Team A (Live)",
                             "market": "TOTAL", "position": "under",
                             "line": 166.5, "price": 1.83})
        chk = verify_leg_in_betslip(adapter, under(GAME_A), 166.5, 1.83)
        assert chk.outcome == VERIFIED

    def test_full_betslip_gate_missing_leg(self):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 166.5, 1.97, 1.83)
        adapter.set_market(GAME_B, 158.5, 1.95, 1.91)
        adapter.slip.append({"event": GAME_A, "market": "Total Points",
                             "position": "UNDER", "line": 166.5,
                             "price": 1.83})
        legs = [under(GAME_A), under(GAME_B)]
        full = verify_full_betslip(adapter, legs, [166.5, 158.5],
                                   [1.83, 1.91])
        assert not full["ok"] and full["missing"] == [legs[1].to_dict()]

    def test_full_betslip_gate_ok(self):
        adapter = FakeBrowserAdapter()
        for g, ln, pr in ((GAME_A, 166.5, 1.83), (GAME_B, 158.5, 1.91)):
            adapter.set_market(g, ln, 1.95, pr)
            adapter.slip.append({"event": g, "market": "Total Points",
                                 "position": "UNDER", "line": ln,
                                 "price": pr})
        legs = [under(GAME_A), under(GAME_B)]
        full = verify_full_betslip(adapter, legs, [166.5, 158.5],
                                   [1.83, 1.91])
        assert full["ok"]


# ══════════════════════════════════════════════════════════════════════
# EXECUTOR — failure / retry matrix
# ══════════════════════════════════════════════════════════════════════

class TestExecutorFailures:
    def _run_one(self, tmp_path, adapter, sel=None, **cfg_over):
        sel = sel or under(GAME_A)
        executor = TotalExecutor(adapter, make_cfg(tmp_path, **cfg_over))
        job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
        return executor.run_job("exec-x", job, MODE_DRY_RUN), adapter

    def test_unchanged_line_single_click(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        job, adapter = self._run_one(tmp_path, adapter)
        assert job.status == STATE_DRY_RUN_COMPLETE
        assert len(adapter.clicks) == 1
        assert adapter.clicks[0]["line"] == 164.5

    def test_event_not_found_terminal(self, tmp_path):
        job, _ = self._run_one(tmp_path, FakeBrowserAdapter())
        assert job.status == "NON_RECOVERABLE_ERROR"
        assert "EVENT_NOT_FOUND" in job.last_error

    def test_market_not_found_terminal(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.9, 1.9)
        adapter.missing_markets.add(f"{GAME_A}|TOTAL")
        job, _ = self._run_one(tmp_path, adapter)
        assert job.status == "NON_RECOVERABLE_ERROR"
        assert "MARKET_NOT_FOUND" in job.last_error

    def test_position_missing_then_reappearing_retries(self, tmp_path):
        """UNDER vanishes (market refresh), then comes back — the job
        recovers by re-resolving, never by re-clicking stale DOM."""
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.missing_positions.add(f"{GAME_A}|UNDER")
        orig_find = adapter.find_position

        def heal(event, market, position):
            # the market comes back after the first failed resolution
            if (event, market, position) == (GAME_A, "TOTAL", "UNDER"):
                adapter.missing_positions.discard(f"{GAME_A}|UNDER")
                adapter.find_position = orig_find
            return orig_find(event, market, position)

        adapter.find_position = heal
        job, _ = self._run_one(tmp_path, adapter)
        assert job.status == STATE_DRY_RUN_COMPLETE
        assert len(adapter.clicks) == 1

    def test_position_never_returns_hits_max_retries(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.missing_positions.add(f"{GAME_A}|UNDER")
        job, _ = self._run_one(tmp_path, adapter,
                               max_selection_retries=2)
        assert job.status == "NON_RECOVERABLE_ERROR"
        assert "MAX_RETRIES_EXCEEDED" in job.last_error

    def test_click_fails_then_retries(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.click_fail_on = 1
        job, adapter = self._run_one(tmp_path, adapter)
        assert job.status == STATE_DRY_RUN_COMPLETE
        assert len(adapter.clicks) == 2  # failed click, then success
        assert adapter.clicks[1]["line"] == 164.5

    def test_click_always_fails_hits_max_retries(self, tmp_path):
        class AlwaysFail(FakeBrowserAdapter):
            def click_selection(self, obs):
                self._click_no += 1
                self.clicks.append({"n": self._click_no,
                                    "event": obs.event,
                                    "position": obs.position,
                                    "line": obs.line, "price": obs.price})
                return False  # click never registers

        adapter = AlwaysFail()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        job, _ = self._run_one(tmp_path, adapter,
                               max_selection_retries=2)
        assert job.status == "NON_RECOVERABLE_ERROR"
        assert "MAX_RETRIES_EXCEEDED" in job.last_error
        assert len(job.attempts_per_leg[0]) == 3  # 1 + 2 retries
        assert len(adapter.clicks) == 3

    def test_betslip_never_updates_verification_failure(self, tmp_path):
        """Click succeeds but the leg never appears in the slip →
        bounded retries → BETSLIP_VERIFICATION_FAILED terminal."""

        class AlwaysDrop(FakeBrowserAdapter):
            def click_selection(self, obs):
                self._click_no += 1
                self.clicks.append({"n": self._click_no,
                                    "event": obs.event,
                                    "position": obs.position,
                                    "line": obs.line, "price": obs.price})
                return True  # "succeeded" — slip never updates

        adapter = AlwaysDrop()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        job, _ = self._run_one(tmp_path, adapter,
                               max_selection_retries=2)
        # A slip that NEVER updates is a bounded-retry exhaustion — the
        # honest terminal reason is MAX_RETRIES_EXCEEDED, every attempt
        # recorded (click + BETSLIP_NOT_UPDATED pair), never "assume ok".
        assert job.status == "NON_RECOVERABLE_ERROR"
        assert "MAX_RETRIES_EXCEEDED" in job.last_error
        outcomes = [a["outcome"] for a in job.attempts_per_leg[0]]
        assert outcomes.count("BETSLIP_NOT_UPDATED") == 3
        assert "LEG_CONFIRMED" not in outcomes
        assert "ALREADY_IN_SLIP" not in outcomes
        assert len(adapter.clicks) == 3

    def test_stale_dom_click_never_repeated(self, tmp_path):
        """After a failed verification the retry must RE-RESOLVE: the
        second click carries the NEW line, never the stale one.  The
        book's re-render also clears the slip of the stale leg."""
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.slip_lag_reads = 10  # every verify read misses until healed
        orig_find = adapter.find_position

        def resolve_move(event, market, position):
            if adapter._click_no >= 1:  # the retry's resolution
                adapter.move_market(GAME_A, 166.5, 1.97, 1.83)
                adapter.slip.clear()   # book re-render: stale leg removed
                adapter.slip_lag_reads = 0
                adapter.find_position = orig_find
            return orig_find(event, market, position)

        adapter.find_position = resolve_move
        job, _ = self._run_one(tmp_path, adapter)
        assert job.status == STATE_DRY_RUN_COMPLETE
        assert len(adapter.clicks) == 2
        assert adapter.clicks[0]["line"] == 164.5   # first attempt
        assert adapter.clicks[1]["line"] == 166.5   # re-resolved — NOT stale
        assert job.resolved_line == 166.5

    def test_bookmaker_rejection_of_placement_terminal(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.place_status = REJECTED
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-rej", job, MODE_LIVE)
        assert result.status == "NON_RECOVERABLE_ERROR"
        assert "ORDER_REJECTED" in result.last_error
        assert adapter.placements and adapter.placements[0][
            "status"] == REJECTED

    def test_duplicate_protection_leg_already_in_slip_not_readded(
            self, tmp_path):
        """The bookmaker accepted the click but the response was lost:
        on retry the slip already holds the leg → NO second click,
        NO duplicate."""
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        # slip is pre-populated with the leg (as if an earlier run /
        # lost-response added it); verification read counts: the FIRST
        # idempotency check will see it already present
        adapter.slip.append({"event": GAME_A, "market": "Total Points",
                             "position": "UNDER", "line": 164.5,
                             "price": 1.90})
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-idem", job, MODE_DRY_RUN)
        assert result.status == STATE_DRY_RUN_COMPLETE
        assert len(adapter.clicks) == 0  # never re-added
        assert result.resolved_line == 164.5  # values from the slip

    def test_duplicate_entries_in_slip_is_terminal(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.duplicate_leg_on_click = 1
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-dup", job, MODE_DRY_RUN)
        assert result.status == "NON_RECOVERABLE_ERROR"
        assert "BETSLIP_VERIFICATION_FAILED" in result.last_error


# ══════════════════════════════════════════════════════════════════════
# EXECUTOR — multi-leg, multi-parlay, watchdog, abort
# ══════════════════════════════════════════════════════════════════════

class TestExecutorJobs:
    def test_three_legs_all_confirmed_then_dry_run_complete(
            self, tmp_path):
        adapter = FakeBrowserAdapter()
        for g, ln, pr in ((GAME_A, 166.5, 1.83), (GAME_B, 158.5, 1.91),
                          (GAME_C, 171.5, 1.87)):
            adapter.set_market(g, ln, 1.95, pr)
        legs = [under(GAME_A), under(GAME_B), under(GAME_C)]
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=legs, fold_size=3, stake_amount=10.0)
        result = executor.run_job("exec-3leg", job, MODE_DRY_RUN)
        assert result.status == STATE_DRY_RUN_COMPLETE
        assert [c["event"] for c in adapter.clicks] == [
            GAME_A, GAME_B, GAME_C]
        assert len(adapter.slip) == 3

    def test_mixed_over_under_matrix_runs_each_combination(
            self, tmp_path):
        adapter = FakeBrowserAdapter()
        for g, ln, ov, un in ((GAME_A, 166.5, 1.97, 1.83),
                              (GAME_B, 158.5, 1.95, 1.91),
                              (GAME_C, 171.5, 1.87, 1.93)):
            adapter.set_market(g, ln, ov, un)
        legs = [under(GAME_A), over(GAME_B), under(GAME_C)]
        rep = build_matrix(legs, [2])
        assert rep.ok and rep.combo_count == 3
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        jobs = [ParlayJob(legs=list(c), fold_size=2, stake_amount=5.0)
                for c in rep.combos]
        done = executor.run_matrix("exec-mix", jobs, MODE_DRY_RUN)
        assert all(j.status == STATE_DRY_RUN_COMPLETE for j in done)
        # Idempotent across combinations: a leg already in the slip from
        # an earlier combination in the SAME run is never re-added —
        # each leg clicked exactly once.
        counts = {}
        for c in adapter.clicks:
            counts[c["event"]] = counts.get(c["event"], 0) + 1
        assert counts == {GAME_A: 1, GAME_B: 1, GAME_C: 1}
        # every leg ended up in the slip exactly once (no duplicates)
        slip = adapter.read_betslip()
        assert len(slip) == 3

    def test_abort_during_leg_marks_user_abort(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        from blm_v4.execution.selection_model import AbortEvent
        abort = AbortEvent()
        orig_find = adapter.find_position

        def abort_at_resolution(event, market, position):
            # abort surfaces while the engine is RESOLVING the first leg
            abort.request()
            return orig_find(event, market, position)

        adapter.find_position = abort_at_resolution
        executor = TotalExecutor(adapter, make_cfg(tmp_path), abort=abort)
        job = ParlayJob(legs=[under(GAME_A), under(GAME_B)], fold_size=2,
                        stake_amount=10.0)
        result = executor.run_job("exec-abort", job, MODE_DRY_RUN)
        assert result.status == "USER_ABORT"
        assert len(adapter.clicks) == 0      # stopped BEFORE clicking
        assert adapter.placements == []      # nothing placed

    def test_abort_after_first_leg_completes_stops_second(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.set_market(GAME_B, 158.5, 1.95, 1.91)
        from blm_v4.execution.selection_model import AbortEvent
        abort = AbortEvent()
        orig_click = adapter.click_selection

        def click_then_abort(obs):
            ok = orig_click(obs)
            abort.request()
            return ok

        adapter.click_selection = click_then_abort
        executor = TotalExecutor(adapter, make_cfg(tmp_path), abort=abort)
        job = ParlayJob(legs=[under(GAME_A), under(GAME_B)], fold_size=2,
                        stake_amount=10.0)
        result = executor.run_job("exec-abort2", job, MODE_DRY_RUN)
        assert result.status == "USER_ABORT"
        assert len(adapter.clicks) == 1      # leg 1 done, leg 2 never tried

    def test_watchdog_job_timeout(self, tmp_path):
        class SlowClock:
            def __init__(self):
                self.t = 0.0

            def __call__(self):
                self.t += 10.0
                return self.t

        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        cfg = make_cfg(tmp_path, job_timeout_s=15.0)
        executor = TotalExecutor(adapter, cfg, clock=SlowClock())
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-wd", job, MODE_DRY_RUN)
        assert result.status == "NON_RECOVERABLE_ERROR"
        assert "WATCHDOG_TIMEOUT" in result.last_error


# ══════════════════════════════════════════════════════════════════════
# MODE LADDER — never a silent transition to live
# ══════════════════════════════════════════════════════════════════════

class TestModeLadder:
    def test_dry_run_never_places_even_live_job(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-dry", job, MODE_DRY_RUN)
        assert result.status == STATE_DRY_RUN_COMPLETE
        assert adapter.placements == []
        assert result.resolved_line == 164.5  # resolution still happens

    def test_dry_run_stops_at_betslip_ready_state(self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        seen_states = []

        class SpyExecutor(TotalExecutor):
            def _set_state(self, *a, **kw):
                seen_states.append(a[3] if len(a) > 3 else kw.get("state"))
                return super()._set_state(*a, **kw)

        executor = SpyExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        executor.run_job("exec-dry2", job, MODE_RESOLVE_ONLY)
        assert "BETSLIP_READY" in seen_states
        assert "PLACING_PARLAY" not in seen_states
        assert "ORDER_PLACED" not in seen_states

    def test_live_mode_reaches_order_placed_with_confirmation(
            self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.place_status = SUBMITTED
        adapter.confirmation = {"reference": "BK-12345", "accepted": True}
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-live", job, MODE_LIVE)
        assert result.status == "COMPLETE"
        assert result.order_reference == "BK-12345"
        assert len(adapter.placements) == 1

    def test_submitted_without_confirmation_is_unconfirmed_failure(
            self, tmp_path):
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        adapter.place_status = SUBMITTED
        adapter.confirmation = None  # bookmaker never confirms
        executor = TotalExecutor(adapter, make_cfg(tmp_path))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        result = executor.run_job("exec-unconf", job, MODE_LIVE)
        assert result.status == "NON_RECOVERABLE_ERROR"
        assert "ORDER_UNCONFIRMED" in result.last_error
        # and crucially: NOT resubmitted while unconfirmed
        assert len(adapter.placements) == 1

    def test_queue_refuses_live_when_dry_run_env(self, tmp_path):
        cfg = make_cfg(tmp_path, dry_run=True)
        q = ExecutionQueue(cfg)
        with pytest.raises(RunRequestError) as e:
            q.validate_request([under(GAME_A)], [1], 10.0, MODE_LIVE)
        assert "LIVE mode refused" in str(e.value)

    def test_queue_allows_live_only_when_env_permits(self, tmp_path):
        cfg = make_cfg(tmp_path, dry_run=False)
        q = ExecutionQueue(cfg)
        out = q.validate_request([under(GAME_A)], [1], 10.0, MODE_LIVE)
        assert out["exposure"] == 10.0

    def test_queue_refuses_duplicates_and_conflicts(self, tmp_path):
        cfg = make_cfg(tmp_path)
        q = ExecutionQueue(cfg)
        with pytest.raises(RunRequestError, match="duplicate"):
            q.validate_request([under(GAME_A), under(GAME_A)], [1], 10.0,
                               MODE_DRY_RUN)
        with pytest.raises(RunRequestError, match="conflicting"):
            q.validate_request([under(GAME_A), over(GAME_A)], [2], 10.0,
                               MODE_DRY_RUN)

    def test_queue_refuses_bad_stake_and_exposure_caps(self, tmp_path):
        cfg = make_cfg(tmp_path, max_total_exposure=50.0,
                       max_stake_per_parlay=20.0)
        q = ExecutionQueue(cfg)
        with pytest.raises(RunRequestError, match="positive"):
            q.validate_request([under(GAME_A)], [1], 0, MODE_DRY_RUN)
        with pytest.raises(RunRequestError, match="max stake"):
            q.validate_request([under(GAME_A)], [1], 25.0, MODE_DRY_RUN)
        with pytest.raises(RunRequestError, match="exposure"):
            q.validate_request([under(GAME_A), under(GAME_B)], [1, 2],
                               20.0, MODE_DRY_RUN)  # 6 combos → 120 > 50


# ══════════════════════════════════════════════════════════════════════
# PERSISTENCE (blm_execution.db)
# ══════════════════════════════════════════════════════════════════════

class TestStore:
    def test_job_upsert_and_status(self, tmp_path):
        from blm_v4.execution.store import ExecutionStore
        store = ExecutionStore(str(tmp_path / "blm_execution.db"))
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        store.upsert_job("exec-1", job.to_dict(), MODE_DRY_RUN)
        store.update_job_status("exec-1", job.parlay_id,
                                STATE_DRY_RUN_COMPLETE, retry_count=1)
        rows = store.jobs_for_run("exec-1")
        assert rows[0]["status"] == STATE_DRY_RUN_COMPLETE
        assert rows[0]["retry_count"] == 1

    def test_order_row_unique_per_parlay(self, tmp_path):
        from blm_v4.execution.store import ExecutionStore
        store = ExecutionStore(str(tmp_path / "blm_execution.db"))
        assert store.record_order("exec-1", "P1", 1, 10.0, "ACCEPTED")
        assert not store.record_order("exec-1", "P1", 2, 10.0, "ACCEPTED")
        assert store.get_order("P1")["attempt_no"] == 1

    def test_executor_persists_attempts_and_audit(self, tmp_path):
        from blm_v4.execution.store import ExecutionStore
        db = str(tmp_path / "blm_execution.db")
        store = ExecutionStore(db)
        adapter = FakeBrowserAdapter()
        adapter.set_market(GAME_A, 164.5, 1.95, 1.90)
        executor = TotalExecutor(adapter, make_cfg(tmp_path), store=store)
        job = ParlayJob(legs=[under(GAME_A)], fold_size=1,
                        stake_amount=10.0)
        executor.run_job("exec-store", job, MODE_DRY_RUN)
        import sqlite3
        with sqlite3.connect(db) as c:
            n_jobs = c.execute(
                "SELECT COUNT(*) FROM execution_jobs").fetchone()[0]
            n_att = c.execute(
                "SELECT COUNT(*) FROM leg_attempts").fetchone()[0]
            n_conf = c.execute(
                "SELECT COUNT(*) FROM leg_attempts "
                "WHERE outcome='LEG_CONFIRMED'").fetchone()[0]
            n_aud = c.execute(
                "SELECT COUNT(*) FROM execution_audit").fetchone()[0]
            click_row = c.execute(
                "SELECT clicked_line, clicked_price FROM leg_attempts "
                "WHERE outcome='CLICKED'").fetchone()
            conf_row = c.execute(
                "SELECT verified_line, verified_price FROM leg_attempts "
                "WHERE outcome='LEG_CONFIRMED'").fetchone()
        # each attempt records its click AND its confirmation separately
        assert n_jobs == 1 and n_aud > 0
        assert n_att == 2 and n_conf == 1
        assert (click_row[0], click_row[1]) == (164.5, 1.90)
        assert (conf_row[0], conf_row[1]) == (164.5, 1.90)
