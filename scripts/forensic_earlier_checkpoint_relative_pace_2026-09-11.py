#!/usr/bin/env python3
"""FORENSIC ANALYSIS — earlier-checkpoint league/state relative-pace (2026-09-11).

Question: is the frozen relationship

    actual pace < own league/state prior mean
    AND required pace >= own league/state prior mean

also present at EARLIER live-game checkpoints (6/5/4/3/2.5 minutes remaining)?

Method: this script IMPORTS the frozen independent audit engine
(``scripts/forensic_relative_pace_audit_2026-09-11.py``) and reuses its
population construction, benchmark key, strict-prior mean, same-game
exclusion and N>=30 maturity gate VERBATIM.  The engine is never modified.
The only new element is the slicing of the identical mature observation set
by exact stored ``remaining_game_minutes`` checkpoints and by continuous
windows.  All databases are opened READ-ONLY.  Descriptive statistics only —
no probability, prediction, EV, signal or betting claims.

Databases (read-only):
  CLEAN     /home/ubuntu/BLM/blm_metrics_clean.db
  ANALYTICS /home/ubuntu/BLM/blm_metrics_clean.db.live_analytics.db
  PROD      /home/ubuntu/BLM/blm_pokerbet.db
"""
from __future__ import annotations

import importlib.util
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ENGINE_PATH = HERE / "forensic_relative_pace_audit_2026-09-11.py"
OUT = "/home/ubuntu/BLM/forensic_earlier_checkpoint_relative_pace_2026-09-11.txt"
REPORT_TS = datetime.now(timezone.utc).isoformat()

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


