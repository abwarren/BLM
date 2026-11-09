# AUTO_BET MARKET SELECTION (Phase Two)

File: `blm_v4/execution/pokerbet/dom.py`
Tests: `tests/test_autobet_live_readiness_2026_11_08.py`,
`tests/test_pokerbet_live_adapter.py`, `tests/test_pokerbet_quarter_dom.py`,
`tests/test_market_line_selection_2026_09_16.py`,
`tests/test_results_dom_rows.py`

## 1. The rule

> The bookmaker outcome is selected by **the original BLM trigger line**, never
> by whichever line has odds closest to 1.85, never by the current total
> substituted for the trigger, and never by a nearest/rounded guess.

A selection is refused when the exact line is not offered, when more than one
game-total block is rendered, when two offers share a line, when the market is
suspended, when the UNDER price is missing/non-decimal, or when the click target
is not uniquely identifiable.

## 2. Distinctions enforced in code

| Concept | Source field | Never confused with |
|---|---|---|
| Game total | the single `Total` market block (`_market_texts` filtered by `_is_game_total_text`) | team totals, handicap/spread blocks |
| Team total | rows/blocks matching `\b(team|home|away)\s+total\b` | game total — dropped at parse time |
| Line / point value | `TotalOffer.line`, parsed only from tokens matching `_LINE_RE` | odds |
| Over odds | `TotalOffer.over` | under odds, line |
| Under odds | `TotalOffer.under` | over odds, line |
| OPL / reference line | **no representation in this repository** (see §5) | trigger line, current line |
| Suspended market | `dom._market_suspended()` (suspended/locked class) and executor `market_status` | active market |
| Selectable outcome | `TotalOffer.price(position)` non-None and one unique clickable element | rendered-but-unpriced row |

The executor also refuses a payload whose `market_status` is not `LIVE`/`OPEN`,
and the executor's `market.under_odds` is a separate field from
`market.total_line`. `api.py` publishes both, plus `observed_lines` (the WS
ladder), so a stale or partial observation is visible to the evaluator.

## 3. Selection algorithm

```
_market_texts()
  ├─ 0 blocks            -> None  (no market: refused)
  ├─ >1 blocks           -> None  (ambiguous structure: refused)
  └─ 1 block -> parse_total_offers(text)
        per row:
          - drop team-total rows
          - require EXACTLY ONE point-line token     (else drop row)
          - collect numeric tokens that are not the line and are inside
            the decimal-odds band [1.01, 1000]       (else drop row)
          - read labelled Over/Under prices when present
          - positional assignment ONLY for a genuinely two-sided row;
            a single unlabelled price is ambiguous -> row dropped
        - drop every line that appears twice (duplicate line = ambiguity)

select_total_offer(offers, trigger_line, position)
  ├─ trigger_line not numeric      -> LINE_NOT_AVAILABLE
  ├─ position not OVER/UNDER       -> MARKET_AMBIGUOUS
  ├─ no offer at trigger_line      -> LINE_NOT_AVAILABLE   (never nearest)
  ├─ >1 offer at trigger_line      -> AMBIGUOUS_LINES
  ├─ market suspended              -> MARKET_SUSPENDED
  ├─ side price missing/non-decimal-> MISSING_ODDS
  └─ otherwise                     -> the one offer

find_position()
  ├─ game not registered           -> None (GAME_NOT_REGISTERED upstream)
  ├─ market not TOTAL              -> None
  ├─ required line bound           -> select_total_offer(..., required_line)
  └─ no required line bound         -> require EXACTLY ONE priced line,
                                       else None (ambiguous, refused)
  └─ _locate_outcome(position, line) must return exactly one element,
     else None (SELECTION_NOT_CLICKED upstream)
```

The adapter is bound to the frozen line with `set_required_line(trigger_line)`
before resolution. `line_tolerance` defaults to `0.0` (exact). A tolerance is
configurable but never widens to "nearest".

## 4. Verification before submission

1. `PokerBetDomAdapter.verify_offer(...)` compares, in order: game id, both team
   names, market is TOTAL, position is OVER/UNDER, exact line (tolerance), odds
   (tolerance), suspension. Each failure returns a named reason.
2. The betslip is read back and must show the same event, market, position,
   line, odds and stake. Any mismatch is `BETSLIP_VERIFICATION_FAILED` and **no**
   submission is attempted.
3. The provider re-reads the account identity at the boundary and asserts its
   idempotency key equals the ledger key.

## 5. OPL — unresolved, deliberately not invented

`grep -rni "opl\|opening line"` finds no implementation, schema column, payload
field, setting or test in this repository. There is therefore **no** OPL
semantics to implement, and none was invented.

Consequences, stated plainly:

- The engine cannot distinguish an "OPL/reference line" from any other offered
  line, because it has no definition of one.
- The system does not depend on OPL: it bets the frozen BLM trigger line and
  refuses when that line is absent. An OPL concept would only add an
  alternative reference line, which the current directive explicitly says must
  not replace the trigger.
- If PokerBet publishes an OPL that differs from the trigger line, the current
  behaviour is **reject** (`LINE_NOT_AVAILABLE`), which is fail-closed.
- **Open question for the operator:** whether PokerBet's OPL is a distinct
  field, and whether it should ever be accepted in place of the trigger line.
  Until that is answered from validated evidence or isolated browser fixtures,
  the adapter must stay exact-match.

## 6. Test coverage

| Scenario | Test evidence |
|---|---|
| every line parsed independently | `test_every_line_is_parsed_independently` |
| multiple simultaneous lines, exact trigger chosen (not nearest 1.85) | `test_trigger_line_wins_even_when_another_line_is_nearer_185` |
| trigger line absent (never nearest/rounded) | `test_missing_trigger_line_is_refused_not_substituted` |
| duplicate line / ambiguous structure | `test_duplicate_line_structure_is_ambiguous` |
| team total excluded | `test_team_total_rows_are_not_game_totals` |
| suspended market | `test_suspended_market_is_refused` |
| odds never mistaken for the line | `test_odds_are_never_mistaken_for_the_line` |
| missing odds on the selected line | `test_missing_odds_on_the_selected_line_is_refused` |
| unbound multi-line market refused | `test_ambiguous_unbound_multi_line_market_never_guesses` |
| click target not unique | `test_browser_refuses_ambiguous_selection`, `test_browser_refuses_unclickable_outcome` |
| changed odds / line at submission | `test_submission_revalidation_rejects_changed_odds`, `test_submission_revalidation_rejects_a_line_change` |
| OPL ambiguity | **not testable** — no OPL representation exists; refusal behaviour above is the fail-closed substitute |
