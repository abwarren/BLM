====================================================================================================
BLM UNDER vs OVER PATTERN DISCOVERY — READ-ONLY — 2026-09-21
cohort = PRODUCTION 75% UNDER-alert condition (mirrored 1:1 from
scripts/audit_alert_improvement_2026-09-18.py, imported not copied):
  first clean_projections row per game with progress>=75% (<100%),
  VALID, non-terminal, actual/required/line present;
  trigger: actual < league_avg AND required > league_avg*1.04 (strict);
  league_avg = point-in-time mean (result_at STRICTLY before trigger);
  outcome = final_total vs FROZEN trigger line; settle on OK finals only.
CLEAN = trigger line fresh (LIVE, age<=300s). ODDS=1.85, breakeven 54.05%.
====================================================================================================
settled-OK universe: 13738 games; INVALID-quality games excluded: 681

§3 COHORT COUNTS (75% tier — the production alert identity)
----------------------------------------------------------------------------------------------------
  baseline 75% (all obs, no condition)         N=10796  UNDER=5618  OVER=5178  PUSH= 0  UNDER%= 52.04%  [51.1%,53.0%]
  baseline 75% CLEAN                           N= 8598  UNDER=4528  OVER=4070  PUSH= 0  UNDER%= 52.66%  [51.6%,53.7%]
  TRIGGERS 75% ALL                             N=  507  UNDER= 337  OVER= 170  PUSH= 0  UNDER%= 66.47%  [62.2%,70.4%]
  TRIGGERS 75% CLEAN                           N=  270  UNDER= 159  OVER= 111  PUSH= 0  UNDER%= 58.89%  [52.9%,64.6%]
  parity reference (stored audit run 2026-09-20): ALL N=499 U=333 O=166 |
  CLEAN N=262 U=155 O=107 | baseline N=9976 U=5136 O=4840
  (small drift vs the stored audit is expected if new games settled since)

§4 UNDER vs OVER BASELINE (by fixed chronological partition)
----------------------------------------------------------------------------------------------------
  discovery  baseline (75% CLEAN)              N= 2015  UNDER=1058  OVER= 957  PUSH= 0  UNDER%= 52.51%  [50.3%,54.7%]
  discovery  triggers (75% CLEAN)              N=  211  UNDER= 121  OVER=  90  PUSH= 0  UNDER%= 57.35%  [50.6%,63.8%]
  validation baseline (75% CLEAN)              N= 1587  UNDER= 840  OVER= 747  PUSH= 0  UNDER%= 52.93%  [50.5%,55.4%]
  validation triggers (75% CLEAN)              N=   13  UNDER=  11  OVER=   2  PUSH= 0  UNDER%= 84.62%  [57.8%,95.7%]
  holdout    baseline (75% CLEAN)              N= 4996  UNDER=2630  OVER=2366  PUSH= 0  UNDER%= 52.64%  [51.3%,54.0%]
  holdout    triggers (75% CLEAN)              N=   46  UNDER=  27  OVER=  19  PUSH= 0  UNDER%= 58.70%  [44.3%,71.7%]

(building quarter totals + point-in-time league Q3 reference ...)
feature rows assembled: 10796  (Q3 segment unprovable: 83; opening line unprovable: 749; boundary-row unprovable: 0)

§5 REQUIRED PACE ANALYSIS (req/league_avg ratio, 75% CLEAN triggers)
----------------------------------------------------------------------------------------------------
  req/avg [0.00,0.80)                          N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  req/avg [0.80,0.90)                          N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  req/avg [0.90,1.00)                          N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  req/avg [1.00,1.04)                          N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  req/avg [1.04,1.10)                          N=   62  UNDER=  36  OVER=  26  PUSH= 0  UNDER%= 58.06%  [45.7%,69.5%]  lift=+5.40pp
  req/avg [1.10,1.20)                          N=   17  UNDER=  15  OVER=   2  PUSH= 0  UNDER%= 88.24%  [65.7%,96.7%]  lift=+35.57pp
  req/avg [1.20,1.35)                          N=   68  UNDER=  33  OVER=  35  PUSH= 0  UNDER%= 48.53%  [37.1%,60.2%]  lift=-4.13pp
  req/avg [1.35,99.00)                         N=  123  UNDER=  75  OVER=  48  PUSH= 0  UNDER%= 60.98%  [52.1%,69.1%]  lift=+8.31pp
  continuous check — triggers split at the median req/avg:
  req/avg < 1.332 (below median)               N=  135  UNDER=  76  OVER=  59  PUSH= 0  UNDER%= 56.30%  [47.9%,64.4%]  lift=+3.63pp
  req/avg >= 1.332 (above median)              N=  135  UNDER=  83  OVER=  52  PUSH= 0  UNDER%= 61.48%  [53.1%,69.3%]  lift=+8.82pp
  baseline UNDER% for lift: 52.66% (75% CLEAN, no condition)

