"""Auto-Bet cohort selection — TEST-ONLY, fail-closed (2026-10-29).

Directive (2026-10-27, docs/AGENT_WORKFLOW_TC_COHORT_STATS_AND_UI.md §6.5): the
operator selects which of the seven named cohorts Auto-Bet is CONFIGURED to use.
One click must select it, the backend must validate it, and NOTHING about
execution may change: not the enabled state, not the unit price, not the caps,
not the kill switch, not dry-run, not the browser-submit authorization, and no
bet may be placed or retried.

ONE NAMED TEST PER CONTRACT PROPERTY:
  test_all_seven_frozen_codes_are_selectable
  test_unknown_code_is_rejected_and_nothing_is_written
  test_display_labels_and_case_variants_are_not_codes
  test_non_string_and_oversized_values_are_rejected
  test_selection_persists_across_store_instances
  test_selection_never_changes_execution_configuration
  test_selection_writes_one_audit_row_and_no_execution
  test_concurrent_selections_converge_on_one_valid_code
  test_missing_risk_limits_keep_execution_locked
  test_fully_configured_environment_unlocks_status
  test_corrupt_persisted_value_falls_back_to_the_default
  test_api_accepts_the_seven_codes_and_rejects_the_rest
  test_api_write_accepts_only_the_cohort_field
  test_betting_status_exposes_the_selected_cohort_read_only
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4.betting import api as betting_api
from blm_v4.betting import cohort_selection as sel
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED
from blm_v4.cohorts import spec


def _cfg(tmp_path, **over) -> BettingConfig:
    base = dict(dry_run=False, max_stake_per_bet=100.0, max_bets_per_day=50,
                max_daily_exposure=1000.0, stake_units=1.0,
                min_unit_price=1.0, max_unit_price=1000.0,
                alert_max_age_s=90.0, db_path=str(tmp_path / "sel.db"))
    base.update(over)
    return BettingConfig(**base)


def _store(tmp_path, *, enabled: bool = False, unit: float = 2.0) -> BettingStore:
    s = BettingStore(str(tmp_path / "sel.db"))
    s.set_unit_price(unit)
    s.set_config(KEY_AUTO_ENABLED, "true" if enabled else "false")
    return s


def _config_snapshot(store: BettingStore) -> dict:
    """Every persisted config key — the witness for 'nothing else changed'."""
    with sqlite3.connect(store.db_path) as c:
        return dict(c.execute("SELECT key, value FROM betting_config").fetchall())


def _count(store: BettingStore, table: str) -> int:
    with sqlite3.connect(store.db_path) as c:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = BettingStore(str(tmp_path / "api.db"))
    cfg = _cfg(tmp_path)
    betting_api.configure_betting(store, cfg)
    app = FastAPI()
    app.include_router(betting_api.router)
    monkeypatch.setattr(betting_api, "_store", store)
    monkeypatch.setattr(betting_api, "_cfg", cfg)
    return TestClient(app)


# ── value validation ───────────────────────────────────────────────────────

def test_all_seven_frozen_codes_are_selectable(tmp_path):
    store = _store(tmp_path)
    codes = sel.valid_codes()
    assert codes == spec.QUALIFYING_ORDER and len(codes) == 7
    for code in codes:
        selection = sel.set_selection(store, code)
        assert selection.cohort == code and selection.source == "persisted"
        assert sel.get_selection(store).cohort == code


def test_unknown_code_is_rejected_and_nothing_is_written(tmp_path):
    store = _store(tmp_path)
    sel.set_selection(store, "TC-C")
    before = _config_snapshot(store)
    for bad in ("TC-Z", "TC-", "", "TC-X ", " TC-X", "TC-C++", "ELITE", "CORE+"):
        with pytest.raises(sel.CohortSelectionError):
            sel.set_selection(store, bad)
    assert _config_snapshot(store) == before
    assert sel.get_selection(store).cohort == "TC-C"


def test_display_labels_and_case_variants_are_not_codes(tmp_path):
    store = _store(tmp_path)
    for bad in ("tc-x", "Tc-X", "elite", "TC-C+ ", "TCX"):
        with pytest.raises(sel.CohortSelectionError):
            sel.normalize_cohort(bad)
    assert sel.normalize_cohort("TC-C+") == "TC-C+"


def test_non_string_and_oversized_values_are_rejected(tmp_path):
    store = _store(tmp_path)
    for bad in (None, True, 0, 1.5, [], {}, "TC-X" * 10, b"TC-X"):
        with pytest.raises(sel.CohortSelectionError):
            sel.set_selection(store, bad)


# ── persistence ────────────────────────────────────────────────────────────

def test_selection_persists_across_store_instances(tmp_path):
    store = _store(tmp_path)
    sel.set_selection(store, "TC-S")
    reopened = BettingStore(str(tmp_path / "sel.db"))
    selection = sel.get_selection(reopened)
    assert selection.cohort == "TC-S" and selection.source == "persisted"
    assert selection.stored is True


def test_corrupt_persisted_value_falls_back_to_the_default(tmp_path):
    store = _store(tmp_path)
    store.set_config(sel.KEY_SELECTED_COHORT, "TC-BOGUS")
    selection = sel.get_selection(store)
    assert selection.cohort == sel.DEFAULT_COHORT
    assert selection.source == "default" and selection.stored is False


# ── the invariant: selection is configuration, never execution ─────────────

def test_selection_never_changes_execution_configuration(tmp_path):
    """Switching cohorts must leave every other config value byte-identical."""
    store = _store(tmp_path, enabled=True)
    cfg = _cfg(tmp_path)
    before = _config_snapshot(store)
    status_before = sel.execution_status(store, cfg, browser_submit=False).as_dict()
    executions_before = _count(store, "bet_executions")
    for code in sel.valid_codes():
        sel.set_selection(store, code)
    after = _config_snapshot(store)
    # only the selection key differs, and only by value
    assert set(after) - set(before) == {sel.KEY_SELECTED_COHORT}
    for key in before:
        if key == sel.KEY_SELECTED_COHORT:
            continue
        assert after[key] == before[key], key
    assert sel.execution_status(store, cfg, browser_submit=False).as_dict() == status_before
    assert _count(store, "bet_executions") == executions_before


def test_selection_writes_one_audit_row_and_no_execution(tmp_path):
    store = _store(tmp_path)
    audit_before = _count(store, "bet_audit")
    sel.set_selection(store, "TC-E", actor="operator")
    assert _count(store, "bet_audit") == audit_before + 1
    assert _count(store, "bet_executions") == 0
    row = store.audit_recent(limit=1)[0]
    assert row["event"] == "autobet_cohort_selection"
    assert "TC-E" in (row["details"] or "")


def test_concurrent_selections_converge_on_one_valid_code(tmp_path):
    store = _store(tmp_path)
    codes = list(sel.valid_codes())
    errors: list[BaseException] = []

    def worker(code: str) -> None:
        try:
            sel.set_selection(BettingStore(str(tmp_path / "sel.db")), code)
        except BaseException as exc:          # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,)) for c in codes * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = sel.get_selection(store)
    assert final.cohort in codes and final.source == "persisted"


# ── fail-closed execution status ───────────────────────────────────────────

def test_missing_risk_limits_keep_execution_locked(tmp_path):
    store = _store(tmp_path, enabled=True)
    cfg = _cfg(tmp_path, max_stake_per_bet=None, max_bets_per_day=None,
               max_daily_exposure=None)
    status = sel.execution_status(store, cfg, browser_submit=False)
    assert status.locked is True
    assert status.missing_limits == ["max_stake_per_bet", "max_bets_per_day",
                                     "max_daily_exposure"]
    assert "max_stake_per_bet_missing" in status.reasons
    # and selecting a cohort does not remove the lock
    sel.set_selection(store, "TC-X")
    assert sel.execution_status(store, cfg, browser_submit=False).locked is True


def test_fully_configured_environment_unlocks_status(tmp_path):
    store = _store(tmp_path, enabled=True, unit=2.0)
    cfg = _cfg(tmp_path)
    status = sel.execution_status(store, cfg, browser_submit=True)
    assert status.locked is False and status.reasons == []
    # the two independent authorities still gate: dry-run and the submit flag
    assert "dry_run" in sel.execution_status(
        store, _cfg(tmp_path, dry_run=True), browser_submit=True).reasons
    assert "browser_submit_not_authorized" in sel.execution_status(
        store, cfg, browser_submit=False).reasons


# ── API contract ───────────────────────────────────────────────────────────

def test_api_accepts_the_seven_codes_and_rejects_the_rest(client):
    r = client.get("/api/v4/betting/cohort-selection")
    assert r.status_code == 200
    body = r.json()
    assert body["selected"]["cohort"] == sel.DEFAULT_COHORT
    assert [o["code"] for o in body["options"]] == list(sel.valid_codes())
    assert body["environment"] == "test" and body["read_only_execution"] is True
    for code in sel.valid_codes():
        r = client.post("/api/v4/betting/cohort-selection", json={"cohort": code})
        assert r.status_code == 200, r.text
        assert r.json()["selected"]["cohort"] == code
    for bad in ({"cohort": "TC-Z"}, {"cohort": "tc-x"}, {"cohort": None},
                {"cohort": 3}, {"cohort": ""}):
        r = client.post("/api/v4/betting/cohort-selection", json=bad)
        assert r.status_code in (400, 422), (bad, r.status_code)
    # the last ACCEPTED write stands: the rejected ones wrote nothing
    assert (client.get("/api/v4/betting/cohort-selection")
            .json()["selected"]["cohort"]) == sel.valid_codes()[-1]


def test_api_write_accepts_only_the_cohort_field(client):
    """A cohort switch must not be able to carry a risk change."""
    before = client.get("/api/v4/betting/cohort-selection").json()["execution"]
    r = client.post("/api/v4/betting/cohort-selection",
                    json={"cohort": "TC-X", "auto_betting": "ON"})
    assert r.status_code == 400
    assert client.get("/api/v4/betting/cohort-selection").json()["execution"] == before
    r = client.post("/api/v4/betting/cohort-selection", json={})
    assert r.status_code == 400


def test_betting_status_exposes_the_selected_cohort_read_only(client):
    status = client.get("/api/v4/betting/status").json()
    assert status["selected_cohort"]["cohort"] == sel.DEFAULT_COHORT
    assert status["selected_cohort"]["rule_version"] == spec.RULE_VERSION
    # reading the status twice changes nothing (a page load never writes)
    store = betting_api._store
    before = _config_snapshot(store)
    client.get("/api/v4/betting/status")
    client.get("/api/v4/betting/cohort-selection")
    assert _config_snapshot(store) == before
