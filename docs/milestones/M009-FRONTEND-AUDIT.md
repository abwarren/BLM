# BLM v4 — FRONTEND AUDIT REPORT (M009-M5 era)

- Date: 2026-08-31 (session)
- Audited tree: `/home/gdi/BLM` working tree @ `36f09c0` (HEAD) — `c842d87` (dashboard M009-M5 frontend) + `36f09c0` (scorecard M009-M5 backend) are the newest commits.
- Method: static audit of the SERVED files (`blm_v4/dashboard/static/index.html`, `dashboard.js`, `styles.css`), the API payload builders (`blm_v4/api.py`), the aggregations (`blm_v4/scorecard.py`, `blm_v4/trends.py`), and the M009-M5 commit diffs. Every finding below cites file + line.
- LIVE HTTP VERIFICATION: NOT performed — `curl` against `:2262` was denied this session. Per BLM convention the working tree IS the deployed tree, but whether `blm-server` has been restarted onto `c842d87` is UNVERIFIED. Treat the deployment status of the M009-M5 frontend as UNKNOWN until a restart + browser check.
- Principle applied: THE DATASET COMES FIRST. Any display that implies more than the stored event data supports is flagged, regardless of how the number was computed.

---

## 1. INVENTORY OF DISPLAYED METRICS

### A. Header + summary strip (`index.html` 12–41, `dashboard.js` 49–109)
| Display | Data source | Backed by |
|---|---|---|
| LIVE/STALE pill | `payload.generated_at` vs client clock (<15s) | API response time — system health, not market state |
| collector: RUNNING/STALLED/OFFLINE | `collector_state.json` via `/status` (`api.py` 748–775) | collector heartbeat file |
| update time / games monitored | `payload.generated_at` / `payload.totals` (`_db_stats`, `api.py` 705–738) | real DB counts |
| CYBER/BETUAL counts + live | `payload.games[]` (served window) | games table (≤100 most recent) |
| SNAPSHOTS "stored" | SUM of `snapshot_count` over the SERVED ≤100 games (`dashboard.js` 92–93) | served window only — NOT the DB total (see finding F5) |
| LAST SNAPSHOT age | MAX `last_update` in served window | served window only |

### B. Game cards (`dashboard.js` 607–778)
- Win-probability bar (`winprobHTML`) ← `model.win_probability` = `_implied_win(w1,w2)` — MARKET-IMPLIED from moneyline odds, falls back to a hard 0.5 when no odds (`api.py` 169–173, 638). See finding F1/F9.
- Six-line divergence block (`divergenceHTML` 623–684): Prematch BLM (always "–", honest — not stored), Opening Line (first verified line, immutable), Current Live Line (ONLY when `live && age<=300`), Last observed (with `@ts · ENDED|STALE`), BLM Prediction / BLM (historical), Closing Line (ended only). `liveEdge` only vs current live line; ended/stale → Edge "–". M007-M7 guards present. GOOD.
- Mkt Total row + "· live / · stale" word + "· ws" source marker (674).
- Momentum gauge (`momentumHTML` 686–709) ← `momentum` = `_momentum(rows)` — velocity/accel over last snapshots (`api.py` 218–241), pure function of stored snapshots. Derived, real data.
- Model Confidence gauge ← `_confidence(...)` heuristic (0.45 base + bumps for snap count / line / spread / odds / freshness, `api.py` 176–185+). See finding F2.
- Signal chips (`signalsHTML` 711–725), e.g. "● False Mom 90%" ← `_detect_signals(rows)` live heuristic (`api.py` 248–320). See finding F4.
- Team projections + projected total (`projHTML`) ← `project()` single source. GOOD.
- `eff` = `market_efficiency` = 1 − |score − line|/line proximity measure (572–576). Obscure label (finding F8).
- Chips LIVE / ENDED / STALE (`cardHTML` 746–747) — NO quality/INVALID badge (finding F3).

