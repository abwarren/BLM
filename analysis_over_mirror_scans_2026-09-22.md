# OVER MIRROR + INTERACTION FINE SCANS — 2026-09-22 (READ-ONLY)

cohort N=281 (IS W36 N=151 OVER 47.68% | OOS W37+ N=130 OVER 30.00%)
baseline OVER% full=39.50  discovery=47.68  OOS=30.00   (PUSH = 0 everywhere in this cohort)

Legend: N / OVER / OVER% / UNDER% / PUSH% / lift(full) / OOS N / OOS OVER / OOS OVER% / OOS lift / Wilson95(full) / size / STATUS

## 1. q3_ratio FINE BANDS (R2 mirror — search, don't assume ≥1.10)
band                                        N  OVR    OV%    UN%  PSH%   lift   ON  OO   OOV%  Olift          CI size         STATUS
q3_ratio in [1.00,1.05)                    26   11  42.31  57.69   0.0   +2.8    5   1  20.00  -10.0     [26,61] SMALL        INSUFFICIENT OOS SAMPLE
q3_ratio in [1.05,1.10)                    40   27  67.50  32.50   0.0  +28.0   19  11  57.89  +27.9     [52,80] SMALL        INSUFFICIENT OOS SAMPLE
q3_ratio in [1.10,1.20)                    43   18  41.86  58.14   0.0   +2.4   14   3  21.43   -8.6     [28,57] SMALL        INSUFFICIENT OOS SAMPLE
q3_ratio >= 1.20                           24   15  62.50  37.50   0.0  +23.0    8   3  37.50   +7.5     [43,79] SMALL        INSUFFICIENT OOS SAMPLE
CUMULATIVE:                                 0    0      —      —     —      —    0   0      —      —           — TINY         NO DATA
q3_ratio > 1.00 (simple)                  133   71  53.38  46.62   0.0  +13.9   46  18  39.13   +9.1     [45,62] SUBSTANTIAL  WEAK OOS
q3_ratio >= 1.05                          107   60  56.07  43.93   0.0  +16.6   41  17  41.46  +11.5     [47,65] SUBSTANTIAL  PROMISING — shadow test only
q3_ratio >= 1.10 [M-R2]                    67   33  49.25  50.75   0.0   +9.8   22   6  27.27   -2.7     [38,61] MODERATE     REJECTED (dead OOS)
q3_ratio >= 1.20                           24   15  62.50  37.50   0.0  +23.0    8   3  37.50   +7.5     [43,79] SMALL        INSUFFICIENT OOS SAMPLE

## 2. recent3-act (acceleration) FINE BANDS — the mirror of C2
band                                        N  OVR    OV%    UN%  PSH%   lift   ON  OO   OOV%  Olift          CI size         STATUS
recent3-act in [0.00,0.25)                 17   10  58.82  41.18   0.0  +19.3    5   3  60.00  +30.0     [36,78] TINY         INSUFFICIENT OOS SAMPLE
recent3-act in [0.25,0.50)                 19   12  63.16  36.84   0.0  +23.7    6   4  66.67  +36.7     [41,81] TINY         INSUFFICIENT OOS SAMPLE
recent3-act in [0.50,0.75)                 23   10  43.48  56.52   0.0   +4.0    6   3  50.00  +20.0     [26,63] SMALL        INSUFFICIENT OOS SAMPLE
recent3-act in [0.75,1.00)                 16    9  56.25  43.75   0.0  +16.7    8   4  50.00  +20.0     [33,77] TINY         INSUFFICIENT OOS SAMPLE
recent3-act >= 1.00                        60   31  51.67  48.33   0.0  +12.2   23   9  39.13   +9.1     [39,64] MODERATE     WEAK OOS
CUMULATIVE:                                 0    0      —      —     —      —    0   0      —      —           — TINY         NO DATA
recent3-act >= 0.00                       135   72  53.33  46.67   0.0  +13.8   48  23  47.92  +17.9     [45,62] SUBSTANTIAL  PROMISING — shadow test only
recent3-act >= 0.25                       118   62  52.54  47.46   0.0  +13.0   43  20  46.51  +16.5     [44,61] SUBSTANTIAL  PROMISING — shadow test only
recent3-act >= 0.50 [M-C2]                 99   50  50.51  49.49   0.0  +11.0   37  16  43.24  +13.2     [41,60] MODERATE     PROMISING — shadow test only
recent3-act >= 1.00                        60   31  51.67  48.33   0.0  +12.2   23   9  39.13   +9.1     [39,64] MODERATE     WEAK OOS

