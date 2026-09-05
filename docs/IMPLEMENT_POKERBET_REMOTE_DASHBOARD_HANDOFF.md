# IMPLEMENTATION DIRECTIVE — REMOTE POKERBET DASHBOARD HANDOFF

## STATUS
PLANNING ONLY — NO WAGER EXECUTION IMPLEMENTATION IS AUTHORIZED BY THIS DIRECTIVE.

## OBJECTIVE
Design a remote-control integration so the BLM dashboard can prepare an Over/Under position on a remotely connected PokerBet browser/session, while the user remains the person who explicitly initiates and confirms the wager.

The immediate goal is NOT autonomous betting, bankroll management, or unattended wager execution. The goal is remote dashboard control of a user-owned browser/session and a reliable handoff from BLM's live analysis to the corresponding PokerBet market controls.

## REQUIRED ARCHITECTURE

### 1. REMOTE BROWSER BRIDGE
Create a separate integration boundary between BLM and the PokerBet browser session.

Responsibilities:
- establish an authenticated remote browser/session connection;
- verify the connected session belongs to the intended user;
- expose only the minimum browser-control operations required by the dashboard;
- keep PokerBet credentials/session secrets out of the BLM database and logs;
- provide connection health, session state, and last-seen timestamps.

Do NOT couple PokerBet DOM logic directly into projection, scorecard, collector, or historical analytics code.

### 2. READ DOM / MARKET STATE
The bridge should be able to inspect the currently rendered PokerBet page and identify:
- game/event identity;
- market identity;
- Over/Under side;
- displayed line;
- displayed price/odds;
- market availability/suspension state;
- relevant clock/period information where available;
- stake-entry control availability;
- confirmation/submission control availability.

Treat DOM selectors as an adapter layer. Do not scatter selectors through dashboard code.

### 3. BLM → POKERBET MATCHING
Before any remote handoff, require a positive match between BLM's live game and the PokerBet DOM state.

Match using stable identifiers where available. Otherwise use a conservative combination of:
- teams/event name;
- classification;
- market type;
- live score;
- period/clock;
- displayed total line.

If identity cannot be established confidently, block the handoff and show a clear mismatch state.

### 4. DASHBOARD POSITION PANEL
Add a dedicated PokerBet panel to each eligible live game.

Display:
- BLM game identity;
- PokerBet connection status;
- matched/unmatched status;
- current PokerBet total line;
- Over price;
- Under price;
- BLM fair/projected total;
- actual Pts/Min;
- required Pts/Min;
- pace gap;
- line freshness;
- last DOM observation timestamp.

The panel must make it obvious when the PokerBet state differs from the BLM state.

### 5. USER-INITIATED REMOTE ACTION
Provide explicit dashboard controls for the user to initiate a remote handoff for the selected side.

The action should:
1. re-read the PokerBet DOM immediately;
2. revalidate game/market identity;
3. revalidate side, line, and price;
4. verify the market is active and available;
5. populate/select the requested PokerBet controls remotely;
6. stop at the final user confirmation/submission point unless a later, separately authorized implementation explicitly changes this behavior.

The dashboard must never silently infer a side or silently act because a model signal changed.

### 6. STALE / CHANGE PROTECTION
Block remote handoff when:
- the DOM observation is stale;
- the market is suspended;
- the game is no longer live;
- the matched event changes;
- the line changes after the dashboard recommendation;
- the price changes after the dashboard recommendation;
- the requested side is no longer available;
- the PokerBet session is disconnected;
- the target selector/control cannot be identified safely.

If a line or price changes, require the dashboard to refresh and present the new state before another user action.

### 7. NO DUPLICATE ACTIONS
Assign every dashboard handoff a unique action ID.

Record state transitions such as:
- requested;
- DOM revalidated;
- controls populated;
- user confirmation pending;
- cancelled;
- failed;
- completed handoff.

Do not retry a partially completed remote action blindly. Require a fresh DOM read and explicit user action.

### 8. AUDIT LOG
Store an immutable audit record for each remote handoff containing:
- action ID;
- BLM game ID;
- PokerBet event/market identity;
- timestamp;
- BLM line/fair value at decision time;
- PokerBet displayed line and prices;
- selected side;
- validation results;
- selector/control status;
- outcome of the remote handoff;
- error reason where applicable.

Never store PokerBet passwords, authentication cookies, access tokens, or other secrets in the audit record.

## SECURITY REQUIREMENTS

- Keep browser credentials/session material outside the BLM analytics database.
- Use an authenticated, encrypted channel for remote browser communication.
- Scope the bridge to the minimum permitted browser operations.
- Require explicit user initiation for every remote handoff.
- Fail closed on identity mismatch, stale state, selector ambiguity, or connection loss.
- Never expose secrets to frontend JavaScript, browser local storage, or ordinary application logs unless the integration architecture explicitly requires it and protects them.

## DATABASE DESIGN
Do NOT contaminate the clean statistical metrics database with PokerBet credentials or browser-session data.

A separate operational integration schema/table set may contain:
- pokerbet_connections;
- pokerbet_market_observations;
- pokerbet_handoff_actions;
- pokerbet_handoff_events;
- pokerbet_errors.

Keep statistical observations and gambling-account/session state logically separated.

## IMPLEMENTATION BOUNDARY
This directive does NOT authorize:
- autonomous wagering;
- unattended wager submission;
- betting triggered solely by model signals;
- automatic stake sizing;
- automatic bankroll decisions;
- automatic retries after submission ambiguity;
- bypassing PokerBet confirmations, authentication, CAPTCHA, or other platform controls.

The first implementation milestone is the remote browser/session bridge, DOM observation, event/market matching, dashboard state display, and user-initiated control handoff stopping before final submission.

## TEST PLAN
Create deterministic tests for:
- successful browser connection;
- disconnected browser;
- valid event match;
- event mismatch;
- market mismatch;
- line change;
- price change;
- suspended market;
- stale DOM observation;
- unavailable selector;
- duplicate action prevention;
- failed remote control;
- audit-log completeness;
- secrets never written to logs/database.

Use a mock PokerBet DOM/adapter for automated tests. Do not require a real betting account for the test suite.

## DEPLOYMENT RULE
Do not modify the live collector, projection, scorecard, clean metrics pipeline, or historical database merely to introduce this integration.

Implement the integration as an isolated feature with its own tests and explicit feature flag. Keep it disabled until the bridge, validation gates, audit logging, and failure modes have been independently verified.

## ACCEPTANCE CRITERIA
The feature is ready for controlled user testing only when:
- the dashboard can connect to the remote PokerBet browser/session;
- the current PokerBet DOM state is visible;
- the correct event and market can be matched to the BLM game;
- line/price changes are detected before handoff;
- an explicit dashboard action can populate the intended controls;
- unsafe/stale/mismatched states fail closed;
- every action is auditable;
- no credentials/secrets enter BLM logs or analytics databases;
- no autonomous or unattended wager submission exists in this phase;
- existing BLM live scoring and analytics behavior remains unchanged.
