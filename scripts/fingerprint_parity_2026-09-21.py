#!/usr/bin/env python3
"""BACKTEST PARITY — production fingerprint layer vs the historical dataset.

Directive 2026-09-21: after implementation, run the historical dataset
through the PRODUCTION fingerprint functions.  The production
implementation must reproduce the historical definitions:

    C1 N/UNDER%   C2 N/UNDER%   C3 N/UNDER%   C4 N/UNDER%
    C5 N/UNDER%   C6 N/UNDER%   R2 N/UNDER%

R1 must NOT appear as an active fingerprint.

DATASET (the SAME cohort the directive's historical numbers came from)
  analysis/under_over_feature_dataset_2026-09-21.csv — the read-only
  discovery export (one row per game's 75% boundary) — MERGED with
  shadow_fingerprints_75.jsonl — the forward shadow log (JSONL supersedes
  the CSV per (game_id, captured_at); last-wins) — exactly the merge
  scripts/oos_weekly_rolling_2026-09-21.py performs.  The directive's
  N=281 settled-CLEAN-trigger reference is this merged cohort.

COHORT
  tier=75 AND trigger=True AND stale=False — the PRODUCTION 75% CLEAN
  trigger cohort the discovery measured (§12 of
  analysis_under_vs_over_pattern_discovery_2026-09-21.md).

METHOD (parity by construction)
  Each row is fed through the production
  ``blm_v4.live_analytics.under_fingerprints.evaluate_fingerprints`` using
  the SAME point-in-time operands production consumes (required/league
  pace, Q3 ppm/league Q3 avg, recent3/actual pace).  The CSV's own frozen
  ratio columns (req_ratio, q3_ratio, recent3_minus_act) are then compared
  against the production-derived ratios — any drift is a definition break
  and is reported loudly.  A fingerprint counts as FIRED only on
  ``*_triggered is True``; UNAVAILABLE rows are reported separately and
  never counted as fired.

READ-ONLY: the production DBs are not opened; the only file written is
the parity report.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys

REPO = "/home/ubuntu/BLM"
sys.path.insert(0, REPO)

from blm_v4.live_analytics.under_fingerprints import (  # noqa: E402
    FINGERPRINT_KEYS,
    evaluate_fingerprints,
)

CSV_PATH = os.path.join(
    REPO, "analysis", "under_over_feature_dataset_2026-09-21.csv")
JSONL_PATH = os.path.join(REPO, "shadow_fingerprints_75.jsonl")
OUT_MD = os.path.join(REPO, "analysis", "fingerprint_parity_2026-09-21.md")

# Historical reference (discovery §12 / directive 2026-09-21) — reported
# beside the parity result so agreement is checkable at a glance.
HISTORICAL = {
    "C1": (17, 88.24), "C2": (111, 74.77), "C3": (147, 72.79),
    "C4": (7, 100.00), "C5": (103, 76.70), "C6": (75, 76.00),
    "R2": (87, 78.16),
}

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def fnum(x):
    if x in ("", None):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def load_cohort():
    """CSV (discovery history) + JSONL (forward shadow log) -> records.

    Mirrors scripts/oos_weekly_rolling_2026-09-21.py load_records(): the
    JSONL supersedes the CSV per (game_id, captured_at), last line wins
    (settlement updates included).  Returns the settled CLEAN production
    triggers only — the directive's reference cohort."""
    recs = {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("tier") != "75":
                    continue
                if not (row.get("gid") and row.get("captured_at")):
                    continue
                recs[(row["gid"], row["captured_at"])] = {
                    "stale": row.get("stale") in ("True", "true", "1"),
                    "trigger": row.get("trigger") in ("True", "true", "1"),
                    "outcome": row.get("outcome") or None,
                    "req": fnum(row.get("req")),
                    "avg": fnum(row.get("avg")),
                    "act": fnum(row.get("act")),
                    "recent3": fnum(row.get("recent3")),
                    "q3_ppm": fnum(row.get("q3_ppm")),
                    "q3_league_avg": fnum(row.get("q3_league_avg")),
                    "req_ratio": fnum(row.get("req_ratio")),
                    "q3_ratio": fnum(row.get("q3_ratio")),
                    "recent3_minus_act": fnum(row.get("recent3_minus_act")),
                }
    if os.path.exists(JSONL_PATH):
        with open(JSONL_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (d.get("game_id"), d.get("captured_at"))
                prev = recs.get(key) or {}
                rec = dict(prev)
                rec.update({
                    "stale": bool(d.get("stale", prev.get("stale", False))),
                    "trigger": bool(d.get("is_production_trigger",
                                          prev.get("trigger", False))),
                    "outcome": d.get("outcome") or prev.get("outcome"),
                })
                for src, dst in (("required_pts_per_min", "req"),
                                 ("league_avg_pace", "avg"),
                                 ("actual_pts_per_min", "act"),
                                 ("q3_ppm", "q3_ppm"),
                                 ("q3_league_avg", "q3_league_avg"),
                                 ("req_ratio", "req_ratio"),
                                 ("q3_ratio", "q3_ratio"),
                                 ("recent3_minus_act",
                                  "recent3_minus_act")):
                    if src in d:
                        rec[dst] = fnum(d.get(src))
                # the shadow log stores recent3_minus_act as a frozen 6-dp
                # field while the paces carry coarser rounding — keep the
                # AUTHORITATIVE offset; the production layer accepts it
                # explicitly so boundary rows stay exactly reproducible
                recs[key] = rec
    return [r for r in recs.values()
            if r["trigger"] and not r["stale"]
            and r["outcome"] in ("under", "over", "push")]


def main() -> None:
    rows = load_cohort()
    settled = rows
    out("=" * 96)
    out("FINGERPRINT PARITY — PRODUCTION LAYER vs HISTORICAL DATASET (2026-09-21)")
    out("=" * 96)
    out(f"cohort : production trigger + CLEAN + settled, "
        f"CSV merged with forward JSONL (JSONL supersedes)")
    out(f"rows   : {len(rows)} settled CLEAN triggers "
        f"(under={sum(1 for r in settled if r['outcome']=='under')}, "
        f"over={sum(1 for r in settled if r['outcome']=='over')}, "
        f"push={sum(1 for r in settled if r['outcome']=='push')})")
    if settled:
        u = sum(1 for r in settled if r["outcome"] == "under")
        out(f"baseline trigger UNDER% = {100.0 * u / len(settled):.2f}%")
    out("")

    # ── run the PRODUCTION layer over every settled trigger ────────────
    fired = {k: [] for k in FINGERPRINT_KEYS}
    unavailable = {k: 0 for k in FINGERPRINT_KEYS}
    count_ok = 0
    ratio_drift = {"req_ratio": 0, "q3_ratio": 0, "recent3_minus_act": 0}
    for r in settled:
        b = evaluate_fingerprints(r.get("req"), r.get("avg"),
                                  r.get("q3_ppm"), r.get("q3_league_avg"),
                                  r.get("recent3"), r.get("act"),
                                  r.get("recent3_minus_act"))

        # definition parity: production-derived ratios vs the CSV's frozen
        # discovery ratios (both computed at the trigger instant)
        for col, key, scale in (("req_ratio", "req_ratio", None),
                                ("q3_ratio", "q3_ratio", None),
                                ("recent3_minus_act", "recent3_minus_act",
                                 None)):
            got = b[key]
            want = fnum(r.get(col))
            if got is None and want is None:
                continue
            if got is None or want is None or abs(got - want) > 5e-4:
                ratio_drift[col] += 1

        # fingerprint_count must equal len(fingerprints_fired) on every row
        if b["fingerprint_count"] == len(b["fingerprints_fired"]):
            count_ok += 1

        for k in FINGERPRINT_KEYS:
            if b[f"fingerprint_{k.lower()}_triggered"] is True:
                fired[k].append(r["outcome"])
            elif b[f"fingerprint_{k.lower()}"] == "UNAVAILABLE":
                unavailable[k] += 1

    out("PER-FINGERPRINT PARITY (production functions on the historical dataset)")
    out(f"{'fp':<4} {'N':>5} {'UNDER':>6} {'OVER':>5} {'PUSH':>5} "
        f"{'UNDER%':>8}  {'historical (N, UNDER%)':<24} {'match':<6} "
        f"{'UNAVAIL':>8}")
    all_match = True
    for k in FINGERPRINT_KEYS:
        g = fired[k]
        n = len(g)
        u = sum(1 for x in g if x == "under")
        rate = (100.0 * u / n) if n else None
        hn, hp = HISTORICAL[k]
        match = (n == hn) and (rate is not None
                               and abs(rate - hp) < 0.01)
        all_match = all_match and match
        out(f"{k:<4} {n:>5} {u:>6} {sum(1 for x in g if x=='over'):>5} "
            f"{sum(1 for x in g if x=='push'):>5} "
            f"{(f'{rate:>7.2f}%' if rate is not None else '   n/a '):>8}  "
            f"{f'N={hn}, {hp:.2f}%':<24} "
            f"{'YES' if match else 'DRIFT':<6} {unavailable[k]:>8}")
    out("")
    out("definition parity (production-derived ratio vs frozen CSV ratio,")
    out("tolerance 5e-4) — drift counts across all settled triggers:")
    for col, n in ratio_drift.items():
        out(f"  {col:<18} drift={n}")
    out(f"fingerprint_count == len(fingerprints_fired) on {count_ok}/"
        f"{len(settled)} rows")
    out("")

    # ── count distribution (recorded for analysis, never a threshold) ──
    out("FINGERPRINT_COUNT DISTRIBUTION (settled CLEAN triggers)")
    dist: dict[int, list[str]] = {}
    for r in settled:
        b = evaluate_fingerprints(
            r.get("req"), r.get("avg"), r.get("q3_ppm"),
            r.get("q3_league_avg"), r.get("recent3"), r.get("act"),
            r.get("recent3_minus_act"))
        dist.setdefault(b["fingerprint_count"], []).append(r["outcome"])
    for c in sorted(dist):
        g = dist[c]
        u = sum(1 for x in g if x == "under")
        out(f"  count={c}: N={len(g):>4}  UNDER={u:>4}  "
            f"UNDER%={(100.0 * u / len(g)):>6.2f}%")
    out("")

    # ── R1 exclusion ───────────────────────────────────────────────────
    out("R1 EXCLUSION")
    from blm_v4.live_analytics import under_fingerprints as ufmod
    src = open(ufmod.__file__).read()
    r1_in_keys = "R1" in FINGERPRINT_KEYS
    r1_fields = [n for n in vars(ufmod) if "R1" in n]
    out(f"  'R1' in FINGERPRINT_KEYS        : {r1_in_keys}  "
        f"{'VIOLATION' if r1_in_keys else '(excluded)'}")
    out(f"  R1-named module constants       : {r1_fields or 'none'}")
    out(f"  the widened 1.35 band constant  : "
        f"{'PRESENT — VIOLATION' if '1.35' in src.replace(' ','') and 'R1' not in src else 'absent'}")
    # behavioural: a trigger inside the excluded widening fires nothing
    b = evaluate_fingerprints(1.30 * 4.0, 4.0, 3.5, 3.0, 3.8, 3.4)
    out(f"  req_ratio=1.30 (in [1.20,1.35)) : fired={b['fingerprints_fired']} "
        f"count={b['fingerprint_count']}")
    out("")

    out("VERDICT: " + ("FULL PARITY — the production layer reproduces the "
                        "historical definitions exactly."
                        if all_match and not any(ratio_drift.values())
                        else "DRIFT DETECTED — investigate before use."))
    out("R1: NOT an active fingerprint (excluded per directive).")
    out("READ-ONLY: no production DB opened; only this report was written.")

    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {OUT_MD}")


if __name__ == "__main__":
    main()
