"""The LIVE payload must NEVER run the first-line scan on the request path.

Measured 2026-11-02 on production: ``first_lines()`` is a ROW_NUMBER() window
over all 5,227,704 ``clean_observations`` rows — 93.5 s — and ``v4_live`` ran it
on every cache-miss build.  That payload is the betting worker's ONLY input, so
a cold cache (any restart) left the fast lane at ``rows: 0`` for as long as the
cache could not warm (25 min observed, auto-bet blind).

``LineReferenceWorker`` exists to precompute it, and ``blm_v4.api`` already
consumes its published pair — but the composition root never wired it, so
production silently took the inline path forever.  These tests pin the contract
that makes the wiring worth anything: with a worker configured the scan is never
reached, and while it is warming the reference fails CLOSED (empty) instead of
falling back to the scan that stalls the money path.
"""
from __future__ import annotations

import blm_v4.api as api
from blm_v4.live_analytics import line_reference


def _no_scan(*_a, **_k):
    raise AssertionError(
        "inline first-line scan ran on the request path — the "
        "LineReferenceWorker is not wired or was bypassed")


def test_published_payload_is_served_without_the_inline_scan(monkeypatch):
    class _Worker:
        def snapshot(self):
            return {"runs": 1}

    monkeypatch.setattr(line_reference, "build_reference", _no_scan)
    api.configure_line_reference_worker(_Worker())
    try:
        api._publish_line_reference(
            {"betual-tbsl": {"avg_line": 178.5, "games": 12}},
            {"31430345": {"line": 176.5, "at": "2026-11-02T03:00:00Z"}})
        ref = api._line_reference()
    finally:
        api.configure_line_reference_worker(None)
        api._LINE_REF_LATEST.update({"avg": None, "open": None})

    assert ref["avg"]["betual-tbsl"]["avg_line"] == 178.5
    assert ref["open"]["31430345"]["line"] == 176.5


def test_wired_but_warming_fails_closed_without_scanning(monkeypatch):
    class _Worker:
        def snapshot(self):
            return {"runs": 0}

    monkeypatch.setattr(line_reference, "build_reference", _no_scan)
    api.configure_line_reference_worker(_Worker())
    api._LINE_REF_LATEST.update({"avg": None, "open": None})
    try:
        ref = api._line_reference()
    finally:
        api.configure_line_reference_worker(None)

    # no reference ⇒ the trade leg must fail CLOSED, never borrow a number
    assert ref == {"avg": {}, "open": {}}


def test_status_reports_the_worker(monkeypatch):
    class _Worker:
        def snapshot(self):
            return {"runs": 3, "skips": 1}

    api.configure_line_reference_worker(_Worker())
    try:
        status = api.line_reference_status()
    finally:
        api.configure_line_reference_worker(None)
    assert status["worker_configured"] is True
    assert status["worker"]["runs"] == 3
