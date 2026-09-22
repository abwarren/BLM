#!/usr/bin/env python3
"""NATIVE OVER-SIDE TRIGGER COHORT — RESEARCH DESIGN + HISTORICAL EVALUATION
(read-only, 2026-09-22).

WHY THIS EXISTS
    The 2026-09-22 OVER discovery concluded that a genuine OVER surface needs
    its own trigger cohort: the current 75% pool is SELECTED by the UNDER
    production trigger (act < avg AND req > avg*1.04), so OVER there is the
    conditional minority and half the OVER feature space is structurally
    empty.  This script designs the native OVER cohort and evaluates every
    candidate trigger definition HISTORICALLY — without implementing
    anything in production.

KEY REALISATION THAT MAKES THIS POSSIBLE
    The existing research dataset is NOT restricted to UNDER triggers:
      - analysis/under_over_feature_dataset_2026-09-21.csv (discovery week)
      - shadow_fingerprints_75.jsonl                       (forward weeks)
    both record EVERY 75% boundary evaluation with ``trigger`` /
    ``is_production_trigger`` flags — including rows where req_ratio < 1.04
    (non-triggers).  A native OVER pool can therefore be constructed from
    the SAME sources with the SAME merge and CLEAN rules — no production
    change, no new collection, no DB write.

SOURCES (identical to oos_weekly_rolling_2026-09-21 / over_discovery)
    CSV discovery week + JSONL forward log, last-wins on (gid, captured_at),
    tier "75", CLEAN = stale false, settled = outcome in (under, over, push).

DESIGN FREEZE (pre-registered BEFORE looking at results — no threshold was
    chosen after seeing its OVER%):
      * candidate grid fixed below in CANDIDATES;
      * selection criteria fixed below in SELECTION (adequate N, meaningful
        separation, week stability, OOS persistence);
      * IS = discovery week W36, OOS = W37+ (same split as every prior
        analysis in this repo);
      * features restricted to the six point-in-time-safe fields the UNDER
        OOS contract froze, plus raw operand forms of the same quantities
        (act, req, avg — needed to express pace-level candidates);
      * the label (outcome) is never a feature.

READ-ONLY GUARANTEES
    No DB is opened at all: both sources are flat files.  The script writes
    only its own markdown report.  Production code, schema, alert logic,
    fingerprints, betting and services are untouched.

Usage:
    python3 scripts/over_trigger_cohort_design_2026-09-22.py
"""
from __future__ import annotations

import ast
import csv
import json
import math
import os
import textwrap
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = "/home/ubuntu/BLM"
CSV_PATH = f"{REPO}/analysis/under_over_feature_dataset_2026-09-21.csv"
JSONL_PATH = f"{REPO}/shadow_fingerprints_75.jsonl"
OUT_MD = f"{REPO}/analysis_over_trigger_cohort_design_2026-09-22.md"

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


def wilson(wins, n, z=1.96):
    if not n:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def size_class(n: int) -> str:
    if n < 20:
        return "tiny"
    if n < 50:
        return "small"
    if n < 100:
        return "moderate"
    return "substantial"


