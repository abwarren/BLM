# 08 — Market / Line Semantics

Retrieval keywords: opening line, OLV, live line, closing line, CLV, fair value,
trigger line, final total, market semantics, which line, settlement, OLV vs BLM,
market vs fair, under value, over value, no edge, line selection, price not position.

**What this pack answers:** what each "line" in BLM means, whether it settles
anything, and the recurring OLV-vs-BLM distinction.

---

## The line taxonomy

| Field | Meaning | Settles? | Source |
|---|---|---|---|
| Opening line (OLV) | Initial market total — first verified line | ❌ | `projection.opening_snapshot` |
| Live line | Current market total (last non-null) | ❌ | `projection.market_snapshot` |
| Closing line (CLV) | Closing market total — last verified line at terminal; `None` while live | ❌ | `projection.closing_snapshot` |
| BLM fair value | Deterministic model estimate (`project`) | ❌ | `projection.project` |
| Trigger line | Immutable line in force at the 75% crossing | ✅ | `under_outcome.trigger_observation` |
| Final total | Actual final combined score | ✅ | `game_results` (OK) |

Only the **trigger line** and the **final total** settle an alert.

---

## Rule 8.1 — OLV is the first verified line and never moves

**Rule.** Opening line = the FIRST snapshot carrying a market total. Immutable. A
game captured mid-game reports the first line observed at capture time (honest: no
pre-game line exists for it).

**Source.** `projection.opening_snapshot`; `DECISIONS.md` §10.K.

**Why.** A stable baseline for line movement.

**Valid.** `opening_snapshot` returns the first non-null `total_line` row.

**Invalid.** Using the current line as the opening.

**Forbidden.** Using the opening line as a fallback for a stale/missing live line
in eligibility (`DECISIONS.md` §14.F).

**Verify.** `opening_snapshot(rows)["total_line"]`.

---

## Rule 8.2 — The live line is the most recent observed line

**Rule.** Live line = the most recent snapshot carrying a total line. Panel/list
ticks without a market payload never count as "no market" — the bookmaker line
persists between captures.

**Source.** `projection.market_snapshot`.

**Why.** A stub snapshot must not be read as "market gone".

**Valid.** Last non-null `total_line` row, regardless of how recent the tick is.

**Invalid.** Treating a `total_line=NULL` tick as a market disappearance.

**Forbidden.** Fabricating a line when none is observed.

**Verify.** `market_snapshot(rows)["total_line"]`.

---

## Rule 8.3 — The closing line is NOT the latest live line

**Rule.** CLV exists only once the market/game has CLOSED (game ended). For a
live game it is `None` — the latest live line is NOT the closing line, however
recent. Ended games receive no further snapshots, so CLV is immutable once set.

**Source.** `projection.closing_snapshot(rows, ended)`; `DECISIONS.md` §10.K.

**Why.** Presenting a live line as a closing line produces a false CLV.

**Valid.** Live game → `closing_line=None`.

**Invalid.** Reporting the current line as the closing line for a live game.

**Forbidden.** Ending a game artificially to freeze a CLV.

**Verify.** `closing_snapshot(rows, ended=False)` → `None`.

---

## Rule 8.4 — The trigger line is the line in force AT the 75% crossing

**Rule.** The frozen trigger line is the last non-null `clean_projections.live_total_line`
observed at-or-before the 75% progress boundary (canonical store); snapshots are
the fallback. It is NEVER the opening line, a later live line, the closing line or a
reconstructed value.

**Source.** `under_outcome.trigger_observation`; ADR-001.

**Why.** The alert and the settlement must read ONE value; the 218-flip audit
(ruling 2026-09-20) made `clean_projections` canonical.

**Valid.** `trigger_observation(rows, 75, cls, projection_rows)["total_line"]`.

**Invalid.** The current live line at the firing tick (inflates the observed rate
by ~14 pp).

**Forbidden.** Rebasing the trigger line after the fact.

**Verify.** Served `trigger_line` == `by_checkpoint[75].trigger_total` ==
`trigger_observation(...)["total_line"]`.

---

## Rule 8.5 — BLM fair value is a model estimate, not a settlement line

**Rule.** `blm_fair_value` = `project(snapshots up to checkpoint)` recompute, frozen
at first write. It compares to the market (OLV/CLV/live) but never settles an alert.

**Source.** `projection.project`; `checkpoint_market.blm_fair_value`.

**Valid.** fair 148 < market 180 → UNDER_VALUE.

**Invalid.** Settling an alert against the fair value.

**Forbidden.** Using fair value as the final or the trigger.

**Verify.** `checkpoint_market` fields; `fair_total` in `clean_projections`.

---

## Rule 8.6 — OLV ≠ BLM prediction

**Rule.** `BLM_vs_OLV` compares the model's fair value against the OPENING line;
`BLM_vs_CLV` against the CLOSING line; `market_vs_fair` against the live line. These
are three distinct benchmarks and are never conflated.

**Source.** `checkpoint_market` (`market_vs_fair`, `blm_vs_olv`, `blm_vs_clv`,
`olv_to_clv`); M008-SCORE-M2.

**Why.** A recurring issue in BLM work: comparing BLM against the wrong line.

**Valid.** The scorecard answers "was BLM closer to the eventual total than the
market" per line type (OLV / CLV / checkpoint).

**Invalid.** Reporting a single "model vs market" number without naming the line
type.

**Forbidden.** Silently substituting one benchmark for another.

**Verify.** Each comparison row carries `market_line_type`.

---

## Rule 8.7 — Line selection is by PRICE/type, never array position

**Rule.** The Total Points market line is selected by price/type
(`market_type='MatchTotal'`), never by position in an array.

**Source.** `DECISIONS.md` §7.E, §10.K.

**Why.** Array order changes; the market identity does not.

**Valid.** Select on `market_type`.

**Invalid.** `markets[0]`.

**Forbidden.** Inferring the total line from display order.

**Verify.** The selected row's `market_type`.

---

## Rule 8.8 — Signal labels vs settlement labels are different

**Rule.**

```
market > fair → UNDER_VALUE
market < fair → OVER_VALUE
market == fair → NO_EDGE     (no bet; never 'PUSH')
```

`PUSH` is RESERVED for the settlement outcome `final == trigger`.

**Source.** `DECISIONS.md` §14.H, §10.J.

**Why.** The old `'PUSH (equal)'` signal conflated a no-value position with a line
landing.

**Valid.** A no-edge position is `NO_EDGE`.

**Invalid.** Labeling a no-edge position `PUSH`.

**Forbidden.** Reintroducing `PUSH` as a signal label.

**Verify.** `checkpoint_market.signal` vs `checkpoint_market.outcome`.