§6 PREVIOUS QUARTER ANALYSIS (Q3 = the quarter before the 75% boundary)
----------------------------------------------------------------------------------------------------
  Q3 provable on 269/270 triggers
  Q3 pts/min vs point-in-time league Q3 average (strict-before):
  Q3 ppm / league-Q3 avg [0.00,0.80)           N=   36  UNDER=  31  OVER=   5  PUSH= 0  UNDER%= 86.11%  [71.3%,93.9%]  lift=+33.45pp
  Q3 ppm / league-Q3 avg [0.80,0.90)           N=   34  UNDER=  26  OVER=   8  PUSH= 0  UNDER%= 76.47%  [60.0%,87.6%]  lift=+23.81pp
  Q3 ppm / league-Q3 avg [0.90,1.00)           N=   52  UNDER=  32  OVER=  20  PUSH= 0  UNDER%= 61.54%  [48.0%,73.5%]  lift=+8.88pp
  Q3 ppm / league-Q3 avg [1.00,1.10)           N=   67  UNDER=  29  OVER=  38  PUSH= 0  UNDER%= 43.28%  [32.1%,55.2%]  lift=-9.38pp
  Q3 ppm / league-Q3 avg [1.10,1.20)           N=   48  UNDER=  27  OVER=  21  PUSH= 0  UNDER%= 56.25%  [42.3%,69.3%]  lift=+3.59pp
  Q3 ppm / league-Q3 avg [1.20,99.00)          N=   32  UNDER=  13  OVER=  19  PUSH= 0  UNDER%= 40.62%  [25.5%,57.7%]  lift=-12.04pp
  Q3 pts/min − required pace (pts/min):
  Q3ppm-req < -1.5                             N=   97  UNDER=  71  OVER=  26  PUSH= 0  UNDER%= 73.20%  [63.6%,81.0%]  lift=+20.53pp
  Q3ppm-req [-1.5,-0.75)                       N=   65  UNDER=  38  OVER=  27  PUSH= 0  UNDER%= 58.46%  [46.3%,69.6%]  lift=+5.80pp
  Q3ppm-req [-0.75,0)                          N=   65  UNDER=  28  OVER=  37  PUSH= 0  UNDER%= 43.08%  [31.8%,55.2%]  lift=-9.59pp
  Q3ppm-req [0,+0.75)                          N=   26  UNDER=  14  OVER=  12  PUSH= 0  UNDER%= 53.85%  [35.5%,71.2%]  lift=+1.18pp
  Q3ppm-req >= +0.75                           N=   15  UNDER=   6  OVER=   9  PUSH= 0  UNDER%= 40.00%  [19.8%,64.3%]  lift=-12.66pp
  quarter-over-quarter change (Q3 − Q2, pts):
  Q3-Q2 below median (+4)                      N=  134  UNDER=  87  OVER=  47  PUSH= 0  UNDER%= 64.93%  [56.5%,72.5%]  lift=+12.26pp
  Q3-Q2 >= median (+4)                         N=  134  UNDER=  70  OVER=  64  PUSH= 0  UNDER%= 52.24%  [43.8%,60.5%]  lift=-0.42pp
  Q1 / Q2 absolute levels (NO point-in-time league reference —
  exploratory only, median split):
  q1 below median (45)                         N=  123  UNDER=  74  OVER=  49  PUSH= 0  UNDER%= 60.16%  [51.3%,68.4%]  lift=+7.50pp
  q1 >= median (45)                            N=  145  UNDER=  83  OVER=  62  PUSH= 0  UNDER%= 57.24%  [49.1%,65.0%]  lift=+4.58pp
  q2 below median (40)                         N=  134  UNDER=  79  OVER=  55  PUSH= 0  UNDER%= 58.96%  [50.5%,66.9%]  lift=+6.29pp
  q2 >= median (40)                            N=  134  UNDER=  78  OVER=  56  PUSH= 0  UNDER%= 58.21%  [49.7%,66.2%]  lift=+5.55pp

§7 QUARTER TRANSITION ANALYSIS (Q3->Q4 break = the 75% boundary)
----------------------------------------------------------------------------------------------------
  Q4 first ~2.5 min pace (pts/min) — points in the first 150 s of
  Q4 (reconstructed from snapshots; trigger games only):
  provable: 268/270 triggers
  Q4-start pace < median (3.60)                N=  114  UNDER=  76  OVER=  38  PUSH= 0  UNDER%= 66.67%  [57.6%,74.7%]  lift=+14.00pp
  Q4-start pace >= median (3.60)               N=  154  UNDER=  82  OVER=  72  PUSH= 0  UNDER%= 53.25%  [45.4%,61.0%]  lift=+0.58pp
  Q4-start pace < game pace (still slowing)    N=  137  UNDER=  90  OVER=  47  PUSH= 0  UNDER%= 65.69%  [57.4%,73.1%]  lift=+13.03pp
  Q4-start pace >= game pace (heating)         N=  131  UNDER=  68  OVER=  63  PUSH= 0  UNDER%= 51.91%  [43.4%,60.3%]  lift=-0.76pp
  NOTE: a trigger fires exactly AT the boundary, so Q4-start pace is
  POST-trigger information — transition research only, NEVER a rule.
  Earlier checkpoints: the 50% tier is RETIRED in production
  (historically a coin flip) and is NOT analysed.

