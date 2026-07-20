# BLM Round Robin / System Bet Generator — Implementation Plan (V2)

> **Status:** Plan for review — NO CODE WRITTEN YET
> **Target:** `./projects/blm-roundrobin/`
> **Architecture:** Tampermonkey userscript injected into PokerBet → FastAPI backend for computation
> **Core Value:** Select games from a single panel → auto-generate all parlays → auto-populate PokerBet's native bet slip

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│  Browser (Chrome)                                        │
│                                                          │
│  ┌─────────────────────────────────────┐  ┌──────────┐  │
│  │  pokerbet.co.za LIVE SPORTSBOOK      │  │  OVERLAY │  │
│  │                                     │  │  PANEL   │  │
│  │  Game 1  [TeamA 1.25] [TeamB 3.57]  │  │          │  │
│  │  Game 2  [TeamC 1.40] [TeamD 2.70]  │  │  ☑ Lakers│  │
│  │  Game 3  [TeamE 1.95] [TeamF 1.75]  │  │  ☑ Celtics│  │
│  │  Game 4  [TeamG 1.42] [TeamH 2.65]  │  │  ☑ Nuggets│  │
│  │                                     │  │          │  │
│  │  ┌────── BET SLIP ──────────────┐   │  │  [Yankee]│  │
│  │  │ ✓ Lakers ML @ 1.25          │   │  │  Stake:  │  │
│  │  │ ✓ Celtics ML @ 3.57         │   │  │  ─────── │  │
│  │  │ Total Stake: R100            │   │  │  EV: 12% │  │
│  │  │ [Place Bet]                  │   │  │  ROI:..  │  │
│  │  └──────────────────────────────┘   │  │          │  │
│  └─────────────────────────────────────┘  └──────────┘  │
│                                            ↑            │
│                                     Tampermonkey inject │
│                                            │            │
│  ┌──────────────────────────────────┐       │            │
│  │  FastAPI Backend (localhost)     │◄──────┘            │
│  │  ┌────────────────────────────┐  │  HTTP + WebSocket │
│  │  │ Combination Generator      │  │                   │
│  │  │ Financial Calculator       │  │                   │
│  │  │ Scenario Engine            │  │                   │
│  │  │ Optimiser                  │  │                   │
│  │  │ BLM Aggregator             │  │                   │
│  │  └────────────────────────────┘  │                   │
│  └──────────────────────────────────┘                   │
└──────────────────────────────────────────────────────────┘
```

**How it works:**

1. **Tampermonkey script** runs on `pokerbet.co.za/en/sports/*`
2. Injects a floating overlay panel (React or vanilla JS) on the right side
3. **Scrapes the live DOM** — reads all visible game data: teams, scores, quarters, odds, game IDs
4. User ticks which games/teams they want in their parlay
5. User selects system type (Yankee, Lucky 15, custom, etc.)
6. Script sends selections to **local FastAPI backend** → gets all combinations + financials
7. Script **auto-clicks the corresponding odds buttons** on the PokerBet page → populates PokerBet's native bet slip
8. User reviews the bet slip, enters stake, clicks "Place Bet" — all through PokerBet's own interface

**No automated bet placement** — the script populates the bet slip; the user still clicks "Place Bet" themselves.

---

## 1. Updated Functional Specification

### 1.1 How it differs from V1 plan

| Aspect | V1 (Standalone App) | V2 (PokerBet Overlay) |
|--------|-------------------|----------------------|
| Selections | User types manually | Scraped from live PokerBet DOM |
| Bet placement | Own bet slip UI | PokerBet's native bet slip |
| Hosting | `localhost:3000` standalone | Tampermonkey script on `pokerbet.co.za` |
| Backend | Full-stack FastAPI | Same backend, accessed via overlay |
| Frontend | Full React SPA | Injected overlay panel (lightweight) |

### 1.2 Core Capabilities (same as V1, plus overlay-specific)

**Overlay-Specific Features:**
- **DOM scraper** — reads game data from PokerBet's live basketball events
- **Click-to-select** — click any game in the overlay to add to parlay builder
- **Auto-populate bet slip** — programmatically clicks PokerBet's native odds buttons
- **Live refresh** — re-scrapes DOM every ~20s (respecting page's own refresh cycle)
- **Toggle columns** — show/hide games by league, sport, or status
- **Quick odds copy** — copy individual odds to clipboard
- **Parlay builder** — visual drag-and-drop ordering of legs
- **Bet slip bridge** — watches PokerBet's bet slip for changes to sync state

**Selection Sources (from PokerBet DOM):**
- Game ID (from URL or data attributes)
- Home team name
- Away team name
- Current score
- Quarter + clock
- W1 / W2 odds
- Live / prematch status

### 1.3 What's Simplified vs V1

- No export/import (selections are live, not persisted — though can be added)
- No project save/load (sessions are ephemeral — optional add)
- No PDF export
- No user auth
- No PostgreSQL for selections (selections are live-scraped; only cache in backend)

---

## 2. UI Wireframes — Overlay Panel

### PokerBet Page with Injected Overlay

```
┌──────────────────────────────────────────────────────────────────────┐
│  POKERBET HEADER                              [SIGN IN] [REGISTER]   │
│                          Balance: R210.28                            │
├─────────────────────────────────────────────────────┬────────────────┤
│  │ Live (74) │ Prematch (2,023) │  Basketball ▾ 4  │  │  BLM Parlay │
│  ┌─────────────────────────────────────────────┐   │  │  ┌────────┐ │
│  │ World Cyber Basketball. 2K26 Matches  ▾     │   │  │  │ + Add  │ │
│  │                                             │   │  │  │ Import │ │
│  │ Timberwolves Cyber  53 : 52  Celtics Cyber  │   │  │  └────────┘ │
│  │  3rd Q 9:35   [W1 1.25]  [W2 3.57]         │   │  │             │
│  │                                             │   │  │  MY PARLAY │
│  │ Lakers Cyber      80 : 77  Warriors Cyber   │   │  │  ┌────────┐ │
│  │  4th Q 10:50  [W1 1.22]  [W2 3.83]          │   │  │  │☑ T'Wolves│
│  │                                             │   │  │  │☑ Lakers │
│  │ Wizards Cyber    71 : 70  Pacers Cyber      │   │  │  │☑ 76ers  │
│  │  3rd Q 8:52   [W1 2.10]  [W2 1.65]          │   │  │  │         │
│  │                                             │   │  │  │ [Clear] │
│  │ 76ers Cyber      30 : 25  Knicks Cyber      │   │  │  └────────┘ │
│  │  1st Q 1:33   [W1 1.50]  [W2 2.40]          │   │  │             │
│  │                                             │   │  │  SYSTEMS   │
│  └─────────────────────────────────────────────┘   │  │  ┌────────┐ │
│                                                     │  │  │Doubles │ │
│  ┌─────── POKERBET BET SLIP ───────────────────┐    │  │  │Trebles │ │
│  │                                              │    │  │  │4-Folds │ │
│  │  No selections yet                           │    │  │  │        │ │
│  │                                              │    │  │  │Yankee  │ │
│  └──────────────────────────────────────────────┘    │  │  │Lucky15 │ │
│                                                      │  │  │Custom  │ │
│                                                      │  │  └────────┘ │
│                                                      │  │             │
│                                                      │  │  GENERATED  │
│                                                      │  │  ┌────────┐ │
│                                                      │  │  │6 Doubles│ │
│                                                      │  │  │4 Trebles│ │
│                                                      │  │  │1 4-Fold │ │
│                                                      │  │  │         │ │
│                                                      │  │  │Stake: R │ │
│                                                      │  │  │Payout:R │ │
│                                                      │  │  │EV: +12% │ │
│                                                      │  │  │ROI: 320%│ │
│                                                      │  │  └────────┘ │
│                                                      │  │             │
│                                                      │  │  [POPULATE │
│                                                      │  │   BETSLIP] │
│                                                      │  └────────────┘ │
└──────────────────────────────────────────────────────┴────────────────┘
```

### Overlay States

- **Collapsed** — small tab on right edge, shows "BLM" badge
- **Expanded** — 320px wide panel with sections: Selection list, System config, Results
- **Empty** — "Click games to add to parlay" placeholder
- **Loading** — spinner while backend computes combinations
- **Populated** — full results with "Populate Bet Slip" button
- **Error** — "Failed to scrape data" / "Backend unreachable" messages

---

## 3. Database Schema (PostgreSQL)

Same as V1 plan — unchanged. The backend is still a FastAPI service with PostgreSQL for:

- **Project storage** (saved parlays for later retrieval)
- **Combination cache** (avoid recomputing same selection sets)
- **Session tracking** (optional, for analytics)

The userscript does NOT directly access the DB — it communicates via the FastAPI REST API.

---

## 4. API Specification

Same as V1 plan with ONE additional endpoint:

| Method | Path | Description |
|--------|------|-------------|
| POST | `/projects/{id}/selections/batch` | Batch add selections scraped from DOM |
| POST | `/scrape/validate` | Validate scraped odds before generation |

The userscript will call:
1. `POST /projects` — create a new "session" project
2. `POST /projects/{id}/selections/batch` — dump all scraped games
3. `POST /projects/{id}/combinations/generate` — generate parlays
4. `POST /projects/{id}/combinations/scenario` — simulate outcomes

---

## 5. Component Hierarchy — Overlay

### Overlay (lightweight — inline, no build step for userscript)

```
injected_overlay/
├── blm-panel.user.js            # Tampermonkey entry point
├── src/
│   ├── main.js                  # Init, inject styles, create container
│   ├── scraper.js               # DOM scraping: games, odds, teams
│   ├── betslip-bridge.js        # Click PokerBet odds, watch bet slip
│   ├── overlay/
│   │   ├── Panel.js             # Root overlay component
│   │   ├── Header.js            # "BLM Parlay" title, collapse/close
│   │   ├── GameList.js          # Scraped games with checkboxes
│   │   ├── GameRow.js           # Single game row (team + odds tickboxes)
│   │   ├── ParlayLegs.js        # Selected legs summary
│   │   ├── SystemPicker.js      # Doubles / Trebles / Yankee / Custom
│   │   ├── StakeInput.js        # Stake amount input
│   │   ├── ResultsPanel.js      # Generated combos
│   │   ├── ComboRow.js          # Single combo leg
│   │   ├── SummaryBar.js        # Total stake, payout, EV, ROI
│   │   └── PopulateButton.js    # "Populate Bet Slip" button
│   ├── services/
│   │   ├── api.js               # FastAPI backend calls
│   │   └── storage.js           # localStorage for session persistence
│   ├── utils/
│   │   ├── odds.js              # Odds conversion
│   │   ├── dom.js               # DOM query helpers
│   │   └── format.js            # Currency/number formatting
│   └── styles/
│       └── blm-overlay.css      # All styles (injected as <style>)
│
└── backend/                     # Same as V1 plan
```

### Key Implementation Detail: No Build Step

The Tampermonkey script is a single `.user.js` file. For development, the overlay components will be written as individual JS files, then bundled into a single userscript via a simple build script (or loaded modularly via `@require` directives in the Tampermonkey header).

The **recommended approach**: use Tampermonkey's `@require` to load each file from a local HTTP server during development, then bundle into a single-file userscript for release.

Example Tampermonkey header:
```javascript
// ==UserScript==
// @name         BLM Parlay Builder
// @namespace    https://blm.nousresearch.com
// @version      0.1.0
// @description  Build parlays from PokerBet live games
// @author       RedCapeTech
// @match        https://www.pokerbet.co.za/en/sports/*
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      localhost
// @require      file:///home/wa/projects/blm-roundrobin/overlay/main.js
// ==/UserScript==
```

---

## 6. DOM Scraping Strategy

### What to Scrape from PokerBet's Page

Based on the actual DOM we observed:

```javascript
// Each game row has this structure:
// generic[ref=eN] -> children:
//   StaticText "Minnesota Timberwolves Cyber"   // Home team
//   StaticText "53"                              // Home score
//   StaticText "Boston Celtics Cyber"            // Away team
//   StaticText "52"                              // Away score
//   StaticText "3rd Quarter"                     // Period
//   StaticText "+51"                             // Spread?
//   generic[clickable] -> StaticText ""          // More info
//   StaticText "53 : 52, (30:20), (19:28), (4:4) 09:35"  // Full score + clock
//   time -> StaticText "23:00"                   // Start time
//   generic[W1 clickable] -> StaticText "W1" + StaticText "1.25"  // Home odds
//   generic[W2 clickable] -> StaticText "W2" + StaticText "3.57"  // Away odds
```

**Scraper algorithm:**
1. Locate the match list container: `#root` → Basketball section → "World Cyber Basketball. 2K26 Matches" region
2. Iterate through game rows (ref=e82, e83, e84, e85 from our snapshot)
3. For each row, extract:
   - Home team (first StaticText after scores)
   - Away team (second StaticText)
   - Home score, away score
   - Quarter text
   - Clock (from the combined score string: "53 : 52, ... 09:35")
   - W1 odds and W2 odds (from the clickable odds buttons)
4. Combine into selection objects

**Resilience:** 
- Use multiple selector strategies (data attributes, class names, text patterns)
- Fall back to regex-based parsing of the raw text if structured selectors fail
- Validate that extracted odds are valid numbers > 1.0
- Report scraping errors as user-visible warnings, not silent failures

### Bet Slip Bridge

When user clicks "Populate Bet Slip":
1. For each selected combination, the script finds the corresponding odds button on the PokerBet page
2. Programmatically clicks it (dispatches a `click` event)
3. PokerBet's native bet slip accumulates the selections
4. Script watches the bet slip for updates (mutation observer)
5. Displays "Bet slip populated — review and place" confirmation

**Safety:** The script only clicks odds buttons on the same page. It does NOT submit the bet slip. The user must manually review and click "Place Bet" in PokerBet's UI.

---

## 7. Combination Generation Algorithm

Same as V1 plan — unchanged. The iterative nCr approach with performance limits at N=20 (warn) and N=30 (reject all-folds).

---

## 8. Performance Strategy

### Backend (same as V1)
- Async FastAPI endpoints
- SHA256 cache key for combination results
- Pagination for large result sets

### Overlay
- **Scraping:** Run DOM scraping every 20s (aligns with PokerBet's live update cadence)
- **Rendering:** Virtual scroll only if > 50 games on page (unlikely — typically 4-20)
- **API calls:** Debounce selection changes (300ms) before sending to backend
- **Storage:** localStorage for session persistence (selected legs, system config)
- **Minimal DOM manipulation:** Batch DOM reads, use DocumentFragment for writes

---

## 9. Testing Strategy

### Overlay Tests
- **Scraper tests:** Mock PokerBet's DOM structure, verify parsing
- **Bet slip bridge:** Verify correct odds buttons are identified and clicked
- **Integration (PokerBet):** Manual testing against live site

### Backend Tests
Same as V1 plan — pytest for generator, calculator, scenarios, optimiser

---

## 10. Risk Assessment

| Risk | Impact | Prob | Mitigation |
|------|--------|------|------------|
| PokerBet DOM changes break scraper | High | Medium | Log DOM structure on failure; version scraper; easy-to-update selector config |
| PokerBet detects automation / bans account | Critical | Low | Script only clicks odds (like a user would); does NOT submit bets; mimics human timing (50-200ms delays between clicks) |
| Tampermonkey `@require` file:// blocked by Chrome security | Medium | Medium | Use local HTTP server during dev; bundle to single-file for release |
| BetConstruct platform update changes bet slip behaviour | Medium | Low | MutationObserver falls back gracefully; user can manually click odds |
| Page re-renders (React) invalidate scraped DOM references | High | Medium | Re-scrape on every mutation; use event delegation on stable parent |
| FastAPI backend not running / unreachable | Medium | Low | Show "Backend offline" in overlay; provide fallback client-side combo generation for small N (≤6) |

---

## 11. Implementation Phases (Revised)

### Phase 0 — Scaffold + Userscript Skeleton (Day 1)
- [ ] Create `./projects/blm-roundrobin/` directory structure
- [ ] Backend: FastAPI scaffold + Docker Compose for PostgreSQL
- [ ] Backend: Alembic initial migration (same tables as V1)
- [ ] Overlay: Create `blm-panel.user.js` with Tampermonkey header
- [ ] Overlay: Inject floating panel shell (vanilla JS, static HTML)
- [ ] Overlay: Style the panel (dark theme, collapsible, draggable)
- [ ] Verify: Script loads on `pokerbet.co.za/en/sports/*`

### Phase 1 — DOM Scraper + Selection List (Days 2-3)
- [ ] Overlay: Implement scraper module reading PokerBet game rows
- [ ] Overlay: Display scraped games as selectable list
- [ ] Overlay: Checkbox selection → add/remove from parlay legs
- [ ] Overlay: Odds display (W1/W2 buttons selectable)
- [ ] Overlay: Session persistence (selected legs survive page refresh)
- [ ] Test: Scrape 4 live games → verify team names + odds match

### Phase 2 — Backend Engine + API (Days 3-5)
- [ ] Backend: Combination generator (nCr, all fold sizes)
- [ ] Backend: Financial calculator (payout, EV, Kelly, breakeven)
- [ ] Backend: System template definitions (Patent → Goliath)
- [ ] Backend: Project + Selection CRUD API
- [ ] Backend: Combination generation API + cache
- [ ] Backend: Scenario simulation API
- [ ] Test: pytest for generator, calculator, templates, API

### Phase 3 — System Picker + Results Display (Days 6-8)
- [ ] Overlay: System template buttons (Doubles, Trebles, Yankee, etc.)
- [ ] Overlay: Custom system config (pick fold sizes, round robin X of N)
- [ ] Overlay: Stake input (per-combo or total)
- [ ] Overlay: Call backend → display generated combinations
- [ ] Overlay: Summary metrics cards (total stake, payout, EV, ROI)
- [ ] Overlay: Scenario simulation controls + results
- [ ] Test: Full cycle: scrape → select → generate → display

### Phase 4 — Bet Slip Bridge (Days 9-10)
- [ ] Overlay: Implement `betslip-bridge.js` — locate and click odds buttons
- [ ] Overlay: MutationObserver on PokerBet's bet slip DOM
- [ ] Overlay: "Populate Bet Slip" flow — click all odds for a system
- [ ] Overlay: Progress indicator (clicking 11 odds for a Yankee)
- [ ] Overlay: Error state if odds button not found / odds changed
- [ ] Overlay: Warning if odds have changed since scrape
- [ ] Test: Select 4 games → Yankee (11 bets) → verify bet slip populates

### Phase 5 — BLM Scores + Optimiser (Days 11-12)
- [ ] Backend: BLM aggregator module (roll up per-leg scores to combos)
- [ ] Overlay: Display BLM / Trap Meter / Confidence per combination
- [ ] Backend: Optimisation engine (remove weak legs, maximise EV)
- [ ] Overlay: "Optimise" button → suggest removal of weak selections
- [ ] Overlay: BLM heatmap per combination
- [ ] Test: Optimiser correctly identifies low-EV legs

### Phase 6 — Comparison + Export (Days 13-14)
- [ ] Overlay: Compare 2-3 system configurations side-by-side
- [ ] Overlay: Export selected parlays as CSV / JSON / clipboard text
- [ ] Overlay: Save/load sessions (localStorage projects)
- [ ] Overlay: Keyboard shortcuts (Esc to close, Enter to populate)
- [ ] Polish: Error states, loading states, empty states
- [ ] Final: Bundle to single-file userscript for distribution

**Total: ~14 days**

**MVP (Phases 0-4): ~10 days** — scraper works, parlays generate, bet slip populates

---

## 12. File Structure (Revised)

```
./projects/blm-roundrobin/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── AGENTS.md
├── README.md
│
├── overlay/                              # ← NEW: Tampermonkey userscript
│   ├── blm-panel.user.js                 # Tampermonkey entry (dev: @require)
│   ├── dist/
│   │   └── blm-panel.user.js             # Bundled single-file release
│   ├── src/
│   │   ├── main.js                       # Init: inject container, mount app
│   │   ├── scraper.js                    # DOM scraping logic
│   │   ├── betslip-bridge.js             # Odds clicker + bet slip watcher
│   │   ├── overlay/
│   │   │   ├── Panel.js                  # Root component (vanilla JS class)
│   │   │   ├── Header.js
│   │   │   ├── GameList.js
│   │   │   ├── GameRow.js
│   │   │   ├── ParlayLegs.js
│   │   │   ├── SystemPicker.js
│   │   │   ├── StakeInput.js
│   │   │   ├── ResultsPanel.js
│   │   │   ├── ComboRow.js
│   │   │   ├── SummaryBar.js
│   │   │   ├── ScenarioView.js
│   │   │   ├── CompareView.js
│   │   │   └── PopulateButton.js
│   │   ├── services/
│   │   │   ├── api.js
│   │   │   └── storage.js
│   │   ├── utils/
│   │   │   ├── odds.js
│   │   │   ├── dom.js
│   │   │   └── format.js
│   │   └── styles/
│   │       └── blm-overlay.css
│   └── tests/
│       └── scraper.test.js
│
├── backend/                              # Same as V1 plan (FastAPI + PostgreSQL)
│   ├── main.py
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   └── versions/
│   ├── api/
│   │   ├── router.py
│   │   ├── projects.py
│   │   ├── selections.py
│   │   ├── combinations.py
│   │   ├── systems.py
│   │   └── compare.py
│   ├── models/
│   │   ├── project.py
│   │   ├── selection.py
│   │   ├── saved_system.py
│   │   ├── combination_cache.py
│   │   └── project_snapshot.py
│   ├── schemas/
│   │   ├── project.py
│   │   ├── selection.py
│   │   ├── combination.py
│   │   ├── system.py
│   │   ├── scenario.py
│   │   └── compare.py
│   ├── engine/
│   │   ├── generator.py
│   │   ├── calculator.py
│   │   ├── scenarios.py
│   │   ├── optimizer.py
│   │   ├── templates.py
│   │   └── blm_aggregator.py
│   ├── services/
│   │   ├── project_service.py
│   │   ├── selection_service.py
│   │   ├── combination_service.py
│   │   └── comparison_service.py
│   ├── db/
│   │   ├── session.py
│   │   └── seed.py
│   └── tests/
│       ├── test_generator.py
│       ├── test_calculator.py
│       ├── test_scenarios.py
│       ├── test_optimizer.py
│       ├── test_templates.py
│       └── test_api_*.py
│
└── tests/
    └── e2e/
        └── full-cycle.spec.ts           # Playwright: page + overlay interaction
```

---

## 13. Key Implementation Notes

### 13.1 Vanilla JS Overlay (Not React)

The overlay uses **vanilla JavaScript** with a lightweight component pattern, not React. Reasons:
- No build step needed for Tampermonkey's `@require` loading
- ~2KB added vs ~40KB for React minimised
- Direct DOM manipulation is actually simpler for scraping and bet slip interaction
- Easier to debug in Tampermonkey's editor
- Can be bundled to a single `.user.js` file with zero dependencies

Component pattern:
```javascript
// Each component is a factory function:
function GameRow({ game, onSelect }) {
    const el = document.createElement('div');
    el.className = 'blm-game-row';
    el.innerHTML = `
        <input type="checkbox" class="blm-check" ${game.selected ? 'checked' : ''}>
        <span class="blm-team">${game.homeTeam}</span>
        <span class="blm-odds">${game.homeOdds}</span>
        <button class="blm-vs">vs</button>
        <span class="blm-odds">${game.awayOdds}</span>
        <span class="blm-team">${game.awayTeam}</span>
        <span class="blm-period">${game.period}</span>
    `;
    el.querySelector('.blm-check').onchange = (e) => onSelect(game.id, e.target.checked);
    return el;
}
```

### 13.2 Backend Location

- Backend runs locally on `localhost:8420`
- The userscript connects via `GM_xmlhttpRequest` to `http://localhost:8420/api/v1`
- `@connect localhost` in the Tampermonkey header enables this
- For production: could deploy backend to a VPS, change `@connect` to the VPS domain

### 13.3 Bet Slip Population — Technical Detail

PokerBet's odds buttons have click handlers. The script:

1. Locates the button by game + team name + odds text
2. Dispatches a native `click` event (not jQuery — PokerBet uses React)
3. Waits 100-200ms between clicks (human-like timing)
4. Uses `MutationObserver` on the bet slip container to confirm each leg was added
5. Reports progress: "Populating bet slip... (3/11)"
6. On completion: "Bet slip ready — review and place"

**Fallback:** If a specific odds button can't be auto-located, highlight the game in the overlay and let the user click it manually.

---

## 14. Decision Log (V2 Updates)

| # | Decision | Rationale | Alternatives Considered |
|---|----------|-----------|------------------------|
| D1 | Tampermonkey userscript, not Chrome extension | Faster development; no packaging/signing; no Chrome Web Store review | Chrome extension — more capabilities but slower to iterate |
| D2 | Vanilla JS overlay, not React | Zero-build for Tampermonkey; ~2KB vs ~40KB; simpler debugging | React — component ecosystem but breaks Tampermonkey's simple injection model |
| D3 | Auto-populate bet slip, not auto-place bets | User MUST manually confirm; prevents accidental bets; PokerBet's TOS safer | Full automation — high ban risk, legal grey area |
| D4 | Single local backend, not on-page computation | Heavy combination maths is fast in Python; clean separation; cache layer | WASM/JS client-side — possible but slower for N > 10 |
| D5 | DOM scraping, not API integration | PokerBet/BetConstruct has no public API; DOM is the only source | Reverse engineering BetConstruct's internal API — fragile, TOS violation, could be blocked |
| D6 | 20s scrape cadence | Matches PokerBet's live update rate; avoids excessive DOM queries | Real-time via MutationObserver — too chatty, PokerBet re-renders entire sections |

---

## 15. Rollback Strategy (same as V1)

Same git-based + database rollback approach from V1 plan. Each phase tagged, alembic downgrade for DB, docker compose rollback for backend.

---

## 16. Git Strategy (same as V1, adjusted)

```
Branch Structure:
  main
  develop
  feature/phase-0-scaffold
  feature/phase-1-scraper
  feature/phase-2-engine
  feature/phase-3-results
  feature/phase-4-betslip
  feature/phase-5-blm
  feature/phase-6-polish
```

---

## Review

**Key architectural change from V1:** This is NOT a standalone web app. It's a **Tampermonkey userscript overlay** injected into `pokerbet.co.za` that:

1. Scrapes live game data from the DOM
2. Lets you select legs and build parlays in a side panel
3. Sends selections to a local FastAPI backend for computation
4. **Populates PokerBet's native bet slip** by clicking the odds buttons programmatically
5. You review and click "Place Bet" yourself — no automation of the actual wager

The backend (FastAPI + PostgreSQL) does all the heavy lifting — combination generation, EV/Kelly calculations, scenario simulation, BLM aggregation. The userscript is a thin UI layer that scrapes and clicks.

**MVP: Phases 0-4 (10 days)** — you'll have a working panel that scrapes live games, generates parlays, and populates the bet slip.

**No code written.** Ready for your approval. Want me to add anything or adjust before we start?
