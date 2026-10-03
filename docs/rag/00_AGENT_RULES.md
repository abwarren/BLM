# 00 — BLM Agent Rules

Retrieval keywords: agent rules, operating rules, handoff, read-only, fail-closed,
evidence, source of truth, do not, forbidden, production safety, betting safety,
what may I do, how to report, do not restart, do not commit.

This pack governs **behaviour**, not facts. The other packs supply facts.

---

## Rule 0.1 — Source of truth

**Rule.** Answer from the current source code, tests and live production state.
The RAG/docs are a retrieval layer, never the authority. On conflict, code wins.

**Source.** `DECISIONS.md` §AUTHORITY; `DECISIONS.md` §6b (memory is a guide, not
ground truth).

**Why.** Documentation drifts; the running pipeline does not (as long as the
process was started after the code changed — see Rule 0.8).

**Valid.** "`under_alert.py` line 60 is `ALERT_PROGRESS_PCT = 75.0`; the served
verdict equals `_v4_live_uncached`" — cite the file, line, function.

**Invalid.** "The docs say the threshold is 75, so it is 75" — without reading
the module.

**Edge.** Memory contradicts code → report the conflict, treat verified code as
authoritative, update memory (§6b).

**Forbidden.** Silently choosing memory over code; silently editing docs to hide
a conflict.

**Verify.** `read_file` the module; run the relevant test; query the live DB
read-only.

---

## Rule 0.2 — READ-ONLY discipline (do this first, always)

**Rule.** A diagnosis must not write. Never construct the production
`BettingStore` (its `__init__` runs `CREATE TABLE`/`ALTER TABLE` migrations and
mutates `blm_betting.db`). Call the real decision functions with `claim=False`
where provided. Open every DB read-only (`sqlite3.connect("file:...?mode=ro",
uri=True)` — or the SQLite `-readonly` CLI flag). Never run `PRAGMA
wal_checkpoint` against a live DB while the collector is running.

**Source.** `DECISIONS.md` §12.G; `blm_v4/betting/store.py`; the read-only shim in
`scripts/blm_gate_ladder.py`.

**Why.** Diagnosing a live pipeline by mutating it destroys the evidence you are
trying to read and can corrupt production state.

**Valid.** `sqlite3 -readonly blm_pokerbet.db "SELECT ..."`; importing
`blm_v4.live_analytics.under_alert.under_alert_state` (pure function) in a probe;
`executor.evaluate(..., claim=False)`.

**Invalid.** `sqlite3 blm_pokerbet.db "UPDATE ..."` during analysis; importing
`BettingStore` just to "check" it.

**Edge.** A read-only *copy* (via SQLite `backup()`) is acceptable for
end-to-end proofs.

**Forbidden.** Any write, migration, checkpoint or restart during a READ-ONLY
directive.

**Verify.** `git -C /home/ubuntu/BLM status --short` unchanged vs baseline;
`blm_betting.db` row counts unchanged.

---

## Rule 0.3 — Never claim a state without evidence directly beneath it

**Rule.** Never write **FIXED / VERIFIED / HEALTHY / COMPLETE** without the
evidence immediately underneath the claim (command, result, row counts, SHA).

**Source.** `AGENTS.md` §2.9 (No False Completion); the user's standing directive.

**Why.** A claim with no evidence is indistinguishable from a guess, and the next
agent will act on it.

**Valid.** "`blm-collector` active — `systemctl --user is-active blm-collector`
→ `active`; newest snapshot `captured_at` = 2026-10-03T21:43Z."

**Invalid.** "Everything looks healthy."

**Edge.** Use `IMPLEMENTED — VERIFICATION PENDING` or `PARTIALLY IMPLEMENTED`
when verification is incomplete.

**Forbidden.** Writing `DONE` before acceptance criteria are verified.

**Verify.** Every claim maps to a command + output shown in the report.

---

## Rule 0.4 — Never fabricate a value

**Rule.** A missing input yields `NULL` / `None` / fail-closed — never a borrowed
or invented value. "No robust parameter found" is a legitimate answer.

**Source.** Recurring test invariant `DECISIONS.md` §15.H; `under_alert.py`
fail-closed; `under_outcome.outcome_status` returns `None` if either operand is
`None`; `result_policy` fail-closed validation.

**Why.** A fabricated final settles a bet; a fabricated league average creates a
false alert.

**Valid.** No league reference → `active=False`. No provable final → verdict
`None` (PENDING).

**Invalid.** Substituting the opening line for a stale live line
(`DECISIONS.md` §14.F); inferring a final from the last snapshot when no
authoritative final exists.

