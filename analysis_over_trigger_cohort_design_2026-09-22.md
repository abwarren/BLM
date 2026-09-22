# NATIVE OVER-SIDE TRIGGER COHORT — RESEARCH DESIGN + HISTORICAL EVALUATION
(read-only, 2026-09-22; flat-file sources only; no DB opened; no production change)

## 0. Research design (frozen before evaluation)

- **Pool:** EVERY CLEAN settled 75% boundary evaluation — trigger and
  non-trigger — from the identical CSV+JSONL merge the UNDER analysis
  used (last-wins on gid+captured_at).  This is the native OVER pool
  candidate: no production change, no new collection.
- **Candidate grid:** 21 definitions pre-registered below, covering
  actual pace vs league, required pace ladders (0.98/0.96/0.94/0.92/0.90),
  act+req combinations, Q3 vs league, recent3 vs act, and triples.
- **Selection criteria (frozen):** adequate N (>=50 full cohort),
  separation (lift >= +8pp AND Wilson CI excludes baseline),
  stability (data in >=3 weeks; OVER% >= baseline in >=2/3 of weeks
  with N>=10; no single week >60% of the candidate's N), OOS
  persistence (OOS OVER% >= OOS baseline +5pp with OOS N>=20).
- **No threshold is chosen because it maximises OVER%.**  Candidates
  are judged against ALL four criteria; the report ranks survivors.

## 1. Native pool composition (CLEAN settled, tier 75)

pool rows: **8609**  ·  weeks: 2026-W36, 2026-W37, 2026-W38, 2026-W39

| subset | N | OVER | OVER% | UNDER% | 95% CI |
|---|---|---|---|---|---|
| full pool | 8609 | 4070 | 47.28 | 52.72 | [46.2, 48.3] |
| UNDER production triggers | 281 | 111 | 39.50 | 60.50 | [34.0, 45.3] |
| non-trigger rows (native pool) | 8328 | 3959 | 47.54 | 52.46 | [46.5, 48.6] |

PUSH rows: 0 (0.00%).

IS/OOS: IS = 2026-W36 (N=370), OOS = 2026-W37, 2026-W38, 2026-W39 (N=8239).
baseline OVER% — full 47.28 · IS 51.08 · OOS 47.11

per-week pool:

| week | N | OVER | OVER% |
|---|---|---|---|
| 2026-W36 | 370 | 189 | 51.08 |
| 2026-W37 | 3378 | 1598 | 47.31 |
| 2026-W38 | 4133 | 1989 | 48.12 |
| 2026-W39 | 728 | 294 | 40.38 |

## 2. Candidate OVER trigger definitions — historical evaluation

lift vs the matching-basis baseline (full vs full, OOS vs OOS).

| candidate | N | OVER | OVER% | UNDER% | PUSH% | lift | 95% CI | size | OOS N | OOS OVER% | OOS lift | UNDER-trig overlap |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1  act > avg | 5321 | 2515 | 47.27 | 52.73 | 0.0 | -0.0 | [45.9, 48.6] | substantial | 5173 | 46.97 | -0.1 | 0% |
| A2  act/avg >= 1.05 | 3882 | 1843 | 47.48 | 52.52 | 0.0 | +0.2 | [45.9, 49.0] | substantial | 3778 | 47.17 | +0.1 | 0% |
| B1  req < avg            (req<1.00x) | 7409 | 3603 | 48.63 | 51.37 | 0.0 | +1.4 | [47.5, 49.8] | substantial | 7220 | 48.45 | +1.3 | 0% |
| B2  req_ratio <= 0.98 | 7012 | 3415 | 48.70 | 51.30 | 0.0 | +1.4 | [47.5, 49.9] | substantial | 6829 | 48.53 | +1.4 | 0% |
| B3  req_ratio <= 0.96 | 6489 | 3196 | 49.25 | 50.75 | 0.0 | +2.0 | [48.0, 50.5] | substantial | 6317 | 49.09 | +2.0 | 0% |
| B4  req_ratio <= 0.94 | 5918 | 2922 | 49.37 | 50.63 | 0.0 | +2.1 | [48.1, 50.6] | substantial | 5760 | 49.18 | +2.1 | 0% |
| B5  req_ratio <= 0.92 | 5347 | 2674 | 50.01 | 49.99 | 0.0 | +2.7 | [48.7, 51.3] | substantial | 5201 | 49.80 | +2.7 | 0% |
| B6  req_ratio <= 0.90 | 4662 | 2352 | 50.45 | 49.55 | 0.0 | +3.2 | [49.0, 51.9] | substantial | 4540 | 50.18 | +3.1 | 0% |
| C1  act>avg AND req<avg | 4483 | 2191 | 48.87 | 51.13 | 0.0 | +1.6 | [47.4, 50.3] | substantial | 4364 | 48.53 | +1.4 | 0% |
| C2  act>avg AND req<=0.96x | 3709 | 1844 | 49.72 | 50.28 | 0.0 | +2.4 | [48.1, 51.3] | substantial | 3602 | 49.42 | +2.3 | 0% |
| C3  act/avg>=1.05 AND req<=0.96x | 2506 | 1266 | 50.52 | 49.48 | 0.0 | +3.2 | [48.6, 52.5] | substantial | 2431 | 50.19 | +3.1 | 0% |
| C4  act>avg AND req_ratio in [0.94,0.98] | 905 | 417 | 46.08 | 53.92 | 0.0 | -1.2 | [42.9, 49.3] | substantial | 890 | 45.84 | -1.3 | 0% |
| D1  q3_ratio > 1.00 | 4294 | 2067 | 48.14 | 51.86 | 0.0 | +0.9 | [46.6, 49.6] | substantial | 4094 | 47.63 | +0.5 | 3% |
| D2  q3_ratio >= 1.05 | 3298 | 1606 | 48.70 | 51.30 | 0.0 | +1.4 | [47.0, 50.4] | substantial | 3148 | 48.03 | +0.9 | 3% |
| E1  recent3 > act | 2325 | 1148 | 49.38 | 50.62 | 0.0 | +2.1 | [47.3, 51.4] | substantial | 2164 | 48.89 | +1.8 | 6% |
| E2  recent3-act >= +0.5 | 1471 | 743 | 50.51 | 49.49 | 0.0 | +3.2 | [48.0, 53.1] | substantial | 1358 | 50.07 | +3.0 | 7% |
| F1  act>avg AND q3>1.0 | 3557 | 1698 | 47.74 | 52.26 | 0.0 | +0.5 | [46.1, 49.4] | substantial | 3456 | 47.45 | +0.3 | 0% |
| F2  act>avg AND accel>=0 | 1438 | 711 | 49.44 | 50.56 | 0.0 | +2.2 | [46.9, 52.0] | substantial | 1381 | 49.24 | +2.1 | 0% |
| F3  req<=0.96x AND q3>1.0 | 2884 | 1430 | 49.58 | 50.42 | 0.0 | +2.3 | [47.8, 51.4] | substantial | 2803 | 49.30 | +2.2 | 0% |
| F4  act>avg AND req<=0.96x AND q3>1.0 | 2335 | 1155 | 49.46 | 50.54 | 0.0 | +2.2 | [47.4, 51.5] | substantial | 2265 | 49.14 | +2.0 | 0% |
| F5  act>avg AND req<avg AND accel>=0 | 1250 | 629 | 50.32 | 49.68 | 0.0 | +3.0 | [47.6, 53.1] | substantial | 1206 | 50.08 | +3.0 | 0% |

## 3. Weekly distribution per candidate (stability evidence)

- **A1  act > avg** — 2026-W36: N=148 OV=57% · 2026-W37: N=2139 OV=48% · 2026-W38: N=2601 OV=47% · 2026-W39: N=433 OV=39%
- **A2  act/avg >= 1.05** — 2026-W36: N=104 OV=59% · 2026-W37: N=1526 OV=49% · 2026-W38: N=1921 OV=47% · 2026-W39: N=331 OV=41%
- **B1  req < avg            (req<1.00x)** — 2026-W36: N=189 OV=56% · 2026-W37: N=2967 OV=49% · 2026-W38: N=3622 OV=49% · 2026-W39: N=631 OV=42%
- **B2  req_ratio <= 0.98** — 2026-W36: N=183 OV=55% · 2026-W37: N=2841 OV=49% · 2026-W38: N=3403 OV=49% · 2026-W39: N=585 OV=42%
- **B3  req_ratio <= 0.96** — 2026-W36: N=172 OV=55% · 2026-W37: N=2650 OV=50% · 2026-W38: N=3140 OV=49% · 2026-W39: N=527 OV=42%
- **B4  req_ratio <= 0.94** — 2026-W36: N=158 OV=56% · 2026-W37: N=2419 OV=50% · 2026-W38: N=2857 OV=50% · 2026-W39: N=484 OV=43%
- **B5  req_ratio <= 0.92** — 2026-W36: N=146 OV=58% · 2026-W37: N=2189 OV=51% · 2026-W38: N=2584 OV=50% · 2026-W39: N=428 OV=42%
- **B6  req_ratio <= 0.90** — 2026-W36: N=122 OV=61% · 2026-W37: N=1914 OV=51% · 2026-W38: N=2250 OV=51% · 2026-W39: N=376 OV=42%
- **C1  act>avg AND req<avg** — 2026-W36: N=119 OV=61% · 2026-W37: N=1847 OV=50% · 2026-W38: N=2168 OV=49% · 2026-W39: N=349 OV=42%
- **C2  act>avg AND req<=0.96x** — 2026-W36: N=107 OV=60% · 2026-W37: N=1591 OV=51% · 2026-W38: N=1750 OV=49% · 2026-W39: N=261 OV=41%
- **C3  act/avg>=1.05 AND req<=0.96x** — 2026-W36: N=75 OV=61% · 2026-W37: N=1080 OV=52% · 2026-W38: N=1176 OV=49% · 2026-W39: N=175 OV=47%
- **C4  act>avg AND req_ratio in [0.94,0.98]** — 2026-W36: N=15 OV=60% · 2026-W37: N=336 OV=45% · 2026-W38: N=468 OV=48% · 2026-W39: N=86 OV=41%
- **D1  q3_ratio > 1.00** — 2026-W36: N=200 OV=58% · 2026-W37: N=1646 OV=49% · 2026-W38: N=2081 OV=48% · 2026-W39: N=367 OV=38%
- **D2  q3_ratio >= 1.05** — 2026-W36: N=150 OV=63% · 2026-W37: N=1263 OV=49% · 2026-W38: N=1615 OV=48% · 2026-W39: N=270 OV=42%
- **E1  recent3 > act** — 2026-W36: N=161 OV=56% · 2026-W37: N=1275 OV=52% · 2026-W38: N=758 OV=45% · 2026-W39: N=131 OV=42%
- **E2  recent3-act >= +0.5** — 2026-W36: N=113 OV=56% · 2026-W37: N=818 OV=54% · 2026-W38: N=470 OV=45% · 2026-W39: N=70 OV=44%
- **F1  act>avg AND q3>1.0** — 2026-W36: N=101 OV=57% · 2026-W37: N=1380 OV=49% · 2026-W38: N=1774 OV=48% · 2026-W39: N=302 OV=38%
- **F2  act>avg AND accel>=0** — 2026-W36: N=57 OV=54% · 2026-W37: N=822 OV=53% · 2026-W38: N=478 OV=44% · 2026-W39: N=81 OV=44%
- **F3  req<=0.96x AND q3>1.0** — 2026-W36: N=81 OV=59% · 2026-W37: N=1188 OV=52% · 2026-W38: N=1390 OV=49% · 2026-W39: N=225 OV=40%
- **F4  act>avg AND req<=0.96x AND q3>1.0** — 2026-W36: N=70 OV=60% · 2026-W37: N=979 OV=52% · 2026-W38: N=1121 OV=48% · 2026-W39: N=165 OV=40%
- **F5  act>avg AND req<avg AND accel>=0** — 2026-W36: N=44 OV=57% · 2026-W37: N=738 OV=54% · 2026-W38: N=405 OV=44% · 2026-W39: N=63 OV=46%

## 4. Frozen selection criteria applied (not highest-OVER% picking)

| candidate | N>=50 | lift>=+8 & CI excl. | weeks>=3 & stable | OOS persist (N>=20, +5pp) | verdict |
|---|---|---|---|---|---|
| A1  act > avg | True | False | True (4wk, maxwk 49%) | False | **rejected** |
| A2  act/avg >= 1.05 | True | False | False (4wk, maxwk 49%) | False | **rejected** |
| B1  req < avg            (req<1.00x) | True | False | True (4wk, maxwk 49%) | False | **rejected** |
| B2  req_ratio <= 0.98 | True | False | True (4wk, maxwk 49%) | False | **rejected** |
| B3  req_ratio <= 0.96 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| B4  req_ratio <= 0.94 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| B5  req_ratio <= 0.92 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| B6  req_ratio <= 0.90 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| C1  act>avg AND req<avg | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| C2  act>avg AND req<=0.96x | True | False | True (4wk, maxwk 47%) | False | **rejected** |
| C3  act/avg>=1.05 AND req<=0.96x | True | False | True (4wk, maxwk 47%) | False | **rejected** |
| C4  act>avg AND req_ratio in [0.94,0.98] | True | False | False (4wk, maxwk 52%) | False | **rejected** |
| D1  q3_ratio > 1.00 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| D2  q3_ratio >= 1.05 | True | False | True (4wk, maxwk 49%) | False | **rejected** |
| E1  recent3 > act | True | False | False (4wk, maxwk 55%) | False | **rejected** |
| E2  recent3-act >= +0.5 | True | False | False (4wk, maxwk 56%) | False | **rejected** |
| F1  act>avg AND q3>1.0 | True | False | True (4wk, maxwk 50%) | False | **rejected** |
| F2  act>avg AND accel>=0 | True | False | False (4wk, maxwk 57%) | False | **rejected** |
| F3  req<=0.96x AND q3>1.0 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| F4  act>avg AND req<=0.96x AND q3>1.0 | True | False | True (4wk, maxwk 48%) | False | **rejected** |
| F5  act>avg AND req<avg AND accel>=0 | True | False | False (4wk, maxwk 59%) | False | **rejected** |

## 5. Leakage audit

- AST audit over 21 candidate predicates: PASS — only whitelisted point-in-time operands referenced
- Label (outcome/final total) never appears in any predicate; it is
  used only to grade.  No closing line, Q4 split, settlement or
  post-trigger feature exists in the grid.
- League operands (avg, q3_league_avg) are the SAME frozen
  point-in-time authorities the UNDER analysis used — realised-pace
  means from games settled BEFORE the boundary instant; no future
  games, no full-history averages.
- Sources are flat files; **no database connection is opened**, so
  no production write is possible by construction.

## 6. Conditional vs native — how much of the earlier OVER signal
was selection effect?

The 2026-09-22 mirror analysis found its strongest OVER candidates
INSIDE the UNDER-trigger cohort.  Re-measuring the SAME conditions on
the native pool isolates the selection effect:

| condition | native N | native OVER% | within-UNDER-trigger N | within-trigger OVER% |
|---|---|---|---|---|
| accel >= 0 (recent3-act) | 2364 | 49.28 | 135 | 53.33 |
| q3_ratio > 1.0 | 4294 | 48.14 | 133 | 53.38 |
| q3_ratio >= 1.05 | 3298 | 48.70 | 107 | 56.07 |
| q3>1.0 AND accel>=0 (hot Q3, no decel) | 1348 | 50.67 | 83 | 62.65 |
| req>1.04x AND q3>1.0 | 452 | 44.47 | 133 | 53.38 |

(native baseline 47.28% · UNDER-trigger baseline 39.50% — the gap between the two columns is the
selection effect, not a native OVER edge.)

## 7. Data sufficiency + design caveats

- Source mix (CLEAN settled): {'jsonl': 8608, 'csv': 1} — the discovery week
  supplies non-trigger history; forward weeks come from the shadow
  JSONL.  OOS non-trigger coverage is therefore limited to the
  shadow-log weeks; native-cohort N will grow as the log accumulates.
- Overlap column in §2 quantifies residual UNDER-trigger
  contamination per candidate — the design's central risk.
- Weekly cells are small; single-week readings are noise (established
  in both prior analyses).

STOP — read-only research design + historical evaluation.  No OVER
alert, fingerprint, betting, schema or production change; nothing
committed or pushed.

---

# VERDICT — NATIVE OVER COHORT DESIGN (2026-09-22)

## Candidate definitions + evidence (all read-only, point-in-time-safe)

**Statistically separable but BELOW the frozen separation bar (CI excludes
baseline, lift < +8pp):**
| definition | N | OVER% | lift | OOS OVER% (N) | weekly pattern |
|---|---|---|---|---|---|
| B6 req_ratio <= 0.90 | 4662 | 50.45 | +3.2 | 50.18 (N=4540) | 61/51/51/42 — above baseline 3 of 4 weeks |
| C3 act/avg>=1.05 AND req<=0.96x | 2506 | 50.52 | +3.2 | 50.19 (N=2431) | 61/52/49/47 |
| E2 recent3-act >= +0.5 | 1471 | 50.51 | +3.2 | 50.07 (N=1358) | 56/54/45/44 |
| F5 act>avg & req<avg & accel>=0 | 1250 | 50.32 | +3.0 | 50.08 (N=1206) | 57/54/44/46 |
| B3 req_ratio <= 0.96 | 6489 | 49.25 | +2.0 | 49.09 (N=6317) | 55/50/49/42 |

**No separation (CI spans baseline):** A1 act>avg (+0.0), A2 act/avg>=1.05
(+0.2), D1/D2 q3_ratio>1.0 / >=1.05 (+0.9/+1.4), F1 (+0.5), and the C4
low-band combination (−1.2, anti-OVER).

**Applied to the frozen criteria (N>=50, lift>=+8 & CI-excludes, week
stability, OOS persistence >= +5pp with N>=20): ZERO candidates qualify.**
Every candidate passes N and most pass stability; every one fails the
separation and OOS-persistence bars.  Per the design freeze — no threshold
is selected for maximising OVER% — **no native OVER trigger definition is
recommended on this evidence.**

## The structural finding

The conditional-vs-native contrast (§6) shows most of the earlier
"OVER mirror" strength was **selection effect**: e.g. hot-Q3+no-decel is
62.65% inside the UNDER trigger (N=83) but 50.67% natively (N=1348);
q3>=1.05 is 56.07% vs 48.70%; req>1.04x & q3>1.0 actually inverts to
**44.47% natively (N=452) — below baseline**.  The native pool's baseline
(47.28%) sits ~8pp above the UNDER-trigger cohort's (39.50%); conditions
that merely look neutral inside the suppressed cohort mechanically show
"lift" there.

## What a native cohort still needs (design conclusion)

1. **The infrastructure already exists** — the shadow fingerprint log
   records every 75% evaluation, so a native OVER cohort can be maintained
   by logging alone (no production change).
2. The pre-registered pace/Q3/momentum grid tops out at **+3.2pp** natively
   — real but far too weak for a trigger whose alerts would fire on
   ~thousands of rows/week (B6 alone covers 54% of all evaluations).
   Any viable OVER trigger would need stronger conditioning not expressible
   from the six frozen features alone (e.g. market-line interaction), or an
   entirely different boundary (e.g. Q3-break geometry).
3. Recommendation embedded in the design: **keep the native pool
   accumulating; re-run this exact frozen grid monthly; revisit only if a
   candidate's OOS lift approaches +5–8pp with stability.**  Nothing to
   implement today.

**STOP — research design + historical analysis complete.  No production
code, alert, fingerprint, betting, schema or service touched; nothing
committed or pushed.**
