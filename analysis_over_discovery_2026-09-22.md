# OVER-SIDE INDICATOR DISCOVERY — 2026-09-22 (READ-ONLY)

Methodology: IDENTICAL to the UNDER fingerprint analysis
(oos_weekly_rolling_2026-09-21): cohort = discovery CSV + forward
shadow JSONL merged last-wins on (game_id, captured_at); CLEAN
(stale=false) production 75% triggers, settled; point-in-time
features only (req_ratio, recent3_minus_act, q3_ratio, line_move,
act_minus_req, score_differential). No inversion of UNDER rules:
both sides of every distribution were scanned for where OVER%
actually rises. No production code, DB, alert, fingerprint or
betting change — this script writes only this report.

## 1. BASELINE (CLEAN settled 75% triggers)

- cohort N = 281  (UNDER 170 = 60.50%, OVER 111 = 39.50%, PUSH 0 = 0.00%)
- baseline OVER% = 39.50%  (Wilson 95% CI 33.96%–45.32%)
- baseline UNDER% = 60.50%  (matches the UNDER analysis)
- production context: 13908 settled (OK) game_results rows in blm_pokerbet.db (read-only, query_only) — the 75% cohort is a production-trigger subset of this

IS/OOS split: discovery(csv) N=151 (OVER 47.68%)  |  forward(jsonl) N=130 (OVER 30.00%)

## 2. DISTRIBUTION SCANS (both sides; OVER% vs baseline 39.50%)

feature window                   N  OVER  UNDER  PUSH   OVER%    lift          95% CI         size
req_ratio in [0.80,0.90)         0     0      0     0       —       —               —         tiny
req_ratio in [0.90,1.00)         0     0      0     0       —       —               —         tiny
req_ratio in [1.00,1.04)         0     0      0     0       —       —               —         tiny
req_ratio in [1.04,1.10)        62    26     36     0   41.94    +2.4     [30.5,54.3]     moderate
req_ratio in [1.10,1.20) [C1]    17     2     15     0   11.76   -27.7      [3.3,34.3]         tiny
req_ratio >= 1.20              202    83    119     0   41.09    +1.6     [34.5,48.0]  substantial
q3_ratio < 0.90 [R2]            87    19     68     0   21.84   -17.7     [14.5,31.6]     moderate
q3_ratio in [0.90,1.00)         60    21     39     0   35.00    -4.5     [24.2,47.6]     moderate
q3_ratio in [1.00,1.10)         66    38     28     0   57.58   +18.1     [45.6,68.8]     moderate
q3_ratio >= 1.10                67    33     34     0   49.25    +9.8     [37.7,60.9]     moderate
recent3-act <= -1.0             76    18     58     0   23.68   -15.8     [15.5,34.4]     moderate
recent3-act in [-1.0,-0.5)      35    10     25     0   28.57   -10.9     [16.3,45.1]        small
recent3-act in [-0.5,0.0)       37    11     26     0   29.73    -9.8     [17.5,45.8]        small
recent3-act in [0.0,0.5)        36    22     14     0   61.11   +21.6     [44.9,75.2]        small
recent3-act in [0.5,1.0)        39    19     20     0   48.72    +9.2     [33.9,63.8]        small
recent3-act >= 1.0              60    31     29     0   51.67   +12.2     [39.3,63.8]     moderate
act-req <= -1.0                217    89    128     0   41.01    +1.5     [34.7,47.7]  substantial
act-req in [-1.0,0.0)           64    22     42     0   34.38    -5.1     [23.9,46.6]     moderate
act-req >= 0.0                   0     0      0     0       —       —               —         tiny
line_move < 0 (drifting down)   110    39     71     0   35.45    -4.0     [27.1,44.7]  substantial
line_move == 0                  88    42     46     0   47.73    +8.2     [37.6,58.0]     moderate
line_move > 0 (drifting up)     71    27     44     0   38.03    -1.5     [27.6,49.7]     moderate
diff < 0 (trailing)            114    47     67     0   41.23    +1.7     [32.6,50.4]  substantial
diff >= 0 (level/ahead)        165    63    102     0   38.18    -1.3     [31.1,45.8]  substantial

## 3. MIRROR CANDIDATES (structure-opposite to each UNDER rule)