### C. Detail modal (`dashboard.js` 896–1012)
- Market vs Model panel (last observed @ ts, model total, total edge, spread, margin, market efficiency, market momentum).
- Model panel: Win probability (home) ← market-implied (finding F1), Confidence ← heuristic (F2), home/away projection, pace, expected total.
- Momentum panel (score/strength/velocity/acceleration) — derived from stored snapshots.
- Signals/traps panel (trap meter + 7 signals with confidence).
- Game info (classification, competition, event id, region, status, W1/W2 odds, team totals, source).
- CHECKPOINTS table (checkpoints[] from predictions: BLM pred / Market @CP / Edge / Actual / Error, frozen-at-or-before footnote, 1000–1007).
- NOTE: the API detail also returns `market_vs_fair[]` (immutable per-checkpoint M-F history WITH momentum/freshness, `api.py` 663–666) but the modal does NOT render it — the only place it surfaces is the EVENT DATASET section (finding F10).
- 4 charts (score, actual vs market vs model total, win prob & confidence history, momentum history) ← `_series()` per-snapshot series (`api.py` 584–588) — real stored data, nulls stay null.
- Technical / Raw Data collapsible.

### D. MODEL SCORECARD section (`dashboard.js` 116–372)
- MARKET VS FAIR VALUE — PRIMARY (`126–179`): per-checkpoint N / avg market / avg fair / signed Avg M-F / Med M-F / Under-Value % / Over-Value % + outcome table (OVER WIN/LOSS, UNDER WIN/LOSS, PUSH, position win rate pushes-excluded, Avg OLV→CLV, move toward/away) + GAME-LEVEL SCORECARD with progressive rows. Population: clean completed games (checkpoint_market by construction). GOOD.
- DISPARITY BANDS (`180–214`): |fair−market| buckets × direction, N, Over/Under/Push (actual vs line), BLM win rate, market win rate, Avg Δ (signed), Fresh/Stale N, Fresh/Stale WR, Avg age, `reliable=false` → "SMALL SAMPLE" flag (min sample 30, env `BLM_MIN_BAND_SAMPLE`). GOOD.
- TIME-OF-DAY (`215–242`): per hour + configurable bands — N, over/under/push, BLM win rate, market win rate, Avg Δ. Population: checkpoint_market rows; hour = `_local_hour(game_start)` where `game_start = MIN(snapshot.captured_at)` — FIRST-OBSERVED hour, not true fixture start (finding F6).
- MODEL v4-pace-1 — DIAGNOSTIC prediction-vs-actual (MAE/RMSE/median/bias/MAPE, fragment=0 population only) — correctly demoted + labelled.
- ACCURACY BY GAME PROGRESS (fixed checkpoints MAE).
- MODEL vs MARKET — DIAGNOSTIC, names line type (`checkpoint_market`), MAE/Bias separate, beat counts with denominators.
- O/U PERFORMANCE — DIAGNOSTIC (BLM Over/Under/Push + hit rate with explicit denominator).
- DATA QUALITY (recorded vs headline vs completed vs valid vs invalid/excluded + reasons).
- GAME ELIGIBILITY audit table.
- RECENT PREDICTIONS (fragment rows badged FRAGMENT, excluded from headline).

### E. MARKET & HISTORICAL TRENDS section (`dashboard.js` 384–462)
- MARKET PERFORMANCE (OLVC vs CLV, over/under/push as "n / denom (pct)"), Avg Δ (actual−line), Median Δ CLV.
- TIME-OF-DAY (start hour, local) — per clean game (market_history), grouped periods, CLV N / CLV OVER / CLV UNDER / Avg ΔCLV / MAE CLV.
- MARKET MOVEMENT (OLVC→CLV counts).
- MODEL VS MARKET (clean games) by version: N / avg edge / model OVER % / direction hit % / beat market %.
- Population: market_history = clean completed games only. Every percentage carries numerator/denominator. GOOD. NOTE: this TIME-OF-DAY block and the scorecard one share a name but different populations/aggregations (finding F7).

### F. EVENT DATASET section (`dashboard.js` 491–603, `api.py` 835–922)
- `/api/v4/scorecard/events`: one row per (clean settled game, checkpoint): game/teams, CP, market line, BLM fair, signed diff, direction, market status chip (LIVE/STALE/MISSING), market age s, momentum state·strength, false mom, BLM side, actual, outcome, BLM won ✓/✗.
- Filters: direction / freshness / checkpoint / min |diff| / game / limit; LARGE EDGES ≥10 preset explicitly labelled "inspection preset — NOT a profitability claim"; "observed rows, never a strategy claim" note. GOOD.
- Backed by checkpoint_market (contaminated games excluded at source by table construction) JOIN games. Settled-only dataset.

