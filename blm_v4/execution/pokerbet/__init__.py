"""BLM EXECUTION — POKERBET LIVE LAYER (phase ③, directive 2026-09-23).

The ONLY place in BLM that knows about Playwright selectors and the
PokerBet DOM.  Everything is behind the existing ``SelectionResolver``
protocol — the engine (phases ①/②) keeps running unchanged on top of
this adapter.

  * ``browser`` — the Playwright-over-CDP bridge to the operator's own
    logged-in Chrome.  No credentials are ever read, stored or typed:
    BLM ATTACHES to an already-authenticated browser session.
  * ``session`` — authentication detection + event-view URL building
    for the canonical game record.
  * ``dom`` — the live adapter: resolve game/market/position from the
    CURRENT DOM, click, re-read the betslip, place, confirm.

DRY_RUN remains the default and the engine stops at BETSLIP_READY in
every non-LIVE mode by construction — enabling this layer does NOT
enable real-money execution.
"""