**Edge.** An "absence" in a feed is an observation, never a failure — skip, never
stamp.

**Forbidden.** `final_total = last_snapshot_total` as a settlement.

**Verify.** Grep the code path for a `None`/fail-closed branch; run the
"no fabricated value" tests.

---

## Rule 0.5 — Production change requires its own authorization

**Rule.** Committing, pushing, restarting a service and deploying each require
their OWN explicit authorization. A directive to "implement a fix" is **not**
authorization to activate it.

**Source.** `DECISIONS.md` §15 (items 15–18: do not make production changes /
restart services / push commits / alter production DB contents unless explicitly
instructed).

**Why.** Activation changes live state and can be irreversible on a running box.

**Valid.** "The deploy requirement is its own section: exact command, target unit,
expected effect — then STOP."

**Invalid.** Restarting `blm-server` because a fix "looks ready".

**Edge.** A READ-ONLY directive forbids every write *and* every restart.

**Forbidden.** `systemctl --user restart ...` without an explicit instruction.

**Verify.** The handoff's §11 PRODUCTION ACTIONS block records YES/NO for each.

---

## Rule 0.6 — Never disturb another agent's work

**Rule.** In a multi-agent repo the dirty tree IS other agents' work. Never
commit, stash, reset, clean, checkout, or force-push to unblock yourself.
Integrate via an isolated `git worktree`. Never reset/force-push/rewrite history.

**Source.** `AGENTS.md` §2.1; the user's multi-agent rule.

**Why.** Destroying an uncommitted fix loses work that is not recoverable.

**Valid.** "63 dirty owner paths preserved, untouched" in the handoff.

**Invalid.** `git stash` "just to run tests".

**Edge.** An ownership-blocked landing is a legitimate STOP — report, do not
force.

**Forbidden.** `git reset --hard`, `git checkout -- .`, `git clean -fd`.

**Verify.** Dirty-file count unchanged across the session; `git status --short`
diffed against the pre-run baseline.

---

## Rule 0.7 — Correct the user's premise when evidence contradicts it

**Rule.** If a cited "stuck" game already holds an OK final, say so and quantify
which cases a fix actually unblocks. Do not inflate a fix's scope to match the
report.

**Source.** The user's reporting style (blm skill §Reporting style).

**Why.** A truth that contradicts the premise is the most valuable finding.

**Valid.** "Of the 3 cited games, 2 already have an OK final; the fix unblocks
1."

**Invalid.** Silently re-scoping the work to include the already-fixed cases.

**Forbidden.** Claiming a fix repaired cases it did not.

**Verify.** Per-game query of `game_results.final_result_status`.

---

## Rule 0.8 — On-disk correctness ≠ live

**Rule.** The collector/server run from the **working tree** and load modules at
process START. Before claiming a fix is live, prove the RUNNING process holds it:
process start time > module mtime, NRestarts unmoved, a behavioural counter
unreachable under the old code, or a `/proc/<pid>/mem` / `py-spy` frame inspection.

**Source.** `blm_v4/collector.py` (imports at start); skill reference
`activating-a-verified-fix.md`.

**Why.** A fix verified on disk with a process still running old code is the exact
trap that cost this pipeline days.

**Valid.** "`pending_resolve` 18–30 is unreachable under the regressed code (cap
≈ 6)."

**Invalid.** "The file is correct, so the fix is live."

**Edge.** A changed collector PID is often the self-watchdog, not you — read the
journal before attributing it.

**Forbidden.** Raising a timeout or restarting to "fix" a watchdog flap.

**Verify.** BEFORE → AFTER counter table (PID, `ExecMainStartTimestamp`, the
counter, module sha256).

---

## Rule 0.9 — Distinguish authoritative / provisional / pending / inferred

**Rule.** Never combine these silently. `final_source="settled"` (a
`game_results` OK row) is authoritative; `final_source="observation"` (a terminal
snapshot) is not; a missing final is PENDING; anything derived is inferred.

**Source.** `under_outcome.under_alert_outcome` (`final_source` / `authoritative`
fields); `result_policy.is_results_page_verdict`.

**Why.** A verdict from an observation looks identical on screen to one from a
verified final — only provenance tells them apart.

**Valid.** "Verdict `over`, `final_source=observation`, non-authoritative."

**Invalid.** Reporting an observation-settled verdict as a settled result.

**Forbidden.** Settling a PENDING row by guessing the final.

**Verify.** Read `final_source` / `authoritative` on the payload.

---

## Rule 0.10 — Mandatory handoff report format

**Rule.** Every technical handoff uses the exact 14-section structure below.
Never claim a state without evidence beneath it. The final section MUST contain an
executable NEXT AGENT DIRECTIVE (exact action / prohibited actions / success
criteria).