---

## 2. THE 12 QUESTIONS

1. **Metrics displayed** — inventory in §1 (A–F): ~40 distinct displays across status, cards, modal, 4 scorecard blocks, trends, event dataset.
2. **Backed by real stored event data** — snapshots (scores/clock/period), market lines with timestamps (event-view + WS, `market_observations`), games/results (game_results OK-verified), checkpoint_market frozen rows, market_history, collector heartbeat. Momentum/signals/projections/winprob/confidence are DERIVED, but from stored snapshots — derived is not fabricated, provided the label says what it is (see findings F1/F2/F9 where labels fall short).
3. **Derived from aggregates** — scorecard blocks (market_vs_fair, disparity bands, TOD, market_compare, fixed checkpoints), trends, event dataset = SQL aggregations over stored tables; card momentum/signals = per-game pure functions.
4. **Prediction-vs-actual vs BLM-vs-market confusion** — the scorecard separates them well: MARKET VS FAIR (BLM-vs-market) is PRIMARY; MODEL MAE / MODEL vs MARKET / O/U PERFORMANCE are labelled DIAGNOSTIC. Confusion vectors remain: (a) "Win probability (home)" in the Model panel is market-implied, not the model's; (b) "Model Confidence" gauge is a data-quality heuristic; (c) cards juxtapose model total + edge without a prediction-vs-market side label; (d) two TIME-OF-DAY blocks with the same title but different populations; (e) "BLM win rate" in TOD/disparity tables = position win vs actual (pushes excluded) — better named "position win rate" for consistency with the M-F table.
5. **Stale/fresh visually distinguishable** — YES on cards ("· live"/"· stale" word, chips LIVE/ENDED/STALE, `styles.css` 145–151, 354–360), YES in event dataset (LIVE/STALE/MISSING chips), YES in scorecard freshness buckets. NO numeric age on cards (finding F5/F6 question 6).
6. **Market age visible** — PARTIALLY. Cards: only a live/stale word (threshold 300s), never the age. Modal: "Last observed @ HH:MM:SSZ" timestamp but no age in minutes. Event dataset: exact `market_age_seconds`. Scorecard: avg_age per bucket. Recommendation: show `fmtAge(total_line_age_s)` on cards and modal (data already in payload).
7. **Checkpoint visible** — YES: modal CHECKPOINTS table, event dataset CP column + filter, scorecard per-checkpoint tables. Cards do not show game progress % (period/clock only) — minor.
8. **Large edges investigable** — YES: DISPARITY BANDS (fresh/stale split + avg_age keep large edges attributable to freshness) + EVENT DATASET with min |diff| filter + LARGE EDGES ≥10 preset. This is the best-implemented part of M009-M5.
9. **False momentum investigable** — PARTIALLY. Frozen per-checkpoint `false_momentum` + confidence are in the EVENT DATASET (row-level, filterable by direction/freshness/CP). NOT drillable per game in the modal (market_vs_fair[] rows with momentum exist in the detail API but are not rendered — finding F10), and the card "False Mom 90%" chip is a different, live-window heuristic (finding F4).
10. **Time-of-day surfaceable** — YES, two blocks already exist (scorecard + trends) and are configurable (`BLM_TOD_BANDS`), hypothesis-neutral. BUT both rest on the first-observed-hour proxy for "game start" (finding F6) — the analytics are surfaceable, the underlying semantic is NOT true tip-off hour until a real fixture-start timestamp is captured.
11. **Contaminated games as normal data** — YES, on the card grid. `/api/v4/live` builds from `SELECT * FROM games ORDER BY last_seen_at DESC LIMIT 100` with NO join to `game_quality` (`api.py` 674–682, 778–789). A game marked INVALID in `game_quality` still renders as a normal ENDED card with model projections, momentum, signal chips (M004-era known presentation artifact, still present). The scorecard headline is quality-gated; the card grid is NOT. Highest-priority honesty gap.
12. **Labels accurate** — MOSTLY, with exceptions: "Win probability" (market-implied, F1), "Model Confidence" (heuristic, F2), "SNAPSHOTS stored" (window sum, F5), "start hour, local" (first-observed hour, F6), "eff" (obscure proximity, F8), "False Mom 90%" (live heuristic vs frozen record ambiguity, F4), winprob 50/50 fallback rendered as a real-looking bar when odds are absent (F9).

