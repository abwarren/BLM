#!/usr/bin/env python3
"""BLM health monitor — the loop that turns probes into health.json + alerts.

Deliberately boring and cheap: one JSON read, one HTTP HEAD-ish GET per service,
one `select 1`, /proc for the host. No browser, no writes to any BLM database,
no imports of the FastAPI app. A cycle is expected to cost single-digit
milliseconds, so it cannot interfere with collector polling.

    python3 scripts/blm_health_monitor.py --once            # one cycle, print it
    python3 scripts/blm_health_monitor.py --interval 15     # run forever
    python3 scripts/blm_health_monitor.py --fixtures DIR    # test environment

Panel: serve the directory holding health.json (see scripts/blm_health_panel.sh)
or open blm_v4/monitoring/static/health.html next to it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from blm_v4.monitoring.alerts import AlertTracker          # noqa: E402
from blm_v4.monitoring.health import (                     # noqa: E402
    Thresholds, collect, dumps)

DEFAULT_STATE = "/home/ubuntu/BLM/blm_v4/state/collector_state.json"
DEFAULT_DB = "/home/ubuntu/BLM/blm_pokerbet.db"
DEFAULT_FRONTEND = "https://blm.haaats.xyz/static/index.html"
DEFAULT_BACKEND = "http://127.0.0.1:2262"
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(__file__), "..", ".monitor")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_history(path: str, keep_s: float) -> list[list[float]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            pts = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return []
    now = time.time()
    return [[p[0], p[1]] for p in pts if now - p[0] <= keep_s][-240:]


def append_history(path: str, pt: list[float]) -> None:
    tmp = f"{path}.tmp"
    pts = load_history(path, 3600) + [pt]
    with open(tmp, "w", encoding="utf-8") as fh:
        for p in pts[-240:]:
            fh.write(json.dumps(p) + "\n")
    os.replace(tmp, path)


def write_atomic(path: str, payload: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def one_cycle(args, th: Thresholds, tracker: AlertTracker) -> dict:
    history = load_history(args.history, th.flat_line_window_s * 2)
    started = time.monotonic()
    report = collect(
        unit=args.unit, state_path=args.state, db_path=args.db,
        backend_url=args.backend, frontend_url=args.frontend,
        frontend_marker=args.frontend_marker,
        snapshot_history=[(t, v) for t, v in history], th=th)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    report["cycle_ms"] = round(elapsed_ms, 2)

    snap = report.get("signals", {}).get("snapshots_total")
    if snap is not None:
        append_history(args.history, [time.time(), snap])

    events = tracker.observe(report, time.time())
    tracker.save()
    report["alerts"] = tracker.summary()
    report["events"] = events

    write_atomic(args.out, dumps(report))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BLM health monitor")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--backend", default=DEFAULT_BACKEND)
    ap.add_argument("--frontend", default=DEFAULT_FRONTEND)
    ap.add_argument("--frontend-marker", default="BLM")
    ap.add_argument("--unit", default="blm-collector",
                    help="systemd --user unit for the collector ('' to disable)")
    ap.add_argument("--out", default=os.path.join(DEFAULT_OUT_DIR, "health.json"))
    ap.add_argument("--alerts", default=os.path.join(DEFAULT_OUT_DIR, "alerts.json"))
    ap.add_argument("--journal", default=os.path.join(DEFAULT_OUT_DIR, "incidents.jsonl"))
    ap.add_argument("--history", default=os.path.join(DEFAULT_OUT_DIR, "history.jsonl"))
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--cycles", type=int, default=0, help="stop after N cycles")
    ap.add_argument("--fixtures", default=None,
                    help="test environment: state/db/out all live under this dir")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.fixtures:
        fx = os.path.abspath(args.fixtures)
        args.state = args.state if args.state != DEFAULT_STATE else os.path.join(fx, "state.json")
        args.db = args.db if args.db != DEFAULT_DB else os.path.join(fx, "metrics.db")
        args.out = os.path.join(fx, "health.json")
        args.alerts = os.path.join(fx, "alerts.json")
        args.journal = os.path.join(fx, "incidents.jsonl")
        args.history = os.path.join(fx, "history.jsonl")
        args.unit = ""                                   # no systemd in fixtures
        args.frontend = args.frontend if args.frontend != DEFAULT_FRONTEND else os.path.join(fx, "no-frontend")
        args.backend = args.backend if args.backend != DEFAULT_BACKEND else "http://127.0.0.1:9"

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    th = Thresholds()
    tracker = AlertTracker(path=args.alerts, journal_path=args.journal,
                           confirm_cycles=th.confirm_cycles).load()

    n = 0
    while True:
        report = one_cycle(args, th, tracker)
        n += 1
        if not args.quiet:
            line = " ".join(f"{c['key']}={c['color']}" for c in report["components"])
            print(f"[{_utcnow()}] {report['overall']:<8} {line} cycle={report['cycle_ms']}ms",
                  flush=True)
            for ev in report["events"]:
                print(f"    {ev['event']}: {ev['label']} ({ev['severity']}) "
                      f"started={ev['started_at']} detected={ev['detected_at']}"
                      + (f" recovered={ev['recovered_at']}" if ev.get("recovered_at") else ""),
                      flush=True)
        if args.once or (args.cycles and n >= args.cycles):
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