```
# BLM AGENT HANDOFF
## 1. EXECUTIVE STATUS   (STATUS · TASK · DATE/TIME UTC · ENVIRONMENT · HEAD · BRANCH · DEPLOYED VERSION · ONE-LINE VERDICT)
## 2. OBJECTIVE          (requested objective · success criteria)
## 3. CURRENT STATE      (services table · database table · data pipeline diagram: SOURCE→COLLECTOR→SNAPSHOT→MARKET OBSERVATION→ALERT→RESULT RECONCILIATION→SETTLEMENT, marking exactly where healthy/broken)
## 4. CHANGES MADE       (table: change · file · commit SHA · reason; explicit "NO CHANGE" where applicable)
## 5. TESTS AND VERIFICATION (exact commands + results; PASS/FAIL/SKIPPED/WARNINGS; production verification table: API/Collector/Database/Alert lifecycle/Settlement)
## 6. DATA / BUSINESS RESULTS (alerts triggered · settled · UNDER · OVER · PUSH · PENDING; by checkpoint/league/model/alert type; distinguish authoritative/provisional/pending/inferred)
## 7. AUTHORITATIVE SOURCE CHECK (exact source for each critical value; CONFLICT/AUTHORITY/REASON if sources disagree)
## 8. UNRESOLVED ITEMS   (Priority · Issue · Impact · Current State · Next Action)
## 9. RISKS / CAVEATS    (RISK · IMPACT · EVIDENCE · MITIGATION — verified risks only)
## 10. FILE / REPO STATE (repository · branch · HEAD · origin · ahead/behind · working tree · dirty files · untracked · owner-held)
## 11. PRODUCTION ACTIONS (DEPLOYED · RESTARTED · DATABASE WRITTEN · CODE WRITTEN · CONFIG CHANGED · ROLLBACK PERFORMED — each YES/NO)
## 12. ARTIFACTS / EVIDENCE (logs · audits · scripts · test outputs · queries · screenshots · API responses · commit SHAs, with paths)
## 13. NEXT AGENT DIRECTIVE (NEXT ACTION · DO NOT list · SUCCESS CRITERIA)
## 14. FINAL HANDOFF VERDICT (exactly one: CONTINUE · BLOCKED · HOLD · READY FOR DEPLOYMENT · DEPLOYED / VERIFIED)
```

**Source.** This directive + `AGENTS.md` §2.8.

**Why.** State → work → evidence → risks → next action must be reconstructable
without chat history.

**Forbidden.** A handoff without an executable NEXT ACTION; a "healthy"/"verified"
claim with no evidence block.

**Verify.** Every section present; §11 explicitly records what production actions
did/did not happen; §14 uses one exact verdict word.

---

## Rule 0.11 — Respect the terminal-checkpoint boundary

**Rule.** Terminal observations (`terminal=1`) are SETTLEMENT / AUDIT only; they
are never predictive. Terminality comes from the row's own game-time evidence, not
a bucket/percentage label.

**Source.** `blm_v4/terminal_eligibility.py`; `DECISIONS.md` §9.F.

**Why.** Leaking a terminal score into predictive statistics inflates accuracy.

**Valid.** A 98.1% snapshot in the `pct100` bucket is non-terminal.

**Invalid.** Stamping terminal because a row is in the 100% bucket.

**Forbidden.** Deleting terminal rows (they are retained for settlement/audit).

**Verify.** `is_terminal_checkpoint(...)` on the row's own fields.

---

## Rule 0.12 — Auto-Bet safety hierarchy (non-negotiable)

**Rule.** Before any Auto-Bet work, hold this priority order:

1. Safety / execution authorization
2. R2.00 exact real-money TEST stake constraint
3. Alert Monitor as the betting-opportunity source
4. Frozen alert trigger line
5. Rung validation
6. Execution

`R2.00` (ZAR 2.00) is the EXACT authorized real-money **test** stake only — never
the production stake. The production stake is the user-configured **BLM Unit
Size**. No valid unit size → FAIL CLOSED → NO BET. Auto-Bet consumes an existing
BLM alert and must NOT discover an opportunity, establish a new baseline, or
create a new trigger line.

**Source.** `docs/autobet/AUTOBET_SAFETY_CONTRACT.md`;
`blm_v4/betting/stake.py`; `blm_v4/betting/rung.py`.

**Forbidden.** Hard-coding a production stake; treating R2.00 as the production
default; performing the R2.00 real-money test without its own authorization.

**Verify.** `tests/test_autobet_stake_and_unit_size_2026_10_03.py`.
