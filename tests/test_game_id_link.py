"""Global PokerBet GameId link component and UI coverage contracts."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "blm_v4" / "dashboard" / "static"
COMPONENT = STATIC / "game-id-link.js"
node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


@node
def test_game_id_link_url_and_anchor_contract():
    code = f"""
const link = require({json.dumps(str(COMPONENT))});
console.log(JSON.stringify({{
  url: link.url("31576449"),
  other: link.url("31576450"),
  instance: link.url("31576449#i2"),
  anchor: link.html("31576449"),
  unsafeLabel: link.html("31576449<script>"),
  missing: link.html(null),
  undefined: link.html(undefined),
  invalid: link.html("test-game-9001"),
  zero: link.html("0"),
  empty: link.html(""),
}}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", code], text=True))
    prefix = ("https://www.pokerbet.co.za/en/sports/live/event-view/"
              "Basketball/Virtual%20Matches/18296900-betual-tbsl/")
    assert out["url"] == prefix + "31576449"
    assert out["other"] == prefix + "31576450"
    assert out["instance"] == prefix + "31576449"
    assert out["anchor"] == (
        f'<a class="game-id-link" href="{prefix}31576449" '
        'target="_blank" rel="noopener noreferrer">31576449</a>')
    assert out["unsafeLabel"] == "31576449&lt;script&gt;"
    assert out["missing"] == out["undefined"] == out["empty"] == ""
    assert out["invalid"] == "test-game-9001"
    assert out["zero"] == "0"


def test_every_user_facing_game_id_renderer_uses_shared_component():
    dashboard = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    explorer = (STATIC / "explorer.js").read_text(encoding="utf-8")
    stats = (STATIC / "stats.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert html.index("game-id-link.js") < html.index("dashboard.js")
    for token in (
        "GameIdLink.html(rec.game_id)",  # resulted UNDER and Q3 rows
        "GameIdLink.html(r.game_id)",    # active alert identity
        "GameIdLink.html(g.game_id)",    # live card and modal detail
        "GameIdLink.html(r.game_id)",    # execution and execution history
    ):
        assert token in dashboard
    assert 'chain("GAME"' in dashboard and "trustedHtml" in dashboard
    assert "rawDataHTML(g.raw)" in dashboard
    assert 'c === "game_id"' in explorer
    assert "GameIdLink.html(r.value)" in explorer
    assert "GameIdLink.html(c)" in stats


@node
def test_modal_raw_snapshot_links_source_id_but_keeps_db_id_as_text(tmp_path):
    dashboard = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    start = dashboard.index("function rawDataHTML(raw) {")
    end = dashboard.index("\n}\n\nfunction closeModal", start) + 2
    module = tmp_path / "raw_data_html.js"
    esc_source = '''const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
        '"': "&quot;" }[c]));\n'''
    module.write_text(
        esc_source
        +
        f'const GameIdLink = require({json.dumps(str(COMPONENT))});\n'
        + dashboard[start:end] + '\nmodule.exports = rawDataHTML;\n',
        encoding="utf-8")
    code = f"""
const render = require({json.dumps(str(module))});
console.log(render({{ game_id: 421, source_game_id: "31576449#i2" }}));
"""
    rendered = subprocess.check_output(["node", "-e", code], text=True)
    assert '&quot;game_id&quot;: 421' in rendered
    assert '&quot;source_game_id&quot;: <a ' in rendered
    assert "31576449#i2</a>" in rendered
    assert 'target="_blank" rel="noopener noreferrer"' in rendered