§8 CURRENT PACE ANALYSIS (75% CLEAN triggers)
----------------------------------------------------------------------------------------------------
  act − required (pts/min):
  act-req < -2                                 N=  128  UNDER=  79  OVER=  49  PUSH= 0  UNDER%= 61.72%  [53.1%,69.7%]  lift=+9.06pp
  act-req [-2,-1)                              N=   77  UNDER=  37  OVER=  40  PUSH= 0  UNDER%= 48.05%  [37.3%,59.0%]  lift=-4.61pp
  act-req [-1,0)                               N=   64  UNDER=  42  OVER=  22  PUSH= 0  UNDER%= 65.62%  [53.4%,76.1%]  lift=+12.96pp
  act-req [0,+1)                               N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  act-req >= +1                                N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  act − league_avg (pts/min):
  act-avg < -1                                 N=   30  UNDER=  18  OVER=  12  PUSH= 0  UNDER%= 60.00%  [42.3%,75.4%]  lift=+7.34pp
  act-avg [-1,-0.5)                            N=  104  UNDER=  61  OVER=  43  PUSH= 0  UNDER%= 58.65%  [49.0%,67.6%]  lift=+5.99pp
  act-avg [-0.5,0)                             N=  136  UNDER=  80  OVER=  56  PUSH= 0  UNDER%= 58.82%  [50.4%,66.7%]  lift=+6.16pp
  act-avg >= 0                                 N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  3-min momentum (recent3 − game pace):
  recent3-game << game (<-0.5)                 N=   98  UNDER=  70  OVER=  28  PUSH= 0  UNDER%= 71.43%  [61.8%,79.4%]  lift=+18.77pp
  recent3-game [-0.5,0)                        N=   37  UNDER=  26  OVER=  11  PUSH= 0  UNDER%= 70.27%  [54.2%,82.5%]  lift=+17.61pp
  recent3-game [0,+0.5)                        N=   36  UNDER=  14  OVER=  22  PUSH= 0  UNDER%= 38.89%  [24.8%,55.1%]  lift=-13.77pp
  recent3-game >= +0.5 (heating)               N=   99  UNDER=  49  OVER=  50  PUSH= 0  UNDER%= 49.49%  [39.9%,59.2%]  lift=-3.17pp
  trajectory_state at trigger:
  trajectory RISING                            N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  trajectory FALLING                           N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  trajectory FLAT                              N=    0  UNDER=   0  OVER=   0  PUSH= 0  UNDER%=  n/a    -
  5-min recent pace − game pace:
  recent5-game < -0.5                          N=   93  UNDER=  65  OVER=  28  PUSH= 0  UNDER%= 69.89%  [59.9%,78.3%]  lift=+17.23pp
  recent5-game [-0.5,0)                        N=   47  UNDER=  28  OVER=  19  PUSH= 0  UNDER%= 59.57%  [45.3%,72.4%]  lift=+6.91pp
  recent5-game [0,+0.5)                        N=   50  UNDER=  28  OVER=  22  PUSH= 0  UNDER%= 56.00%  [42.3%,68.8%]  lift=+3.34pp
  recent5-game >= +0.5                         N=   80  UNDER=  38  OVER=  42  PUSH= 0  UNDER%= 47.50%  [36.9%,58.3%]  lift=-5.16pp

§9 LINE / MARKET ANALYSIS (75% CLEAN triggers)
----------------------------------------------------------------------------------------------------
  opening line provable: 258/270 (scorecard checkpoint_market, game-start opening line)
  closing_line is deliberately NEVER used (post-game information).
  line movement (trigger live − opening, pts):
  line_move < -2 (collapsed)                   N=   88  UNDER=  54  OVER=  34  PUSH= 0  UNDER%= 61.36%  [50.9%,70.9%]  lift=+8.70pp
  line_move [-2,-0.5)                          N=   19  UNDER=  14  OVER=   5  PUSH= 0  UNDER%= 73.68%  [51.2%,88.2%]  lift=+21.02pp
  line_move ~flat                              N=   85  UNDER=  43  OVER=  42  PUSH= 0  UNDER%= 50.59%  [40.2%,61.0%]  lift=-2.08pp
  line_move [+0.5,+2)                          N=    5  UNDER=   2  OVER=   3  PUSH= 0  UNDER%= 40.00%  [11.8%,76.9%]  lift=-12.66pp
  line_move >= +2 (stacked up)                 N=   61  UNDER=  37  OVER=  24  PUSH= 0  UNDER%= 60.66%  [48.1%,71.9%]  lift=+7.99pp
  |fair − line| at trigger (model-vs-market deviation):
  |fair-line| [0,2)                            N=    1  UNDER=   0  OVER=   1  PUSH= 0  UNDER%=  0.00%  [0.0%,79.3%]  lift=-52.66pp
  |fair-line| [2,5)                            N=   17  UNDER=  12  OVER=   5  PUSH= 0  UNDER%= 70.59%  [46.9%,86.7%]  lift=+17.92pp
  |fair-line| [5,10)                           N=   41  UNDER=  25  OVER=  16  PUSH= 0  UNDER%= 60.98%  [45.7%,74.3%]  lift=+8.31pp
  |fair-line| [10,999)                         N=  211  UNDER= 122  OVER=  89  PUSH= 0  UNDER%= 57.82%  [51.1%,64.3%]  lift=+5.16pp
  market-implied required pace ((line−score)/remaining) − projector
  required pace (pts/min); provable: 268/270
  market-req below median (+0.00)              N=   25  UNDER=  16  OVER=   9  PUSH= 0  UNDER%= 64.00%  [44.5%,79.8%]  lift=+11.34pp
  market-req >= median (+0.00)                 N=  243  UNDER= 142  OVER= 101  PUSH= 0  UNDER%= 58.44%  [52.2%,64.5%]  lift=+5.77pp

