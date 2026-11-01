#!/usr/bin/env python3
"""tc-games — every game the LIVE cohort evaluated, and whether money went on it.

tc-live prints what the gate TOOK.  This prints every game that entered the
75-92% band in the window, what the gate decided about it, and what the
betting ledger actually did — so "qualified but refused at submission" and
"never qualified" are never confused with each other.

    tc-games                       today (UTC)
    tc-games 30                    last 30 days
    tc-games 2026-10-30 2026-10-31 explicit range
    tc-games --csv /tmp/x.csv      also write the full table

Adopts the running engine's TRADE_* env, exactly like tc-live.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _adopt_running_gate() -> dict:
    """Use the LIVE engine's TRADE_* gate, not the code defaults.

    The deployment selects its cohort through systemd drop-ins, so a report run
    from a shell would otherwise describe a different gate than the one firing —
    this is what made an early version of this tool report 89 "qualified" games
    against the 12 the engine actually took: it was running the -0.50 module
    default for the shortfall margin instead of the live +0.50.
    """
    import subprocess

    adopted = {}
    try:
        pid = subprocess.run(
            ["systemctl", "--user", "show", "blm-server.service", "-p",
             "MainPID", "--value"], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not pid or pid == "0":
            return adopted
        with open(f"/proc/{pid}/environ", "rb") as fh:
            for kv in fh.read().split(b"\0"):
                k, _, v = kv.partition(b"=")
                name = k.decode()
                if (name.startswith("TRADE_") or name.startswith("BET_REVALIDATE")) \
                        and name not in os.environ:
                    os.environ[name] = v.decode()
                    adopted[name] = v.decode()
    except Exception:
        pass
    return adopted


ADOPTED_GATE = _adopt_running_gate()

from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402
from blm_v4.trade_window import (  # noqa: E402
    exec_alert_reason,
    exec_shortfall_reason,
    execution_window_reason,
    score_reason,
    trade_competition_reason,
    trade_price_reason,
)

DATA = Path(os.environ.get("BLM_DATA_ROOT", "/home/ubuntu/BLM"))
CLEAN = DATA / "blm_metrics_clean.db"
MAIN_DB = DATA / "blm_pokerbet.db"
BET_DB = DATA / "blm_betting.db"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("dates", nargs="*", help="[days] | start end (YYYY-MM-DD)")
    p.add_argument("--csv", default=None, help="write the full table to this CSV")
    p.add_argument("--all", action="store_true", help="print every game, not just gate/ledger decisions")
    return p.parse_args()


def window(dates: list[str]) -> tuple[str, str]:
    if not dates:
        return "date('now')", "date('now','+1 day')"
    if len(dates) == 1 and dates[0].isdigit():
        return f"date('now','-{int(dates[0])} day')", "date('now','+1 day')"
    if len(dates) == 1:
        return f"'{dates[0]}'", f"date('{dates[0]}','+1 day')"
    return f"'{dates[0]}'", f"date('{dates[1]}','+1 day')"


def main() -> int:
    args = parse_args()
    lo, hi = window(args.dates)

    conn = sqlite3.connect(f"file:{CLEAN}?mode=ro", uri=True)
    conn.execute(f"ATTACH DATABASE 'file:{MAIN_DB}?mode=ro' AS p")
    conn.execute(f"ATTACH DATABASE 'file:{BET_DB}?mode=ro' AS b")
    ref = competition_pace_reference(conn)

    rows = conn.execute(
        f"""SELECT o.source_game_id, o.progress_pct, o.total_points,
                   o.required_pts_per_min, o.actual_pts_per_min, o.under_price,
                   g.competition_slug, g.home_team, g.away_team,
                   o.captured_at, r.final_total
            FROM clean_observations o
            LEFT JOIN p.games g ON g.source_game_id = o.source_game_id
            LEFT JOIN p.game_results r ON r.source_game_id = o.source_game_id
            WHERE o.status = 'VALID'
              AND o.progress_pct BETWEEN 75 AND 92
              AND o.captured_at >= {lo} AND o.captured_at < {hi}
            ORDER BY o.captured_at"""
    ).fetchall()

    # best in-band moment per game: fewest failing legs, then latest progress
    best: dict[str, dict] = {}
    for (gid, prog, pts, req, act, price, slug, home, away, ts, final) in rows:
        avg = (ref.get(slug) or {}).get("avg_pace") if isinstance(ref.get(slug), dict) else None
        legs = {
            "comp": trade_competition_reason(slug),
            "pace": exec_alert_reason(req, avg, prog),
            "short": exec_shortfall_reason(req, act),
            "win": execution_window_reason(prog),
            "score": score_reason(pts),
            "price": trade_price_reason(price),
        }
        fails = [k for k, v in legs.items() if v]
        key = (len(fails), -prog)
        cur = best.get(gid)
        if cur is None or key < cur["key"]:
            best[gid] = dict(
                key=key, gid=gid, prog=prog, pts=pts, price=price, slug=slug,
                home=home, away=away, ts=ts, final=final, fails=fails,
                # the gate's own definition: required - actual, positive is good for UNDER
                shortfall=(req - act) if act is not None and req is not None else None,
            )

    # what the ledger did, per game (latest execution wins)
    led: dict[str, dict] = {}
    for (gid, status, err, stake, ref_id, stamp) in conn.execute(
        f"""SELECT game_id, status, error_code, stake_amount, provider_ref, created_at
            FROM b.bet_executions
            WHERE created_at >= {lo} AND created_at < {hi}
            ORDER BY id"""
    ):
        led[gid] = dict(status=status, err=err, stake=stake, ref=ref_id, at=stamp)

    out = []
    for gid, d in sorted(best.items(), key=lambda kv: kv[1]["ts"]):
        l = led.get(gid)
        if l:
            money = l["status"] + (f"/{l['err']}" if l["err"] else "")
            if l["stake"] is not None:
                money += f" R{l['stake']:.2f}"
        else:
            money = "-"
        verdict = "QUALIFIED" if not d["fails"] else ",".join(d["fails"])
        out.append(
            dict(
                when_utc=d["ts"][11:19], game_id=gid, competition=(d["slug"] or "")[:14],
                fixture=f'{d["home"]} v {d["away"]}', progress=round(d["prog"], 1),
                points=d["pts"], shortfall=None if d["shortfall"] is None else round(d["shortfall"], 2),
                price=d["price"], line=None, final=d["final"], gate=verdict, ledger=money,
                placed="YES" if l and l["status"] in ("SUBMITTED", "ACCEPTED", "SETTLED") else "no",
            )
        )

    print(f"tc-games — every game in the 75-92% band, {lo} → {hi}")
    print(f"gate: pace>1.04 · shortfall>=+0.50 · progress 75-92 · revalidate {os.environ.get('BET_REVALIDATE','?')}")
    print(f"games evaluated: {len(out)}")
    c = Counter(r["gate"] for r in out)
    print("gate verdicts: " + " · ".join(f"{k}={v}" for k, v in c.most_common(6)))
    money = Counter(r["placed"] for r in out)
    print(f"money placed: YES={money.get('YES',0)} · no={money.get('no',0)}")
    print()
    print(f'{"when":8} {"game":9} {"comp":14} {"prog":>5} {"pts":>4} {"short":>6} {"price":>5} {"final":>5}  {"gate":22} {"ledger":26} fixture')
    # The console view shows the decisions that matter: everything the gate
    # would have taken, plus every game the ledger touched.  The rest (the
    # bulk of the 75-92% band that failed pace/shortfall) goes to --csv, or
    # to the console with --all.
    shown = out if args.all else [
        r for r in out if r["gate"] == "QUALIFIED" or r["ledger"] != "-"
    ]
    for r in shown:
        print(
            f'{r["when_utc"]:8} {r["game_id"]:9} {r["competition"]:14} {r["progress"]:>5} '
            f'{str(r["points"]):>4} {str(r["shortfall"]):>6} {str(r["price"]):>5} '
            f'{str(r["final"]):>5}  {r["gate"][:22]:22} {r["ledger"][:26]:26} {r["fixture"][:40]}'
        )
    if not args.all:
        print(f"\n({len(out) - len(shown)} games that failed the gate are in the --csv / --all output)")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out[0]) if out else ["when_utc"])
            w.writeheader()
            w.writerows(out)
        print(f"\nfull table → {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
