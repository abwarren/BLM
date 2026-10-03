# 10 — Trap Meter & Signal Layer

Retrieval keywords: trap meter, trap, bull trap, bear trap, reverse bull trap, late trap,
sharp trap, dead market, false momentum, fingerprints, C1, C3, C5, R2, C2 retired,
retired logic, do not resurrect, line vs score, Q3 collapse.

**What this pack answers:** what the trap/line-vs-score signals are, what the
fingerprint layer is, and — critically — which logic is RETIRED and must never be
resurrected.

> ⚠️ Naming note: **"Q3 Trap" is NOT a code term.** The code concepts are the
> `_detect_signals` trap family (below) and the Q3 leg of the fingerprint layer
> (`q3_ratio`). Do not reference a "Q3 Trap" constant; it does not exist.

---

## Part A — Line-vs-score trap signals

**Rule.** `api._detect_signals(rows)` labels heuristics from line-vs-score dynamics.
Pure function of the snapshot history; seven signals, each `{active, confidence}`:

| Signal | Condition (from the code) |
|---|---|
| `dead_market` | ≥3 ticks identical line while score advanced ≥4 |
| `false_momentum` | recent score burst (vel > 2.5) with no line response (`|Δline| < 0.5`) |
| `bull_trap` | line raised (Δ>0.5) while scoring stalled (Δscore ≤ 1) |
| `bear_trap` | line cut (Δ<−0.5) while scoring accelerates (Δscore ≥ 2) |
| `reverse_bull_trap` | line cut (Δ<−0.5) into a scoring surge (vel > 2.0) |
| `late_trap` | line moved (|Δ|≥0.5) on the freshest tick after ≥3 static ticks |
| `sharp_trap` | abrupt line move (|Δ|≥2.0) without score movement (|Δscore| < 2) |

**Source.** `blm_v4/api.py::_detect_signals`.

**Why.** Honest, data-backed descriptive signals of market/score misalignment.

**Valid.** Line up while scoring stalled → `bull_trap` active.

**Invalid.** Reading a trap signal as a production alert or a bet trigger.

**Edge.** Needs ≥3 scored snapshots; otherwise all signals inactive.

**Forbidden.** Wiring a trap signal into the alert or bet gate.

**Verify.** `_detect_signals(rows)` output; `10`'s signals gate nothing.

---

## Part B — The fingerprint layer (C1, C3, C5, R2)

**Rule.** The approved historical fingerprints are EXACTLY four, in canonical order:

```
C1  1.10 <= req_ratio < 1.20                       (lower INCLUSIVE, upper EXCLUSIVE)
C3  req_ratio > 1.04  AND q3_ratio < 1.00          (req leg STRICT > production margin)
C5  req_ratio > 1.10  AND q3_ratio < 1.00
R2  q3_ratio < 0.90                                (material Q3 slump)
where req_ratio = required_pts_per_min / league_average_pace
      q3_ratio  = q3_ppm / league Q3 average (same competition)
```

Three-state semantics: TRUE / FALSE / UNAVAILABLE (any missing operand ⇒
UNAVAILABLE — missing data is NEVER TRUE). A conjunction is UNAVAILABLE if any leg
is. `fingerprint_count` counts TRUE only; recorded for analysis, gates nothing.

**Source.** `live_analytics/under_fingerprints.py`; `live_analytics/fingerprint_c5.py`
(`C5_REQ_RATIO_MIN=1.10`, `C5_Q3_RATIO_MAX=1.00`); authorization 2026-09-21.

**Why.** An enrichment/context layer on the existing alert — never a second alert
source and never a loosening of the condition.

**Valid.** req_ratio 1.15 → C1 TRUE.

**Invalid.** req_ratio exactly 1.20 → C1 FALSE (upper edge exclusive).

**Edge.** An unavailable league Q3 reference → the Q3 leg (and any conjunction
using it) is UNAVAILABLE, not FALSE.

**Forbidden.** Treating UNAVAILABLE as a pass; making a fingerprint a gate.

**Verify.** `evaluate_fingerprints(...)`; a boundary value at each edge.

---

## Part C — RETIRED / EXCLUDED logic (do not resurrect)

## Rule 10.C.1 — C2, C4, C6 are RETIRED

**Rule.** The live fingerprint layer is EXACTLY C1, C3, C5, R2. **C2 and its
dependent composites (C4, C6) were REMOVED.** They must NOT be restored from
historical code or docs.

**Source.** `under_fingerprints.py` module docstring: "C2/C4/C6 are deliberately
absent"; `FINGERPRINT_KEYS = ("C1","C3","C5","R2")`; the legacy momentum operands
(`recent3_pace`/`actual_pace`/`recent3_minus_act`) are accepted for compatibility
but "deliberately ignored: C2 and its dependent composites were removed from the
live historical fingerprint layer."

**Why.** C2 (`recent3_minus_act <= -0.5`) and its composites were retired. The
operands still exist for API compatibility, so an agent reading old code could
wrongly decide to restore them.

> ⚠️ **Contradiction:** `DECISIONS.md` §16.K still describes SEVEN fingerprints
> (C1..C6 + R2) including C2/C4/C6, with per-fingerprint rates. That description
> predates the retirement and is STALE. Current code (four fingerprints) is
> authoritative. See `CONTRADICTIONS.md`.

**Valid.** A live payload shows at most C1, C3, C5, R2.

**Invalid.** Re-adding `fingerprint_c2` because an old doc lists it.

**Forbidden.** Registering C2/C4/C6 as active conditions.

**Verify.** `FINGERPRINT_KEYS` == `("C1","C3","C5","R2")`; AST test asserts no C2
key.

---

## Rule 10.C.2 — R1 is EXCLUDED

**Rule.** R1 (the widened required-pace band `[1.10, 1.35)`) is NOT implemented,
NOT registered, NOT used in combinations, NOT referenced as an active condition. The
C1 upper cut at 1.20 (exclusive) exists precisely because `[1.20, 1.35)` ran below
baseline.

**Source.** `under_fingerprints.py` (R1 exclusion, enforced by test); R1 historical
value 56.47% vs the 59.71% baseline.

**Why.** R1 measured BELOW the historical UNDER baseline and was rejected.

**Valid.** req_ratio 1.30 fires NOTHING.

**Invalid.** Widening C1 to 1.35.

**Forbidden.** Reintroducing R1.

**Verify.** An AST/source scan finds no R1 key/name/1.35 constant.

---

## Rule 10.C.3 — Other retired / superseded logic

**Rule.** Do not resurrect:

- The `actual < BOTH` alert condition (commit 73b7277) — superseded by the single
  relative-margin rule (`DECISIONS.md` §14.A).
- The 50% progress tier — removed (48.51% = coin flip) (§14.B).
- The `actual < league_average` leg (§14.C).
- An absolute pts/min margin — rejected; the margin is relative (§14.D).
- Bucket-name-based terminality — superseded by bucket independence (§14.G).
- The `'PUSH (equal)'` signal label — replaced by NO_EDGE (§14.H); PUSH = settlement
  only.

**Source.** `DECISIONS.md` §14.

**Why.** Each was measured and rejected; restoring it re-introduces a known defect.

**Forbidden.** Restoring any of the above from historical code.

**Verify.** `DECISIONS.md` §14; the corresponding tests (`test_z_*` guard retired
numbers/terminology).
