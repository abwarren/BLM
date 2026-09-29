/* ═══════════════════════════════════════════════════════════════
   STATS TAB — the fired-alert cohort, its fingerprints, the league
   standings and the alert-timing window.

   SELF-CONTAINED ON PURPOSE.  This file shares no helper, constant or
   state with dashboard.js: it defines its own DOM/format helpers under a
   private namespace.  The two scripts render different views of the same
   page, and a shared `$`/`esc` would couple them such that editing either
   could silently break the other.

   IT NEVER COMPUTES ANYTHING.  Every number comes from /api/v4/stats,
   which is served from a background worker's last COMPLETED scan.  The
   page performs no aggregation, no division and no rounding of raw data —
   if a rate is not in the payload, it is rendered as "–" rather than
   derived here.  That keeps the screen and the audited artifact in
   agreement: there is exactly one place a number is calculated, and it is
   the Python module the CLI and the tests both use.

   The scan behind the payload takes ~10 s over the full history, so
   requesting on a poll would be unusable.  Instead the payload is pulled
   on a timer and the response is tiny; a "warming" status is rendered as
   such rather than as zeros, so an unfinished first scan can never look
   like an empty archive.
   ═══════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const STATS_URL = "/api/v4/stats";
  const POLL_MS = 5000;

  const S = {
    timer: null,
    inflight: false,
    lastOk: null,
    failures: 0,
    active: false,
  };

  /* ── DOM + format helpers (private to this file) ───────────── */
  const el = (id) => document.getElementById(id);
  const TXT = { "-": "–" };

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /** A payload number, or the em-dash placeholder. Never invents a value. */
  function num(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return TXT["-"];
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }
  function int(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return TXT["-"];
    return String(Math.round(Number(v)));
  }
  function pct(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return TXT["-"];
    return num(v, digits === undefined ? 2 : digits) + "%";
  }
  /** Signed percentage-point delta, for "vs cohort" columns. */
  function dpp(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return TXT["-"];
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(2);
  }

  function cls(v) {
    if (v === "PROFITABLE") return "v-profitable";
    if (v === "LOSING") return "v-losing";
    if (v === "COIN FLIP") return "v-coin";
    return "v-none";
  }

  /** Inline proportion bar, so a table of percentages scans visually. */
  function bar(p) {
    if (p === null || p === undefined || Number.isNaN(p)) return "";
    const w = Math.max(0, Math.min(100, Number(p)));
    return '<span class="st-bar"><i style="width:' + w.toFixed(1) +
           '%"></i></span>';
  }

  /**
   * A count column with the proportion bar beside it.
   *
   * Returns a CELL OBJECT, never a string: `table()` escapes plain strings, so
   * concatenating markup into a string would print the tags literally (the
   * bars rendered as visible `<span class="st-bar">…` text before this was
   * fixed).  Anything carrying markup must go through the `{html: …}` form.
   */
  const barCell = (text, p) => ({ html: esc(text) + bar(p), cls: "" });

  /** A left-aligned text cell — the table default is right-aligned numerics. */
  const txtCell = (text) => ({ html: esc(text), cls: "txt" });

  function table(node, headers, rows) {
    if (!node) return;
    let h = "<thead><tr>";
    for (const col of headers) h += "<th>" + esc(col) + "</th>";
    h += "</tr></thead><tbody>";
    if (!rows.length) {
      h += '<tr><td class="muted" colspan="' + headers.length +
           '">no data</td></tr>';
    } else {
      for (const r of rows) {
        h += "<tr>";
        for (const c of r) {
          if (c && typeof c === "object" && c.html !== undefined) {
            h += '<td class="' + (c.cls || "") + '">' + c.html + "</td>";
          } else {
            h += "<td>" + esc(c) + "</td>";
          }
        }
        h += "</tr>";
      }
    }
    h += "</tbody>";
    node.innerHTML = h;
  }

  const cell = (text, extra) => ({ html: esc(text), cls: extra || "" });

  /* ── renderers ─────────────────────────────────────────────── */

  function renderHeadline(p) {
    const c = p.cohort || {};
    const a = p.actionable || {};
    const kpis = el("stKpis");
    if (kpis) {
      // The headline is the number the operator asked for: of all games where
      // the alert fired, what share ended UNDER.
      const cards = [
        ["COHORT", int(c.n), "games where the alert fired", "plain"],
        ["ENDED UNDER", pct(c.under_pct), int(c.under) + " of " + int(c.n),
         c.under_pct >= 50 ? "good" : "bad"],
        ["95% INTERVAL", num(c.wilson_lo, 1) + "–" + num(c.wilson_hi, 1),
         "Wilson score", "plain"],
        ["ENDED OVER", int(c.over), "final above the line", "plain"],
        ["PUSH", int(c.push), "level finish", "plain"],
        ["ACTIONABLE", pct(a.under_pct),
         int(a.n) + " with a LIVE market", "plain"],
      ];
      kpis.innerHTML = cards.map(([label, value, sub, tone]) =>
        '<div class="st-kpi">' +
        '<div class="st-kpi-label">' + esc(label) + "</div>" +
        '<div class="st-kpi-value ' + tone + '">' + esc(value) + "</div>" +
        '<div class="st-kpi-sub">' + esc(sub) + "</div></div>").join("");
    }
    const note = el("stHeadlineNote");
    if (note) {
      // The scan cost is shown because it is NOT free: it is a full-history
      // read that the worker skips when nothing has settled.  Making that
      // visible keeps the cost auditable instead of assumed.
      const skipped = (p.skips || 0);
      const ran = (p.runs || 0);
      note.innerHTML =
        "Settled games only, one observation per game (the one closest to " +
        esc(String((p.definitions && p.definitions.alert) || "")) + "). " +
        "Settled games in archive: " + esc(int(c.settled_games_total)) +
        " · in a scorable league: " + esc(int(c.universe_settled)) + ".<br>" +
        "Scans run: " + esc(int(ran)) + " · skipped as unchanged: " +
        esc(int(skipped)) + " (the scan is a full-history read, so it is " +
        "skipped whenever no new game has settled).";
    }
    const ago = el("stAsOf");
    if (ago) {
      ago.textContent = "scan age " + num(p.age_s, 0) + "s";
      ago.title = "payload as_of " + (p.as_of || "?");
      ago.style.color = p.stale ? "var(--amber)" : "";
    }
    const model = el("stModel");
    if (model) model.textContent = p.model ? "· " + p.model : "";
  }

  function renderLeagues(p) {
    const rows = (p.leagues || []).map((lg) => [
      cell(lg.label, "lbl"),
      int(lg.n),
      barCell(pct(lg.under_pct), lg.under_pct),
      num(lg.wilson_lo, 1) + "–" + num(lg.wilson_hi, 1),
      { html: esc(lg.verdict || TXT["-"]), cls: cls(lg.verdict) },
      dpp(lg.vs_50),
      pct(lg.selection_pct, 1),
      num(lg.median_req_bar, 3),
      int(lg.firings_n),
      int(lg.late_n),
      pct(lg.late_under_pct, 1),
      num(lg.ref_avg_pace, 3),
    ]);
    table(el("stLeagues"),
      ["league", "N", "UNDER%", "95% interval", "verdict", "vs 50pp",
       "fires %", "med req/bar", "firings", "late", "late UNDER%",
       "league pace"],
      rows);

    const note = el("stLeaguesNote");
    if (note) {
      note.innerHTML =
        "<strong>How to read this.</strong> Raw UNDER% alone cannot rank " +
        "leagues — a small sample reads 100% or 0% by luck, which is why the " +
        "95% interval is shown and the verdict asks the honest question: is " +
        "the interval's <em>lower</em> bound above 50%? \"fires %\" is how " +
        "often that league triggers at all: a league that fires often AND " +
        "wins rarely is the real problem, whereas one that fires rarely and " +
        "wins rarely is simply weak. Leagues are keyed on competition_slug, " +
        "never on classification — BETUAL_NBA bundles five competitions.";
    }
  }

  function renderWindow(p) {
    const w = p.window || {};
    const buckets = [["early", "before 6th min Q3"],
                     ["in_window", "6th min Q3 → 4th min Q4"],
                     ["late", "after 4th min Q4"]];
    const rows = [];
    for (const [key, label] of buckets) {
      const b = w[key];
      if (!b) continue;
      const tone = (b.under_pct !== null && b.under_pct >= 50) ? "good" : "bad";
      rows.push([
        cell(label, "lbl"),
        int(b.n),
        int(b.scored),
        barCell(int(b.under), b.under_pct),
        { html: '<span class="st-kpi-value ' + tone +
                '" style="font-size:13px">' + esc(pct(b.under_pct)) +
                "</span>", cls: "" },
        num(b.wilson_lo, 1) + "–" + num(b.wilson_hi, 1),
        int(b.actionable),
      ]);
    }
    table(el("stWindow"),
      ["firing moment", "games", "scored", "UNDER", "UNDER%", "95% interval",
       "live market"],
      rows);

    table(el("stWindowBounds"),
      ["classification", "quarter", "game", "floor: 6th min Q3",
       "ceiling: 4th min Q4", "trigger floor"],
      (w.per_classification || []).map((c) => [
        cell(c.classification, "lbl"),
        num(c.quarter_minutes, 0) + " min",
        num(c.full_minutes, 0) + " min",
        num(c.floor_minutes, 0) + " min (" + pct(c.floor_pct, 2) + ")",
        num(c.ceiling_minutes, 0) + " min (" + pct(c.ceiling_pct, 2) + ")",
        "progress ≥ " + pct(c.trigger_floor_pct, 1),
      ]));

    const note = el("stWindowNote");
    if (note) {
      const cost = w.cost_of_enforcing_ceiling || {};
      const drop = cost.dropped_under_pct;
      const keep = cost.kept_under_pct;
      let verdict = "";
      if (drop !== null && drop !== undefined && keep !== null &&
          keep !== undefined) {
        verdict = drop > keep
          ? " On this archive the alerts the ceiling would DROP are the " +
            "BETTER ones (" + pct(drop) + " vs " + pct(keep) +
            ") — enforcing it would remove the most profitable alerts."
          : " On this archive the alerts the ceiling would drop run " + pct(drop) +
            " against " + pct(keep) + " for those kept.";
      }
      note.innerHTML =
        "<strong>Measured from each game's FIRST firing moment</strong>, not " +
        "from the cohort observation — that one sits at ~75% by construction " +
        "and could never show lateness. The window is " +
        "<strong>reported, not enforced</strong>: no alert is suppressed. " +
        "Enforcing the ceiling would drop " + esc(int(cost.dropped_n)) +
        " alerts (" + esc(pct(drop)) + ") and keep " +
        esc(int(cost.kept_n)) + " (" + esc(pct(keep)) + ")." + esc(verdict) +
        "<br>" + esc(w.note || "");
    }
  }

  function renderFingerprints(p) {
    const rules = {};
    for (const d of ((p.definitions || {}).fingerprints || [])) {
      rules[d.key] = d.rule;
    }
    const cohortPct = (p.cohort || {}).under_pct;
    table(el("stFingerprints"),
      ["fp", "rule", "N", "UNDER", "OVER", "UNDER%", "coverage",
       "vs cohort", "unavailable"],
      (p.fingerprints || []).map((f) => [
        cell(f.key, "lbl"),
        txtCell(rules[f.key] || f.label || ""),
        int(f.n),
        barCell(int(f.under), f.under_pct),
        int(f.over),
        pct(f.under_pct),
        pct(f.coverage_pct, 1),
        cell(dpp(f.delta_pp)),
        int(f.unavailable),
      ]));
    const n = el("stFingerprintsNote");
    if (n) {
      n.textContent = cohortPct === undefined
        ? ""
        : "Cohort baseline " + pct(cohortPct) + ". \"vs cohort\" is the " +
          "difference in percentage points. UNAVAILABLE means an operand was " +
          "missing — it is never silently counted as TRUE.";
    }
  }

  function renderLadder(p) {
    table(el("stLadder"),
      ["fingerprints", "N", "UNDER", "OVER", "UNDER%", "95% interval"],
      (p.ladder || []).map((l) => [
        cell(l.count === 0 ? "none" : String(l.count), "lbl"),
        int(l.n),
        barCell(int(l.under), l.under_pct),
        int(l.over),
        pct(l.under_pct),
        num(l.wilson_lo, 1) + "–" + num(l.wilson_hi, 1),
      ]));
    const note = el("stCollapse");
    const c = p.collapse || {};
    if (note) {
      note.innerHTML = c.identical
        ? "<strong>Read with care:</strong> a full house is " +
          esc(c.definition) + " — verified set-identical on this archive (" +
          esc(int(c.seven_n)) + " games both ways). " + esc(c.explanation) +
          " The ladder therefore reflects ONE conjunction, and the keys are " +
          "not independent votes."
        : "Full-house set vs C1&amp;C2&amp;R2: seven_n=" +
          esc(int(c.seven_n)) + ", intersection_n=" +
          esc(int(c.intersection_n)) + " (not identical on this archive).";
    }
  }

  function renderBands(p) {
    table(el("stBands"),
      ["req / bar", "N", "UNDER", "OVER", "UNDER%"],
      (p.bands || []).map((b) => [
        cell(b.label, "lbl"),
        int(b.n),
        barCell(int(b.under), b.under_pct),
        int(b.over),
        pct(b.under_pct),
      ]));
  }

  function renderCombos(p) {
    table(el("stCombos"),
      ["fired set", "N", "UNDER", "OVER", "UNDER%"],
      (p.combos || []).map((c) => [
        cell(c.label, "lbl"),
        int(c.n),
        barCell(int(c.under), c.under_pct),
        int(c.over),
        pct(c.under_pct),
      ]));
  }

  function renderDefinitions(p) {
    const d = p.definitions || {};
    const rows = [];
    for (const f of (d.fingerprints || [])) {
      rows.push([cell(f.key, "lbl"), txtCell(f.rule)]);
    }
    const ops = d.operands || {};
    for (const k of Object.keys(ops)) {
      rows.push([cell(k, "lbl"), txtCell(ops[k])]);
    }
    if (d.alert) rows.push([cell("alert", "lbl"), txtCell(d.alert)]);
    table(el("stDefinitions"), ["term", "definition"], rows);

    const note = el("stNotes");
    if (note && p.notes) {
      note.innerHTML = "<strong>Caveats</strong><ul>" +
        p.notes.map((x) => "<li>" + esc(x) + "</li>").join("") + "</ul>";
    }
  }

  function renderAll(p) {
    renderHeadline(p);
    renderLeagues(p);
    renderWindow(p);
    renderFingerprints(p);
    renderLadder(p);
    renderBands(p);
    renderCombos(p);
    renderDefinitions(p);
  }

  function renderStatus(p) {
    // "warming" / "unconfigured" must never look like an empty archive.
    const kpis = el("stKpis");
    if (!kpis) return;
    const msg = p.message || "waiting for the first scan";
    kpis.innerHTML = '<div class="st-kpi"><div class="st-kpi-label">' +
      esc(p.status === "unconfigured" ? "NOT WIRED" : "WARMING UP") +
      '</div><div class="st-kpi-value warn">…</div>' +
      '<div class="st-kpi-sub">' + esc(msg) + "</div></div>";
    const note = el("stHeadlineNote");
    if (note) {
      note.textContent = "The stats scan runs on a background worker; this " +
        "view fills in as soon as the first scan completes. Nothing is " +
        "requested from the network on a poll.";
    }
  }

  /* ── polling ───────────────────────────────────────────────── */

  async function pull() {
    if (S.inflight) return;
    S.inflight = true;
    try {
      const resp = await fetch(STATS_URL, { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const p = await resp.json();
      S.failures = 0;
      S.lastOk = p;
      if (p.status === "ok") renderAll(p);
      else renderStatus(p);
    } catch (err) {
      S.failures += 1;
      const ago = el("stAsOf");
      if (ago) {
        ago.textContent = "stats unavailable (" + S.failures + ")";
        ago.style.color = "var(--red)";
      }
      // Keep the last good render on screen — a blip must not blank the tab.
    } finally {
      S.inflight = false;
    }
  }

  function startPolling() {
    if (S.timer) return;
    S.timer = setInterval(pull, POLL_MS);
  }
  function stopPolling() {
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
  }

  /* ── view switching ────────────────────────────────────────── */

  function show(view) {
    const live = el("viewLive"), stats = el("viewStats");
    const tabLive = el("tabLive"), tabStats = el("tabStats");
    const note = el("tabNote");
    if (!live || !stats) return;
    const onStats = view === "stats";
    S.active = onStats;
    live.hidden = onStats;
    stats.hidden = !onStats;
    if (tabLive) {
      tabLive.classList.toggle("active", !onStats);
      tabLive.setAttribute("aria-selected", String(!onStats));
    }
    if (tabStats) {
      tabStats.classList.toggle("active", onStats);
      tabStats.setAttribute("aria-selected", String(onStats));
    }
    if (note) {
      note.textContent = onStats
        ? "fired-alert cohort · fingerprints · league standings"
        : "live games + alerts";
    }
    if (onStats) {
      // Fetch immediately on open (the payload is already computed
      // server-side, so this is a memory read, not a scan), then poll.
      pull();
      startPolling();
    } else {
      stopPolling();
    }
  }

  function init() {
    const tabs = el("tabs");
    if (tabs) {
      tabs.addEventListener("click", (ev) => {
        const btn = ev.target.closest(".tab");
        if (btn && btn.dataset.view) show(btn.dataset.view);
      });
    }
    // Hoist the fetched logs/notes out of the way when the live view is up:
    // the stats view is opt-in, so the live board is never bled into.
    show("live");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Exposed for tests / manual console use only.
  window.__statsTab = { pull, show, state: S };
})();
