# BLM V4 — REMOTE POKERBET OVER/UNDER HANDOFF PLAN

## STATUS

Planning/specification only.

The objective is to let the BLM dashboard control a user's own authenticated PokerBet browser session far enough to **prepare an Over or Under wager remotely**, while keeping the final wager submission under explicit user confirmation.

This phase must remain separate from the BLM data collector, pace projector, clean metrics database, and future statistical modelling layers.

---

## 1. OBJECTIVE

Provide a secure remote workflow:

```text
BLM dashboard
    ↓
user selects a LIVE game
    ↓
BLM resolves the corresponding PokerBet event
    ↓
remote browser/session locates the event
    ↓
DOM is inspected for available totals/markets
    ↓
user selects OVER or UNDER
    ↓
user selects/enters stake
    ↓
BLM prepares the PokerBet bet slip
    ↓
DOM is re-read and validated
    ↓
STOP AT FINAL SUBMISSION
    ↓
user explicitly confirms in PokerBet
```

The system must never assume that a prepared wager was successfully placed.

Only an explicit confirmed submission/result from the PokerBet UI may be recorded as submitted.

---

## 2. IMPORTANT SCOPE BOUNDARY

This plan is for a **user-controlled remote handoff**.

The system may:

- connect to the user's authenticated browser/session
- inspect the DOM
- identify events and markets
- read available Over/Under lines and prices
- prepare a selected market in the bet slip
- enter the user-specified stake where supported
- re-check the live state before final confirmation
- present the final PokerBet state to the user
- record an audit trail

The system must NOT:

- autonomously decide to bet
- automatically choose Over or Under from a model signal
- automatically determine stake size
- submit wagers in the background
- submit repeatedly after a failed attempt
- retry a wager automatically after a state change
- convert pace/projector output into a betting command
- run a betting loop independent of an explicit user action

The final submission remains a deliberate user action in the connected PokerBet session.

---

## 3. SYSTEM ARCHITECTURE

Use a separated integration layer.

Recommended components:

```text
blm_v4/
  pokerbet_remote/
    session.py
    dom_reader.py
    event_matcher.py
    market_reader.py
    handoff.py
    validation.py
    audit.py
```

Do not put PokerBet browser logic into:

- collector.py
- clean_metrics.py
- pace_projector.py
- projection.py
- scorecard.py

The betting handoff must be isolated from the live-data pipeline.

---

## 4. REMOTE BROWSER SESSION

Support an authenticated browser connection rather than collecting PokerBet username/password credentials into BLM.

Preferred architecture:

```text
User browser
     ↕
approved remote/browser-control bridge
     ↕
BLM dashboard/backend
```

The user's existing authenticated session should remain the source of authentication.

Do not store PokerBet passwords in the BLM database.

Do not log authentication cookies, bearer tokens, session identifiers, or equivalent secrets.

Support an explicit session state:

```text
DISCONNECTED
CONNECTED
AUTHENTICATED
SESSION_EXPIRED
ERROR
```

The dashboard must show the current connection state.

---

## 5. DOM INSPECTION

The DOM reader should treat the PokerBet page as an external, changing interface.

Do not hard-code a single brittle selector if semantic or attribute-based identification is available.

Identify elements using multiple signals where possible:

- event/container identity
- visible team/event names
- market title
- Over/Under text
- line value
- price/odds
- enabled/disabled state
- visible market status
- accessibility attributes
- stable data attributes if present

Selectors must be versioned and isolated from BLM's game-analysis code.

Never assume that a selector that worked yesterday remains valid.

---

## 6. EVENT MATCHING

The dashboard and PokerBet may use different event names or IDs.

Build an explicit event matcher.

Primary matching inputs:

1. normalized home/team name
2. normalized away/team name
3. classification/sport where available
4. live status
5. event date/time where available
6. PokerBet event identifier when discovered

Use a confidence/state result such as:

```text
EXACT_MATCH
LIKELY_MATCH
AMBIGUOUS
NOT_FOUND
```

Never select an event when the match is ambiguous.

A wrong event is a hard stop.

---

## 7. MARKET IDENTIFICATION

Once the correct event is identified, locate the totals market.

Required market characteristics:

```text
market = Total Points / Match Total / equivalent
side = OVER or UNDER
line = numeric
price = numeric
status = OPEN / SUSPENDED / CLOSED / UNKNOWN
```

Do not infer a market from position in the DOM alone.

Confirm the market title and line explicitly.

If several totals markets exist, show them distinctly and require the user to choose the intended line.

---

## 8. LINE AND PRICE CAPTURE

