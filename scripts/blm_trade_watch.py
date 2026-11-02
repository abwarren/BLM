#!/usr/bin/env python3
"""blm-trade-watch — silent watch on the auto-bet path; speaks only on events.

Designed for a no_agent cron tick: **print nothing when nothing happened** and a
short, self-contained alert when something did.

Events it reports
  BET      a new row appeared in bet_executions (placed / accepted / failed)
  ARMED    the fast lane logged armed > 0 in the last window
  STALE    no capture for BLM_WATCH_STALE_S (default 180 s) while the board has games
  DOWN     blm-server is not active, or its MainPID changed (a restart)
  PAYLOAD  betting_payload_unusable warnings in the last window (the blind-lane fault)

Read-only over the databases; it never writes to blm_* tables. Its own state
(last execution id, last seen pid) lives in a JSON file next to it.

    blm-trade-watch            one tick (cron)
    blm-trade-watch --reset    re-baseline without alerting on history
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("BLM_DATA_ROOT", "/home/ubuntu/BLM"))
BETTING_DB = DATA / "blm_betting.db"
POKERBET_DB = DATA / "blm_pokerbet.db"
STATE = Path(os.environ.get(
    "BLM_WATCH_STATE",
    str(Path.home() / ".hermes/profiles/blm/cache/scratch/blm_trade_watch.json")))
UNIT = "blm-server.service"
STALE_S = float(os.environ.get("BLM_WATCH_STALE_S", "180"))
UNITS_PER_ALERT = 1


def _sh(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _ro(path: Path) -> sqlite3.Connection | None:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    except OSError:
        pass


def _service() -> tuple[bool, int | None]:
    active = _sh(["systemctl", "--user", "is-active", UNIT]) == "active"
    raw = _sh(["systemctl", "--user", "show", UNIT, "-p", "MainPID", "--value"])
    try:
        pid = int(raw) or None
    except ValueError:
        pid = None
    return active, pid


def _journal(since: str) -> str:
    return _sh(["journalctl", "--user", "-u", UNIT, "--since", since,
                "--no-pager"], timeout=25)


def _max_capture_age() -> float | None:
    c = _ro(POKERBET_DB)
    if c is None:
        return None
    try:
        row = c.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    if not row or not row[0]:
        return None
    try:
        last = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - last).total_seconds()


def _new_executions(last_id: int) -> list[sqlite3.Row]:
    c = _ro(BETTING_DB)
    if c is None:
        return []
    try:
        return c.execute(
            "SELECT id, game_id, status, stake_amount, price, created_at,"
            "       provider_ref, error_code"
            "  FROM bet_executions WHERE id > ? ORDER BY id", (last_id,)
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()


def _max_execution_id() -> int:
    c = _ro(BETTING_DB)
    if c is None:
        return 0
    try:
        return int(c.execute("SELECT COALESCE(MAX(id),0) FROM bet_executions")
                   .fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        c.close()


def _live_counts() -> tuple[int, int]:
    """(games with a capture in the last 15 min, games currently in the band).

    ``snapshots`` has no progress column, so the band is read from
    ``clean_observations`` — bounded by an ID window (append-only) rather than a
    ``captured_at`` scan, which is the same trick the fast lane uses: 20k rows
    back is ~0.04 s against the 5.2M-row table.
    """
    c = _ro(DATA / "blm_metrics_clean.db")
    if c is None:
        return (0, 0)
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = c.execute(
            "SELECT source_game_id, progress_pct, captured_at"
            "  FROM clean_observations"
            " WHERE id > (SELECT MAX(id) - 20000 FROM clean_observations)"
            "   AND captured_at > ?", (cutoff,)).fetchall()
    except sqlite3.Error:
        return (0, 0)
    finally:
        c.close()
    latest: dict[str, tuple] = {}
    for gid, pct, cap in rows:
        if gid not in latest or (cap or "") > (latest[gid][1] or ""):
            latest[gid] = (pct, cap)
    band = 0
    for pct, _cap in latest.values():
        try:
            if pct is not None and 75.0 <= float(pct) <= 92.0:
                band += 1
        except (TypeError, ValueError):
            continue
    return (len(latest), band)


def main(argv: list[str]) -> int:
    reset = "--reset" in argv
    state = _load_state()
    first_run = not state
    lines: list[str] = []

    active, pid = _service()
    last_pid = state.get("pid")
    if not active:
        lines.append("blm-server is NOT ACTIVE — auto-bet cannot fire")
    elif last_pid and pid and pid != last_pid:
        lines.append(f"blm-server restarted (pid {last_pid} → {pid})")

    window = "10 min ago"
    log = _journal(window)
    alerted = re.findall(r'"armed": ([1-9]\d*)', log)
    if alerted:
        armed_lines = [ln for ln in log.splitlines()
                       if re.search(r'"armed": [1-9]', ln)]
        stamp = ""
        m = re.search(r'"timestamp": "([^"]+)"', armed_lines[-1])
        if m:
            stamp = m.group(1)[11:19] + "Z"
        # one alert per armed EPISODE, not one per tick: the window is 10 min
        # and the tick is 5, so without this the same event repeats.
        if stamp and stamp != state.get("last_armed_ts"):
            state["last_armed_ts"] = stamp
            lines.append(f"ARMED: fast lane had {alerted[-1]} candidate(s) armed"
                         f"{' at ' + stamp if stamp else ''} — a bet should be firing")

    payload_warn = log.count("betting_payload_unusable")
    if payload_warn:
        lines.append(f"PAYLOAD: {payload_warn} 'betting_payload_unusable' in the"
                     f" last 10 min — the betting worker has no live payload")

    placed = _new_executions(int(state.get("last_execution_id", 0)))
    if placed and not first_run and not reset:
        for r in placed:
            ref = r["provider_ref"] or "–"
            err = r["error_code"] or ""
            lines.append(
                f"BET #{r['id']} {r['status']} {r['game_id']} "
                f"stake={r['stake_amount']} price={r['price']} ref={ref}"
                f"{' ' + err if err else ''}")

    age = _max_capture_age()
    games, band = _live_counts()
    if age is not None and games and age > STALE_S:
        lines.append(f"STALE: last capture {age:.0f}s old across {games} live"
                     f" game(s) — capture is not flowing")

    _save_state({"pid": pid, "last_execution_id": _max_execution_id(),
                 "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    if reset or (first_run and not lines):
        print(f"watch armed · {games} live games · {band} in the 75-92 band · "
              f"next bet when pace ratio > 1.04 clears all legs")
        return 0

    if lines:
        print("BLM · " + " | ".join(lines))
        print(f"live: {games} games · {band} in band · capture age "
              f"{'n/a' if age is None else f'{age:.0f}s'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
