# M008 — BetConstruct Swarm API Forensic / Feed Evaluation

Status: IN PROGRESS — M008-M1, M008-M2 COMPLETE (investigative, read-only).
Docs (official, v1.2.19, 2020-07-22, 181pp) saved in this dir:
- swarm-api-1.2.19.pdf (original)
- swarm-api.txt (extracted text, 234K chars)
All page/section citations refer to the official doc.

## M008-M1 (COMPLETE) — API capability audit
- Declarative datastore over HTTP: Long poll + WebSocket.  One `get`
  {source:betting, what:{level:[fields]}, where:{filters}, subscribe:bool}.
  Hierarchy: sport -> region -> competition -> game -> market -> event.
  Globally-unique node `id`.  (§1.13, §3.1)
- Sports incl. Virtual (type=1) and Electronic (0).  (§3.3.1)
- E-Basketball first-class: MatchTotal Over/Under, MatchHandicap,
  QuarterTotal, QuarterHandicap, team totals, odd/even, winner.  (App E)
- line = market.base / event.base; odds = event.price; `optimal` flag on
  market marks the primary total among several.  (§3.3.5/.6)
- Status: game.type 0=Prematch/1=Live/2=Future Live; is_started;
  is_blocked; bet-status CHANGE_ODD / EVENT_LOCKED / BASIS_CHANGED /
  GAME_STARTED.  (App D 8.5)
- Timestamps: game.start_ts; PriceHistory.TS (unix).  PriceHistory = last
  3 price changes only; NO explicit opening/closing fields — client must
  persist.  (§3.3.6)
- Subscriptions push field-level deltas on the same WS channel
  (e.g. event:{923782145:{price:5.35}}).  where:{competition:{id:N}}
  filters the tree.  (§1.13.2)

## M008-M2 (COMPLETE) — access / authentication
- Session model: request_session {site_id, language[, source, terminal,
  afec]} -> sid.  site_id = PartnerID "provided to specific partner by
  BetConstruct" — commercial credential, NOT self-serve.  (§1.5.1)
- All commands except request_session need the SID (Long poll:
  `swarm-session` header; WS: channel-bound).  Session timeout default
  1 min (Long poll; keep-alive via whats_up).  No recovery — on dead
  session, re-request + resubscribe.  (§1.5.2-.5, §1.4)
- Login (user/password, user_id+auth_token, jwe) is for USER/BETTING
  operations (place bets, balance, profile).  Data-only `get` on the
  `betting` source appears to be session-level (no user login required)
  — NOT explicitly stated; must be empirically confirmed.  (§1.6, §2)
- ENDPOINTS: test = https://eu-swarm-test.betconstruct.com (+ wss).
  Production URL is customized and provided only AFTER BetConstruct
  engineers review the front-end integration.  (§4.1)
- TERMS RISK: "Do not use SWARM for back-end to back-end connection,
  client must be a front-end application."  "If the following practices
  aren't followed the integration may be inefficient and automatically
  blocked."  BLM is a headless back-end collector — LIKELY NOT
  COMPLIANT.  (§4.1)
- No explicit rate-limit numbers in the doc; only "avoid repeated
  polling" + auto-block language + error 22 "Limit reached".
- recaptcha can be enabled on request_session (v2/v3) — additional
  friction; empirical.

## M008-M3 (NEXT) — fixture discovery (BETUAL_NBA / CYBER_2K26)
Read-only investigation, no code changes:
1. Probe test endpoint https://eu-swarm-test.betconstruct.com (GET/WS)
   WITHOUT credentials: does request_session return a sid for any
   site_id (e.g. 1) — i.e. is the TEST feed publicly reachable?
   Document HTTP/WS response honestly.  No invented creds.
2. If reachable: get sport list (type Virtual/Electronic) -> find
   E-Basketball -> regions/competitions -> look for BETUAL_NBA /
   CYBER_2K26 / Betual / Cyber 2K26 equivalents.
3. Document exact competition ids + names + whether same fixture class.

## M008-M4..M010 — queued (market total, correspondence, O/L/C,
## realtime, POC-only-if-creds, comparison, recommendation)

Open questions (carry forward):
- Are Swarm E-Basketball fixtures the SAME virtual games as PokerBet's
  (BetConstruct powers many virtual operators — plausible but unproven)?
- Correspondence keys available: game.id, team1_id/team2_id,
  competition, start_ts, sport/region.  No shared event ID with
  PokerBet today.
- Latency/update-frequency numbers not in doc (empirical, if reachable).
