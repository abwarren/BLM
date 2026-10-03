# EXAMPLES — Alert Triggering

Retrieval keywords: alert examples, positive example, negative example, edge case,
trigger, 75, required pace, eligible, Q3_BREAK, fingerprints.

Each case: the RULE, a POSITIVE example, a NEGATIVE example, an EDGE case.

---

## Case A-1 — The production UNDER trigger

**Rule.** `active = eligible AND progress_pct >= 75.0 AND required_pts_per_min >
league_average_pace * 1.04` (STRICT).

**Positive.**
```
progress_pct = 83.0        (>= 75)
required_pts_per_min = 1.30
league_average_pace  = 1.20
eligible = True            (live game + LIVE market)
1.30 > 1.20*1.04 = 1.248  ->  active = TRUE
```

**Negative.**
```
progress_pct = 68.0        (< 75 -> no alert of any kind)
-> active = FALSE (checkpoint 50 only)
```

**Edge (strict boundary).**
```
required_pts_per_min = 1.248, league_average_pace = 1.20
1.248 > 1.248  ->  FALSE   (exactly at the bar does NOT qualify)
```

---

## Case A-2 — Eligibility (fail closed)

**Rule.** A stale/missing market or stale state suppresses; it never creates. The
opening line is never substituted.

**Positive.**
```
market_status = "LIVE"  (line observed <= 300s), live = True
-> eligible = True, reason = "market_live"
```

**Negative.**
```
market_status = "STALE"  (line observed > 300s)
-> eligible = False, reason = "market_stale"   (quantitative block still served)
```

**Edge.**
```
market_status = None / unrecognised
-> eligible = False, reason = "market_missing" (fail closed)
```

**Forbidden.** eligible=True with the opening line standing in for a missing live
line.

---

## Case A-3 — The frozen trigger line (75% crossing)

**Rule.** The trigger line is the line in force at the FIRST observation >= 75%,
from `clean_projections` (fallback snapshots).

**Positive.**
```
75% boundary observation: progress 0.752, clean_projections.live_total_line = 205.5
-> trigger_line = 205.5 (frozen)
```

**Negative.**
```
firing tick (a later, eligible tick): live line moved to 199.5
-> NOT the trigger line (using it inflated the observed rate by ~14 pp)
```

**Edge.**
```
no clean_projections line by the boundary, but a snapshot line of 203.5 exists
-> trigger_line = 203.5 (snapshot fallback)
neither store has a line -> trigger unprovable (None), never fabricated
```

---

## Case A-4 — Q3_BREAK (additive)

**Rule.** At 75.0% with `remaining_minutes = quarter_minutes`,
`required = (triggered_line - score_at_trigger)/quarter_minutes`; same condition.

**Positive (CYBER_2K26, quarter 12).**
```
triggered_line = 199.5, score_at_trigger = 168, quarter_minutes = 12
required = (199.5-168)/12 = 2.625 ; league*1.04 = 1.248 -> active = TRUE
checkpoint = "Q3_BREAK"
```

**Negative.**
```
league reference missing -> active = FALSE (fail closed, never borrow)
```

**Edge.** Q3_BREAK settles against the SAME 75% boundary line, under its own key
`by_checkpoint["Q3_BREAK"]`.

---

## Case A-5 — Fingerprints are context, never a gate

**Rule.** C1, C3, C5, R2 are recorded context; they create no alert.

**Positive.**
```
required 1.30, league 1.20 -> req_ratio 1.083  -> C1 FALSE (below 1.10), C3 TRUE
active (from the rule) = TRUE ; fingerprints_fired = ["C3"]
```

**Negative.**
```
A hypothetical alert is NOT active (required 1.10, league 1.20)
even though C-something "matches" -> still FALSE (fingerprints cannot create)
```

**Edge.**
```
league Q3 reference unavailable -> the Q3 leg (and C3/C5/R2) = UNAVAILABLE
UNAVAILABLE is NEVER TRUE (not a pass)
```
