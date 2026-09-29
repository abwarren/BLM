"""Static frontend contracts for the operational command-center additions
(2026-09-26 frontend directive): global search, combined game-state filter,
database/data explorer, and ALERT → GAME → SIGNAL → EXECUTION → RESULT
traceability.

These are presentation-layer contracts only: every feature is bound to
fields the backend already serves (docs/BETTING_API_CONTRACT.md,
blm_v4/api.py routes).  No execution state, id or reason may be invented
in the browser — the tested tokens here are exactly the API-served fields
or the contract's own state vocabulary.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "blm_v4" / "dashboard" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# ── 1. GLOBAL SEARCH ─────────────────────────────────────────────────

def test_global_search_element_is_wired_to_game_and_execution_fields():
    html = _read("index.html")
    js = _read("dashboard.js")
    assert 'id="globalSearch"' in html
    assert '"globalSearch"' in js
    # search operates on the game's identity fields AND the bound
    # execution's ids (the operator must find a game by its exec/alert id)
    for token in ("g.game_id", "g.home_team", "g.away_team",
                  "g.competition_slug", "exec.execution_id", "exec.alert_id",
                  "exec.provider_ref"):
        assert token in js


def test_global_search_is_a_display_filter_only():
    js = _read("dashboard.js")
    # the search feeds renderCards (display), never the alert stores
    assert "gameMatchesSearch(g, state.search)" in js
    assert "renderCards(state.lastPayload)" in js


# ── 2. COMBINED FILTERS ──────────────────────────────────────────────

def test_game_state_filter_extends_the_existing_filter_set():
    html = _read("index.html")
    js = _read("dashboard.js")
    assert 'id="gameStateFilter"' in html
    for element_id in ("checkpointFilter", "directionFilter",
                       "alertStateFilter", "betStateFilter",
                       "gameStateFilter"):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in js
    # all five filters apply together inside ONE predicate (AND semantics)
    assert "gameMatchesGameState(g, state.gameStateFilter)" in js
    # the state filter uses the same live gate as the cards
    assert "isActuallyLive(g)" in js


# ── 3. DATABASE / DATA EXPLORER ──────────────────────────────────────

def test_explorer_view_and_controls_exist():
    html = _read("index.html")
    js = _read("explorer.js")
    assert 'id="viewExplorer"' in html
    assert 'id="exDataset"' in html
    assert 'id="exQuery"' in html
    assert 'id="exTable"' in html
    assert 'id="exReload"' in html
    assert 'id="exLimit"' in html
    assert "explorer.js" in html


def test_explorer_uses_only_existing_api_endpoints():
    js = _read("explorer.js")
    # every dataset maps to a route that exists in blm_v4/api.py or
    # blm_v4/betting/api.py — nothing is invented
    for endpoint in ("/api/v4/games",
                     "/api/v4/scorecard/events",
                     "/api/v4/betting/history",
                     "/api/v4/collection/quarters",
                     "/api/v4/results/integrity"):
        assert endpoint in js


def test_explorer_datasets_cover_the_operational_objects():
    js = _read("explorer.js")
    for dataset in ("games", "checkpoints", "executions",
                    "quarters", "integrity"):
        assert dataset in js


def test_explorer_is_read_only():
    js = _read("explorer.js")
    assert "fetch(" in js
    # no mutating verb anywhere in the explorer
    for verb in ("method: \"POST\"", "method: \"PUT\"", "method: \"DELETE\""):
        assert verb not in js


# ── 4. EXECUTION / AUDIT TRACEABILITY ────────────────────────────────

def test_trace_chain_covers_alert_game_signal_execution_result():
    js = _read("dashboard.js")
    assert 'id="traceList"' in _read("index.html")
    # the chain steps, in order
    alert_pos = js.index('"ALERT"')
    game_pos = js.index('"GAME"')
    signal_pos = js.index('"SIGNAL"')
    exec_pos = js.index('"EXECUTION"')
    result_pos = js.index('"RESULT"')
    assert alert_pos < game_pos < signal_pos < exec_pos < result_pos


def test_trace_uses_only_served_ids_and_states():
    js = _read("dashboard.js")
    # ids and reasons come from the betting status payload / per-game state
    for token in ("alert_id", "execution_id", "provider_ref",
                  "error_message", "rejection_reason",
                  "blocked_reason", "reconciliation_state"):
        assert token in js
    # the per-game state endpoint is the server authority for the row
    assert "/game/" in js and "/state" in js
    # states are normalized against the contract's vocabulary — unknown
    # raw values are shown as served, never mapped to a fake state
    for st in ("PENDING", "WOULD_BET", "SUBMITTING", "SUBMITTED",
               "ACCEPTED", "REJECTED", "BLOCKED", "FAILED", "UNKNOWN"):
        assert st in js


def test_trace_row_state_hydrates_from_server_state_endpoint():
    js = _read("dashboard.js")
    # the per-game state fetch hydrates the row's server state
    assert "hydrateBettingState" in js
    assert "bstate" in js
    assert "API_BETTING_STATE" in js


# ── 5. EXISTING CONTRACTS UNCHANGED (regression pins) ────────────────

def test_existing_command_center_contract_still_holds():
    js = _read("dashboard.js")
    html = _read("index.html")
    for text in ("market-compact", "trade-state", "exec-trace",
                 "exec.alert_id", "exec.execution_id", "exec.provider_ref",
                 "exec.error_message", "state.betting.enabled"):
        assert text in js
    for element_id in ("checkpointFilter", "directionFilter",
                       "alertStateFilter", "betStateFilter"):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in js
    assert "g.under_alert" in js
    assert "state.betting.recent" in js
    assert 'class="manual-bet"' in js
    assert 'id="abHistoryQuery"' in html
    assert 'id="abHistoryList"' in html
    assert 'id="abAuditList"' in html