---

## 3. METRIC-BY-METRIC TABLE

### F1. Win-probability bar + "Win probability (home)"
- CURRENT DISPLAY: 0–100 bar on every card; "Win probability (home)" row inside the modal's **Model** panel.
- DATA SOURCE: `_implied_win(w1, w2)` = 1/odds normalised (`api.py` 169–173, 638). Market moneyline odds (or 0.5 default when absent).
- SEMANTIC MEANING: the market's implied probability from the book's prices — not a BLM output.
- POTENTIAL MISINTERPRETATION: reads as the model's win assessment, especially sitting beside "Model Confidence".
- RECOMMENDED FUTURE UI: relabel "Market-implied win prob (from odds)"; move it out of the Model panel or into a Market panel; when odds are missing show "–" instead of a 50/50 bar (see F9).

### F2. "Model Confidence" gauge
- CURRENT DISPLAY: 0–100 gauge, green/amber/red at 70/50, on cards + modal.
- DATA SOURCE: `_confidence(...)` — 0.45 base + bumps for snapshot count, line, spread, odds, freshness (`api.py` 176–185+).
- SEMANTIC MEANING: a data-availability heuristic (how much evidence the projection had).
- POTENTIAL MISINTERPRETATION: reads as statistical/model confidence ("90% sure"). It is not calibrated, not a probability, and says nothing about expected accuracy.
- RECOMMENDED FUTURE UI: rename "Data completeness" (or "Input freshness"); keep thresholds neutral; reserve "Confidence" for a statistically derived value (CI/calibration) once sample sizes support it.

### F3. No quality badge on cards / /live
- CURRENT DISPLAY: chips LIVE/ENDED/STALE only (`dashboard.js` 746–747).
- DATA SOURCE: `games.status` — no `game_quality` join (`api.py` 674–682, 778–789).
- SEMANTIC MEANING: capture-state only; says nothing about data integrity.
- POTENTIAL MISINTERPRETATION: an INVALID (contaminated) game displays as a normal ENDED card with full model panel — analytics presented on data the backend itself has rejected.
- RECOMMENDED FUTURE UI: join `game_quality.status` into `/live` and `/game/{id}`; add an EXCLUDED/INVALID chip (distinct colour) to cards; suppress or badge the model panel for INVALID games. (Backend field exists; no schema change needed — game_quality is already written by the scorecard loop.)

### F4. "● False Mom 90%" chips
- CURRENT DISPLAY: signal chip on cards (`dashboard.js` 711–725); "False mom" column in event dataset (530).
- DATA SOURCE: cards = `_detect_signals(rows)` live heuristic over the current snapshot window (burst vel > 2.5 with |line move| < 0.5, `api.py` 303–305); event dataset = FROZEN `checkpoint_market.false_momentum` at that checkpoint.
- SEMANTIC MEANING: two different things share one name — a live, re-computed heuristic vs a frozen per-checkpoint record.
- POTENTIAL MISINTERPRETATION: the 90% is a heuristic confidence, not a probability; the two numbers are not comparable and are not labelled as different sources.
- RECOMMENDED FUTURE UI: card chips carry a "live heuristic" tooltip; event dataset header notes "frozen at checkpoint (M009-M4)".

### F5. "SNAPSHOTS stored"
- CURRENT DISPLAY: summary card value.
- DATA SOURCE: `renderSummary` sums `g.snapshot_count` over the served ≤100 games (`dashboard.js` 92–93, 100–101).
- SEMANTIC MEANING: snapshots in the served window, not the store.
- POTENTIAL MISINTERPRETATION: "stored" implies the DB total; the real total (`totals.total_snapshots`, `api.py` 728) is in the same payload but unused.
- RECOMMENDED FUTURE UI: read `totals.total_snapshots` and label "SNAPSHOTS (DB)" vs window.

