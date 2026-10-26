# Trade Cohort refactor — implementation report (EC2 test only)

**Status:** implementation complete on the isolated test environment. **Test instance
deployed and verified.** Not merged. Not promoted. Production and Auto-Bet untouched.

| item | value |
|---|---|
| branch | `feature/tc-cohort-refactor-test` |
| base (rebased onto) | `992cd21` — *as-deployed production working tree 2026-10-26* (= `52c879d` + 27 uncommitted paths) |
| upstream pushed head | `62f1191` on `origin/fix/price-mapping-2026-10-06` — **17 commits behind what runs in production**, therefore not used as the base |
| last code commit | see `git log -1` (implementation ring: `c013824` → `1f7c9ce` → `08aa1a0` → docs) |
| test instance | `blm-tc2263.service` on `127.0.0.1:2263` (transient user unit) |
| staging mirror | `:2262` / `:80` — untouched, `blm-server` still the process started 2026-10-26 20:45Z |
| production | untouched (`portable-ubuntu`, `blm.haaats.xyz`) |

## 1. Environment re-verification (not taken from the earlier report)

```
host      blm-stage-cpt (i-0d6c4d01114f52bf4, af-south-1)
account   243047261878 (sts get-caller-identity, read-only)
data      /srv/blm/data on /dev/nvme0n1p17; blm_pokerbet.db 272,490,496 B
isolation no route to 192.168.1.100; no private keys; no AWS_* / secret env
services  blm-server (2262, up since 20:45Z) · blm-tc2263 (2263, this deploy)
```

Difference from the report: none blocking. The only surprise was **repository** state —
production's branch carries 17 unpushed commits and 27 uncommitted paths, so the base had
to be reconstructed (below).

## 2. Base branch and rebase

`origin/fix/price-mapping-2026-10-06` = `62f1191` (2026-10-10). The production host runs
`52c879d` with 27 modified files on top. Rebasing onto the pushed head would have based the
refactor on code that is 18 days and 17 commits behind production.

Reconstruction (all read-only on the production side, **code only, no data, no secrets —
`git diff` scanned for key/password patterns before transfer**):

1. `git bundle` of `52c879d` → fetched into the test repo (identical SHA, verified).
2. `git diff` of the production working tree (46 KB, 23 tracked files) → applied cleanly on
   that tip and committed as `992cd21` "snapshot: as-deployed production working tree".
   That commit is **not my work** and is not pushed for review as code; it exists only so
   the refactor sits on what actually runs.
3. `git rebase 992cd21` replayed 13 commits. **One conflict point, four files**
   (`server.py`, `index.html`, `styles.css`, `dashboard.js`), all at EOF/insert points:

| file | resolution |
|---|---|
| `server.py` | live content kept, cohort router mounted after the assistant router (3 added lines) |
| `index.html` | live alerts section kept as-is, TC panel inserted **after** its `</section>` |
| `styles.css` | live sheet kept, TC rules appended |
| `dashboard.js` | live tail kept, TC IIFE appended, **my duplicate `setInterval(refresh, POLL_MS)` dropped** (live already polls) |

Verification that no live fix was regressed: `git diff --numstat 992cd21..HEAD` shows
**0 deleted lines across all 22 changed files** — the branch is purely additive on top of
the as-deployed tree.

Conflict fall-out caught by the headless capture, not by pytest: `blmAuthBootstrap`'s
closing `})();` was consumed by the shared EOF line, so the whole bundle was a
`SyntaxError` and the panel never initialised. Restored in `08aa1a0`; the capture now
boots with `window.__tcCohorts` defined and zero page errors.

## 3. Review fixes implemented

### 3.1 Scored points — strict integer contract (`blm_v4/cohorts/evaluator.py`)

New `scored_points()` returns `(value, reason_code)` and never truncates:

| input | result |
|---|---|
| `150`, `150.0`, `"150"`, `" 70 "` | accepted (`70`/`150`) |
| `150.5`, `10.9` | `UNKNOWN` / `fractional_points` — **150.9 can no longer become 150** |
| `-1`, `-5.0` | `UNKNOWN` / `negative_points` |
| `True`, `False` | `UNKNOWN` / `invalid_points` (a boolean is not a score) |
| `"150.5"`, `"1.5e3"`, `"abc"`, `""` | `UNKNOWN` / `invalid_points` (canonical decimal-integer text only) |
| `NaN`, `±inf`, `{}` | `UNKNOWN` / `invalid_points` |
| `0` | legal integer, then fails the ≥ 70 gate → `NOT_QUALIFIED` / `points_below_min` |

### 3.2 Strictly-prior league reference (`blm_v4/cohorts/rescore.py`)

- `parse_utc_epoch()` normalises `Z`, `±HH:MM`, `±HHMM`, space separator, 0–9 fractional
  digits, naive-UTC and epoch numbers to UTC instants; unparseable → `None` (never "now").
- The reference now compares **parsed instants** with `bisect_left` (**strictly before**).
  A result stamped at exactly the observation time is excluded, in any format.
- Test proving the old bug: `"…T12:05:00.5Z" < "…T12:05:00Z"` lexically but is *later* in
  time — the previous string comparison ordered it wrongly.
- Unparseable result timestamps are skipped and counted
  (`results_unparseable_ts`, `observations_unparseable_ts`); on this slice both are **0**.

### 3.3 Coverage denominators split (`blm_v4/cohorts/api.py`, dashboard)

The outcome rate is measured on **first firing per game**, so its coverage must be too:

- `firing_coverage{rows_with_market, rows_total, pct}` — same population as `under_rate`.
- `observation_coverage{rows_with_market, rows_total, pct}` — every ledger row for the
  cohort (firings + checkpoints); a data-quality measure, never the outcome denominator.
- A third, different number stays separate and labelled as such: **source-level market
  presence** (37,816 of 163,500 snapshots = 23.1 % carry line + price).
- Dashboard shows `firing cover` and `obs cover` with explanatory `title` text; the
  ambiguous `market_coverage` key is gone (API test asserts the split and that mutating a
  checkpoint's line moves only `observation_coverage`).

## 4. Tests

Commands (all on `blm-stage-cpt`, venv `/home/ubuntu/blm/.venv`):

| run | command | result |
|---|---|---|
| focused | `pytest tests/test_tc_cohort_spec_2026_10_26.py tests/test_tc_rescore_prior_semantics_2026_10_27.py tests/test_tc_cohort_api_2026_10_26.py tests/test_tc_rescore_firewall_2026_10_26.py -q` | **149 passed**, exit 0 |
| full suite, branch (final tree) | `pytest tests/ -q` | **111 failed, 2,484 passed, 146 skipped** (466 s) |
| full suite, base `992cd21` | same, in a `git worktree` | **111 failed, 2,335 passed, 146 skipped** (499 s) |

Delta on the final tree: **+149 passed** (= exactly the 149 cohort tests) and **0 net new
failures**. An intermediate run (before the vocabulary-ban fix) showed 113 failed / 144
skipped; the difference was investigated and closed:

1. `test_predictive_settlement_separation::test_modal_has_no_checkpoint_or_settlement_sections`
   — genuinely caused by the refactor: the test banned the bare word `checkpoints` from the
   whole dashboard bundle, and the TC panel legitimately names the checkpoint **source
   type**. Fixed by scoping that one ban to the pre-existing dashboard surface (compound
   strings stay banned globally). File now passes.
2. `test_under_alert_lifecycle` (3 tests: `…required_order`, `…every_game_carries_its_own_verdict`,
   `…no_alert_vocabulary_regression`) — **pre-existing**: run in isolation on the untouched
   base worktree they fail identically (`3 failed, 26 passed, 10 skipped`). They were only
   *visible* on the branch because the base full-run skipped them (missing repo-root
   `blm_pokerbet.db`, an artefact that has since been moved out of both trees).

Comparing the two final failure *sets* they are identical except one swapped pair:
`test_betual_line_worker_2026_10_04::test_persistence_failure_is_observable` (branch only)
and `test_v1_api::test_api_live_from_db` (base only). Both pass on the branch in isolation;
the base one fails with `sqlite3.OperationalError` because the base worktree has no
repo-root `blm_pokerbet.db`. They flip on the presence of that stray file, not on the
refactor — net new failures: **0**.

The remaining 113 failures are the tree's pre-existing state on this box (frontend
migration vocabulary guards, `pytest-asyncio`-less async tests, DB-dependent tests), all
present at the base commit. Nothing was skipped or silenced to improve the count.