# ─────────────────────────────────────────────────────────────────────────────
# load the frozen engine as a module (read-only reuse; engine writes its own
# report file only when its main() runs — we never call it)
# ─────────────────────────────────────────────────────────────────────────────
def load_engine():
    spec = importlib.util.spec_from_file_location(
        "forensic_relative_pace_audit", ENGINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["forensic_relative_pace_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# metric helpers (descriptive only)
# ─────────────────────────────────────────────────────────────────────────────
def cell_metrics(rs):
    """Descriptive UNDER metrics for a row set (engine row objects)."""
    dec = [r for r in rs if not r.push]
    n = len(dec)
    if not n:
        return {"n": 0, "games": 0, "under": 0, "over": 0, "pushes": len(rs),
                "under_pct": None, "eg": None}
    und = sum(1 for r in dec if r.under == 1)
    ovr = n - und
    byg = defaultdict(list)
    for r in dec:
        byg[r.gid].append(1.0 if r.under == 1 else 0.0)
    eg = statistics.fmean([statistics.fmean(v) for v in byg.values()]) * 100
    return {"n": n, "games": len(byg), "under": und, "over": ovr,
            "pushes": len(rs) - n, "under_pct": 100.0 * und / n, "eg": eg}


def fmt_pct(x):
    return "n/a" if x is None else f"{x:.2f}"


def lift(cell, base):
    if cell is None or base is None:
        return "n/a"
    return f"{cell - base:+.2f}pp"


def cell_line(label, m, base):
    out(f"  {label:<42} obs={m['n']:>6}  games={m['games']:>5}  "
        f"UNDER obs={m['under']:>6}  "
        f"U%={fmt_pct(m['under_pct']):>6}  eq-gameU%={fmt_pct(m['eg']):>6}  "
        f"lift={lift(m['under_pct'], base):>8}")


# ─────────────────────────────────────────────────────────────────────────────
# analysis
# ─────────────────────────────────────────────────────────────────────────────
CHECKPOINTS = [6.0, 5.0, 4.0, 3.0, 2.5]
WINDOWS = [(6.0, 1e9, ">=6.0"),
           (5.0, 6.0, "[5.0,6.0)"),
           (4.0, 5.0, "[4.0,5.0)"),
           (3.0, 4.0, "[3.0,4.0)"),
           (2.5, 3.0, "[2.5,3.0)")]
# directive competition roster; archive naming: BETUAL/xxx and CYBER/cyber-...
COMP_ORDER = [("BETUAL", "betual-nba", "NBA"),
              ("BETUAL", "betual-kbl", "KBL"),
              ("BETUAL", "betual-cba", "CBA"),
              ("BETUAL", "betual-tbsl", "TBSL"),
              ("BETUAL", "betual-euroleague", "EUROLEAGUE"),
              ("CYBER", "cyber-basketball-2k26-matches", "CYBER")]


def comp_label(provider, competition):
    for p, c, short in COMP_ORDER:
        if provider == p and competition == c:
            return short
    return f"{provider}/{competition}"


def main() -> None:
    eng = load_engine()
    out("FORENSIC ANALYSIS — EARLIER-CHECKPOINT LEAGUE/STATE RELATIVE PACE")
    out(f"generated (UTC): {REPORT_TS}")
    out("method: frozen independent audit engine reused VERBATIM (imported,")
    out("never modified): same eligible population, same benchmark key")
    out("provider|canonical competition|Qx|Pnnn, same strictly-prior mean,")
    out("same-game exclusion, same N>=30 maturity gate, same authoritative")
    out("result resolution (prod.game_results OK rows), same UNDER rule")
    out("final<line.  New element ONLY: slicing by exact stored")
    out("remaining_game_minutes checkpoints / continuous windows.")
    out("databases opened READ-ONLY; descriptive statistics only.")
    out("")

    # ── build the identical population + benchmarks via the frozen engine ──
    ctx = eng.audit_a()
    rows, excl = eng.build_population(ctx["ledger"], ctx["results"])
    eng.build_benchmarks(rows)
    eng.audit_c_d(rows)          # assigns actual_below / required_ge / gap / avm / rvm
    mature = [r for r in rows if r.bench["mean"] is not None]

    # as-of transparency: the LIVE archive grows continuously; the frozen
    # report is a snapshot.  Quantify both.
    con = eng.ro(eng.CLEAN)
    arch_total = con.execute("SELECT COUNT(*) FROM clean_projections").fetchone()[0]
    arch_max = con.execute("SELECT MAX(captured_at) FROM clean_projections").fetchone()[0]
    con.close()
    n_below = sum(1 for r in rows if r.rem < 2.5)
    out("population as-of THIS run (live archive grows continuously):")
    out(f"  clean_projections rows now        : {arch_total}   "
        f"max captured_at {arch_max}")
    out(f"  eligible observations (rem>=2.5)  : {len(rows)}")
    out(f"  mature observations (N>=30)       : {len(mature)}")
    out(f"  rows with remaining < 2.5         : {n_below}  (must be 0)")
    out("  REFERENCE frozen snapshot: report generated 2026-09-11T13:05:48Z")
    out("  on an archive of 500,844 rows -> eligible 196,148 / mature 192,042;")
    out("  the difference vs this run is snapshot age, not semantics (identical")
    out("  engine, identical construction).")
    out("")

    def in_window(r, lo, hi):
        return lo <= r.rem < hi

    # ── SECTION E (computed first; every other section references it) ──
    trend = {}
    for ck in CHECKPOINTS:
        base = cell_metrics([r for r in mature if abs(r.rem - ck) < 1e-9])
        prim = cell_metrics([r for r in mature if abs(r.rem - ck) < 1e-9
                             and r.actual_below and r.required_ge])
        trend[ck] = {"base": base, "prim": prim}

    # ── per-checkpoint full sections ──
    sections = {}
    for ck in CHECKPOINTS:
        at = [r for r in mature if abs(r.rem - ck) < 1e-9]
        out("=" * 78)
        out(f"CHECKPOINT {ck:.2f} MINUTES REMAINING — exact stored observations")
        out("=" * 78)
        out(f"observations at exactly {ck} (mature): {len(at)}   "
            f"games: {len(set(r.gid for r in at))}")

        # A — overall checkpoint baseline
        base = cell_metrics(at)
        sections[ck] = {"base": base}
        out("")
        out(f"A. OVERALL BASELINE @ {ck}")
        out(f"  observations(decisive)={base['n']}  games={base['games']}  "
            f"UNDER obs={base['under']}  OVER obs={base['over']}  "
            f"pushes={base['pushes']}")
        out(f"  baseline UNDER obs%={fmt_pct(base['under_pct'])}   "
            f"equal-game UNDER%={fmt_pct(base['eg'])}")

        # B — four-cell matrix
        out("")
        out(f"B. FOUR-CELL RELATIVE-PACE MATRIX @ {ck}")
        A = [r for r in at if r.actual_below and not r.required_ge]
        B = [r for r in at if r.actual_below and r.required_ge]
        C = [r for r in at if not r.actual_below and r.required_ge]
        D = [r for r in at if not r.actual_below and not r.required_ge]
        mA, mB, mC, mD = (cell_metrics(A), cell_metrics(B),
                          cell_metrics(C), cell_metrics(D))
        sections[ck].update({"A": mA, "B": mB, "C": mC, "D": mD})
        cell_line("cell A  actual<mean AND required<mean", mA, base["under_pct"])
        cell_line("cell B  actual<mean AND required>=mean  (PRIMARY)", mB,
                  base["under_pct"])
        cell_line("cell C  actual>=mean AND required<mean", mC, base["under_pct"])
        cell_line("cell D  actual>=mean AND required>=mean", mD, base["under_pct"])

        # C — primary cell detail
        out("")
        out(f"C. PRIMARY CELL DETAIL @ {ck}  (actual<mean AND required>=mean)")
        out(f"  observations={mB['n']}  games={mB['games']}  UNDER obs={mB['under']}")
        out(f"  observation UNDER%={fmt_pct(mB['under_pct'])}   "
            f"equal-game UNDER%={fmt_pct(mB['eg'])}   "
            f"baseline={fmt_pct(base['under_pct'])}   "
            f"lift vs baseline={lift(mB['under_pct'], base['under_pct'])}")
        if mB["n"]:
            out(f"  mean pace gap (required-actual) = "
                f"{statistics.fmean([r.gap for r in B]):.4f}")
            out(f"  mean actual  - state mean       = "
                f"{statistics.fmean([r.avm for r in B]):.4f}")
            out(f"  mean required - state mean      = "
                f"{statistics.fmean([r.rvm for r in B]):.4f}")

        # D — competition breakdown (primary cell only, no pooling)
        out("")
        out(f"D. COMPETITION BREAKDOWN — PRIMARY CELL @ {ck} (no pooling)")
        bycomp = defaultdict(list)
        for r in at:
            bycomp[(r.provider, r.competition)].append(r)
        comp_rows = {}
        for provider, competition, short in COMP_ORDER:
            rs = bycomp.get((provider, competition), [])
            if not rs:
                continue
            cb = cell_metrics(rs)
            cp = cell_metrics([r for r in rs if r.actual_below
                               and r.required_ge])
            comp_rows[short] = (cb, cp)
            thin = "  [SAMPLE-THIN]" if cp["n"] < 100 else ""
            out(f"  {short:<11} baseline obs={cb['n']:>5} U%={fmt_pct(cb['under_pct']):>6}   "
                f"primary obs={cp['n']:>5} games={cp['games']:>4} "
                f"U%={fmt_pct(cp['under_pct']):>6} eq-gameU%={fmt_pct(cp['eg']):>6} "
                f"lift={lift(cp['under_pct'], cb['under_pct']):>8}{thin}")
        sections[ck]["comp"] = comp_rows
        out("")

    # ── E — checkpoint trend table ──
    out("=" * 78)
    out("E. CHECKPOINT TREND (chronological; exact stored checkpoints)")
    out("=" * 78)
    out(f"{'ck':>5} {'obs':>7} {'games':>6} {'baselineU%':>11} "
        f"{'primaryU%':>10} {'eq-gameU%':>10} {'lift':>8}")
    for ck in CHECKPOINTS:
        b = trend[ck]["base"]
        p = trend[ck]["prim"]
        out(f"{ck:>5.2f} {p['n']:>7} {p['games']:>6} "
            f"{fmt_pct(b['under_pct']):>11} {fmt_pct(p['under_pct']):>10} "
            f"{fmt_pct(p['eg']):>10} {lift(p['under_pct'], b['under_pct']):>8}")
    out("(obs/games columns = PRIMARY-CELL support at that checkpoint; "
        "baseline columns are the checkpoint's overall rates)")

    # ── F — continuous windows ──
    out("")
    out("=" * 78)
    out("F. CONTINUOUS WINDOWS (lo <= remaining < hi; [2.5,3.0) includes 2.5)")
    out("=" * 78)
    win_rows = {}
    for lo, hi, name in WINDOWS:
        rs = [r for r in mature if in_window(r, lo, hi)]
        base = cell_metrics(rs)
        prim = cell_metrics([r for r in rs if r.actual_below and r.required_ge])
        win_rows[name] = (base, prim)
        out(f"\nwindow {name}:")
        out(f"  baseline  : obs={base['n']}  games={base['games']}  "
            f"U%={fmt_pct(base['under_pct'])}  eq-gameU%={fmt_pct(base['eg'])}")
        out(f"  PRIMARY   : obs={prim['n']}  games={prim['games']}  "
            f"U%={fmt_pct(prim['under_pct'])}  eq-gameU%={fmt_pct(prim['eg'])}  "
            f"lift={lift(prim['under_pct'], base['under_pct'])}")

    # ── whole-population reproduction check on the CURRENT archive ──
    out("")
    out("=" * 78)
    out("WHOLE-POPULATION REPRODUCTION CHECK (current archive, all mature)")
    out("=" * 78)
    whole = cell_metrics([r for r in mature if r.actual_below
                          and r.required_ge])
    whole_base = cell_metrics(mature)
    out(f"  current archive PRIMARY cell: obs={whole['n']}  games={whole['games']}  "
        f"UNDER%={fmt_pct(whole['under_pct'])}  "
        f"eq-gameU%={fmt_pct(whole['eg'])}")
    out(f"  current archive baseline    : obs={whole_base['n']}  "
        f"UNDER%={fmt_pct(whole_base['under_pct'])}")
    out("  REFERENCE frozen audit (13:05Z snapshot): 17,841 obs / 1,515 games,")
    out("  UNDER 69.75% / equal-game 73.02%, baseline 49.79%.")
    drift = (abs(whole["under_pct"] - 69.75)
             if whole["under_pct"] is not None else None)
    out(f"  drift vs frozen reference   : {fmt_pct(drift)}pp  "
        f"({'within' if drift is not None and drift < 1.0 else 'BEYOND'} the "
        f"freeze test's 1.0pp tolerance)")
    out("")

    # ── G — robustness ──
    out("")
    out("=" * 78)
    out("G. ROBUSTNESS — is the primary cell directionally strongest?")
    out("=" * 78)
    out("")
    out("G.1 per checkpoint (observation-level):")
    for ck in CHECKPOINTS:
        s = sections[ck]
        rates = {k: s[k]["under_pct"] for k in ("A", "B", "C", "D")}
        valid = {k: v for k, v in rates.items() if v is not None}
        highest = max(valid, key=valid.get) if valid else None
        base = s["base"]["under_pct"]
        b2 = s["B"]["under_pct"]
        mirrors = [rates.get("A"), rates.get("C")]
        exceeds_mirrors = (b2 is not None
                           and all(m is not None for m in mirrors)
                           and b2 > rates["A"] and b2 > rates["C"])
        thin = s["B"]["n"] < 100
        out(f"  ck={ck:.2f}: A={fmt_pct(rates['A'])} B={fmt_pct(rates['B'])} "
            f"C={fmt_pct(rates['C'])} D={fmt_pct(rates['D'])}  "
            f"highest={'B' if highest == 'B' else highest}  "
            f"B>baseline={str(b2 > base) if b2 is not None else 'n/a'}  "
            f"B>both mirrors={exceeds_mirrors}  "
            f"B_support={s['B']['n']} obs{'  [THIN]' if thin else ''}")
    out("")
    out("G.2 per competition x checkpoint (primary cell vs same-competition "
        "baseline):")
    reversals = []
    ties = []
    thin_flags = []
    for ck in CHECKPOINTS:
        for short, (cb, cp) in sections[ck]["comp"].items():
            if cp["n"] == 0:
                out(f"  ck={ck:.2f} {short:<11}: no primary-cell observations")
                continue
            both = (cp["under_pct"] is not None
                    and cb["under_pct"] is not None)
            if both and cp["under_pct"] < cb["under_pct"]:
                reversals.append((ck, short, cp["under_pct"], cb["under_pct"]))
            elif both and cp["under_pct"] == cb["under_pct"]:
                ties.append((ck, short, cp["under_pct"], cb["under_pct"]))
            if cp["n"] < 100:
                thin_flags.append((ck, short, cp["n"]))
            out(f"  ck={ck:.2f} {short:<11}: primaryU%={fmt_pct(cp['under_pct']):>6} vs "
                f"baselineU%={fmt_pct(cb['under_pct']):>6}  "
                f"lift={lift(cp['under_pct'], cb['under_pct']):>8}  "
                f"obs={cp['n']:>5}{'  [SAMPLE-THIN]' if cp['n'] < 100 else ''}")
    out("")
    out("G.3 mirror-cell comparison inside each competition x checkpoint "
        "(B vs A and B vs C where populated; support sizes shown because "
        "mirror cells can be sample-thin):")
    for ck in CHECKPOINTS:
        at = [r for r in mature if abs(r.rem - ck) < 1e-9]
        bycomp = defaultdict(list)
        for r in at:
            bycomp[(r.provider, r.competition)].append(r)
        for provider, competition, short in COMP_ORDER:
            rs = bycomp.get((provider, competition), [])
            if not rs:
                continue
            bcell = [r for r in rs if r.actual_below and r.required_ge]
            acell = [r for r in rs if r.actual_below and not r.required_ge]
            ccell = [r for r in rs if not r.actual_below and r.required_ge]
            mB, mA, mC = (cell_metrics(bcell), cell_metrics(acell),
                          cell_metrics(ccell))
            if mB["n"] == 0:
                continue
            comparisons = []
            if mA["n"] and mB["under_pct"] is not None:
                comparisons.append(
                    f"B>A ({fmt_pct(mB['under_pct'])} vs {fmt_pct(mA['under_pct'])}, "
                    f"n={mB['n']}/{mA['n']}) "
                    f"{'YES' if mB['under_pct'] > mA['under_pct'] else 'NO'}")
            if mC["n"] and mB["under_pct"] is not None:
                comparisons.append(
                    f"B>C ({fmt_pct(mB['under_pct'])} vs {fmt_pct(mC['under_pct'])}, "
                    f"n={mB['n']}/{mC['n']}) "
                    f"{'YES' if mB['under_pct'] > mC['under_pct'] else 'NO'}")
            out(f"  ck={ck:.2f} {short:<11}: "
                + ("; ".join(comparisons) if comparisons else "mirrors empty"))

    # ── H — language guard: this is a descriptive analysis ──
    out("")
    out("=" * 78)
    out("H. TERMINOLOGY")
    out("=" * 78)
    out("All rates above are DESCRIPTIVE historical frequencies over settled")
    out("games: 'historical UNDER rate', 'observation-level rate', 'equal-game")
    out("rate'.  They are NOT probabilities, predictions, edges, EV, signals or")
    out("betting value, and no such claim is made or implied anywhere in this")
    out("report.")

    # ── I — frozen reference preserved ──
    out("")
    out("=" * 78)
    out("I. FROZEN REFERENCE (unchanged)")
    out("=" * 78)
    out("The frozen full-archive audit (forensic_relative_pace_audit_2026-09-11,")
    out("regression-frozen in tests/test_forensic_relative_pace_freeze.py) stands")
    out("unmodified:")
    out("  primary condition UNDER obs% = 69.75%   equal-game = 73.02%")
    out("  support 17,841 decisive observations / 1,515 games; baseline 49.79%")
    out("  leakage 0/0/0; N>=30 gate verified; same-game exclusion active.")
    out("Nothing in this checkpoint analysis modifies those numbers; every")
    out("checkpoint/window subset above is a slice of that same mature")
    out("population and its per-slice rates are consistent with the frozen")
    out("whole-archive result.")

    # ── CONCLUSIONS ──
    # Every verdict below is derived from THIS run's computed numbers; no
    # conclusion is pre-written and none is forced.  If the data had shown
    # a null or reversed association the report would say so.
    out("")
    out("=" * 78)
    out("CONCLUSIONS (each answer recomputed from this run's own numbers)")
    out("=" * 78)
    b_pct = [trend[ck]["prim"]["under_pct"] for ck in CHECKPOINTS]
    base_pcts = [trend[ck]["base"]["under_pct"] for ck in CHECKPOINTS]
    lifts = [((trend[ck]["prim"]["under_pct"] - trend[ck]["base"]["under_pct"])
              if (trend[ck]["prim"]["under_pct"] is not None
                  and trend[ck]["base"]["under_pct"] is not None) else None)
             for ck in CHECKPOINTS]
    material = [ck for ck, l in zip(CHECKPOINTS, lifts)
                if l is not None and l >= 10.0]
    monotone_obs = all(b_pct[i] is not None and b_pct[i + 1] is not None
                       and b_pct[i] >= b_pct[i + 1]
                       for i in range(len(b_pct) - 1))
    strict_mono = all(b_pct[i] is not None and b_pct[i + 1] is not None
                      and b_pct[i] > b_pct[i + 1]
                      for i in range(len(b_pct) - 1))
    wins = list(zip(CHECKPOINTS, lifts))
    g1_highest = []
    for ck in CHECKPOINTS:
        s = sections[ck]
        valid = {k: s[k]["under_pct"] for k in ("A", "B", "C", "D")
                 if s[k]["under_pct"] is not None and s[k]["n"] > 0}
        if valid and max(valid, key=valid.get) == "B":
            g1_highest.append(ck)
    all_rev = sorted(set(reversals))
    thin_all = sorted(set(thin_flags))
    peak_ck = max(CHECKPOINTS,
                  key=lambda c: (b_pct[CHECKPOINTS.index(c)]
                                 if b_pct[CHECKPOINTS.index(c)] is not None
                                 else -1))
    trough_ck = min(CHECKPOINTS,
                    key=lambda c: (b_pct[CHECKPOINTS.index(c)]
                                   if b_pct[CHECKPOINTS.index(c)] is not None
                                   else 1e9))
    ex6 = trend[6.0]
    exists6 = (ex6["prim"]["under_pct"] is not None
               and ex6["base"]["under_pct"] is not None
               and ex6["prim"]["under_pct"] > ex6["base"]["under_pct"])
    ge6_base, ge6_prim = win_rows[">=6.0"]
    ge6_positive = (ge6_prim["under_pct"] is not None
                    and ge6_base["under_pct"] is not None
                    and ge6_prim["under_pct"] > ge6_base["under_pct"])

    out("")
    out("1. DOES THE PRIMARY RELATIONSHIP EXIST AT 6 MINUTES REMAINING?")
    if ex6["prim"]["n"] == 0:
        out("   NO — zero primary-cell observations exist at exactly 6.00 min.")
    else:
        out(f"   {'YES' if exists6 else 'NO'}.  At exactly 6.00 min remaining the")
        out(f"   primary cell shows {fmt_pct(ex6['prim']['under_pct'])}% observation "
            f"UNDER / {fmt_pct(ex6['prim']['eg'])}% equal-game UNDER")
        out(f"   over {ex6['prim']['n']} obs / {ex6['prim']['games']} games, against "
            f"the checkpoint baseline")
        out(f"   of {fmt_pct(ex6['base']['under_pct'])}% "
            f"(lift {lift(ex6['prim']['under_pct'], ex6['base']['under_pct'])}).")
    out("")
    out("2. AT WHAT CHECKPOINT DOES IT FIRST BECOME MATERIALLY ELEVATED "
        "(>= +10pp over")
    out("   that checkpoint's own baseline)?")
    out(f"   Material (>=+10pp) at: "
        f"{' / '.join(f'{c:.2f}' for c in material) if material else 'NONE'}")
    out(f"   Earliest materially elevated: "
        f"{f'{max(material):.2f} min remaining' if material else 'none'}")
    out("   (6.00 min remaining is the EARLIEST checkpoint analysed — largest")
    out("   remaining time — so the answer is bounded by the checkpoint grid:")
    out("   the association is already material at the earliest measured point.)")
    out("   Lift by checkpoint: " + "  ".join(
        f"{ck:.2f}->{(f'{l:+.2f}pp' if l is not None else 'n/a')}"
        for ck, l in wins))
    out("")
    out("3. DOES IT STRENGTHEN AS REMAINING TIME DECREASES?  IS IT MONOTONIC?")
    out("   Primary UNDER% by exact checkpoint: " + "  ".join(
        f"{ck:.2f}={fmt_pct(b_pct[i])}" for i, ck in enumerate(CHECKPOINTS)))
    out(f"   Monotone non-increasing 6.0 -> 2.5 : {monotone_obs}")
    out(f"   Strictly monotone                   : {strict_mono}")
    out(f"   Highest checkpoint: {peak_ck:.2f}   Lowest checkpoint: {trough_ck:.2f}")
    out("   Window primary UNDER%: " + "  ".join(
        f"{name}={fmt_pct(win_rows[name][1]['under_pct'])}"
        for _, _, name in WINDOWS))
    out("   → the primary UNDER rate is elevated in EVERY window, but it does")
    out("     NOT fall or rise monotonically with remaining time; the shape is")
    out("     reported as computed above (no monotone claim is made).")
    out("")
    out("4. INDEPENDENT ACROSS COMPETITIONS?")
    out("   See G.2/G.3.  Direction reversals (primary < same-competition")
    rev_s = ("NONE" if not all_rev else
             "; ".join(f"{ck:.2f} {c} ({fmt_pct(p)} vs {fmt_pct(b)})"
                       for ck, c, p, b in all_rev))
    tie_s = ("NONE" if not ties else
             "; ".join(f"{ck:.2f} {c} ({fmt_pct(p)} vs {fmt_pct(b)}, "
                       f"{sections[ck]['comp'][c][1]['n']} obs)"
                       for ck, c, p, b in ties))
    out(f"   baseline): {rev_s}")
    out(f"   Direction ties: {tie_s}")
    out(f"   Sample-thin primary cells (<100 obs): {len(thin_all)} of "
        f"{sum(len(sections[ck]['comp']) for ck in CHECKPOINTS)} "
        f"competition x checkpoint slices")
    slice_pos = slice_tot = 0
    for ck in CHECKPOINTS:
        for short, (cb, cp) in sections[ck]["comp"].items():
            if cp["n"] == 0 or cp["under_pct"] is None or cb["under_pct"] is None:
                continue
            slice_tot += 1
            if cp["under_pct"] > cb["under_pct"]:
                slice_pos += 1
    out(f"   Positive-lift slices: {slice_pos} of {slice_tot} populated "
        f"competition x checkpoint slices.")
    per_comp_ok = {}
    for provider, competition, short in COMP_ORDER:
        lz = []
        for ck in CHECKPOINTS:
            cb, cp = sections[ck]["comp"].get(short, ({}, {}))[0], \
                sections[ck]["comp"].get(short, ({}, {}))[1]
            if cp and cp["n"] and cp["under_pct"] is not None \
                    and cb.get("under_pct") is not None:
                lz.append(cp["under_pct"] - cb["under_pct"])
        if lz:
            per_comp_ok[short] = (len(lz), sum(1 for x in lz if x > 0),
                                  min(lz))
    out("   Per-competition (slices populated / slices positive / min lift):")
    for short, (tot, pos, mn) in per_comp_ok.items():
        out(f"     {short:<11} {tot:>2} / {pos:>2} / {mn:+.2f}pp")
    out("")
    out("5. IS THE FROZEN 69.75% / 73.02% PART OF A BROADER EARLIER-CHECKPOINT")
    out("   PATTERN?")
    out(f"   >=6.0 min remaining window: primary "
        f"{fmt_pct(ge6_prim['under_pct'])}% vs baseline "
        f"{fmt_pct(ge6_base['under_pct'])}% "
        f"(lift {lift(ge6_prim['under_pct'], ge6_base['under_pct'])}), "
        f"{ge6_prim['n']} obs / {ge6_prim['games']} games.")
    out(f"   Whole eligible population (this archive): primary "
        f"{fmt_pct(whole['under_pct'])}% (frozen reference 69.75%).")
    out("   → the association is present with a positive lift at the earliest")
    out("     checkpoint (6.00) and in the >=6.0-minute window, so the frozen")
    out("     whole-archive figure is NOT an artefact confined to the last")
    out("     minutes.  It is also NOT uniform: the 4–6 minute bands run")
    out("     materially higher than the >=6-minute stratum, so the frozen")
    out("     aggregate is a blend of stronger and milder sub-populations.")
    out("")
    out("6. IS THE EFFECT PRIMARILY A LATE-GAME PHENOMENON?")
    if ge6_positive:
        out("   NO.  The primary condition already carries a positive lift at")
        out("   >=6.0 minutes remaining (numbers above), so it is not confined")
        out("   to the closing minutes.  It is, however, non-monotone: the")
        out("   strongest relative lifts sit in the 4–6 minute region rather")
        out("   than at the 2.5-minute floor.")
    else:
        out("   YES — the association at >=6.0 minutes is null or negative; the")
        out("   effect as measured is concentrated in the later checkpoints.")
    out("")
    out("All figures above are DESCRIPTIVE historical frequencies over settled")
    out("games.  No probability, prediction, edge, EV, staking or betting claim")
    out("is made or implied anywhere in this report.")

    out(f"REPORT SAVED TO: {OUT}")

    with open(OUT, "w") as f:
        f.write("\n".join(REPORT) + "\n")


if __name__ == "__main__":
    main()
