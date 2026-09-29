"""Static frontend contracts for the live command center additions."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "blm_v4" / "dashboard" / "static"


def test_game_cards_expose_market_alert_and_execution_trace():
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    for text in ("market-compact", "trade-state", "exec-trace",
                 "exec.alert_id", "exec.execution_id", "exec.provider_ref",
                 "exec.error_message", "state.betting.enabled"):
        assert text in js


def test_live_game_filters_are_bound_to_existing_payload_fields():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    for element_id in ("checkpointFilter", "directionFilter",
                       "alertStateFilter", "betStateFilter"):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in js
    assert "g.under_alert" in js
    assert "state.betting.recent" in js


def test_recent_execution_rows_include_alert_identity_and_reason():
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    assert "r.alert_id" in js
    assert "r.execution_id" in js
    assert "r.error_message" in js


def test_manual_total_form_and_searchable_execution_audit_view_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    assert 'class="manual-bet"' in js
    assert '"/manual"' in js
    assert 'id="abHistoryQuery"' in html
    assert 'id="abHistoryList"' in html
    assert 'id="abAuditList"' in html
    assert '"/history' in js or '`${BETTING_API}/history' in js