### F6. TIME-OF-DAY "start hour, local"
- CURRENT DISPLAY: two blocks titled "TIME-OF-DAY (start hour, local)" — scorecard (`dashboard.js` 215–242) and trends (419–426).
- DATA SOURCE: scorecard: `game_start = MIN(snapshot.captured_at)` (`scorecard.py` 1542–1544, 1710); trends: `market_history.started_at` = first snapshot (schema comment). `upsert_game` hardcodes `first_seen_at = now` (collector discovery time).
- SEMANTIC MEANING: the hour the collector FIRST OBSERVED the game — not the fixture's true start. Virtual replays are discovered mid-cycle; first observation ≠ tip-off.
- POTENTIAL MISINTERPRETATION: the headline hypothesis ("01:00–05:00 = Under") is implicitly about game start time; it is actually tested against first-capture time. These are different hypotheses and could disagree.
- RECOMMENDED FUTURE UI: relabel "first-observed hour (local) — true start not yet captured"; when a prematch/fixture-start timestamp becomes available (new collector slice), switch and label accordingly.

### F7. Two "TIME-OF-DAY" blocks
- CURRENT DISPLAY: scorecard TOD (per checkpoint row, 24h + bands) and trends TOD (per clean game, grouped periods) with the same title.
- DATA SOURCE: checkpoint_market rows vs market_history games.
- SEMANTIC MEANING: different populations and different units (checkpoints vs games) — the numbers legitimately differ.
- POTENTIAL MISINTERPRETATION: read as the same statistic.
- RECOMMENDED FUTURE UI: distinct subtitles: "per checkpoint (scorecard)" / "per clean game (trends)".

### F8. Card "eff"
- CURRENT DISPLAY: card foot `eff 0.832` (`dashboard.js` 775).
- DATA SOURCE: `market_efficiency = 1 − |combined − market_total|/market_total` (`api.py` 572–576) — score-vs-line proximity.
- SEMANTIC MEANING: how close the current score total is to the market line.
- POTENTIAL MISINTERPRETATION: "efficiency" is opaque; could read as model efficiency or market efficiency in an economic sense.
- RECOMMENDED FUTURE UI: rename "mkt proximity" or drop from the card (it is in the modal already).

### F9. 50/50 win-prob bar when odds missing
- CURRENT DISPLAY: card always renders the winprob bar.
- DATA SOURCE: `_implied_win` returns hard 0.5 when w1/w2 are NULL (`api.py` 173).
- SEMANTIC MEANING: "no odds captured" — the 50/50 is a fallback sentinel.
- POTENTIAL MISINTERPRETATION: a symmetric 50/50 bar looks like a genuine model assessment.
- RECOMMENDED FUTURE UI: render "–" (no bar) when odds are absent, matching the Prematch BLM "–" honesty convention.

### F10. market_vs_fair[] not rendered in the modal
- CURRENT DISPLAY: modal shows CHECKPOINTS (predictions-based) only; M-F history absent.
- DATA SOURCE: detail API already returns `market_vs_fair[]` with per-checkpoint line/fair/signal/outcome + momentum + freshness (`api.py` 663–666).
- SEMANTIC MEANING: the immutable M-F history is stored and served but not surfaced per-game.
- POTENTIAL MISINTERPRETATION: none (absence, not wrongness) — but the per-game drill-down of the PRIMARY scorecard metric is missing.
- RECOMMENDED FUTURE UI: render a "MARKET VS FAIR" table in the modal (data already in the payload; no backend change).

