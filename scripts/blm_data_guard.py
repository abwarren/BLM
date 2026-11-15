#!/usr/bin/env python3
"""Data guard — restart the collector when it is alive but has stopped capturing.

Why this exists
---------------
2026-11-02: the collector's browser died (``TargetClosedError``). The process
stayed up and its tick kept *completing* in 0.2 s, so systemd's watchdog — which
pings on tick — never fired. But every tick persisted zero games. Capture was
dead for 25 minutes, the slate went empty, and auto-bet was blind. Nothing in
the stack watched the only thing that matters: is data still arriving?

This guard watches that. It is deliberately conservative:

  * it only acts when the collector process is ACTIVE **and** games are being
    tracked (so an empty overnight board never triggers it),
  * it requires the newest row in ``snapshots`` to be older than STALE_S,
  * it refuses to restart more than MAX_PER_HOUR times (no restart loops),
  * ``--dry-run`` prints what it would do and changes nothing.

The freshness read is ``SELECT captured_at FROM snapshots ORDER BY id DESC
LIMIT 1`` — a primary-key lookup, O(1), on a read-only connection. It cannot
starve the collector's writer.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_DB = "/home/ubuntu/BLM/blm_pokerbet.db"
DEFAULT_STATE = "/home/ubuntu/BLM/blm_v4/state/collector_state.json"
DEFAULT_UNIT = "blm-collector"
DEFAULT_GUARD_STATE = os.path.join(
    os.path.expanduser("~"), ".hermes/profiles/blm/cache/scratch/blm_data_guard.json")

STALE_S = 180.0          # no new row for this long = capture is dead
COOLDOWN_S = 900.0       # never restart twice inside 15 min
MAX_PER_HOUR = 3
GRACE_S = 120.0          # a just-started collector gets this long to produce


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def newest_capture_age(db: str) -> tuple[float | None, str | None]:
    """(age_seconds, captured_at) of the newest snapshot row — O(1) via the PK."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            row = conn.execute(
                "SELECT captured_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, f"db error: {exc}"
    if not row or not row[0]:
        return None, "no snapshots"
    try:
        ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None, f"unparsable captured_at {row[0]!r}"
    return max(0.0, _now() - ts), row[0]


def unit_active(unit: str) -> tuple[bool, float | None]:
    """(active, seconds since the unit last started).

    Parsed as Key=Value: systemctl prints properties in ITS OWN order, not the
    order requested — reading positionally put the monotonic timestamp where
    the state should be and made every call look inactive.
    """
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "ExecMainStartTimestampMonotonic"],
            capture_output=True, text=True, timeout=5, check=False).stdout
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        active = props.get("ActiveState", "").strip() == "active"
        started_ago = None
        mono = (props.get("ExecMainStartTimestampMonotonic") or "").strip()
        if mono.isdigit() and int(mono) > 0:
            with open("/proc/uptime") as fh:
                uptime = float(fh.read().split()[0])
            started_ago = uptime - int(mono) / 1_000_000.0
        return active, started_ago
    except (OSError, subprocess.SubprocessError, ValueError):
        return False, None


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def log(path: str, message: str) -> None:
    line = f"{_iso(_now())} {message}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="restart the collector when capture dies")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--unit", default=DEFAULT_UNIT)
    ap.add_argument("--guard-state", default=DEFAULT_GUARD_STATE)
    ap.add_argument("--log", default=os.path.join(os.path.dirname(DEFAULT_GUARD_STATE),
                                                  "blm_data_guard.log"))
    ap.add_argument("--stale-s", type=float, default=STALE_S)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="skip the games-tracked test")
    args = ap.parse_args(argv)

    age, captured = newest_capture_age(args.db)
    if age is None:
        log(args.log, f"GUARD: cannot read capture freshness ({captured}) — no action")
        return 0

    active, started_ago = unit_active(args.unit)
    state = read_json(args.state)
    tracked = int(state.get("games_tracked") or 0)

    # a collector that started seconds ago has not had time to produce anything
    if active and started_ago is not None and started_ago < GRACE_S:
        return 0
    if not active:
        return 0                       # systemd owns this case, not us
    if age < args.stale_s:
        return 0                       # healthy: say nothing
    if tracked <= 0 and not args.force:
        log(args.log, f"GUARD: capture {age:.0f}s old but games_tracked=0 "
                      f"(board idle) — no action")
        return 0

    gs = read_json(args.guard_state)
    history = [t for t in gs.get("restarts", []) if _now() - t < 3600]
    last = max(history) if history else 0.0
    if last and _now() - last < COOLDOWN_S:
        log(args.log, f"GUARD: capture stale {age:.0f}s but a restart happened "
                      f"{_now() - last:.0f}s ago — waiting")
        return 0
    if len(history) >= MAX_PER_HOUR:
        log(args.log, f"GUARD: {len(history)} restarts in the last hour — refusing "
                      f"(this needs a human, not a loop)")
        return 0

    msg = (f"GUARD: capture stale {age:.0f}s (newest {captured}), unit active, "
           f"games_tracked={tracked} -> restarting {args.unit}")
    if args.dry_run:
        log(args.log, "DRY-RUN would: " + msg)
        return 0
    log(args.log, msg)
    try:
        subprocess.run(["systemctl", "--user", "restart", args.unit],
                       capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log(args.log, f"GUARD: restart failed: {type(exc).__name__}: {exc}")
        return 1

    history.append(_now())
    write_json(args.guard_state, {"restarts": history, "last_reason": msg,
                                  "last_restart_at": _iso(_now())})
    log(args.log, f"GUARD: restart issued — newest capture was {captured}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
