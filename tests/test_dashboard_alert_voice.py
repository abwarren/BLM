"""Alert VOICE layer — spoken description after the existing alarm cue.

No TTS model and no TTS service: speech uses the browser's Web Speech API
(window.speechSynthesis), unlocked by the same AUDIO toggle gesture that
already unlocks WebAudio.  The spoken sentence is built ONLY from served
facts (alert level, teams, frozen trigger line, descriptive pace-z); it
introduces no new gate, no classification and no prediction.

Tests prove:
  * the alarm still sounds FIRST and speech is a strictly optional follow-on;
  * speech is unavailable-safe and degrades to tones-only;
  * one voiceover per alert identity (card + modal cannot double-speak);
  * every branch of the pure sentence builder, executed in Node.
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


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    j = js.index(end, i)
    return js[i:j]


node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


def _run_node(js: str, tmp_path: Path, expr: str):
    mod = tmp_path / "voice_pure.js"
    mod.write_text(
        _block(js, PURE_ALERT_BEGIN, PURE_ALERT_END)
        + "\n" + _block(js, PURE_AUDIO_BEGIN, PURE_AUDIO_END)
        + "\nmodule.exports = { alertSpokenText, alertEscalation,"
          " audioWindowUpdate, ALERT_LEVEL_WORDS };",
        encoding="utf-8")
    script = ("const m = require(%s); console.log(JSON.stringify(%s));"
              % (json.dumps(str(mod)), expr))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── alarm first, speech optional ───────────────────────────────────

def test_alarm_sounds_before_voice(client):
    js = _js(client)
    audio_at = js.index("playAlertCue(upd.play);")
    speech_at = js.index("speechSynthesis.speak(utterance)")
    assert audio_at < speech_at


def test_speech_is_unavailable_safe(client):
    js = _js(client)
    assert 'typeof speechSynthesis === "undefined"' in js
    assert "new SpeechSynthesisUtterance" in js
    assert "speechSynthesis.cancel()" in js        # no overlapping speech
    assert "catch (_)" in js                        # failure never blocks alarm
    # browsers still reject speak() without a gesture; the AUDIO toggle is
    # that gesture and already unlocks the AudioContext
    assert 'if (on) unlockAudio();' in js


def test_one_voiceover_per_alert_identity(client):
    js = _js(client)
    assert "let spokenAlertId = null;" in js
    assert "if (id && id === spokenAlertId) return;" in js
    # switching AUDIO reinstates the current set
    assert "spokenAlertId = null;                   // re-announce" in js
    # speech is reached only through the SINGLE alertAudio entry point
    assert js.count("speechSynthesis.speak(") == 1


def test_voice_uses_only_served_facts(client):
    js = _js(client)
    # the two call sites pass the frozen trigger snapshot, never a derived
    # verdict; z is consumed from the authoritative /pace-z payload.
    assert "triggerLine: (g.under_alert || {}).trigger_line," in js
    assert "z: state.modalZMeta && state.modalZMeta.z != null" in js
    assert "z: zm.z != null ? zm.z : null," in js


def test_no_tts_asset_or_service(client):
    js = _js(client)
    for ext in (".mp3", ".wav", ".ogg", ".m4a"):
        assert ext not in js.lower(), ext
    assert "fetch(" not in js[js.index("function alertAudio("):
                            js.index("function syncAudioButton()")]


# ── pure sentence builder branches (node) ──────────────────────────

@node
def test_spoken_text_branches(client, tmp_path):
    js = _js(client)
    cases = [
        ("pace-state", {"z": 2.4},
         "Alert. pace state alert triggered. Z greater than two."),
        ("base", {"z": 1.42},
         "Alert. pace gap alert triggered. Z 1.42."),
        ("high", {"z": -1.37},
         "Alert. late game pace and Z alert triggered. Z negative 1.37."),
        ("strong", {"z": None},
         "Alert. late game pace gap alert triggered. Z unavailable."),
        ("base", {}, "Alert. pace gap alert triggered. Z unavailable."),
        ("pace-state",
         {"teams": "Aces vs Kings", "checkpoint": 75,
          "triggerLine": 218.5, "z": -0.8},
         "Alert. pace state alert triggered for Aces vs Kings. "
         "Checkpoint 75. Trigger line 218.5. Z negative 0.80."),
    ]
    for level, facts, expected in cases:
        expr = ("m.alertSpokenText(%s, %s)"
                % (json.dumps(level), json.dumps(facts)))
        assert _run_node(js, tmp_path, expr) == expected


@node
def test_escalation_gate_unchanged(client, tmp_path):
    js = _js(client)
    assert _run_node(js, tmp_path,
                     "m.alertEscalation('base', 'base')") is None
    assert _run_node(js, tmp_path,
                     "m.alertEscalation('base', 'high')") == "high"