### F11. "BLM win rate" / "Market win rate" (TOD + disparity bands)
- CURRENT DISPLAY: rate columns.
- DATA SOURCE: `win` = outcome ∈ {OVER_WIN, UNDER_WIN} (BLM's side of the line beat the market line vs the actual); `market_win_rate = loss/denom`; pushes excluded (`scorecard.py` 1784–1820, 1723–1734).
- SEMANTIC MEANING: position win rate vs actual, not profitability, not vs-market-accuracy.
- POTENTIAL MISINTERPRETATION: "win rate" without a qualifier reads like a strategy hit-rate; the muted notes mitigate but the column header stays bare.
- RECOMMENDED FUTURE UI: "position win rate (pushes excl.)" headers, consistent with the M-F table's `position_win_rate` naming.

### F12. Scorecard diagnostics (MODEL / MODEL vs MARKET / O/U / RECENT)
- CURRENT DISPLAY: correctly demoted + labelled DIAGNOSTIC; line type named; denominators everywhere; fragments badged.
- DATA SOURCE: prediction_scores fragment=0; checkpoint_market line identity.
- SEMANTIC MEANING: prediction-vs-actual accuracy (separate from M-F value).
- POTENTIAL MISINTERPRETATION: low — the labels are the reference implementation for the rest of the dashboard.
- RECOMMENDED FUTURE UI: keep; extend with Wilson CIs on the rates when samples grow.

### F13. Event dataset / disparity bands (M009-M5)
- CURRENT DISPLAY: DISPARITY BANDS + EVENT DATASET + LARGE EDGES preset.
- DATA SOURCE: checkpoint_market (clean settled games only) — contaminated excluded at source; freshness computed at read time via `_market_status` (`api.py` 883).
- SEMANTIC MEANING: inspection of observed rows; `reliable=false` flags small samples (n<30).
- POTENTIAL MISINTERPRETATION: low — explicit "never a strategy claim" notes on both surfaces.
- RECOMMENDED FUTURE UI: add Wilson CI columns to band rates; add server-side pagination/offset when the dataset grows (filters currently run in Python over the full table, `api.py` 876–922); subtitle "settled clean games only".

---

## 4. MISLEADING-HEADLINE ASSESSMENT (prioritised)

1. **CRITICAL — INVALID games render as normal cards (F3).** The backend gates headlines; the card grid bypasses the gate. A contaminated game can display projections, momentum and signal chips as if trustworthy. This is the one display that can actively mislead.
2. **HIGH — "start hour, local" is first-observed hour (F6).** Every time-of-day number in both blocks rests on a proxy the labels don't disclose. Any future 01:00–05:00-style hypothesis tested on this column is a different hypothesis than the label claims.
3. **MEDIUM — "Win probability" and "Model Confidence" present heuristics/market data as model output (F1/F2/F9).**
4. **MEDIUM — 50/50 winprob fallback renders as a real assessment (F9).**
5. **LOW — "SNAPSHOTS stored" window sum (F5); duplicate TIME-OF-DAY titles (F7); bare "win rate" headers (F11); "eff" (F8); False Mom name collision (F4).**
6. **LOW — per-game M-F drill-down absent (F10) — omission, not a lie.**

## 5. DATASET-FIRST CONSTRAINTS (what the event data CANNOT support today)

- **True game start time** — not stored. `first_seen_at` is collector discovery; `game_start = MIN(captured_at)` is a proxy. Time-of-day hypotheses cannot be validated against real start time yet.
- **Prematch prediction** — not stored; rendered "–" honestly. Do not let any future card/section imply one exists.
- **Closing line while live** — correctly null until terminal state (M007-M3). Keep.
- **Confidence intervals / uncertainty** — none exist anywhere; the "Confidence" gauge is the only "confidence" in the UI and it is a heuristic. No rate anywhere carries a CI. Adding CIs (Wilson for rates, bootstrap for means) is a backend/analytics milestone, not UI.
- **Live-vs-settled** — event dataset is settled-only by construction; card grid mixes live + ended + stale deliberately (toggle available). Keep the toggle semantics; never show a settled outcome as a live edge (guards exist and held in the M007-M7 regression tests).
- **Statistical independence** — not representable in any current display; multiple checkpoints per game are correlated. Any future CI must account for per-game clustering (see analytics spec), otherwise band/TOD CIs will be over-confident.

## 6. ALREADY HONEST (do not regress)

- Six distinct line concepts on cards + modal, live edge suppressed for ended/stale (M007-M7 tests).
- MARKET VS FAIR PRIMARY with signed disparity, N per checkpoint, pushes-excluded position win rate.
- DISPARITY BANDS with fresh/stale split, avg age, SMALL SAMPLE flag, "never a strategy claim" labels.
- EVENT DATASET inspection section with LIVE/STALE/MISSING chips and honest NULLs.
- Diagnostics demoted + line type named; every percentage with numerator/denominator; fragments badged.
- Prematch BLM "–", missing markets NULL, INVALID excluded from all headline aggregates.
- Frozen checkpoint semantics (INSERT OR IGNORE, never rebased) preserved end-to-end.

## 7. HOUSEKEEPING

- `docs/milestones/CURRENT.md` still records M009-M5 as NOT STARTED while `36f09c0` + `c842d87` are committed — stale; the next milestone record should reconcile this.
- Deployment of the M009-M5 frontend is unverified (restart needed; API verification denied this session).
- No application-code, schema, or dashboard changes were made by this audit. This document is the only deliverable.
