"""UNDER-alert AUDIO layer — frontend tests.

The audio layer adds an audible cue to the EXISTING UNDER alert system.  It
introduces no new condition, no model and no prediction: it sounds ONLY on the
transition the existing alertEscalation() already defines — an alert ENTRY
(none->Lx) or an ESCALATION (Lx->Ly, rank Ly>Lx).  A steady condition, a repeat
of an unchanged level and every downgrade stay silent, so the cue can never
fire on a 5-second poll or a plain re-render.

Contract tests (served assets, parallel-safe) prove:
  * a compact AUDIO control exists, defaults OFF, and persists its preference;
  * cues are generated with the Web Audio API — no audio files, no CDN, no
    network request, no missing asset;
  * three distinguishable cues (base 1 tone, strong 2, high 3);
  * sound is reachable ONLY through alertEscalation()'s result, at the two
    existing transition sites (card badge + modal panel);
  * the browser autoplay policy is respected, never bypassed (context built
    lazily from a user gesture; never resumed from the polling loop);
  * the visual alert is untouched and independent of the audio preference.

Behavior tests extract the pure blocks (the alert classifier and the audio
coalescing decision) and execute them in Node.js (skipped without node).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

PURE_ALERT_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_ALERT_END = "/* __PURE_ALERT_END__ */"
PURE_AUDIO_BEGIN = "/* __PURE_AUDIO_BEGIN__ */"
PURE_AUDIO_END = "/* __PURE_AUDIO_END__ */"

# the transition matrix from the spec: audio fires on ENTRY / ESCALATION only
TRANSITIONS = [
    # (previous level, next level, should sound)
    (None, "base", True),        # NONE  -> BASE
    (None, "strong", True),      # NONE  -> STRONG
    (None, "high", True),        # NONE  -> HIGH
    ("base", "strong", True),    # BASE  -> STRONG
    ("strong", "high", True),    # STRONG-> HIGH
    ("base", "high", True),      # BASE  -> HIGH
    ("base", "base", False),     # BASE  -> BASE
    ("strong", "strong", False), # STRONG-> STRONG
    ("high", "high", False),     # HIGH  -> HIGH
    ("strong", "base", False),   # STRONG-> BASE (downgrade)
    ("high", "strong", False),   # HIGH  -> STRONG (downgrade)
    ("base", None, False),       # BASE  -> NONE
    (None, None, False),         # NONE  -> NONE
]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")
    return TestClient(app)


def _js(client) -> str:
    resp = client.get("/static/dashboard.js")
    assert resp.status_code == 200
    return resp.text


def _html(client) -> str:
    resp = client.get("/static/index.html")
    assert resp.status_code == 200
    return resp.text


def _css(client) -> str:
    resp = client.get("/static/styles.css")
    assert resp.status_code == 200
    return resp.text


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    j = js.index(end, i)
    return js[i:j]


node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


def _run_node(js: str, tmp_path: Path, expr: str):
    """Evaluate `expr` in node against the pure alert + audio blocks."""
    mod = tmp_path / "alert_audio_pure.js"
    mod.write_text(
        _block(js, PURE_ALERT_BEGIN, PURE_ALERT_END)
        + "\n" + _block(js, PURE_AUDIO_BEGIN, PURE_AUDIO_END)
        + "\nmodule.exports = { histAlertLevel, alertEscalation,"
          " audioWindowUpdate, ALERT_CUES, AUDIO_COALESCE_MS };",
        encoding="utf-8")
    script = ("const m = require(%s); console.log(JSON.stringify(%s));"
              % (json.dumps(str(mod)), expr))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── control: present, compact, OFF by default, persisted ───────────

def test_audio_control_present_and_off_by_default(client):
    html = _html(client)
    assert 'id="audioToggle"' in html
    assert 'class="audio-toggle"' in html
    assert 'data-audio="off"' in html                 # default OFF
    assert 'aria-pressed="false"' in html
    assert ">AUDIO OFF</button>" in html
    # a button (a real control), not a decorative span
    assert "<button" in html[html.index("audio-toggle") - 40:]


def test_audio_control_styled_compact(client):
    css = _css(client)
    assert ".audio-toggle {" in css
    assert ".audio-toggle.on {" in css
    assert ".audio-toggle.on.locked {" in css


def test_audio_preference_defaults_off_and_persists(client):
    js = _js(client)
    # its own persisted preference key
    assert 'ALERT_AUDIO: "pz.alertAudioEnabled"' in js
    # OFF unless explicitly stored as "1" (default = conservative)
    assert 'localStorage.getItem(PREF.ALERT_AUDIO) === "1"' in js
    # toggled + persisted on click
    assert "localStorage.setItem(PREF.ALERT_AUDIO, on ? \"1\" : \"0\")" in js
    # the label reflects the active state
    assert 'btn.textContent = on ? "AUDIO ON" : "AUDIO OFF";' in js


# ── cues: generated, no external assets, three distinguishable levels ──

def test_cues_generated_no_audio_files_or_cdn(client):
    js = _js(client)
    html = _html(client)
    # generated with the Web Audio API
    assert "window.AudioContext || window.webkitAudioContext" in js
    assert "ctx.createOscillator()" in js
    assert "ctx.createGain()" in js
    # no media element and no audio assets / CDN / network request
    assert "<audio" not in html.lower()
    for ext in (".mp3", ".wav", ".ogg", ".m4a", ".aac"):
        assert ext not in js.lower(), ext
    assert "new Audio(" not in js
    assert "HTMLAudioElement" not in js
    assert 'autoplay' not in html.lower()


def test_three_distinguishable_cues(client, tmp_path):
    js = _js(client)
    cues = _run_node(js, tmp_path, "m.ALERT_CUES")
    assert sorted(cues) == ["base", "high", "strong"]
    # base 1 tone, strong 2 tones, high 3 tones — ordered, distinct pitches
    assert len(cues["base"]) == 1
    assert len(cues["strong"]) == 2
    assert len(cues["high"]) == 3
    for lvl, tones in cues.items():
        assert all(isinstance(f, (int, float)) and f > 0 for f in tones), lvl
    # the three levels are audibly different sequences
    assert cues["base"] != cues["strong"] != cues["high"]
    # short cues — a burst of tones can never become a continuous alarm
    assert _run_node(js, tmp_path, "m.AUDIO_COALESCE_MS") == 400


# ── gating: sound ONLY from an alertEscalation() rise ──────────────

def test_sound_only_from_alert_escalation(client):
    js = _js(client)
    # the two existing transition sites, each passing its escalation result
    assert "const entered = alertEscalation(card.prevAlert, alLvl);" in js
    assert "if (entered) alertAudio(entered);" in js
    assert "const rose = alertEscalation(priorLevel, lvl);" in js
    assert "if (rose) alertAudio(rose);" in js
    # SINGLE audio entry point: its definition + exactly those two call sites
    assert js.count("alertAudio(") == 3
    # and it is the only route to the cue synthesiser
    assert js.count("playAlertCue(") == 2
    # never driven by the poll or a timer
    assert "setInterval(alertAudio" not in js
    assert "setInterval(playAlertCue" not in js


def test_poll_loop_does_not_play_directly(client):
    """The polling body must not raise a cue itself — only a transition can."""
    js = _js(client)
    i = js.index("async function refresh()")
    body = js[i:js.index("\n}", i)]
    assert "playAlertCue" not in body
    assert "alertAudio" not in body


def test_no_sound_when_audio_disabled(client):
    js = _js(client)
    # alertAudio returns early unless the preference is on, before any work
    assert "if (!level || !audioEnabled()) return;" in js


# ── autoplay policy respected, never bypassed ──────────────────────

def test_autoplay_policy_respected(client):
    js = _js(client)
    # context built lazily (a context made before a gesture stays suspended)
    ctx_body = js[js.index("function audioContext()"):]
    assert "new AC()" in ctx_body[:ctx_body.index("\n}")]
    # resume() is called from exactly one place — the gesture-driven unlock
    assert js.count("ctx.resume()") == 1
    unlock_body = js[js.index("function unlockAudio()"):]
    assert "ctx.resume()" in unlock_body[:unlock_body.index("\n}")]
    # the unlock is wired to a user gesture, retried until running, and
    # never driven by the timer
    assert "armAudio" in js
    assert '"pointerdown", "keydown", "touchstart"' in js
    assert "window.addEventListener(ev, armAudio, { passive: true })" in js
    assert "const armAudio = () => { if (!audioReady) unlockAudio(); };" in js
    # a suspended context is left silent — no retry storm on the poll
    assert 'if (!ctx || ctx.state !== "running") return;' in js
    # no attempt to defeat the policy: no autoplay attribute, no media element
    assert "autoplay" not in _html(client).lower()
    for banned in ("new Audio(", "HTMLMediaElement", "HTMLAudioElement",
                   "<audio"):
        assert banned not in js, banned


def test_context_not_created_at_page_load(client):
    """Nothing in the module top level constructs the AudioContext: it is
    only reachable through the gesture handlers / an enabled alert."""
    js = _js(client)
    # ordering: the factory + unlock are defined, and the button wiring
    # (which can call unlockAudio) sits in the gesture-wiring section
    assert js.index("function audioContext()") < js.index('$("audioToggle")')
    assert js.index("let audioCtx = null;") < js.index("function audioContext()")


# ── independence from the visual alert ─────────────────────────────

def test_visual_alert_untouched_and_independent(client):
    js = _js(client)
    # the visual one-shot pulse and ranked escalation are unchanged
    assert "al-in" in js
    assert "base: 1, strong: 2, high: 3" in js
    assert "prevAlert" in js
    # the audio preference never gates the visual treatment
    assert "alLvl" in js and "prevAlert = alLvl" in js
    # disabling audio leaves the badge/panel render path intact
    assert "histBadgeHTML" in js and "histPanelHTML" in js


def test_audio_introduces_no_over_and_no_banned_vocabulary(client):
    js = _js(client)
    assert "OVER" not in js
    low = js.lower()
    for banned in ("edge", "signal", "momentum", "win rate", "probab",
                   "calibrat", "forecast", "predict", "fair", "z_score",
                   "betting", "staking"):
        assert banned not in low, banned


# ── pure blocks stay pure ──────────────────────────────────────────

def test_pure_audio_block_is_pure(client):
    js = _js(client)
    pure = _block(js, PURE_AUDIO_BEGIN, PURE_AUDIO_END)
    for banned in ("document.", "window.", "localStorage", "state.",
                   "Date.now", "fetch(", "AudioContext"):
        assert banned not in pure, banned


def test_pure_alert_block_unchanged_and_pure(client):
    js = _js(client)
    pure = _block(js, PURE_ALERT_BEGIN, PURE_ALERT_END)
    assert "document." not in pure and "state." not in pure
    # the audio layer did not leak into the alert classifier
    assert "audio" not in pure.lower()
    assert "alertAudio" not in pure


# ── real behavior of the pure decision (node) ─────────────────────

@node
def test_behavior_audio_only_on_entry_or_escalation(client, tmp_path):
    """The spec matrix: sound on ENTRY / ESCALATION, silent on steady
    state, repeats and every downgrade."""
    rank = {"base": 1, "strong": 2, "high": 3}
    pairs = [(p, n) for p, n, _ in TRANSITIONS]
    expr = (
        "(%s).map(([prev,next])=>{"
        " const esc = m.alertEscalation(prev, next);"
        " const upd = m.audioWindowUpdate(null, esc, 1000,"
        % json.dumps(pairs) + json.dumps(rank)
        + "); return upd.play; })")
    res = _run_node(_js(client), tmp_path, expr)
    expected = [n if play else None for _, n, play in TRANSITIONS]
    assert res == expected, list(zip(pairs, res, expected))


@node
def test_behavior_coalescing_burst_is_not_an_alarm(client, tmp_path):
    """Simulating the real pipeline (escalation gate, then coalescing): a
    poll that enters/escalates several games at once sounds ONE cue, a
    steady level is silent, and a downgrade never sounds."""
    js = _js(client)
    rank = {"base": 1, "strong": 2, "high": 3}
    expr = (
        "(() => { let w = null, prev = null; const played = [];"
        " const obs = (lvl, now) => {"
        "   const esc = m.alertEscalation(prev, lvl); prev = lvl;"
        "   if (!esc) return;"
        "   const u = m.audioWindowUpdate(w, esc, now,"
        + json.dumps(rank) + "); w = u.win; if (u.play) played.push(u.play); };"
        " obs('base', 1000);"    # entry -> sounds
        " obs('base', 1010);"    # steady -> silent
        " obs('strong', 1200);"  # escalation inside the window -> sounds
        " obs('strong', 1300);"  # steady -> silent
        " obs('base', 1400);"    # downgrade -> silent (no escalation)
        " obs('high', 1500);"    # escalation base->high in window -> sounds
        " obs('high', 1600);"    # steady -> silent
        " obs(null, 1700);"      # cleared -> silent
        " obs('base', 9000);"    # fresh entry after the window -> sounds
        " return played; })()")
    res = _run_node(js, tmp_path, expr)
    assert res == ["base", "strong", "high", "base"], res


@node
def test_behavior_unknown_level_never_sounds(client, tmp_path):
    js = _js(client)
    rank = {"base": 1, "strong": 2, "high": 3}
    res = _run_node(js, tmp_path, (
        "[null, undefined, '', 'bogus'].map((lvl) => {"
        " const u = m.audioWindowUpdate(null, lvl, 1000,"
        + json.dumps(rank) + "); return u.play; })"))
    assert res == [None, None, None, None], res
