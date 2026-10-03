# EXAMPLES — Collector Failures

Retrieval keywords: collector examples, positive, negative, edge case, thread affinity,
page.content, watchdog, discovery coverage, counter, WS frames, quarter scores.

Each case: the RULE, a POSITIVE example, a NEGATIVE example, an EDGE case.

---

## Case C-1 — Thread affinity

**Rule.** Playwright calls, including `page.content()`, must stay on the owning
thread.

**Positive.**
```
on the owning thread: page.locator('[class*="market-game-section"][class*="active"]')
  .first.evaluate("el => el.outerHTML")  -> selected section markup
```

**Negative.**
```
a worker thread calls page.content() -> "Cannot switch to a different thread"
```

**Edge.** A single-element DOM call avoids the whole-page round-trip that overruns
the tick budget.

---

## Case C-2 — Discovery coverage is a counter

**Rule.** Compare a live external truth (panel row count) to the collector's counters.

**Positive.**
```
panel = 40 relevant rows / 6 competitions
collector: games_tracked ~40, pending_resolve 18-30 (drains)
-> healthy
```

**Negative (the indentation bug).**
```
panel = 40 rows ; collector: games_tracked = 6, pending_resolve = 1
-> REGRESSION (per-row discovery ran once per competition on the leaked last row)
```

**Edge.** A game in `ws_raw_frames` but absent from `games` is a DISCOVERY symptom —
check `games_tracked` vs the panel first.

---

## Case C-3 — Watchdog flap

**Rule.** A `page.content()` overrun stops the watchdog ping; systemd restarts the
unit. Read the journal before attributing it.

**Positive (diagnosis).**
```
journalctl --user -u blm-collector --since ... | grep -iE "watchdog|Stopped|Started"
-> Watchdog timeout (limit 1min 30s) ; St..d ; Started (NRestarts 0->1)
-> report as a separate finding; do NOT restart to "fix"
```

**Negative.**
```
Attributing the new PID to your own action -> wrong (no agent restarted anything)
```

**Edge.** Raising `WatchdogSec` hides the defect — forbidden.

---

## Case C-4 — Quarter-score capture (0 rows)

**Rule.** Quarter scores require the selected sidebar section's MARKUP; the rendered
text alone cannot satisfy the identity guard.

**Positive.**
```
parse_event_view(text, identity_html=<selected section outerHTML>)
-> teams parsed correctly + quarters captured
```

**Negative.**
```
parse_event_view(page.inner_text("body"))   # no markup
-> teams ""/"" ; _verified_event_view rejects every capture ; table stays 0 rows
(the INSERT and its caller were intact — the data died at the identity guard)
```

**Edge.** The rendered body inlines EVERY listed game's scoreboard (exactly one
`active`), so a text-only parse picks the FIRST game's score — cross-game
contamination.

---

## Case C-5 — WS frames carry quarters (a coverage lever)

**Rule.** A `ws_raw_frames` payload holds `info.additional_data.quarterScores`
for every listed game — a panel-independent path.

**Positive.**
```
inspect a live ws_raw_frames payload -> quarterScores = [{quarterNumber, score:{team1,team2}}...]
-> a coverage lever the DOM path cannot match
```

**Negative.**
```
Trusting the docstring "WS frames carry no quarter scores — audited 2026-09-22"
-> contradicts the live frame
```

**Edge.** Confirm the field in a live frame before wiring the path.
