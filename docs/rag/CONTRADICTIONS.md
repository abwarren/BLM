# CONTRADICTIONS & Documentation Gaps

Retrieval keywords: contradiction, conflict, stale doc, documentation gap, source of truth,
discrepancy, unresolved, register.

Every contradiction between this KB (and the docs it cites) and the CURRENT
production code is recorded here explicitly. **None was silently resolved.** In each
case the current repository code is authoritative; the correction is flagged for the
owning doc.

Built 2026-10-03 against HEAD `d864988` (branch `handoff-2026-09-07`).

---

## C-01 — Fingerprint count: 4 (code) vs 7 (DECISIONS.md)

- **Code (authoritative):** `blm_v4/live_analytics/under_fingerprints.py` —
  `FINGERPRINT_KEYS = ("C1","C3","C5","R2")`; module docstring: "C2/C4/C6 are
  deliberately absent"; legacy momentum operands accepted but ignored.
- **Doc (stale):** `DECISIONS.md` §16.K describes "seven fingerprints (C1..C6 + R2)"
  including C2/C4/C6 with per-fingerprint rates; §16.J lists C2 74.8% (N=111).
- **Resolution:** code wins. C2/C4/C6 are RETIRED (see
  `DECISIONS/ADR-005-c2-retired.md`). `DECISIONS.md` §16.K/§16.J predate the
  retirement and are stale.
- **Risk if ignored:** an agent reading §16 would restore C2/C4/C6.

## C-02 — Deployment: `deploy/*.service` (stale) vs live `--user` units

- **Live (authoritative):** `~/.config/systemd/user/blm-collector.service` — user
  `ubuntu`, `WorkingDirectory=/home/ubuntu/BLM`, `ExecStart=... blm_v4.collector
  --tick 10`, `Type=notify`, `WatchdogSec=90`, `MemoryHigh=2500M`.
  `blm-server.service` — user `ubuntu`, `/home/ubuntu/BLM`, `PORT=2262`,
  `BLM_ENV=production`.
- **Doc (stale):** `DECISIONS.md` §12.B says collector
  `deploy/blm-collector.service`, `--tick 20`, user `gdi`, `/home/gdi/BLM`;
  `BLM_ENV=production` server "User gdi". The in-repo `deploy/*.service` files carry
  these old values.
- **Resolution:** live units win; `deploy/*.service` are STALE and must not be
  edited/trusted as production.

## C-03 — Scorecard percentage checkpoints: 75 missing in DECISIONS.md

- **Code (authoritative):** `blm_v4/scorecard.py` —
  `FIXED_CHECKPOINT_PCTS = [10, 20, 30, 40, 50, 60, 70, 75, 80, 90]` (75 added by the
  M-QUARTERLY-1 work; `SCORECARD_LOGIC_REVISION` "2").
- **Doc (stale):** `DECISIONS.md` §9.B lists primary predictive checkpoints as
  `10, 20, 30, 40, 50, 60, 70, 80, 90` — no 75.
- **Resolution:** code wins; 75 is a first-class bucket.

## C-04 — Test-suite counts are stale

- **Code/tree (authoritative):** the tree carries 2026-10-03 test files
  (`test_resulted_alert_settlement_2026_10_03.py`, `test_resulted_backfill_2026_10_03.py`,
  `test_quarter_score_capture_identity_2026_10_03.py`,
  `test_panel_discovery_coverage_2026_10_03.py`). STATUS records a focused suite of
  "218 passed, 0 failed".
- **Doc (stale):** `DECISIONS.md` §15.A says 88 files / 1,178 tests; §15.F and §13.E
  say "1053 passed / 3 failed, all 3 pre-existing"; §15.I says "42 passed".
  `docs/milestones/CURRENT.md` separately records "4 failed / 1533 passed" and
  "4 pre-existing".
- **Resolution:** do not trust any recorded count as current; run the named targeted
  files and count from the live run. The "3 vs 4 pre-existing failures" figures
  disagree across docs and are both stale.

## C-05 — `result_reconciliation.outcome` vocabulary vs DDL comment

- **Code (authoritative):** `blm_v4/result_reconciler.py` writes `VERIFIED`,
  `REJECTED`, `TEMPLATE_FAILED`, `FAILED_ATTEMPT`; the worker also branches on
  `CONFLICT`.