§10 SCORE / GAME-STATE ANALYSIS (75% CLEAN triggers)
----------------------------------------------------------------------------------------------------
  remaining minutes at trigger:
  remaining [0,6)                              N=   19  UNDER=  15  OVER=   4  PUSH= 0  UNDER%= 78.95%  [56.7%,91.5%]  lift=+26.28pp
  remaining [6,9)                              N=    2  UNDER=   2  OVER=   0  PUSH= 0  UNDER%=100.00%  [34.2%,100.0%]  lift=+47.34pp
  remaining [9,12)                             N=  221  UNDER= 128  OVER=  93  PUSH= 0  UNDER%= 57.92%  [51.3%,64.2%]  lift=+5.26pp
  remaining [12,24)                            N=   28  UNDER=  14  OVER=  14  PUSH= 0  UNDER%= 50.00%  [32.6%,67.4%]  lift=-2.66pp
  score differential at trigger (home − away):
  provable: 268/270
  |diff| < 8 (close game)                      N=  134  UNDER=  73  OVER=  61  PUSH= 0  UNDER%= 54.48%  [46.0%,62.7%]  lift=+1.81pp
  |diff| >= 8 (separated)                      N=  134  UNDER=  85  OVER=  49  PUSH= 0  UNDER%= 63.43%  [55.0%,71.1%]  lift=+10.77pp
  progress depth past 75%:
  progress [75,80)                             N=  251  UNDER= 144  OVER= 107  PUSH= 0  UNDER%= 57.37%  [51.2%,63.3%]  lift=+4.71pp
  progress [80,90)                             N=    2  UNDER=   2  OVER=   0  PUSH= 0  UNDER%=100.00%  [34.2%,100.0%]  lift=+47.34pp
  progress [90,100)                            N=   17  UNDER=  13  OVER=   4  PUSH= 0  UNDER%= 76.47%  [52.7%,90.4%]  lift=+23.81pp
  line level:
  line in [0,150)                              N=   23  UNDER=  15  OVER=   8  PUSH= 0  UNDER%= 65.22%  [44.9%,81.2%]  lift=+12.55pp
  line in [150,170)                            N=  104  UNDER=  58  OVER=  46  PUSH= 0  UNDER%= 55.77%  [46.2%,64.9%]  lift=+3.11pp
  line in [170,999)                            N=  143  UNDER=  86  OVER=  57  PUSH= 0  UNDER%= 60.14%  [52.0%,67.8%]  lift=+7.48pp
  fouls / possessions / blowout-risk indicators: NOT COLLECTED in
  any historical table — cannot be analysed (documented gap).

§11 FEATURE INTERACTION ANALYSIS (75% CLEAN triggers, full history —
    discovery-only frozen rules are tested out-of-sample in §14)