## 3. req_ratio BANDS INSIDE THE TRIGGER (C1 mirror — the trigger
   precludes req_ratio < 1.04, so the C1 band [1.10,1.20) is where
   OVERs DIE (11.8%); the reachable mirror is the LOWEST band)
band                                        N  OVR    OV%    UN%  PSH%   lift   ON  OO   OOV%  Olift          CI size         STATUS
req_ratio in [1.04,1.10)                   62   26  41.94  58.06   0.0   +2.4   52  22  42.31  +12.3     [30,54] MODERATE     PROMISING — shadow test only
req_ratio in [1.10,1.20) = C1 band         17    2  11.76  88.24   0.0  -27.7   10   1  10.00  -20.0      [3,34] TINY         INSUFFICIENT OOS SAMPLE
req_ratio in [1.20,1.35)                   68   35  51.47  48.53   0.0  +12.0   19   7  36.84   +6.8     [40,63] MODERATE     INSUFFICIENT OOS SAMPLE
req_ratio >= 1.35 (R1's tail)             134   48  35.82  64.18   0.0   -3.7   49   9  18.37  -11.6     [28,44] SUBSTANTIAL  REJECTED (dead OOS)

## 4. PER-FINGERPRINT MIRROR VERDICTS (data-found thresholds)
candidate                                   N  OVR    OV%    UN%  PSH%   lift   ON  OO   OOV%  Olift          CI size         STATUS
C1-mirror: req in [1.04,1.10)              62   26  41.94  58.06   0.0   +2.4   52  22  42.31  +12.3     [30,54] MODERATE     PROMISING — shadow test only
C2-mirror: accel >= 0                     135   72  53.33  46.67   0.0  +13.8   48  23  47.92  +17.9     [45,62] SUBSTANTIAL  PROMISING — shadow test only
C3-mirror: req>1.04 & q3>1.0              133   71  53.38  46.62   0.0  +13.9   46  18  39.13   +9.1     [45,62] SUBSTANTIAL  WEAK OOS
C4-mirror: req[1.04,1.10) & accel>=0       27   16  59.26  40.74   0.0  +19.8   21  12  57.14  +27.1     [41,75] SMALL        PROMISING — shadow test only
C5-mirror: req>1.10 & q3>1.0              116   61  52.59  47.41   0.0  +13.1   32  11  34.38   +4.4     [44,61] SUBSTANTIAL  WEAK OOS
C6-mirror: accel>=0.5 & q3>1.0             61   36  59.02  40.98   0.0  +19.5   17   8  47.06  +17.1     [46,70] MODERATE     INSUFFICIENT OOS SAMPLE
R2-mirror: q3 >= 1.05                     107   60  56.07  43.93   0.0  +16.6   41  17  41.46  +11.5     [47,65] SUBSTANTIAL  PROMISING — shadow test only

## 5. INTERACTION / TRIPLE SCANS (meaningful sample sizes only)
combination                                 N  OVR    OV%    UN%  PSH%   lift   ON  OO   OOV%  Olift          CI size         STATUS
q3>1.0 AND accel>=0 (hot, no decel)        83   52  62.65  37.35   0.0  +23.1   21  12  57.14  +27.1     [52,72] MODERATE     PROMISING — shadow test only
req[1.04,1.10) AND q3>1.0 (low-req, hot Q3)   17   10  58.82  41.18   0.0  +19.3   14   7  50.00  +20.0     [36,78] TINY         INSUFFICIENT OOS SAMPLE
req[1.04,1.10) AND accel>=0 (low-req, momentum)   27   16  59.26  40.74   0.0  +19.8   21  12  57.14  +27.1     [41,75] SMALL        PROMISING — shadow test only
line_move>0 AND accel>=0                   29   13  44.83  55.17   0.0   +5.3    4   2  50.00  +20.0     [28,62] SMALL        INSUFFICIENT OOS SAMPLE
line_move>0 AND q3>1.0                     45   20  44.44  55.56   0.0   +4.9   11   3  27.27   -2.7     [31,59] SMALL        INSUFFICIENT OOS SAMPLE
TRIPLE q3>1.0 & accel>=0 & req<=1.10       12    9  75.00  25.00   0.0  +35.5    9   6  66.67  +36.7     [47,91] TINY         INSUFFICIENT OOS SAMPLE
TRIPLE q3>1.0 & accel>=0 & line_move>=0    56   35  62.50  37.50   0.0  +23.0   11   6  54.55  +24.5     [49,74] MODERATE     INSUFFICIENT OOS SAMPLE

## 6. LEAKAGE VERIFICATION
- Features used: req_ratio, recent3_minus_act, q3_ratio, line_move,
  act_minus_req, score_differential — EXACTLY the six fields the
  UNDER OOS contract froze as point-in-time-safe at the 75% boundary.
- Label: outcome (under/over/push) — used ONLY as the label, never
  as a feature.  No final score, closing line, Q4 split, settlement
  timestamp, or post-trigger information appears in any condition.
- Future league averages: none — q3_ratio uses the same frozen
  point-in-time league Q3 authority as the UNDER analysis.
- Scan verification: every candidate condition above references only
- AST/source check over 37 conditions: PASS — only whitelisted features referenced

## 7. OOS VALIDATION SUMMARY (discovery vs OOS)

candidate                                disc N disc OV% disc lift OOS N  OOS OV%  OOS lift status
C1-mirror: req in [1.04,1.10)                62    41.94      +2.4    52    42.31     +12.3  PROMISING — shadow test only
C2-mirror: accel >= 0                       135    53.33     +13.8    48    47.92     +17.9  PROMISING — shadow test only
C3-mirror: req>1.04 & q3>1.0                133    53.38     +13.9    46    39.13      +9.1  WEAK OOS
C4-mirror: req[1.04,1.10) & accel>=0         27    59.26     +19.8    21    57.14     +27.1  PROMISING — shadow test only
C5-mirror: req>1.10 & q3>1.0                116    52.59     +13.1    32    34.38      +4.4  WEAK OOS
C6-mirror: accel>=0.5 & q3>1.0               61    59.02     +19.5    17    47.06     +17.1  INSUFFICIENT OOS SAMPLE
R2-mirror: q3 >= 1.05                       107    56.07     +16.6    41    41.46     +11.5  PROMISING — shadow test only

---
---

# FINAL REPORT — OVER-SIDE INDICATOR DISCOVERY (READ-ONLY, 2026-09-22)

Research artifacts only: `scripts/over_discovery_2026-09-22.py`,
`scripts/over_mirror_scans_2026-09-22.py`, this report.
Production code, DB schema, alert/fingerprint/betting logic, dashboard,
and services: UNTOUCHED. DB access was `mode=ro` + `PRAGMA query_only=1` throughout.

## 1. Historical cohort size
**N=281 CLEAN settled 75% triggers** — identical merge to the UNDER fingerprint
analysis (discovery CSV week 36 + shadow JSONL weeks 37+, last-wins on
`game_id + captured_at`), identical CLEAN/settled filter, identical frozen
six-field point-in-time feature set.

## 2. Baseline OVER rate
- Full cohort: **39.50%** (111/281) · UNDER 60.50% · **PUSH 0.0%** (no pushes in cohort)
- Discovery week (W36): 47.68% (N=151)
- OOS weeks (W37+): **30.00%** (N=130) — the baseline itself decays ~18pp OOS
  and swings weekly (47.7 → 25.0 → 38.6). All lifts below are quoted against
  the matching-basis baseline (full vs full, OOS vs 30.0).

## 3. Meaningful single-feature OVER indicators
| condition | N | OVER% | UNDER% | PUSH% | lift | OOS OVER% | status |
|---|---|---|---|---|---|---|---|
| q3_ratio >= 1.05 | 107 | 56.07 | 43.93 | 0 | +16.6 | 41.46 (N=41) | promising |
| recent3−act >= 0 (any momentum) | 135 | 53.33 | 46.67 | 0 | +13.8 | 47.92 (N=48) | promising |
| recent3−act >= 0.25 | 118 | 52.54 | 47.46 | 0 | +13.0 | 46.51 (N=43) | promising |
| recent3−act >= 0.5 (acceleration) | 99 | 50.51 | 49.49 | 0 | +11.0 | 43.24 (N=37) | promising |
| q3_ratio > 1.0 | 133 | 53.38 | 46.62 | 0 | +13.9 | 39.13 (N=46) | weak OOS |
| req_ratio in [1.04,1.10) | 62 | 41.94 | 58.06 | 0 | +2.4 | 42.31 (N=52) | weak discovery, OOS-favoured |
| line_move > 0 / < 0 (either) | ~110 | ≈ baseline | — | 0 | ≈ 0 | — | rejected |
| score_differential bands | 165 | ≈ baseline | — | 0 | ≈ 0 | — | rejected |
| req_ratio >= 1.35 (R1's tail) | 134 | 35.82 | 64.18 | 0 | −3.7 | 18.37 (N=49) | rejected (dead OOS) |
| req_ratio >= 1.20 | ~85 | ≈ 41 | — | 0 | ≈ 0 | 36.8 (N=19) | rejected (no lift) |
| recent3−act <= −0.5 (decel) | mirrors C2's own side | ≈ 30 | — | 0 | ≈ −9 | — | rejected (pro-UNDER) |

## 4. Meaningful interaction indicators
| combination | N | OVER% | lift | OOS N | OOS OVER% | OOS lift |
|---|---|---|---|---|---|---|
| q3>1.0 AND recent3−act>=0 (hot Q3, no decel) | 83 | 62.65 | +23.1 | 21 | 57.14 | +27.1 |
| req[1.04,1.10) AND accel>=0 (low-req + momentum) | 27 | 59.26 | +19.8 | 21 | 57.14 | +27.1 |
| TRIPLE q3>1.0 & accel>=0 & line_move>=0 | 56 | 62.50 | +23.0 | 11 | 54.55 | +24.5 |
| TRIPLE q3>1.0 & accel>=0 & req<=1.10 | 12 | 75.00 | +35.5 | 9 | 66.67 | +36.7 | TINY |
| req[1.04,1.10) AND q3>1.0 | 17 | 58.82 | +19.3 | 14 | 50.00 | +20.0 | TINY |
| line_move>0 AND accel>=0 | 29 | 44.83 | +5.3 | 4 | — | — | too small |
| line_move>0 AND q3>1.0 | 45 | 44.44 | +4.9 | 11 | 27.27 | −2.7 | rejected |

## 5. C1 mirror analysis
C1 = req_ratio ∈ [1.10,1.20). **The true operator mirror (req_ratio < 1.04)
is structurally impossible**: the production UNDER trigger requires
req > avg×1.04, so `req_ratio < 1.04` has N=0 in this cohort by construction.
Inside the reachable range, the C1 band [1.10,1.20) is where OVERs **die**
(11.76%, N=17 — it is C1's own UNDER zone). The best reachable "low-req"
condition is the **lowest band [1.04,1.10): OVER 41.94%, N=62, OOS 42.31%
(N=52, +12.3 vs OOS baseline)** — weak in discovery (lift +2.4 vs full
baseline) but the strongest OOS survivor among single features. **Verdict:
no true C1 mirror exists on this data; the lowest-band condition is the only
reachable analogue — shadow-only candidate at best.**

## 6. C2 mirror analysis
C2 = recent3−act <= −0.5 (deceleration). Data-found mirror: **acceleration**.
Fine bands show OVER% is elevated for *every* accel >= 0 band and decays with
larger thresholds, so the natural boundary is **recent3−act >= 0** (N=135,
53.33%, OOS 47.92%) rather than the naive +0.5 inversion. The naive M-C2
(>= +0.5) also works (N=99, 50.51%, OOS 43.24%) but is weaker and smaller.
**Verdict: GENUINE mirror — C2's strongest opposite. ~half C2's strength
(C2 holds 75% UNDER OOS; best accel mirror holds ~48% OVER OOS).**

## 7. C3 mirror analysis
C3 = req>1.04× AND q3<league. Mirror: **req>1.04× AND q3>league** (M-C3).
Largest substantial sample (N=133, 53.38%) and significant full-cohort CI,
but OOS lift decays to **+9.1pp** (39.13% vs 30.0% baseline) and weekly OOS
cells swing widely. **Verdict: genuine but the weakest of the strong mirrors
— "weak OOS" flag; shadow-test only.**

## 8. C4 mirror analysis
C4 = C1 AND C2. True mirror (low-req band AND deceleration) is impossible
(momentum is pro-OVER, and C1's band is anti-OVER: N≈0 by construction).
Best reachable analogue: **req ∈ [1.04,1.10) AND accel >= 0** — N=27 (SMALL),
59.26% discovery, **OOS 57.14% (N=21), +27.1pp** — the best OOS lift of any
non-tiny candidate. **Verdict: the most interesting reachable analogue, but
SMALL sample — shadow-test only, do not promote.**

## 9. C5 mirror analysis
C5 = req>1.10× AND q3<league. Mirror: **req>1.10× AND q3>league** (M-C5).
N=116 (substantial), 52.59% discovery, but OOS lift only **+4.4pp**
(34.38%) — barely above the OOS baseline. **Verdict: WEAK OOS — rejected as
a standalone; its signal is subsumed by M-C3 and the hot-Q3 combo.**

## 10. C6 mirror analysis
C6 = decel AND q3<league. Mirror: **accel >= 0.5 AND q3>league** (M-C6).
N=61 (moderate), 59.02% discovery, OOS 47.06% (N=17), +17.1pp. **Verdict:
genuine-looking — best per-sample OOS among the pure mirrors — but OOS N=17
is below the substantial threshold. Shadow-test only.**

## 11. R2 mirror analysis
R2 = q3_ratio < 0.90. The naive inversion (q3 >= 1.10) was tested and
**FAILED OOS** (OOS 27.27%, below baseline — REJECTED). Searching the actual
distribution instead: the fine bands show the OVER signal lives in
**[1.05,1.10)** (67.5%, N=40) and **>=1.20** (62.5%, N=24), with a *dip* at
[1.10,1.20) (41.9%). The best data-found threshold is **q3_ratio >= 1.05**:
N=107 (substantial), 56.07%, OOS 41.46% (N=41), +11.5pp. **Verdict: a
genuine mirror exists but at a NON-symmetric threshold (>=1.05, not >=1.10);
modest OOS. Shadow-test only.**

## 12. OOS validation summary
| candidate | disc N | disc OVER% | disc lift | OOS N | OOS OVER% | OOS lift | status |
|---|---|---|---|---|---|---|---|
| hot-Q3 + no-decel combo | 83 | 62.65 | +23.1 | 21 | 57.14 | +27.1 | PROMISING |
| C2-mirror: accel >= 0 | 135 | 53.33 | +13.8 | 48 | 47.92 | +17.9 | PROMISING |
| R2-mirror: q3 >= 1.05 | 107 | 56.07 | +16.6 | 41 | 41.46 | +11.5 | PROMISING |
| C4-mirror: low-band + accel | 27 | 59.26 | +19.8 | 21 | 57.14 | +27.1 | PROMISING (SMALL) |
| C6-mirror: accel>=0.5 & q3>1 | 61 | 59.02 | +19.5 | 17 | 47.06 | +17.1 | OOS N too small |
| C3-mirror: req>1.04 & q3>1 | 133 | 53.38 | +13.9 | 46 | 39.13 | +9.1 | WEAK OOS |
| C5-mirror: req>1.10 & q3>1 | 116 | 52.59 | +13.1 | 32 | 34.38 | +4.4 | WEAK OOS |
| M-R2 naive: q3 >= 1.10 | 67 | 49.25 | +9.8 | 22 | 27.27 | −2.7 | REJECTED |
| req >= 1.35 (R1 tail) | 134 | 35.82 | −3.7 | 49 | 18.37 | −11.6 | REJECTED |
| line_move (any combo) | 29–45 | ≈base | ≈0 | 4–11 | mixed | mixed | REJECTED |

## 13. Discovery vs OOS comparison
Every OVER candidate decays out-of-sample — universally, and much more than
the UNDER fingerprints did: best UNDER fingerprints hold 72–88% OOS; best
OVER mirrors hold 43–57%. Weekly OOS cells swing widely (e.g. M-C3:
60.9 → 37.9 → 43.8 → 0.0), so single-week readings are noise. The baseline
itself drops 47.7 → 30.0 OOS, which mechanically compresses every OOS lift.

## 14. Sample-size warnings
- SUBSTANTIAL (100+): M-C3 (133), C2-mirror accel>=0 (135), M-C5 (116),
  R2-mirror q3>=1.05 (107), req>=1.35 (134).
- MODERATE (50–99): M-C2 (99), hot-Q3 combo (83), M-R2 naive (67),
  M-C6 (61), C1-band (62), req[1.04,1.10) (62).
- SMALL (20–49): C4-mirror (27), line-move combos (29–45).
- TINY (<20): triple q3&accel&low-req (12), req[1.04,1.10)&q3>1 (17),
  q3>=1.20 (24→SMALL). **None of the TINY candidates are evidence of
  anything — the 75% OVER triple at 75.0% discovery / 66.7% OOS on N=12/9
  must not be treated as a finding.**
- OOS N for several "promising" candidates is 17–21 — well below reliability.

## 15. Leakage verification
- Every condition references ONLY the six fields the UNDER OOS contract
  froze as point-in-time-safe at the 75% boundary: `req_ratio`,
  `recent3_minus_act`, `q3_ratio`, `line_move`, `act_minus_req`,
  `score_differential`.
- `outcome` (under/over/push) is used ONLY as the label — never as a feature.
- No final score, final total, closing line, Q4 split, post-trigger pace,
  settlement timestamp, or future league averages appear in any condition.
  `q3_ratio` uses the same frozen point-in-time league-Q3 authority as the
  UNDER analysis (no full-history averages).
- Automated AST/source check over all 37 scanned conditions: PASS
  (whitelisted features only).

## DATA FINDING: **B — Some possible OVER fingerprints exist but with
insufficient OOS evidence.**

Four genuine-looking mirrors were found (acceleration; hot-Q3+no-decel;
req>1.04 & q3>league; q3>=1.05 — the last two with non-symmetric, data-found
thresholds), and none of them fails OOS outright except the naive M-R2 and
M-C5. But: (a) all decay materially OOS (+4 to +27pp vs a decaying 30%
baseline); (b) the cohort is selected by the UNDER trigger, so OVER is the
conditional minority and half the OVER feature space is structurally empty
(no true C1/C4 mirror can exist here); (c) every candidate's OOS N is small.
The OVER side is NOT symmetric with the UNDER side. Nothing here is
production-ready; if pursued, the correct next step is shadow logging of the
four candidates — and, for a true OVER surface, an OVER-side trigger cohort
of its own (e.g. `act > avg AND req < avg×0.96` at 75%) with this discovery
re-run there.

**STOP — read-only analysis complete. No production code, DB, alert,
fingerprint, betting, dashboard, schema, or service was modified; nothing
committed or pushed.**
