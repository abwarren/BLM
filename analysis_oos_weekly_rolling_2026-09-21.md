====================================================================================================
WEEKLY ROLLING OOS — PRE-REGISTERED UNDER-FINGERPRINT RULES (READ-ONLY)
run: 2026-09-21T21:29:31Z   records: 10819 (csv rows used=10796, dropped_no_key=0, jsonl lines=10818)
base condition: PRODUCTION 75% trigger (act<avg AND req>avg*1.04);
rules C1..C6 (discovery) + R1..R8 (pre-registered extensions) ON TOP;
partitions = ISO weeks of trigger time; current week reported separately.
====================================================================================================
75% boundary records: 10819   triggers: 521   CLEAN triggers: 281   settled CLEAN triggers: 281

week           N  UNDER  OVER  PUSH   UNDER%  (trigger baseline per week, CLEAN)
2026-W36     151     79    72     0   52.32%
2026-W37      80     60    20     0   75.00%
2026-W38      44     27    17     0   61.36%
2026-W39       6      4     2     0   66.67%  <- OPEN

PER-RULE WEEKLY TABLE (N per week; UNDER% per week in parens)
rule                                      TOTAL            36           37           38
C1 req_ratio in [1.10,1.20)          17  88.24%     7(  86%)    9(  89%)    1( 100%)
C2 recent3 <= act-0.5               111  74.77%    45(  69%)   40(  88%)   22(  68%)
C3 req>1.04x AND q3<avg             147  72.79%    63(  70%)   51(  82%)   28(  64%)
C4 C1 AND C2                          7 100.00%     3( 100%)    4( 100%)    -
C5 req>1.10x AND q3<avg             103  76.70%    57(  68%)   39(  85%)    6( 100%)
C6 C2 AND q3<avg                     75  76.00%    31(  71%)   27(  93%)   14(  64%)
R1 req_ratio in [1.10,1.35)          85  56.47%    56(  48%)   27(  70%)    2( 100%)
R2 q3_ratio < 0.90                   87  78.16%    38(  82%)   25(  88%)   20(  60%)
R3 q3<avg AND line_move<=0          112  71.43%    45(  71%)   45(  80%)   19(  53%)
R4 q3<avg AND diff>=0                82  74.39%    34(  71%)   28(  82%)   16(  69%)
R5 recent3 <= act-1.0                76  76.32%    26(  73%)   28(  89%)   18(  67%)
R6 C2 AND diff>=0                    65  76.92%    27(  70%)   22(  91%)   13(  69%)
R7 q3 in [0.8,1.0) AND C2            47  68.09%    17(  53%)   16(  94%)   11(  64%)
R8 act-req<=-1 AND q3<avg           105  74.29%    58(  67%)   38(  84%)    8(  75%)

trigger baseline (CLEAN settled): UNDER%=60.50% (N=281)

PER-RULE TOTALS vs BASELINE (Wilson 95% CI; 'ON TRACK' = rolling
UNDER% >= trigger baseline — an observation, NOT a promotion):
  C1 req_ratio in [1.10,1.20)      N=   17  UNDER=  15   88.24%  [65.7,96.7%]         ON TRACK
  C2 recent3 <= act-0.5            N=  111  UNDER=  83   74.77%  [66.0,81.9%]         ON TRACK
  C3 req>1.04x AND q3<avg          N=  147  UNDER= 107   72.79%  [65.1,79.3%]         ON TRACK
  C4 C1 AND C2                     N=    7  UNDER=   7  100.00%  [64.6,100.0%]        ON TRACK
  C5 req>1.10x AND q3<avg          N=  103  UNDER=  79   76.70%  [67.7,83.8%]         ON TRACK
  C6 C2 AND q3<avg                 N=   75  UNDER=  57   76.00%  [65.2,84.2%]         ON TRACK
  R1 req_ratio in [1.10,1.35)      N=   85  UNDER=  48   56.47%  [45.9,66.5%]         below baseline
  R2 q3_ratio < 0.90               N=   87  UNDER=  68   78.16%  [68.4,85.5%]         ON TRACK
  R3 q3<avg AND line_move<=0       N=  112  UNDER=  80   71.43%  [62.5,79.0%]         ON TRACK
  R4 q3<avg AND diff>=0            N=   82  UNDER=  61   74.39%  [64.0,82.6%]         ON TRACK
  R5 recent3 <= act-1.0            N=   76  UNDER=  58   76.32%  [65.6,84.5%]         ON TRACK
  R6 C2 AND diff>=0                N=   65  UNDER=  50   76.92%  [65.4,85.5%]         ON TRACK
  R7 q3 in [0.8,1.0) AND C2        N=   47  UNDER=  32   68.09%  [53.8,79.6%]         ON TRACK
  R8 act-req<=-1 AND q3<avg        N=  105  UNDER=  78   74.29%  [65.2,81.7%]         ON TRACK

Open week (current): excluded from ON-TRACK judgement (incomplete).
Next re-run rolls partitions forward as new games settle;
append --append to record this evaluation in
  /home/ubuntu/BLM/analysis/oos_weekly_history.jsonl

READ-ONLY: production DBs not even opened this pass (log+CSV only);
no alert, threshold, service or production table touched.
history appended: /home/ubuntu/BLM/analysis/oos_weekly_history.jsonl