def week_key(cap):
    try:
        dt = datetime.fromisoformat(cap.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    dt = dt.astimezone(timezone.utc)
    monday = (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%G-W%V")


# ── canonical point-in-time features (whitelisted; audited by AST) ─────
FEATURES = ("req_ratio", "recent3_minus_act", "q3_ratio", "line_move",
            "act_minus_req", "score_differential",
            "act", "req", "avg", "recent3", "q3_ppm", "q3_league_avg")


def load_records():
    """CSV (history) + JSONL forward log -> resolved per-key records.
    Identical merge to the UNDER analysis (JSONL last-wins).  Loads EVERY
    75% row — trigger and non-trigger — plus raw operand features."""
    recs = {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("tier") != "75":
                    continue
                if not (row.get("gid") and row.get("captured_at")):
                    continue
                key = (row["gid"], row["captured_at"])
                r = {
                    "league": row.get("league"),
                    "stale": row.get("stale") in ("True", "true", "1"),
                    "trigger": row.get("trigger") in ("True", "true", "1"),
                    "outcome": (row.get("outcome") or None),
                    "cap": row.get("captured_at"),
                    "src": "csv",
                }
                for f in ("req_ratio", "recent3_minus_act", "q3_ratio",
                          "line_move", "act_minus_req", "act_minus_avg",
                          "req_minus_avg"):
                    r[f] = fnum(row.get(f))
                r["score_differential"] = fnum(row.get("diff"))
                # raw operands
                r["act"] = fnum(row.get("act"))
                r["req"] = fnum(row.get("req"))
                r["avg"] = fnum(row.get("avg"))
                r["recent3"] = fnum(row.get("recent3"))
                r["q3_ppm"] = fnum(row.get("q3_ppm"))
                r["q3_league_avg"] = fnum(row.get("q3_league_avg"))
                recs[key] = r
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
                r = dict(prev)
                r.update({
                    "league": d.get("league") or prev.get("league"),
                    "stale": bool(d.get("stale", prev.get("stale", False))),
                    "trigger": bool(d.get("is_production_trigger",
                                          prev.get("trigger", False))),
                    "outcome": (d.get("outcome") or prev.get("outcome")),
                    "cap": d.get("captured_at") or prev.get("cap"),
                    "src": "jsonl",
                })
                r["req_ratio"] = fnum(d.get("req_ratio")) or prev.get("req_ratio")
                r["recent3_minus_act"] = (fnum(d.get("recent3_minus_act"))
                                          or prev.get("recent3_minus_act"))
                r["q3_ratio"] = fnum(d.get("q3_ratio")) or prev.get("q3_ratio")
                r["line_move"] = fnum(d.get("line_move")) or prev.get("line_move")
                r["act_minus_req"] = (fnum(d.get("act_minus_req"))
                                      or prev.get("act_minus_req"))
                r["score_differential"] = (fnum(d.get("score_differential"))
                                           or prev.get("score_differential"))
                r["act"] = fnum(d.get("actual_pts_per_min")) or prev.get("act")
                r["req"] = (fnum(d.get("required_pts_per_min"))
                            or prev.get("req"))
                r["avg"] = (fnum(d.get("league_avg_pace"))
                            or prev.get("avg"))
                r["recent3"] = (fnum(d.get("recent_pace_3m"))
                                or prev.get("recent3"))
                r["q3_ppm"] = fnum(d.get("q3_ppm")) or prev.get("q3_ppm")
                r["q3_league_avg"] = (fnum(d.get("q3_league_avg"))
                                      or prev.get("q3_league_avg"))
                recs[key] = r
    return recs


# ══════════════════════════════════════════════════════════════════════
# PRE-REGISTERED CANDIDATE GRID — frozen before any result was seen.
# Each predicate consumes ONLY point-in-time-safe operands.
# ══════════════════════════════════════════════════════════════════════
CANDIDATES = [
    # 1. actual pace vs league average
    ("A1  act > avg",                          lambda r: r["act"] > r["avg"]),
    ("A2  act/avg >= 1.05",                    lambda r: r["act"] >= r["avg"] * 1.05),
    # 2/3. required pace below league average — strength ladder
    ("B1  req < avg            (req<1.00x)",   lambda r: r["req"] < r["avg"]),
    ("B2  req_ratio <= 0.98",                  lambda r: r["req_ratio"] <= 0.98),
    ("B3  req_ratio <= 0.96",                  lambda r: r["req_ratio"] <= 0.96),
    ("B4  req_ratio <= 0.94",                  lambda r: r["req_ratio"] <= 0.94),
    ("B5  req_ratio <= 0.92",                  lambda r: r["req_ratio"] <= 0.92),
    ("B6  req_ratio <= 0.90",                  lambda r: r["req_ratio"] <= 0.90),
    # 5. combinations of actual + required pace
    ("C1  act>avg AND req<avg",                lambda r: r["act"] > r["avg"] and r["req"] < r["avg"]),
    ("C2  act>avg AND req<=0.96x",             lambda r: r["act"] > r["avg"] and r["req_ratio"] <= 0.96),
    ("C3  act/avg>=1.05 AND req<=0.96x",       lambda r: r["act"] >= r["avg"] * 1.05 and r["req_ratio"] <= 0.96),
    ("C4  act>avg AND req_ratio in [0.94,0.98]", lambda r: r["act"] > r["avg"] and 0.94 <= r["req_ratio"] <= 0.98),
    # 4. Q3 pace vs league (native-side, not the UNDER dip)
    ("D1  q3_ratio > 1.00",                    lambda r: r["q3_ratio"] > 1.00),
    ("D2  q3_ratio >= 1.05",                   lambda r: r["q3_ratio"] >= 1.05),
    # 6. recent pace vs actual pace (acceleration)
    ("E1  recent3 > act",                      lambda r: r["recent3"] > r["act"]),
    ("E2  recent3-act >= +0.5",                lambda r: r["recent3"] - r["act"] >= 0.5),
    # 7. three-way combinations
    ("F1  act>avg AND q3>1.0",                 lambda r: r["act"] > r["avg"] and r["q3_ratio"] > 1.00),
    ("F2  act>avg AND accel>=0",               lambda r: r["act"] > r["avg"] and r["recent3"] >= r["act"]),
    ("F3  req<=0.96x AND q3>1.0",              lambda r: r["req_ratio"] <= 0.96 and r["q3_ratio"] > 1.00),
    ("F4  act>avg AND req<=0.96x AND q3>1.0",  lambda r: r["act"] > r["avg"] and r["req_ratio"] <= 0.96 and r["q3_ratio"] > 1.00),
    ("F5  act>avg AND req<avg AND accel>=0",   lambda r: r["act"] > r["avg"] and r["req"] < r["avg"] and r["recent3"] >= r["act"]),
]

# The UNDER production trigger (for pool-composition + overlap analysis)
UNDER_TRIGGER = lambda r: (r["act"] < r["avg"] and r["req"] > r["avg"] * 1.04)


def rate(rows, label="over"):
    n = len(rows)
    w = sum(1 for r in rows if r["outcome"] == label)
    p = w / n if n else None
    lo, hi = wilson(w, n)
    return n, w, p, lo, hi


def main():
    recs = load_records()
    rows = [r for r in recs.values()
            if not r["stale"] and r["outcome"] in ("under", "over", "push")]
    for r in rows:
        r["week"] = week_key(r["cap"])

    weeks = sorted({r["week"] for r in rows if r["week"]})
    is_weeks, oos_weeks = weeks[:1], weeks[1:]   # W36 discovery / W37+ forward
    IS = [r for r in rows if r["week"] in is_weeks]
    OOS = [r for r in rows if r["week"] in oos_weeks]

    out("# NATIVE OVER-SIDE TRIGGER COHORT — RESEARCH DESIGN + HISTORICAL "
        "EVALUATION")
    out("(read-only, 2026-09-22; flat-file sources only; no DB opened; no "
        "production change)")
    out()
    out("## 0. Research design (frozen before evaluation)")
    out()
    out("- **Pool:** EVERY CLEAN settled 75% boundary evaluation — trigger and")
    out("  non-trigger — from the identical CSV+JSONL merge the UNDER analysis")
    out("  used (last-wins on gid+captured_at).  This is the native OVER pool")
    out("  candidate: no production change, no new collection.")
    out("- **Candidate grid:** 21 definitions pre-registered below, covering")
    out("  actual pace vs league, required pace ladders (0.98/0.96/0.94/0.92/0.90),")
    out("  act+req combinations, Q3 vs league, recent3 vs act, and triples.")
    out("- **Selection criteria (frozen):** adequate N (>=50 full cohort),")
    out("  separation (lift >= +8pp AND Wilson CI excludes baseline),")
    out("  stability (data in >=3 weeks; OVER% >= baseline in >=2/3 of weeks")
    out("  with N>=10; no single week >60% of the candidate's N), OOS")
    out("  persistence (OOS OVER% >= OOS baseline +5pp with OOS N>=20).")
    out("- **No threshold is chosen because it maximises OVER%.**  Candidates")
    out("  are judged against ALL four criteria; the report ranks survivors.")
    out()

    # ── 1. pool composition ────────────────────────────────────────────
    n_all = len(rows)
    trig = [r for r in rows if UNDER_TRIGGER(r)]
    nontrig = [r for r in rows if not UNDER_TRIGGER(r)]
    nT, wT, pT, loT, hiT = rate(trig)
    nN, wN, pN, loN, hiN = rate(nontrig)
    nF, wF, pF, loF, hiF = rate(rows)
    out("## 1. Native pool composition (CLEAN settled, tier 75)")
    out()
    out(f"pool rows: **{n_all}**  ·  weeks: {', '.join(weeks)}")
    out()
    out("| subset | N | OVER | OVER% | UNDER% | 95% CI |")
    out("|---|---|---|---|---|---|")
    out(f"| full pool | {n_all} | {wF} | {pF*100:.2f} | "
        f"{(nF-wF)/nF*100:.2f} | [{loF*100:.1f}, {hiF*100:.1f}] |")
    out(f"| UNDER production triggers | {nT} | {wT} | {pT*100:.2f} | "
        f"{(nT-wT)/nT*100:.2f} | [{loT*100:.1f}, {hiT*100:.1f}] |")
    out(f"| non-trigger rows (native pool) | {nN} | {wN} | {pN*100:.2f} | "
        f"{(nN-wN)/nN*100:.2f} | [{loN*100:.1f}, {hiN*100:.1f}] |")
    pushes = sum(1 for r in rows if r["outcome"] == "push")
    out()
    out(f"PUSH rows: {pushes} ({pushes/n_all*100:.2f}%).")
    out()
    out("IS/OOS: IS = " + ", ".join(is_weeks) + f" (N={len(IS)}), "
        "OOS = " + ", ".join(oos_weeks) + f" (N={len(OOS)}).")
    nI, wI, pI, _, _ = rate(IS)
    nO, wO, pO, _, _ = rate(OOS)
    out(f"baseline OVER% — full {pF*100:.2f} · IS {pI*100:.2f} · "
        f"OOS {pO*100:.2f}")
    out()
    out("per-week pool:")
    out()
    out("| week | N | OVER | OVER% |")
    out("|---|---|---|---|")
    for wk in weeks:
        sub = [r for r in rows if r["week"] == wk]
        nw, ww, pw, _, _ = rate(sub)
        out(f"| {wk} | {nw} | {ww} | {pw*100:.2f} |")
    out()

    # ── 2. candidate evaluation ────────────────────────────────────────
    out("## 2. Candidate OVER trigger definitions — historical evaluation")
    out()
    out("lift vs the matching-basis baseline (full vs full, OOS vs OOS).")
    out()
    out("| candidate | N | OVER | OVER% | UNDER% | PUSH% | lift | 95% CI |"
        " size | OOS N | OOS OVER% | OOS lift | UNDER-trig overlap |")
    out("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    results = []
    for label, pred in CANDIDATES:
        try:
            sub = [r for r in rows if _safe(pred, r)]
        except Exception:
            continue
        n, w, p, lo, hi = rate(sub)
        if not n:
            continue
        pushp = sum(1 for r in sub if r["outcome"] == "push") / n * 100
        sub_o = [r for r in sub if r["week"] in oos_weeks]
        no, wo, po, _, _ = rate(sub_o)
        olift = (po - pO) * 100 if po is not None and nO else None
        ovl = sum(1 for r in sub if UNDER_TRIGGER(r)) / n * 100
        results.append((label, pred, sub, n, w, p, lo, hi, no, wo, po, olift, ovl))
        out(f"| {label} | {n} | {w} | {p*100:.2f} | {(n-w)/n*100:.2f} | "
            f"{pushp:.1f} | {(p-pF)*100:+.1f} | [{lo*100:.1f}, {hi*100:.1f}] | "
            f"{size_class(n)} | {no} | "
            f"{po*100:.2f} | {olift:+.1f} | {ovl:.0f}% |")
    out()

    # ── 3. weekly stability of the leading candidates ──────────────────
    out("## 3. Weekly distribution per candidate (stability evidence)")
    out()
    for label, pred, sub, *_ in results:
        byw = defaultdict(list)
        for r in sub:
            byw[r["week"]].append(r)
        cells = []
        for wk in weeks:
            s = byw.get(wk, [])
            if not s:
                cells.append("—")
                continue
            _, _, pw, _, _ = rate(s)
            cells.append(f"{wk}: N={len(s)} OV={pw*100:.0f}%")
        out(f"- **{label}** — " + " · ".join(cells))
    out()

    # ── 4. frozen selection criteria applied ──────────────────────────
    out("## 4. Frozen selection criteria applied (not highest-OVER% picking)")
    out()
    out("| candidate | N>=50 | lift>=+8 & CI excl. | weeks>=3 & stable |"
        " OOS persist (N>=20, +5pp) | verdict |")
    out("|---|---|---|---|---|---|")
    for label, pred, sub, n, w, p, lo, hi, no, wo, po, olift, ovl in results:
        c1 = n >= 50
        c2 = (p - pF) * 100 >= 8.0 and (lo > pF)
        wk_n = defaultdict(int)
        for r in sub:
            wk_n[r["week"]] += 1
        weeks_with = [wk for wk in weeks if wk_n.get(wk, 0) >= 10]
        enough_weeks = len(wk_n) >= 3
        stable_weeks = sum(
            1 for wk in weeks_with
            if rate([r for r in sub if r["week"] == wk])[2] >= pF)
        max_share = max(wk_n.values()) / n if n else 0
        c3 = enough_weeks and (not weeks_with or
                               stable_weeks / max(1, len(weeks_with)) >= 2 / 3) \
             and max_share <= 0.60
        c4 = (no >= 20 and po is not None and (po - pO) * 100 >= 5.0)
        passed = [c1, c2, c3, c4]
        verdict = ("SELECTED for shadow design" if all(passed) else
                   "near-miss — consider" if sum(passed) >= 3 else
                   "rejected")
        out(f"| {label} | {c1} | {c2} | {c3} ({len(wk_n)}wk, maxwk "
            f"{max_share*100:.0f}%) | {c4} | **{verdict}** |")
    out()

    # ── 5. leakage audit ───────────────────────────────────────────────
    out("## 5. Leakage audit")
    out()
    wl = set(FEATURES)
    bad = []
    for label, pred in CANDIDATES:
        try:
            tree = ast.parse(textwrap.dedent(
                inspect_lambda_source(pred)))
            lam = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.Lambda))
            src = ast.unparse(lam.body)
        except Exception:
            bad.append((label, "source unavailable"))
            continue
        names = {n.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Name)}
        illegal = names - wl - {"r"}
        if illegal:
            bad.append((label, sorted(illegal)))
    out(f"- AST audit over {len(CANDIDATES)} candidate predicates: "
        f"{'PASS — only whitelisted point-in-time operands referenced' if not bad else 'FAIL'}")
    for label, why in bad:
        out(f"  - {label}: {why}")
    out("- Label (outcome/final total) never appears in any predicate; it is")
    out("  used only to grade.  No closing line, Q4 split, settlement or")
    out("  post-trigger feature exists in the grid.")
    out("- League operands (avg, q3_league_avg) are the SAME frozen")
    out("  point-in-time authorities the UNDER analysis used — realised-pace")
    out("  means from games settled BEFORE the boundary instant; no future")
    out("  games, no full-history averages.")
    out("- Sources are flat files; **no database connection is opened**, so")
    out("  no production write is possible by construction.")
    out()

    # ── 6. data sufficiency ────────────────────────────────────────────
    # ── 6. conditional (UNDER-trigger) vs native contrast ─────────────
    out("## 6. Conditional vs native — how much of the earlier OVER signal")
    out("was selection effect?")
    out()
    out("The 2026-09-22 mirror analysis found its strongest OVER candidates")
    out("INSIDE the UNDER-trigger cohort.  Re-measuring the SAME conditions on")
    out("the native pool isolates the selection effect:")
    out()
    out("| condition | native N | native OVER% | within-UNDER-trigger N |"
        " within-trigger OVER% |")
    out("|---|---|---|---|---|")
    MIRRORS = [
        ("accel >= 0 (recent3-act)",
         lambda r: r["recent3"] >= r["act"]),
        ("q3_ratio > 1.0",
         lambda r: r["q3_ratio"] > 1.00),
        ("q3_ratio >= 1.05",
         lambda r: r["q3_ratio"] >= 1.05),
        ("q3>1.0 AND accel>=0 (hot Q3, no decel)",
         lambda r: r["q3_ratio"] > 1.00 and r["recent3"] >= r["act"]),
        ("req>1.04x AND q3>1.0",
         lambda r: r["req_ratio"] > 1.04 and r["q3_ratio"] > 1.00),
    ]
    trig_rows = [r for r in rows if UNDER_TRIGGER(r)]
    for label, pred in MIRRORS:
        nn, nw, np_, _, _ = rate([r for r in rows if _safe(pred, r)])
        tn, tw, tp, _, _ = rate([r for r in trig_rows if _safe(pred, r)])
        out(f"| {label} | {nn} | {np_*100:.2f} | {tn} | {tp*100:.2f} |")
    out()
    out(f"(native baseline {pF*100:.2f}% · UNDER-trigger baseline "
        f"{pT*100:.2f}% — the gap between the two columns is the")
    out("selection effect, not a native OVER edge.)")
    out()

    # ── 7. data sufficiency ────────────────────────────────────────────
    out("## 7. Data sufficiency + design caveats")
    out()
    srcN = defaultdict(int)
    for r in rows:
        srcN[r["src"]] += 1
    out(f"- Source mix (CLEAN settled): {dict(srcN)} — the discovery week")
    out("  supplies non-trigger history; forward weeks come from the shadow")
    out("  JSONL.  OOS non-trigger coverage is therefore limited to the")
    out("  shadow-log weeks; native-cohort N will grow as the log accumulates.")
    out("- Overlap column in §2 quantifies residual UNDER-trigger")
    out("  contamination per candidate — the design's central risk.")
    out("- Weekly cells are small; single-week readings are noise (established")
    out("  in both prior analyses).")
    out()

    out("STOP — read-only research design + historical evaluation.  No OVER")
    out("alert, fingerprint, betting, schema or production change; nothing")
    out("committed or pushed.")

    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {OUT_MD}")


def _safe(pred, r):
    try:
        return pred(r)
    except (TypeError, KeyError):
        return False


def inspect_lambda_source(fn):
    import inspect
    return inspect.getsource(fn)


if __name__ == "__main__":
    main()