## 5. Historical rescore — two clearly separated analyses

Source: the **entire** history available in the isolated environment —
`blm_pokerbet.db`, 2026-10-22T13:50Z → 2026-10-23T20:34Z (31 h), 163,500 snapshots,
1,084 non-Cyber games evaluated (31 Cyber games excluded by slug).

### 5.1 `prior_only` (default, causally strict)

| stage | count |
|---|---:|
| observations evaluated | 134,730 |
| … qualified | 52 |
| … UNKNOWN (reason-coded) | 120,719 |
| reconstructed firings | 75 |
| qualifying checkpoints stored | 1,080 |
| ledger rows | 1,155 |
| settled firings | 73 (55 UNDER / 18 OVER / 0 PUSH / 2 pending) |

| cohort | games | UNDER | OVER | rate | Wilson 95 % | break-even @1.60 |
|---|---:|---:|---:|---:|---|---:|
| TC-X ELITE | 1 | 1 | 0 | 100.0 % | 20.7–100 % | 62.5 % |
| TC-E EDGE | 6 | 5 | 1 | 83.3 % | 43.6–97.0 % | 62.5 % |
| TC-S SHARP | 9 | 8 | 1 | 88.9 % | 56.5–98.0 % | 62.5 % |
| TC-C CORE | 19 | 13 | 6 | 68.4 % | 46.0–84.6 % | 62.5 % |
| TC-W WIDE | 19 | 13 | 6 | 68.4 % | 46.0–84.6 % | 62.5 % |
| TC-C+ CORE+ | 0 | – | – | – | – | 62.5 % |
| TC-D DEEP | 0 | – | – | – | – | 62.5 % |

### 5.2 `full_sample` (production-parity reference)

Reconstructed firings **72**, settled **69** (51 UNDER / 18 OVER), 3 pending, ledger rows
1,011, unknown 120,212. TC-X 1 (1-0), TC-E 3 (3-0), TC-S 6 (5-1), TC-C 16 (11-5), TC-W 16
(11-5), TC-C+ 0, TC-D 0.

Both DBs are kept side by side (`derived/cohort_rescore.db`, `derived/cohort_rescore_full.db`)
and every API payload states which reference produced it (`league_ref` pill).

### 5.3 Idempotence, reconciliation, immutability

- Two consecutive `prior_only` runs → **byte-identical metrics JSON**; duplicate ledger keys
  (`source_game_id, observed_at, source_type`) = **0** in both DBs.
- Source DB content fingerprint before → after: snapshots `163,500 / 687179921711 /
  9415866 / 9225238`, game_results `2,504 / 42113745659`, size `272,490,496`,
  `PRAGMA quick_check = ok` — **identical**.
- Read-only opening is enforced in code (`mode=ro` URI); the rescore writes only the
  derived path.

### 5.4 Limitations (stated, not buried)

31-hour window; n ≤ 19 per cohort; TC-C ≡ TC-W on this slice (identical populations, not
independent samples); TC-C+/TC-D have **zero** observations here; firings are
**reconstructed**, since the system persists no alert ledger; historical rescore is
**descriptive, not prospective confirmation**; no profitability claim is made or implied.
The earlier 613-game / 58.73 % legacy figures are a different (legacy) rule set and were
not reused.

### 5.5 Blocker found — no broader dataset exists in the test environment

