================================================================================================
FINGERPRINT PARITY — PRODUCTION LAYER vs HISTORICAL DATASET (2026-09-21)
================================================================================================
cohort : production trigger + CLEAN + settled, CSV merged with forward JSONL (JSONL supersedes)
rows   : 281 settled CLEAN triggers (under=170, over=111, push=0)
baseline trigger UNDER% = 60.50%

PER-FINGERPRINT PARITY (production functions on the historical dataset)
fp       N  UNDER  OVER  PUSH   UNDER%  historical (N, UNDER%)   match   UNAVAIL
C1      17     15     2     0   88.24%  N=17, 88.24%             YES           0
C2     111     83    28     0   74.77%  N=111, 74.77%            YES           0
C3     147    107    40     0   72.79%  N=147, 72.79%            YES           1
C4       7      7     0     0  100.00%  N=7, 100.00%             YES           0
C5     103     79    24     0   76.70%  N=103, 76.70%            YES           1
C6      75     57    18     0   76.00%  N=75, 76.00%             YES           1
R2      87     68    19     0   78.16%  N=87, 78.16%             YES           1

definition parity (production-derived ratio vs frozen CSV ratio,
tolerance 5e-4) — drift counts across all settled triggers:
  req_ratio          drift=0
  q3_ratio           drift=0
  recent3_minus_act  drift=0
fingerprint_count == len(fingerprints_fired) on 281/281 rows

FINGERPRINT_COUNT DISTRIBUTION (settled CLEAN triggers)
  count=0: N=  92  UNDER=  33  UNDER%= 35.87%
  count=1: N=  49  UNDER=  33  UNDER%= 67.35%
  count=2: N=  37  UNDER=  24  UNDER%= 64.86%
  count=3: N=  34  UNDER=  27  UNDER%= 79.41%
  count=4: N=  32  UNDER=  20  UNDER%= 62.50%
  count=5: N=  32  UNDER=  28  UNDER%= 87.50%
  count=6: N=   1  UNDER=   1  UNDER%=100.00%
  count=7: N=   4  UNDER=   4  UNDER%=100.00%

R1 EXCLUSION
  'R1' in FINGERPRINT_KEYS        : False  (excluded)
  R1-named module constants       : none
  the widened 1.35 band constant  : absent
  req_ratio=1.30 (in [1.20,1.35)) : fired=[] count=0

VERDICT: FULL PARITY — the production layer reproduces the historical definitions exactly.
R1: NOT an active fingerprint (excluded per directive).
READ-ONLY: no production DB opened; only this report was written.