- **DDL comment (incomplete):** the `result_reconciliation` schema comment reads
  `VERIFIED | FAILED_ATTEMPT | TEMPLATE_FAILED | CONFLICT` — it OMITS `REJECTED`,
  which the candidate SQL and writers use.
- **Resolution:** treat the outcome set as
  {VERIFIED, REJECTED, FAILED_ATTEMPT, TEMPLATE_FAILED, CONFLICT}. The schema comment
  is incomplete (a documentation gap in the DDL, not a code defect).

## C-06 — `game_results.final_result_status` DDL comment vs production values

- **Production (authoritative):** values observed live include `OK`, `UNKNOWN`,
  `NEEDS_RECONCILIATION`, `INVALID`.
- **DDL comment (incomplete):** the live `game_results` DDL comment on
  `final_result_status` reads `-- OK | UNKNOWN`.
- **Resolution:** treat the value set as
  {OK, UNKNOWN, NEEDS_RECONCILIATION, INVALID}. Documentation gap only.

## C-07 — `_record_quarter_scores` docstring vs live WS frames

- **Live data (authoritative):** a `ws_raw_frames` payload holds
  `info.additional_data.quarterScores` for every listed game (STATUS 2026-10-03).
- **Docstring (contradicted):** `collector._record_quarter_scores` asserts "WS frames
  carry no quarter scores — audited 2026-09-22".
- **Resolution:** verify the docstring before trusting it; the live frame is
  authoritative. A coverage lever exists on the WS path.

## C-08 — The `docs/rag/` documentation KB vs DECISIONS.md §6g.1 RAG freeze

- **Not a code contradiction — a scope clarification.** `DECISIONS.md` §6g.1 freezes
  the RAG *architecture*: the SQLite FTS5 index (`rag/blm_context.db`) + knowledge
  store (`~/.clai/knowledge.db`) + `DECISIONS.md` + `FOUNDATION.md`.
- **This KB** (`docs/rag/**`) is a DOCUMENTATION knowledge base — retrieval-oriented
  prose packs — NOT a redesign of that index. It feeds the index; it does not replace
  or redesign it. No index code was changed.

---

## Unresolved documentation gaps (not contradictions)

## G-01 — BLM model weighting (P/L/I/B/T) is not in the code

The directive references a model weighting "P = 0.30, L = 0.20, I = 0.20, B = 0.10,
T = 0.20". A search of `blm_v4/**` and `docs/BLM_MODEL_SPEC.md` found NO such
weighting. The production model blend is the 70/30 pace/market blend
(`projection.project`). **Do not implement an invented weighting**; if this
weighting exists it lives outside the production code and must be sourced before it
is documented as a rule. Recorded here so no agent invents it.

## G-02 — "Q3 Trap" / "Reverse Bull Trap" naming

The directive names a "Trap Meter" with a "Q3 Trap" and "Reverse Bull Trap". The code
has `reverse_bull_trap` (in `api._detect_signals`) but NO "Q3 Trap" concept. The Q3
logic that exists is (a) the `Q3_BREAK` checkpoint and (b) the `q3_ratio` leg of the
fingerprint layer. Documented under those real names in `10_TRAP_METER.md`; a "Q3
Trap" constant does not exist and must not be referenced.

## G-03 — `result_conflicts` vs `reconciliation` table naming

Two distinct tables: `result_conflicts` (flagged DOM-vs-results-page disagreements)
and `reconciliation` (BetConstruct event correspondence). Neither is the other; the
near-identical names are a documentation hazard. Clarified in `04_DATABASE_SCHEMA.md`.

## G-04 — `blm_v1` legacy post-final writes

`DECISIONS.md` records legacy `blm_v1` post-final snapshot writes as pre-existing and
out of scope. Not verified in this build; treat as a known open item.

## G-05 — No CONTEXT.md / living-index coupling

This KB is authored against HEAD `d864988`. It is NOT auto-regenerated. On a material
change to `blm_v4/**`, `05_PRODUCTION_RULES.yaml` and the affected packs must be
re-verified (see `01_AUTHORITATIVE_SOURCE_CONTRACT.md`).