Before preparing the wager, capture:

- event identity
- market identity
- side
- line
- price/odds
- timestamp
- page/session state
- market availability

Example handoff payload:

```json
{
  "event_id": "...",
  "home": "...",
  "away": "...",
  "market": "MATCH_TOTAL",
  "side": "UNDER",
  "line": 214.5,
  "price": 1.91,
  "observed_at": "..."
}
```

This is a handoff description, not a betting recommendation.

---

## 9. DASHBOARD ACTION

The user should explicitly invoke the remote action from the dashboard.

Example UI:

```text
LIVE GAME

Team A  88
Team B  82
Q4 05:14

LIVE TOTAL 214.5
CURRENT PACE 5.72 pts/min
REQUIRED PACE 5.36 pts/min
PACE GAP -0.36 pts/min

[ PREPARE OVER ]   [ PREPARE UNDER ]
```

The buttons mean:

> prepare the selected PokerBet market in my connected session

They do NOT mean:

> place the wager automatically.

---

## 10. USER-STATED STAKE

The stake must come from the user.

Do not derive a stake from:

- model confidence
- pace gap
- historical win rate
- z-score
- bankroll rules
- any automated formula

Recommended flow:

```text
Side: UNDER
Line: 214.5
Price: 1.91
Stake: [ user enters amount ]

[ PREPARE BET SLIP ]
```

---

## 11. PREPARATION PHASE

After the user invokes preparation:

1. verify connected session
2. verify user is authenticated
3. verify event match
4. open/navigate to the correct event
5. locate the totals market
6. locate the requested line
7. locate requested side
8. select the requested market
9. enter user-stated stake if supported
10. read the DOM again
11. verify bet-slip state

Then STOP.

The dashboard should display:

```text
BET SLIP PREPARED

Event: Team A vs Team B
Market: Total Points
Side: UNDER
Line: 214.5
Price: 1.91
Stake: 100

READY FOR YOUR FINAL CONFIRMATION IN POKERBET
```

---

## 12. FINAL CONFIRMATION GATE

The system must not automatically click the final wager-submit control.

The user must explicitly confirm the wager through the PokerBet UI.

The BLM integration may observe the resulting page state after the user acts.

Possible final states:

```text
USER_CONFIRMED
USER_CANCELLED
PRICE_CHANGED
LINE_CHANGED
MARKET_SUSPENDED
SESSION_EXPIRED
SUBMISSION_FAILED
UNKNOWN
```

---

## 13. STALE-LINE PROTECTION

Before preparation and again immediately before final user confirmation, validate:

- event unchanged
- market unchanged
- side unchanged
- line unchanged
- price unchanged or explicitly updated in the confirmation display
- market still open
- session authenticated

If any material field changes:

```text
DO NOT PROCEED
```

Instead show:

```text
MARKET CHANGED
The PokerBet line/price has changed.
Review the updated bet slip before confirming.
```

Do not automatically reselect a different line.

---

## 14. DUPLICATE ACTION PROTECTION

One explicit dashboard action should create one handoff request.

Assign a unique:

```text
handoff_id
```

Reject accidental duplicate submissions generated by:

- double clicks
- page refresh
- websocket reconnect
- browser retries
- HTTP retries
- frontend re-render

A completed handoff must not automatically start another handoff.

---

## 15. AUDIT LOG

Record a non-secret audit trail for every handoff.

Recommended fields:

```text
handoff_id
user_action_timestamp
BLM_game_id
PokerBet_event_id
market
side
line
price
stake
session_state
match_state
preparation_state
final_observed_state
failure_reason
```

Never record authentication credentials or raw session secrets.

Important distinction:

```text
PREPARED
```

is not the same as:

```text
SUBMITTED
```

Only record `SUBMITTED` when the external UI provides reliable evidence of the user's completed action.

---

## 16. FAILURE STATES

Every failure should stop safely and return a clear reason.

Examples:

```text
NO_BROWSER_SESSION
NOT_AUTHENTICATED
SESSION_EXPIRED
EVENT_NOT_FOUND
AMBIGUOUS_EVENT
MARKET_NOT_FOUND
LINE_NOT_FOUND
PRICE_CHANGED
MARKET_SUSPENDED
STALE_PAGE
DOM_CHANGED
BET_SLIP_NOT_CONFIRMED
USER_CANCELLED
UNKNOWN_EXTERNAL_STATE
```

Do not silently fall back to another event or market.

---

## 17. OBSERVABILITY

Expose integration status in the dashboard:

```text
PokerBet Remote Session
● Connected
● Authenticated
● Event Matched
● Market Found
● Bet Slip Ready
```

Use explicit timestamps for observed values.

Never display an old line as though it were current.

---

## 18. SECURITY

Requirements:

- no PokerBet password storage
- no plaintext secrets in logs
- no session-token logging
- authenticated-session reuse only
- explicit user initiation
- clear session disconnect control
- least-privilege browser access where feasible
- audit all remote handoff operations
- never expose remote-browser controls to unauthenticated users

---

## 19. SEPARATION FROM MODEL / DATA LAYERS

The PokerBet integration must not consume model predictions.

It should receive only explicit user selections:

```text
GAME
MARKET
SIDE
LINE
PRICE
STAKE
```

The pace projector remains descriptive.

The clean-data database remains the analytical source.

No future z-score or statistical component may automatically invoke the remote wager handoff.

---

## 20. TESTING

Add integration/unit coverage for at minimum:

1. session disconnected
2. session authenticated
3. event exact match
4. ambiguous event
5. event not found
6. market found
7. market not found
8. Over selection
9. Under selection
10. line extraction
11. price extraction
12. stake validation
13. bet-slip preparation
14. stale line detected
15. price changed
16. market suspended
17. duplicate dashboard click
18. session expiry
19. user cancellation
20. successful observed post-confirmation state
21. submission state remains UNKNOWN when evidence is insufficient
22. credentials are never logged
23. clean-data pipeline remains unaffected
24. pace projector remains unaffected
25. no model/prediction function invokes the handoff

Use mocked DOM fixtures for the initial test suite.

Do not test against a real-money wager during automated CI.

---

## 21. PHASED IMPLEMENTATION

### Phase 1 — READ-ONLY DOM DISCOVERY

Implement:

- browser/session connection
- page detection
- event discovery
- market discovery
- line/price extraction
- DOM fixture tests

No clicks.

### Phase 2 — REMOTE NAVIGATION / MARKET PREPARATION

Implement:

- event navigation
- market selection
- Over/Under selection
- bet-slip preparation
- duplicate protection
- stale-line validation

Still stop before final wager submission.

### Phase 3 — USER-CONFIRMED HANDOFF OBSERVATION

Add:

- final-state observation
- audit log
- confirmation/result detection
- failure-state reporting

### Phase 4 — HARDENING

Add:

- selector versioning
- DOM-change detection
- session recovery UX
- comprehensive integration tests
- operational metrics

---

## 22. DEPLOYMENT RULE

Do not modify or restart the collector merely to implement the browser handoff.

Keep the integration isolated.

Before production enablement:

- full tests pass
- mocked DOM tests pass
- existing BLM suite passes
- clean metrics continue unchanged
- pace projector continues unchanged
- no prediction generation is introduced
- no automatic submission path exists
- audit logging is verified

Enable behind a feature flag such as:

```text
POKERBET_REMOTE_HANDOFF_ENABLED=false
```

Default to disabled until manually enabled.

---

## 23. STOP CONDITIONS

Stop the operation immediately when:

- event matching is ambiguous
- market identity is uncertain
- line changed
- price changed materially
- market is suspended
- session expires
- DOM structure is unknown
- bet slip cannot be verified
- external state is inconsistent

Never guess.

Never silently choose another line.

Never submit because a previous attempt was interrupted.

---

## 24. FINAL ACCEPTANCE CRITERIA

The implementation passes this specification when the user can:

1. connect their authenticated PokerBet browser/session
2. see connection state in BLM
3. select a current live BLM game
4. have BLM locate the matching PokerBet event
5. inspect available totals markets
6. choose Over or Under explicitly
7. specify the stake explicitly
8. have BLM prepare the PokerBet bet slip remotely
9. see the exact line and price revalidated
10. take over and make the final confirmation themselves
11. see a clear audit record of the handoff result

And when the system can prove:

```text
NO AUTOMATIC BET DECISION
NO AUTOMATIC STAKE DECISION
NO AUTOMATIC FINAL SUBMISSION
NO MODEL-DRIVEN BET INVOCATION
NO CLEAN-DATA CONTAMINATION
```

---

## 25. CURRENT BLM PHASE

BLM is currently collecting clean descriptive data and building a baseline.

Therefore the PokerBet integration must remain a **remote execution/handoff utility only**.

It must not be connected to:

- pace signals
- z-scores
- model confidence
- historical win rates
- prediction generation
- automated Over/Under recommendation

Those remain separate concerns.

**STOP after implementation, testing, and verification of the remote handoff layer.**