----------------------------------------------------------------------------------------------------
  baseline UNDER% = 52.66%  (single conditions first:)
    b1 req>avg*1.10                            N=  208  UNDER= 123  OVER=  85  PUSH= 0  UNDER%= 59.13%  [52.3%,65.6%]  lift=+6.47pp
    b2 act<req                                 N=  270  UNDER= 159  OVER= 111  PUSH= 0  UNDER%= 58.89%  [52.9%,64.6%]  lift=+6.23pp
    b3 recent3<=act-0.5                        N=   99  UNDER=  71  OVER=  28  PUSH= 0  UNDER%= 71.72%  [62.2%,79.6%]  lift=+19.05pp
    b4 Q3<leagueQ3avg                          N=  122  UNDER=  89  OVER=  33  PUSH= 0  UNDER%= 72.95%  [64.5%,80.0%]  lift=+20.29pp
    b5 line_move<=0                            N=  192  UNDER= 111  OVER=  81  PUSH= 0  UNDER%= 57.81%  [50.7%,64.6%]  lift=+5.15pp
    b6 act<avg                                 N=  270  UNDER= 159  OVER= 111  PUSH= 0  UNDER%= 58.89%  [52.9%,64.6%]  lift=+6.23pp
    b7 req/avg in [1.04,1.20)                  N=   79  UNDER=  51  OVER=  28  PUSH= 0  UNDER%= 64.56%  [53.6%,74.2%]  lift=+11.89pp

  pairwise interactions (N>=25, sorted by N then UNDER%):
    b2 act<req AND b6 act<avg                  N=  270  UNDER= 159  OVER= 111  PUSH= 0  UNDER%= 58.89%  [52.9%,64.6%]  lift=+6.23pp
    b1 req>avg*1.10 AND b2 act<req             N=  208  UNDER= 123  OVER=  85  PUSH= 0  UNDER%= 59.13%  [52.3%,65.6%]  lift=+6.47pp
    b1 req>avg*1.10 AND b6 act<avg             N=  208  UNDER= 123  OVER=  85  PUSH= 0  UNDER%= 59.13%  [52.3%,65.6%]  lift=+6.47pp
    b2 act<req AND b5 line_move<=0             N=  192  UNDER= 111  OVER=  81  PUSH= 0  UNDER%= 57.81%  [50.7%,64.6%]  lift=+5.15pp
    b5 line_move<=0 AND b6 act<avg             N=  192  UNDER= 111  OVER=  81  PUSH= 0  UNDER%= 57.81%  [50.7%,64.6%]  lift=+5.15pp
    b1 req>avg*1.10 AND b5 line_move<=0        N=  143  UNDER=  84  OVER=  59  PUSH= 0  UNDER%= 58.74%  [50.5%,66.5%]  lift=+6.08pp
    b2 act<req AND b4 Q3<leagueQ3avg           N=  122  UNDER=  89  OVER=  33  PUSH= 0  UNDER%= 72.95%  [64.5%,80.0%]  lift=+20.29pp
    b4 Q3<leagueQ3avg AND b6 act<avg           N=  122  UNDER=  89  OVER=  33  PUSH= 0  UNDER%= 72.95%  [64.5%,80.0%]  lift=+20.29pp
    b2 act<req AND b3 recent3<=act-0.5         N=   99  UNDER=  71  OVER=  28  PUSH= 0  UNDER%= 71.72%  [62.2%,79.6%]  lift=+19.05pp
    b3 recent3<=act-0.5 AND b6 act<avg         N=   99  UNDER=  71  OVER=  28  PUSH= 0  UNDER%= 71.72%  [62.2%,79.6%]  lift=+19.05pp
    b1 req>avg*1.10 AND b4 Q3<leagueQ3avg      N=   97  UNDER=  73  OVER=  24  PUSH= 0  UNDER%= 75.26%  [65.8%,82.8%]  lift=+22.59pp
    b4 Q3<leagueQ3avg AND b5 line_move<=0      N=   93  UNDER=  67  OVER=  26  PUSH= 0  UNDER%= 72.04%  [62.2%,80.1%]  lift=+19.38pp
    b2 act<req AND b7 req/avg in [1.04,1.20)   N=   79  UNDER=  51  OVER=  28  PUSH= 0  UNDER%= 64.56%  [53.6%,74.2%]  lift=+11.89pp
    b6 act<avg AND b7 req/avg in [1.04,1.20)   N=   79  UNDER=  51  OVER=  28  PUSH= 0  UNDER%= 64.56%  [53.6%,74.2%]  lift=+11.89pp
    b1 req>avg*1.10 AND b3 recent3<=act-0.5    N=   74  UNDER=  55  OVER=  19  PUSH= 0  UNDER%= 74.32%  [63.3%,82.9%]  lift=+21.66pp
    b3 recent3<=act-0.5 AND b5 line_move<=0    N=   72  UNDER=  54  OVER=  18  PUSH= 0  UNDER%= 75.00%  [63.9%,83.6%]  lift=+22.34pp
    b3 recent3<=act-0.5 AND b4 Q3<leagueQ3avg  N=   61  UNDER=  46  OVER=  15  PUSH= 0  UNDER%= 75.41%  [63.3%,84.5%]  lift=+22.75pp
    b5 line_move<=0 AND b7 req/avg in [1.04,1.20) N=   60  UNDER=  37  OVER=  23  PUSH= 0  UNDER%= 61.67%  [49.0%,72.9%]  lift=+9.00pp
    b4 Q3<leagueQ3avg AND b7 req/avg in [1.04,1.20) N=   34  UNDER=  25  OVER=   9  PUSH= 0  UNDER%= 73.53%  [56.9%,85.4%]  lift=+20.87pp
    b3 recent3<=act-0.5 AND b7 req/avg in [1.04,1.20) N=   32  UNDER=  23  OVER=   9  PUSH= 0  UNDER%= 71.88%  [54.6%,84.4%]  lift=+19.21pp
  (pairs with N<25 suppressed — explore the dataset CSV instead)

