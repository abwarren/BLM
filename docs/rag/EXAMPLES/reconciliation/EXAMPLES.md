# EXAMPLES — Reconciliation

Retrieval keywords: reconciliation examples, positive, negative, edge case, PENDING,
candidate_games, REJECTED, retry cooldown, base id, #iN, feed absence, identity.

Each case: the RULE, a POSITIVE example, a NEGATIVE example, an EDGE case.

---

## Case R-1 — A PENDING row is a missing final

**Rule.** PENDING <=> a missing final. The repair is to recover the final.

**Positive.**
```
game ended; game_results.final_result_status = NULL/UNKNOWN; final_total NULL
-> PENDING ; fix = recover the final into game_results
```

**Negative.**
```
Searching for rows in an "alerts" table to update -> none exist (derived alerts)
```

**Edge.** A NO-LINE row is PENDING by design (line unprovable), not a missing final.

---

## Case R-2 — The retry cooldown re-arms a rejected game

**Rule.** A REJECTED/FAILED_ATTEMPT game re-enters the candidate set once
`updated_at <= now - RETRY_COOLDOWN_S` (3600s). `retry_cooldown_s=0` restores the
old terminal cap. TEMPLATE_FAILED stays terminal.

**Positive.**
```
attempt = 3, outcome = REJECTED, updated_at 2h ago
-> candidate (re-armed) -> feed returns a complete final -> VERIFIED, OK written
```

**Negative.**
```
attempt = 3, outcome = REJECTED, updated_at 10 min ago (inside 1h cooldown)
-> NOT readmitted (no request storm)
```

**Edge (old bug).**
```
attempt = 3, outcome = REJECTED, no cooldown -> left the work set FOREVER
-> 2,486 games permanently exhausted ; their alerts stuck PENDING
```

---

## Case R-3 — Identity decides an accepted final

**Rule.** A base id's reply is REJECTED for an `#iN` instance game.

**Positive.**
```
base game 31000000 matches fixture identity; score consistent with history
-> VERIFIED, OK written for the base id
```

**Negative.**
```
game 31000000#i1 (an instance); feed answers for the BASE id 31000000
-> REJECTED ; the #i1 keeps result_source NULL instead of a foreign score
```

**Edge.** The fixture-start window uses `max(regulation, observed_span)`; a virtual
fixture's wall-clock span (2,915s for 2,400s regulation) is accepted where the old
regulation-only bound rejected it.

**Edge observed.** game 31000000#i1 INVALID final, base game OK final -> DO NOT
attach the base final to #i1 (identity protected).

---

## Case R-4 — Page-result validity

**Rule.** Accept only when identity AND complete render (4 quarters) AND quarter
sums AND history consistency hold.

**Positive.**
```
parse_quality = "full" (4 quarters), sums match the final line,
final sides >= observed max -> passed
```

**Negative.**
```
parse_quality = "partial", render_complete = false -> REJECTED
(final published minutes later is the real final — re-arm catches it)
```

**Edge.** No captured history at all -> history-consistency is INAPPLICABLE (the
render stands on its own), not a failure.

---

## Case R-5 — Feed absence is an observation, not a failure

**Rule.** A feed reply with no entry for the id is skipped, never stamped.

**Positive.**
```
feed has no entry for the id -> skip ; the live worker re-arms hourly
(late publication still caught)
```

**Negative.**
```
No feed entry -> stamp the game as unresolved/failed -> WRONG (absence != failure)
```

**Edge.** The feed answers a ROTATING subset per call -> run several rounds; rounds
6-10 verified 0 (convergence).