candidate                          mirror of          N  OVER   OVER%    lift         size  OOS N  OOS OVER%
M-C1a req_ratio in [0.80,0.90)     C1 [1.10,1.20)     0     0       —       —         tiny      0          —
M-C1b req_ratio in [0.90,1.00)     C1 [1.10,1.20)     0     0       —       —         tiny      0          —
M-C2 recent3-act >= +0.5           C2 <= -0.5        99    50   50.51   +11.0     moderate     37      43.24
M-C3 req>1.04x AND q3>avg          C3               133    71   53.38   +13.9  substantial     46      39.13
M-C5 req>1.10x AND q3>avg          C5               116    61   52.59   +13.1  substantial     32      34.38
M-C6 accel>=0.5 AND q3>avg         C6                61    36   59.02   +19.5     moderate     17      47.06
M-R2 q3_ratio > 1.10               R2 < 0.90         67    33   49.25    +9.8     moderate     22      27.27
M-C4 [0.90,1.00) AND accel>=0.5    C4                 0     0       —       —         tiny      0          —

## 4. COMBINATION / INTERACTION SCANS

combination                            N  OVER   OVER%    lift          95% CI         size  OOS N  OOS OVER%
q3>avg AND line_move>0                45    20   44.44    +4.9     [30.9,58.8]        small     11      27.27
q3>avg AND accel>=0                   83    52   62.65   +23.1     [51.9,72.3]     moderate     21      57.14
req<1.00 AND q3>avg                    0     0       —       —               —         tiny      0          —
q3>avg AND diff>=0                    82    42   51.22   +11.7     [40.6,61.7]     moderate     28      35.71
q3>avg AND act-req>=0                  0     0       —       —               —         tiny      0          —
accel>=0.5 AND line_move>=0           60    32   53.33   +13.8     [40.9,65.4]     moderate     18      38.89
M-C2 AND M-R2 (accel + hot Q3)        31    17   54.84   +15.3     [37.8,70.8]        small      6      16.67
q3>avg NOT trigger-style (req<=1.10)    17    10   58.82   +19.3     [36.0,78.4]         tiny     14      50.00

## 5. WEEKLY STABILITY OF TOP CANDIDATES (ISO weeks, OVER%)