§12 CANDIDATE UNDER IDENTIFIERS (full-history view; OOS in §14)
----------------------------------------------------------------------------------------------------
  C1 req/avg in [1.10,1.20)                    N=  17  U=  15  O=   2  P= 0  UNDER%= 88.24%  [65.7%,96.7%]  coverage=6.3% of triggers
  C2 recent3 <= act - 0.5 (decelerating)       N=  99  U=  71  O=  28  P= 0  UNDER%= 71.72%  [62.2%,79.6%]  coverage=36.7% of triggers
  C3 req>avg*1.04 AND Q3<leagueQ3avg           N= 122  U=  89  O=  33  P= 0  UNDER%= 72.95%  [64.5%,80.0%]  coverage=45.2% of triggers
  C4 C1 AND C2                                 N=   7  U=   7  O=   0  P= 0  UNDER%=100.00%  [64.6%,100.0%]  coverage=2.6% of triggers
  C5 req>avg*1.10 AND Q3<leagueQ3avg           N=  97  U=  73  O=  24  P= 0  UNDER%= 75.26%  [65.8%,82.8%]  coverage=35.9% of triggers
  C6 recent3<=act-0.5 AND Q3<leagueQ3avg       N=  61  U=  46  O=  15  P= 0  UNDER%= 75.41%  [63.3%,84.5%]  coverage=22.6% of triggers
  (baseline triggers UNDER% = 52.66%)

§13 CANDIDATE OVER IDENTIFIERS (mirror side — what marks the losers)
----------------------------------------------------------------------------------------------------
  recent3-act >= +0.5 (heating up)             N=  99  OVER=  50  OVER%= 50.51%  (baseline OVER%=47.34%)
  Q4-start pace >= game pace (post-trigger, research only) N= 131  OVER=  63  OVER%= 48.09%  (baseline OVER%=47.34%)
  line_move >= +2 (market stacked up)          N=  61  OVER=  24  OVER%= 39.34%  (baseline OVER%=47.34%)
  req/avg >= 1.35 (extreme requirement)        N= 123  OVER=  48  OVER%= 39.02%  (baseline OVER%=47.34%)
  req/avg in [1.20,1.35)                       N=  68  OVER=  35  OVER%= 51.47%  (baseline OVER%=47.34%)