The directive asked to rebuild from "a broader historical dataset available in the isolated
test environment". There is none, verified three ways: the only DB > 100 MB on the box is
`blm_pokerbet.db` (272 MB, 31 h); `blm_historical.db` is 140 KB; the S3 objects are all
derived from the same slice. Obtaining a wider window requires production data, which this
task forbids — so the rescore above covers 100 % of the data that exists here and is
labelled exploratory. **This is the main reason no cohort conclusion should be drawn yet.**

## 6. Test deployment

```
unit     blm-tc2263.service (systemd --user, --collect, transient)
workdir  /home/ubuntu/blm-tc-refactor   (this branch)
env      BLM_DATA_ROOT=/srv/blm/data
         BLM_POKERBET_DB=/srv/blm/data/blm_pokerbet.db      (read-only use)
         BLM_COHORT_DB=/srv/blm/data/derived/cohort_rescore.db
         BLM_AUTH_ENABLED=0        ← test instance only; see below
         HOST=127.0.0.1 PORT=2263
```

`BLM_AUTH_ENABLED=0` is a documented config knob. It is used **only** on this loopback-only
instance (no production data, no betting, security group does not expose 2263) because the
single account in `blm_auth.db` has no credential available to me and I will not guess one.
Consequence: the dashboard's auth bootstrap logs one expected `404 /api/auth/me`; there are
no other console or page errors.

Verification:

| check | result |
|---|---|
| `systemctl --user is-active blm-tc2263` | active |
| `GET /` | 200 |
| `GET /api/v4/cohorts/{spec,summary,ledger,ledger?cohort=TC-C&source_type=reconstructed_firing,comparisons?by=price,quality}` | 200, 200, 200, 200, 200, 200 |
| Playwright (chromium headless) capture | 7 cohort cards, `TC-X` card reads `games 1 · UNDER 1 · rate 100.0% · Wilson 20.7–100.0% · firing cover 100.0% · obs cover 100.0%`, ledger row `2026-10-22T16:37 betual-nba TC-X … UNDER (239) QUALIFIED`, meta pills `rule tc-1.0.0 / window 2026-10-22 → 2026-10-23 / league ref prior` |
| console / page errors | 1 HTTP 404 (`/api/auth/me`, expected) / 0 |
| screenshot | `/tmp/tc/cohorts-panel-v2.png` on the test host |
| staging mirror untouched | `blm-server` still the process started 2026-10-26T20:45Z; `:2262/healthz` 200; `:80` 303 → login |

### Rollback (test instance only)

```
systemctl --user stop blm-tc2263          # panel disappears; :2262/:80 unaffected
# derived read model is droppable; nothing else depends on it
#   /srv/blm/data/derived/cohort_rescore{,_full}.db
git -C /home/ubuntu/blm-tc-refactor checkout <previous-sha>
```

No production DB, schema, migration, proxy, DNS or firewall change was made at any point,
and no Auto-Bet configuration, stake, cap, kill switch or submit path was touched. The
executor fails closed on this instance (no `BETTING_*` env), and no bet was ever submitted.

## 7. Remaining risks

1. **No production-scale evidence.** Every number here comes from 31 hours of test data.
2. **Rebase base is a snapshot**, not a reviewed commit: `992cd21` imports another agent's
   uncommitted work. The refactor is additive against it, but promotion must re-rebase onto
   whatever is actually checked out at promotion time and re-run the suites.
3. **Scored-points contract changes behaviour for dirty rows**: a source that emits
   `150.5` used to be silently truncated into a tradeable score; it is now `UNKNOWN`. That
   is the correct behaviour, but it changes counts. On this slice: no affected rows.
4. **League-reference policy is a product decision** (`prior_only` vs `full_sample`): 75 vs
   72 firings here, and materially different league averages. Do not default silently.
5. **Dashboard wording debt**: the panel intentionally uses "checkpoint", which the legacy
   vocabulary test bans for the older surface — resolved by scoping, worth a rename later.
