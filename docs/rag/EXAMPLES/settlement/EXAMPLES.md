# EXAMPLES — Settlement

Retrieval keywords: settlement examples, positive, negative, edge case, under, over,
push, final_total, trigger_total, final_source, settled, observation, authoritative.

Each case: the RULE, a POSITIVE example, a NEGATIVE example, an EDGE case.

---

## Case S-1 — The outcome rule

**Rule.** `final < trigger -> under`; `final > trigger -> over`; `final == trigger ->
push`; either unprovable -> `None` (no colour).

**Positive (authoritative).**
```
game_results: final_total = 201, final_result_status = OK
trigger_total = 205.5
201 < 205.5 -> "under"  (GREEN), final_source = "settled", authoritative = True
```

**Negative (not settled).**
```
game_results: final_result_status = INVALID, final_total = NULL
trigger_total = 205.5
-> status = None  (PENDING, no colour)   [game.status='ended' was NOT used]
```

**Edge (push).**
```
final_total = 205.5 == trigger_total 205.5 -> "push" (neutral)
```

---

## Case S-2 — final_source provenance

**Rule.** `authoritative` is True only for `final_source == "settled"` (an OK
`game_results` row). A terminal-observation final is NOT authoritative and can never
revise a settlement.

**Positive.**
```
game_results OK row present -> final_source "settled", authoritative True
```

**Negative.**
```
no OK row, terminal snapshot score present -> final_source "observation",
authoritative False  -> verdict reported but NOT settlement-grade
```

**Edge.** `final_total = None` -> final_source `None`, status "pending".

---

## Case S-3 — Real production pair (2026-10-03)

**Rule.** The reconciler recovers a complete final; the derived alert then settles
itself against the frozen trigger line.

**Positive.**
```
31093644 (CYBER_2K26): game_results final 217, status OK, source RESULTS_PAGE
  Q3_BREAK trigger line 199.5 -> 217 > 199.5 -> OVER (RESOLVED)
31101100 (BETUAL_NBA): game_results final 265, status OK
  Q3_BREAK trigger line 269.5 -> 265 < 269.5 -> UNDER (RESOLVED)
```

**Negative (before recovery).**
```
same games with final_total NULL -> verdict RESULT PENDING indefinitely
(the verdict rule was never broken; the FINAL was missing)
```

**Edge.** 31101100 already held an OK final before activation — it was never
actually stuck (correct the premise).

---

## Case S-4 — RESULTS_PAGE immutability under a disagreeing endpoint

**Rule.** A RESULTS_PAGE OK row is never overwritten; a disagreement is flagged.

**Positive.**
```
game_results: final_total 217, OK, source RESULTS_PAGE
captured history endpoint total = 215 (disagrees)
-> row KEEPS 217; result_conflicts row written (live_dom_total 215, results_total 217)
```

**Negative (the pre-fix clobber — must not happen).**
```
game_results: final_total NULL, status UNKNOWN, result_source RESULTS_PAGE (stale lie)
-> destroyed a verified final; the game never retried
```

**Edge.** A SNAPSHOT-derived OK row (no RESULTS_PAGE) IS legitimately corrected in
place — only the page-verified verdict is immutable.

---

## Case S-5 — Colour vs blank

**Rule.** No colour when a final OR a line is unprovable; it is correct by design,
not a bug.

**Positive.**
```
final provable (OK) AND trigger line provable -> GREEN/RED/PUSH
```

**Negative (blank, correctly).**
```
final_result_status ∈ {UNKNOWN, NEEDS_RECONCILIATION, INVALID} -> no colour
```

**Edge.** A NO-LINE row (trigger line unprovable at the boundary) is blank BY
DESIGN — never force-resolve it.