week           N   OVER%       36      37      38      39
M-C1a req      0     0.0        —       —       —       —
M-C1b req      0     0.0        —       —       —       —
M-C2 rece     99    50.5     54.8    36.0    58.3       —
M-C3 req>    133    53.4     60.9    37.9    43.8     0.0
M-C5 req>    116    52.6     59.5    40.0    16.7     0.0
M-C6 acce     61    59.0     63.6    38.5    75.0       —
M-R2 q3_r     67    49.3     60.0    30.8    22.2       —
M-C4 [0.9      0     0.0        —       —       —       —
q3>avg AN     45    44.4     50.0    33.3    20.0       —
q3>avg AN     83    62.7     64.5    46.7    83.3       —
req<1.00       0     0.0        —       —       —       —
q3>avg AN     82    51.2     59.3    33.3    44.4     0.0
q3>avg AN      0     0.0        —       —       —       —
accel>=0.     60    53.3     59.5    38.9       —       —
M-C2 AND      31    54.8     64.0    16.7       —       —
q3>avg NO     17    58.8    100.0    25.0    60.0       —
baseline(    281    39.5     47.7    25.0    38.6    33.3

## 6. VERDICT SUMMARY

(completed below in the analysis narrative — see
analysis_over_discovery_2026-09-22.md as written by this script)


---

## 6. VERDICTS (written 2026-09-22, analysis only — nothing implemented)

### 6.1 Candidates strong enough for SHADOW TESTING (none promoted to production)

| candidate | N (class) | OVER% | OOS N | OOS OVER% | OOS lift vs OOS base 30.0% | verdict |
|---|---|---|---|---|---|---|
| q3_ratio>1.0 AND recent3-act>=0 (hot-Q3 + no decel) | 83 moderate | 62.65 | 21 | 57.14 | **+27.1pp** | BEST OOS. Full-cohort CI [51.9,72.3] excludes baseline 39.5. Shadow-test first. |
| M-C3: req_ratio>1.04 AND q3_ratio>1.0 | 133 substantial | 53.38 | 46 | 39.13 | +9.1pp | Largest N; full CI excludes baseline. Shadow-test. |
| M-C2: recent3-act >= +0.5 (acceleration) | 99 moderate | 50.51 | 37 | 43.24 | +13.2pp | Real but modest; weekly cells 54.8/36.0/58.3 noisy. Shadow-test. |
| M-C6: accel>=0.5 AND q3>1.0 | 61 moderate | 59.02 | 17 | 47.06 | +17.1pp | Highest mirror OVER%; OOS N tiny — shadow only, expect drift. |

### 6.2 Rejected / unstable / structurally impossible

| candidate | evidence | verdict |
|---|---|---|
| M-C1a/M-C1b (req_ratio < 1.00) and M-C4 ([0.90,1.00)+accel) | **N=0 — empty by construction**: the production trigger requires req > avg*1.04, so req_ratio can never be < 1.04 inside this cohort | structurally impossible ON THIS COHORT; not evidence against |
| M-C5 (req>1.10 AND q3>1.0) | substantial N=116, 52.6% IS, but OOS 34.4% (+4.4pp only) | weak OOS — reject for now |
| M-R2 (q3_ratio >= 1.10) | OOS 27.3% — BELOW OOS baseline | dead OOS — reject |
| accel>=0.5 AND q3>=1.10 combo | OOS 16.7% (N=6) | dead OOS — reject |
| line_move (either direction) | all windows within ~8pp of baseline; CI spans baseline | no usable signal |
| score_differential (trailing vs level) | 41.2% vs 38.2% — no separation | no usable signal |
| req_ratio >= 1.20, act-req <= -1.0 | ~41% (no lift) — just "the rest of the cohort" | no signal |
| q3>avg AND req<=1.10 | 58.8% but tiny (N=17) | too thin — do not pursue |

### 6.3 Are the true OVER equivalents of C1/C2/C3/C5/C6/R2 found?

- **C2 -> M-C2 (acceleration >= +0.5)**: GENUINE — 50.5% full / 43.2% OOS on moderate N, and it is half of the best combo. The opposite-momentum effect is real but HALF as strong as C2's UNDER effect (74.8%).
- **C3/C5 -> M-C3 (req>1.04 & Q3 hot)**: GENUINE with the largest sample (133, substantial) and significant CI, but OOS lift decays to ~+9pp (UNDER's C3 holds ~73% OOS).
- **C6 -> M-C6 (accel + hot Q3)**: GENUINE-looking (59.0% full, +19.5pp) with the best mirror OOS (+17.1pp), but moderate/tiny cells — the least certain of the four.
- **R2 -> M-R2 (q3 >= 1.10)**: NOT found — the mirror FAILS out-of-sample (27.3%). Q3-hotness alone is not an OVER indicator; it only works combined with non-deceleration.
- **C1/C4 -> M-C1a/b, M-C4**: cannot exist on this cohort (empty by construction) — the production trigger's req>avg*1.04 leg precludes low-required-pace states. A true C1-equivalent would need an OVER-side trigger cohort (see section 7).

## 7. SYMMETRY VERDICT

**The OVER side is NOT genuinely symmetrical with the UNDER side on this data — and could not be, for two structural reasons:**

1. **One-sided sampling.** The cohort is SELECTED by the production UNDER trigger (act < league_avg AND req > avg*1.04). Every record is already conditioned to be UNDER-favorable; OVER outcomes here are the conditional minority (39.5% overall, 30.0% OOS). Mirrors evaluated on this cohort measure "rescue from an UNDER-selected state", not a native OVER edge. Half the natural OVER feature space (req_ratio < 1.04, act-req >= 0) is literally EMPTY in the data.
2. **Strength asymmetry.** The best UNDER fingerprints hold 72-88% UNDER out-of-sample; the best OVER mirrors hold 43-57% OVER out-of-sample. Momentum/Q3-hotness lifts OVER% meaningfully (+13 to +27pp OOS) but nowhere near UNDER-fingerprint strength.

**If a genuine OVER surface is ever wanted, the correct first step is an OVER-side trigger cohort of its own** (e.g., act > league_avg AND req < avg*0.96 at the 75% boundary, recorded by a shadow logger exactly like the UNDER one) — then rerun this same discovery on THAT cohort. Mirrors-on-UNDER-triggers are the wrong instrument for a production OVER decision.

## 8. METHOD NOTES / INTEGRITY

- Read-only: DB opened mode=ro + PRAGMA query_only=1; no code/alert/fingerprint/betting/production change; this script + its markdown report are the only artifacts.
- Cohort identical to the UNDER analysis (281 CLEAN settled 75% triggers; merge last-wins on (game_id, captured_at)); features identical to the frozen six-field OOS contract. No final scores, closing lines, Q4 data, or post-trigger information used anywhere.
- IS/OOS split: discovery week 2026-W36 (N=151, OVER 47.68%) vs forward weeks W37+ (N=130, OVER 30.00%). The BASELINE itself decays ~18pp out-of-sample and swings weekly (W36 47.7 -> W37 25.0 -> W38 38.6) — small-N weekly noise is high; treat any single-week cell, including candidates', with caution.
- Multiplicity: 24 scan windows + 8 mirrors + 8 combos were scanned; the strongest findings (q3>avg AND accel>=0; M-C3) survive on both N and OOS direction, but all OVER-side rates remain modest in absolute terms. Nothing here is an approval to implement anything.