§14 OUT-OF-SAMPLE VALIDATION (fixed a-priori partitions)
----------------------------------------------------------------------------------------------------
  discovery : trigger < 2026-09-10 14:15Z
  validation: 2026-09-10 14:15Z .. 2026-09-13 06:32Z
  holdout   : >= 2026-09-13 06:32Z (untouched until rules frozen)
  rules C1..C4 are DISCOVERED on the discovery partition only, then
  applied unchanged downstream. Selection rule (stated a priori):
  highest discovery UNDER% subject to discovery N>=30; ties -> larger N.

  trigger counts per partition: discovery N=211 (U=121), validation N=13 (U=11), holdout N=46 (U=27)

  discovery-period scan (these numbers CHOSE the frozen rule; min N=30):
    C4 C1 AND C2                                   N=   6  UNDER%=100.00%
    C1 req/avg in [1.10,1.20)                      N=  15  UNDER%= 86.67%
    C6 recent3<=act-0.5 AND Q3<leagueQ3avg         N=  49  UNDER%= 77.55%  <== FROZEN (best among N>=30)
    C2 recent3 <= act - 0.5 (decelerating)         N=  74  UNDER%= 74.32%
    C3 req>avg*1.04 AND Q3<leagueQ3avg             N=  99  UNDER%= 72.73%
    C5 req>avg*1.10 AND Q3<leagueQ3avg             N=  87  UNDER%= 72.41%

  FROZEN RULE: trigger75 AND C6 recent3<=act-0.5 AND Q3<leagueQ3avg
  partition       N  UNDER  OVER   UNDER%  coverage   base%               Wilson95
  discovery      49     38    11   77.55%     23.2%  57.35%           [64.1%,87.0%]
  validation      3      3     0  100.00%     23.1%  84.62%          [43.8%,100.0%]
  holdout         9      5     4   55.56%     19.6%  58.70%           [26.7%,81.1%]
  baseline per partition = the production triggers in that
  partition (the frozen rule's lift ON TOP of the alert).

  VERDICT: holdout UNDER%=55.56% vs trigger baseline 58.70% (lift -3.14pp, N=9)
  VERDICT: NOT QUALIFIED against the pre-stated §19 bar (lift>=2pp AND holdout N>=100).
  All other candidates and every §5-§11 bucket remain FULL-HISTORY observations
  (they were NOT re-validated out-of-sample and make no OOS claim).

§15 LEAKAGE CHECKS (each feature's timestamp authority)
----------------------------------------------------------------------------------------------------
  - trigger line  : clean_projections.live_total_line FROZEN at the
    boundary observation (market_captured_at <= trigger) — the same
    authority the live alert settles from (under_outcome).
  - act/req pace  : the trigger row itself (points so far, elapsed).
  - league avg    : mean realised pace of OK-settled games with
    result_at STRICTLY BEFORE the trigger instant (no self-inclusion).
  - recent3/5, trajectory, acceleration: trailing windows ENDING at
    the trigger observation (clean_projections definitions).
  - Q1/Q2/Q3 totals: cumulative snapshot maxima per quarter label —
    at-or-before the 75% trigger by construction.
  - Q4-start pace : points in the FIRST 150s of Q4 — AFTER a trigger
    that fired AT the boundary; transition research only, NOT part
    of any frozen rule (explicitly flagged post-trigger).
  - opening line  : checkpoint_market.opening_line — a game-START
    market fact read from the game's earliest checkpoint row; known
    before the game began, hence legal at the trigger instant.
  - line_move     : trigger live line − opening line (both <= trigger).
  - closing_line  : EXCLUDED everywhere (post-game information).
  - final_total   : used ONLY as the outcome label, never as a feature.
  - post-settlement trigger rows are excluded by the mirror loader;
    self-inclusion is structurally impossible (strict <).

§16 RAG FINGERPRINT ANALYSIS
----------------------------------------------------------------------------------------------------
  Every cohort observation is exported as a structured case
  fingerprint to /home/ubuntu/BLM/analysis/under_over_feature_dataset_2026-09-21.csv
  (one row = one game's 75% boundary; trigger + non-trigger).
  Template (fields available on every row):

    CHECKPOINT:            75% boundary (Q3->Q4 break)
    LEAGUE:                <league>
    CAPTURED_AT:           <trigger instant UTC>
    CURRENT SCORE:         <total> (home <h> + away <a>)
    SCORE DIFFERENTIAL:    <home-away>
    TRIGGERED LINE:        <frozen live line>
    OPENING LINE:          <opening>  (movement <+move>)
    REQUIRED PACE:         <req> pts/min
    LEAGUE AVG:            <avg> pts/min
    REQUIRED VS AVG:       <ratio>x
    CURRENT PACE:          <act> pts/min
    ACT VS REQUIRED:       <act-req> pts/min
    3-MIN MOMENTUM:        <recent3-act> pts/min
    PREVIOUS QUARTER (Q3): <q3> pts (<q3_ratio>x league Q3 avg)
    REMAINING MINUTES:     <remaining>
    MARKET-IMPLIED REQ:    <market_req_ppm> pts/min
    FINAL RESULT:          <UNDER/OVER/PUSH> (final <ft> vs line)

  Example fingerprints — first 3 UNDER and 3 OVER CLEAN triggers,
  chronological:
    ----------------------------------------------------------------------------
    CHECKPOINT: 75% boundary | LEAGUE: CYBER | CAPTURED_AT: 2026-09-05T08:21:54.729596Z
    CURRENT SCORE: 144 (home 72 + away 72) | SCORE DIFFERENTIAL: 0.0 | REMAINING: 10.3 min
    TRIGGERED LINE: 193.5 | OPENING: n/a | MOVE: n/a
    REQUIRED PACE: 4.815 | LEAGUE AVG: 4.451 | REQ VS AVG: 1.082x
    CURRENT PACE: 3.818 | ACT-REQ: -0.998 | 3-MIN MOMENTUM: -1.28
    PREVIOUS QUARTER (Q3): unprovable
    MARKET-IMPLIED REQ: 4.82
    FINAL RESULT: UNDER (final 192 vs line 193.5)
    ----------------------------------------------------------------------------
    CHECKPOINT: 75% boundary | LEAGUE: KBL | CAPTURED_AT: 2026-09-05T10:14:07.514760Z
    CURRENT SCORE: 115 (home 54 + away 61) | SCORE DIFFERENTIAL: -7.0 | REMAINING: 8.2 min
    TRIGGERED LINE: 149.5 | OPENING: 146.5 | MOVE: 3.0
    REQUIRED PACE: 4.182 | LEAGUE AVG: 3.872 | REQ VS AVG: 1.080x
    CURRENT PACE: 3.622 | ACT-REQ: -0.560 | 3-MIN MOMENTUM: -1.78
    PREVIOUS QUARTER (Q3): 24 pts (0.60x league Q3 avg)
    MARKET-IMPLIED REQ: 4.18
    FINAL RESULT: UNDER (final 144 vs line 149.5)
    ----------------------------------------------------------------------------
    CHECKPOINT: 75% boundary | LEAGUE: KBL | CAPTURED_AT: 2026-09-05T10:14:07.554865Z
    CURRENT SCORE: 118 (home 71 + away 47) | SCORE DIFFERENTIAL: 24.0 | REMAINING: 8.2 min
    TRIGGERED LINE: 151.5 | OPENING: 147.5 | MOVE: 4.0
    REQUIRED PACE: 4.061 | LEAGUE AVG: 3.872 | REQ VS AVG: 1.049x
    CURRENT PACE: 3.716 | ACT-REQ: -0.344 | 3-MIN MOMENTUM: -1.87
    PREVIOUS QUARTER (Q3): 36 pts (0.89x league Q3 avg)
    MARKET-IMPLIED REQ: 4.06
    FINAL RESULT: UNDER (final 142 vs line 151.5)
    ----------------------------------------------------------------------------
    CHECKPOINT: 75% boundary | LEAGUE: TBSL | CAPTURED_AT: 2026-09-05T20:34:58.729501Z
    CURRENT SCORE: 107 (home 59 + away 48) | SCORE DIFFERENTIAL: 11.0 | REMAINING: 10.0 min
    TRIGGERED LINE: 169.5 | OPENING: 172.5 | MOVE: -3.0
    REQUIRED PACE: 6.250 | LEAGUE AVG: 4.167 | REQ VS AVG: 1.500x
    CURRENT PACE: 3.567 | ACT-REQ: -2.683 | 3-MIN MOMENTUM: 0.43
    PREVIOUS QUARTER (Q3): 56 pts (1.28x league Q3 avg)
    MARKET-IMPLIED REQ: 6.25
    FINAL RESULT: OVER (final 179 vs line 169.5)
    ----------------------------------------------------------------------------
    CHECKPOINT: 75% boundary | LEAGUE: TBSL | CAPTURED_AT: 2026-09-05T20:34:58.773708Z
    CURRENT SCORE: 117 (home 57 + away 60) | SCORE DIFFERENTIAL: -3.0 | REMAINING: 10.0 min
    TRIGGERED LINE: 171.5 | OPENING: 160.5 | MOVE: 11.0
    REQUIRED PACE: 5.450 | LEAGUE AVG: 4.167 | REQ VS AVG: 1.308x
    CURRENT PACE: 3.900 | ACT-REQ: -1.550 | 3-MIN MOMENTUM: -0.21
    PREVIOUS QUARTER (Q3): 57 pts (1.30x league Q3 avg)
    MARKET-IMPLIED REQ: 5.45
    FINAL RESULT: OVER (final 203 vs line 171.5)
    ----------------------------------------------------------------------------
    CHECKPOINT: 75% boundary | LEAGUE: EuroLeague | CAPTURED_AT: 2026-09-05T21:00:21.759734Z
    CURRENT SCORE: 97 (home 55 + away 42) | SCORE DIFFERENTIAL: 13.0 | REMAINING: 10.0 min
    TRIGGERED LINE: 143.5 | OPENING: 149.5 | MOVE: -6.0
    REQUIRED PACE: 4.650 | LEAGUE AVG: 4.285 | REQ VS AVG: 1.085x
    CURRENT PACE: 3.233 | ACT-REQ: -1.417 | 3-MIN MOMENTUM: 0.77
    PREVIOUS QUARTER (Q3): 45 pts (1.00x league Q3 avg)
    MARKET-IMPLIED REQ: 4.65
    FINAL RESULT: OVER (final 155 vs line 143.5)

  Recurrence scan (UNDER vs OVER triggers, full history):
    req_ratio >= 1.10            UNDER 123/159 (77.4%)        OVER 85/111 (76.6%)
    recent3-act <= -0.5          UNDER 71/159 (44.7%)         OVER 28/111 (25.2%)
    Q3 ratio < 1.0               UNDER 89/159 (56.0%)         OVER 33/111 (29.7%)
    line_move <= 0               UNDER 111/159 (69.8%)        OVER 81/111 (73.0%)
    act-req < -1 (far behind)    UNDER 117/159 (73.6%)        OVER 89/111 (80.2%)

§17 STRONG FINDINGS (high sample, survives OOS — see §14 for the numbers)
----------------------------------------------------------------------------------------------------
  Filled from §14: only patterns whose frozen-rule holdout UNDER% stays
  above the trigger baseline AND whose discovery+validation behaviour is
  consistent qualify. Everything else stays OUT of this list.
  This run's §14 VERDICT did NOT meet the §19 bar — therefore §17 is
  EMPTY for this dataset. The strongest full-history interactions
  (§11: Q3 below its league average, recent-3min deceleration, and
  their combinations, +19..+23pp lift, N=61-122) remain full-history
  observations UNTIL re-tested on accumulating out-of-sample data.

§18 WEAK / INSUFFICIENT FINDINGS
----------------------------------------------------------------------------------------------------
  - Q4-start-pace features: post-trigger information AND low provability
    (snapshot cadence at the quarter break) — exploratory only.
  - Q1/Q2 absolute levels without a point-in-time league reference.
  - fouls / possessions / blowout indicators: not collected at all.
  - trajectory_state: sparse in the trigger cohort (see §8).
  - any bucket with N<100: treat as directional noise, not evidence.

§19 RECOMMENDED CANDIDATES FOR SHADOW MODE
----------------------------------------------------------------------------------------------------
  Pre-stated bar: holdout UNDER% beats the trigger baseline by >=2pp
  AND holdout N>=100.
  This run's frozen rule (C6 recent3<=act-0.5 AND Q3<leagueQ3avg) scored holdout UNDER% 55.56% vs baseline 58.70% (lift -3.14pp, N=9) — VERDICT: NOT QUALIFIED.
  NOTHING is recommended for shadow mode from this dataset.
  No production change is proposed here.

§20 NO PRODUCTION CHANGES
----------------------------------------------------------------------------------------------------
  This task is READ-ONLY research. No code, threshold, alert, collector,
  dashboard, service or database row was touched. Both DBs were opened
  mode=ro. The only files written are this report and the CSV dataset.
  Per the task contract: STOP and wait for approval before any change
  to BLM is considered.

dataset written: /home/ubuntu/BLM/analysis/under_over_feature_dataset_2026-09-21.csv  (10796 rows, 45 cols)
