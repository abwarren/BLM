#!/usr/bin/env python3
"""blm-live-params — declared vs effective trading parameters, and the drift.

Two sources, compared:

  DECLARED   the systemd drop-ins, parsed in the order systemd loads them
             (filename order, last writer wins per key)
  EFFECTIVE  /proc/<MainPID>/environ of the running blm-server

They can disagree — a drop-in edited without `daemon-reload && restart` shows up
here as drift, which is exactly the state where the file on disk says one thing
and the money path does another.

Also names the cohort the effective legs equal (``blm_v4.trade_cohorts``), so a
hand-tuned number that matches no cohort is visible instead of silent.

    blm-live-params                 compare and exit non-zero on drift
    blm-live-params --declared      only the drop-ins (works with the service down)
    blm-live-params --json          machine-readable
    blm-live-params --dir PATH      read drop-ins from PATH instead of this host

Exit codes: 0 everything agrees · 1 drift · 2 cannot read one of the sources.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TRADE_KEYS = (
    "TRADE_MIN_PACE_RATIO",
    "TRADE_SHORTFALL_MARGIN",
    "TRADE_EXEC_MIN_PRICE",
    "TRADE_MOVE_FILTER_ENABLED",
    "TRADE_MIN_PROGRESS_PCT",
    "TRADE_MAX_PROGRESS_PCT",
    "TRADE_MIN_SCORED_POINTS",
    "BET_REVALIDATE",
    "BET_REVALIDATE_MAX_AGE_S",
    "BETTING_DRY_RUN",
    "BETTING_BROWSER_SUBMIT",
    "BETTING_MAX_STAKE_PER_BET",
    "BETTING_MAX_BETS_PER_DAY",
    "BLM_EXPECTED_ACCOUNT_ID",
)

DROPIN_DIRS = (
    Path.home() / ".config/systemd/user/blm-server.service.d",
    Path.home() / ".config/systemd/user.control/blm-server.service.d",
)
UNIT = "blm-server.service"


def _dropin_files(dirs=None) -> list[Path]:
    files: list[Path] = []
    for d in (dirs or DROPIN_DIRS):
        if d.is_dir():
            files.extend(sorted(d.glob("*.conf")))      # systemd: name order
    return files


def declared(dirs=None) -> tuple[dict, dict]:
    """({key: value}, {key: [files that set it, in load order]})"""
    values: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    for path in _dropin_files(dirs):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("Environment="):
                continue
            body = line[len("Environment="):].strip().strip('"')
            if "=" not in body:
                continue
            k, v = body.split("=", 1)
            k = k.strip()
            if k in TRADE_KEYS:
                values[k] = v.strip()
                sources.setdefault(k, []).append(path.name)
    return values, sources


def _main_pid() -> int | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", UNIT, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        pid = int(out or 0)
    except (subprocess.SubprocessError, ValueError):
        return None
    return pid or None


def effective(pid: int) -> dict:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return {k: env[k] for k in TRADE_KEYS if k in env}


def cohort_for(eff: dict) -> str:
    """Which named cohort the effective legs equal, or a plain description."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from blm_v4.trade_cohorts import COHORTS
    except Exception:
        return "(cohort registry unavailable)"

    def num(d, k):
        try:
            return float(d[k])
        except (KeyError, TypeError, ValueError):
            return None

    pace = num(eff, "TRADE_MIN_PACE_RATIO")
    short = num(eff, "TRADE_SHORTFALL_MARGIN")
    price = num(eff, "TRADE_EXEC_MIN_PRICE")
    for code, c in COHORTS.items():
        if pace is None:
            continue
        if (abs(pace - c["pace"]) < 1e-9 and short is not None
                and abs(short - (-c["shortfall"])) < 1e-9
                and price is not None and abs(price - c["price"]) < 1e-9):
            return f"{code} · {c['name']}"
    return "no cohort matches these legs"


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    dirs = [Path(argv[i + 1]) for i, a in enumerate(argv)
            if a == "--dir" and i + 1 < len(argv)] or None
    decl, sources = declared(dirs)
    if not decl:
        print("no drop-ins found in:", ", ".join(str(d) for d in (dirs or DROPIN_DIRS)),
              file=sys.stderr)
        return 2

    eff: dict = {}
    pid = None
    if "--declared" not in argv:
        pid = _main_pid()
        if pid is None:
            print(f"{UNIT} is not running — showing DECLARED only", file=sys.stderr)
        else:
            eff = effective(pid)

    drift = {k: (decl.get(k), eff.get(k)) for k in set(decl) | set(eff)
             if eff and decl.get(k) != eff.get(k)}
    payload = {"declared": decl, "effective": eff, "drift": drift,
               "pid": pid, "sources": sources,
               "cohort": cohort_for(eff) if eff else None}

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0 if not drift else 1

    print(f"declared  : {len(decl)} keys from {len(_dropin_files(dirs))} drop-ins")
    print(f"effective : {len(eff)} keys from pid {pid}" if eff
          else "effective : (service down)")
    print()
    print(f"{'key':<32}{'declared':>12}{'effective':>12}   source")
    for k in TRADE_KEYS:
        if k not in decl and k not in eff:
            continue
        d, e = decl.get(k, "–"), eff.get(k, "–")
        mark = "" if (not eff or d == e) else "  DRIFT"
        shadows = sources.get(k, [])
        note = shadows[-1] if shadows else "code default"
        if len(shadows) > 1:
            note += f"  (overrides {', '.join(shadows[:-1])})"
        print(f"{k:<32}{d:>12}{e:>12}   {note}{mark}")

    if eff:
        print(f"\ncohort    : {payload['cohort']}")
    if drift:
        print("\nDRIFT — the file and the money path disagree; "
              "daemon-reload && restart blm-server:", file=sys.stderr)
        for k, (d, e) in drift.items():
            print(f"  {k}: declared {d!r} · effective {e!r}", file=sys.stderr)
        return 1
    print("\nOK — declared and effective agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
