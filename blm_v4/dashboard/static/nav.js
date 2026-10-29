/* ═══════════ ONE canonical destination switcher ═══════════════════════════
   Why this file exists: the view switcher used to live in three places
   (stats.js, explorer.js and the tab markup), each knowing its own two or
   three views.  Adding destinations that way means N² edits and a tab that
   hides a view another script still thinks is visible.  Here there is ONE
   function: every `.view[data-view]` is hidden except the target, every
   `.tab[data-view]` mirrors the selection, and a `blm:view` event tells the
   polling modules (stats / explorer / cohorts) to start or stop.

   Destinations only — no dataset filters, no model state, no Auto-Bet state.
   Switching a destination must never change what is being measured. */
(function () {
  const VIEWS = ["overview", "live", "alerts", "cohorts", "history",
                 "model", "health", "settings"];
  const DEFAULT_VIEW = "live";
  const PREF_KEY = "blm.destination";
  let current = null;

  const $ = (id) => document.getElementById(id);

  function views() {
    return Array.from(document.querySelectorAll(".view[data-view]"));
  }

  function normalise(name) {
    return VIEWS.includes(name) ? name : DEFAULT_VIEW;
  }

  function show(name) {
    const view = normalise(name);
    const nodes = views();
    if (!nodes.length) return view;
    for (const node of nodes) {
      node.hidden = node.dataset.view !== view;
    }
    document.querySelectorAll("#tabs .tab[data-view]").forEach((tab) => {
      const on = tab.dataset.view === view;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", String(on));
    });
    const note = $("tabNote");
    if (note) note.textContent = NOTES[view] || "";
    current = view;
    try { localStorage.setItem(PREF_KEY, view); } catch (_) {}
    // One event, dispatched AFTER the DOM is consistent, so a listener can
    // read `hidden` truthfully.  Bubbles so a wrapper element can listen too.
    document.dispatchEvent(new CustomEvent("blm:view", { detail: { view } }));
    // Returning to a destination keeps its scroll position; a fresh one starts
    // at the top so the first visible thing is the destination's own header.
    if (view !== "live" || !window.__blmLiveScrollKept) {
      window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
    }
    return view;
  }

  const NOTES = {
    overview: "desk state · live games · alerts · cohort link",
    live: "live games + alerts",
    alerts: "what is firing right now",
    cohorts: "fired-alert cohort · denominators · ledger · data quality",
    history: "settled alerts against their frozen trigger line",
    model: "legacy fingerprints · league standings (LEGACY rules)",
    health: "read-only data / database explorer",
    settings: "Auto-Bet configuration — cohort selection only",
  };

  function init() {
    const tabs = $("tabs");
    if (tabs) {
      tabs.addEventListener("click", (ev) => {
        const btn = ev.target.closest(".tab[data-view]");
        if (btn) show(btn.dataset.view);
      });
    }
    // Arrow-key navigation across the destination bar (role="tablist").
    if (tabs) {
      tabs.addEventListener("keydown", (ev) => {
        if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
        const all = Array.from(tabs.querySelectorAll(".tab[data-view]"));
        const i = all.indexOf(document.activeElement);
        if (i < 0) return;
        ev.preventDefault();
        const next = all[(i + (ev.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
        next.focus();
        show(next.dataset.view);
      });
    }
    // Overview destination cards are plain navigation, not state.
    document.querySelectorAll(".desk-card[data-goto]").forEach((card) => {
      card.addEventListener("click", () => show(card.dataset.goto));
    });
    let first = DEFAULT_VIEW;
    try { first = normalise(localStorage.getItem(PREF_KEY) || ""); } catch (_) {}
    show(first);
  }

  window.blmNav = { show, views: VIEWS.slice(), get current() { return current; } };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
