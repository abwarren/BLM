/* ══════════════════════════════════════════════════════════════════════════
   SLATE — the auto-bet CANDIDATE board (prod dashboard, additive file)

   Operator directive 2026-11-02: "remove the other stats and i want my slate
   there in realtime".  The STATS tab is hidden in index.html; this adds a
   SLATE tab that shows what the engine will actually do, live.

   It reads the SAME payload the LIVE view already polls (/api/v4/live) and
   renders the SAME verdict the worker composes — under_alert_exec_armed is
   served by the backend from blm_v4.trade_window, so this file can never claim
   a game is tradeable that the executor would refuse.  Every field is read
   defensively: a missing one degrades to "–", never to a false verdict.

   Self-contained on purpose: no coupling to dashboard.js's internals, so it
   cannot break the existing views.  Poll: 5 s, only while the tab is visible.
   ═════════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  if (window.__blmSlate) return;            // never double-install
  window.__blmSlate = true;

  var POLL_MS = 5000;
  var timer = null;

  function $(id) { return document.getElementById(id); }
  function num(v) { return (typeof v === "number" && isFinite(v)) ? v : null; }
  function f(v, d) {
    var n = num(v);
    return n === null ? "–" : n.toFixed(d === undefined ? 2 : d);
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ── one game → the fields the board needs, with fallbacks ───────────── */
  function view(g) {
    var ua = g.under_alert || {};
    var win = g.under_alert_execution_window || {};
    var req = num(ua.required_pace) !== null ? num(ua.required_pace)
                                             : num(g.required_pts_per_min);
    var avg = num(ua.league_average_pace);
    var act = num(ua.actual_pace) !== null ? num(ua.actual_pace)
                                           : num(g.actual_pts_per_min);
    var pct = num(g.progress_pct);
    if (pct === null) pct = num(g.remaining_game_minutes) !== null ? null : null;
    var price = num((g.market || {}).under_odds);
    if (price === null) price = num(g.under_price);
    var lo = num(win.min), hi = num(win.max);
    var band = (pct !== null && lo !== null && hi !== null)
      ? (pct < lo ? "below" : (pct > hi ? "above" : "in")) : "unknown";
    return {
      id: g.game_id,
      home: g.home_team || g.home || g.homeName || "?",
      away: g.away_team || g.away || g.awayName || "?",
      comp: g.competition_slug || "",
      pct: pct, ratio: (req !== null && avg) ? req / avg : null,
      shortfall: (req !== null && act !== null) ? req - act : null,
      price: price, band: band,
      armed: g.under_alert_exec_armed === true,
      cohort: (g.trade_cohort || {}).code || null,
      reason: win.reason || null,
      rung: num(win.min_pace_ratio), floor: num(win.min_price),
      excluded: !!(g.under_alert_competition && g.under_alert_competition.reason)
    };
  }

  function pill(v) {
    if (v.armed) return '<span class="sl-pill sl-armed">ARMED</span>';
    if (v.excluded) return '<span class="sl-pill sl-bad">EXCLUDED</span>';
    var why = String(v.reason || "");
    if (/price/i.test(why)) return '<span class="sl-pill sl-bad">PRICE</span>';
    if (/short/i.test(why)) return '<span class="sl-pill sl-bad">SHORTFALL</span>';
    if (/pace|window|progress/i.test(why)) return '<span class="sl-pill sl-bad">PACE</span>';
    return '<span class="sl-pill sl-wait">WAIT</span>';
  }

  function bar(v) {
    var rung = v.rung === null ? 1.04 : v.rung;
    var r = v.ratio === null ? 0 : v.ratio;
    var pct = Math.max(0, Math.min(1, r / (rung * 1.6))) * 100;
    var tick = (rung / (rung * 1.6)) * 100;
    var col = v.ratio === null ? "var(--line, #2a3542)"
      : (v.ratio > rung ? "#39d0a6" : (v.ratio > rung - 0.15 ? "#f2b544" : "#ef5f6b"));
    return '<div class="sl-bar"><i style="width:' + pct.toFixed(0) + '%;background:' + col +
           '"></i><span style="left:' + tick.toFixed(1) + '%"></span></div>';
  }

  function row(v) {
    return '<tr class="' + (v.armed ? "sl-hit" : "") + '">' +
      '<td class="sl-l sl-team" title="' + esc(v.home) + " v " + esc(v.away) +
        " · " + esc(v.id) + '">' + esc(v.home) + ' <span class="muted">v</span> ' +
        esc(v.away) + '</td>' +
      '<td>' + f(v.pct, 1) + '</td>' +
      '<td class="sl-l">' + bar(v) + '</td>' +
      '<td>' + (v.ratio === null ? "–" : f(v.ratio, 3)) + '</td>' +
      '<td class="' + (v.shortfall !== null && v.shortfall < 0 ? "sl-bad" : "sl-ok") + '">' +
        f(v.shortfall, 2) + '</td>' +
      '<td>' + f(v.price, 2) + '</td>' +
      '<td class="muted">' + esc(v.cohort || "–") + '</td>' +
      '<td class="sl-l">' + pill(v) + '</td></tr>';
  }

  function paint(payload) {
    var host = $("slateRows");
    if (!host) return;
    var games = (payload && payload.games) || [];
    var vs = games.map(view);

    var inband = vs.filter(function (v) { return v.band === "in"; });
    var armed = vs.filter(function (v) { return v.armed; });
    var approaching = vs.filter(function (v) { return v.band === "below"; })
      .sort(function (a, b) { return (b.ratio || 0) - (a.ratio || 0); }).slice(0, 8);

    var counts = $("slateCounts");
    if (counts) {
      counts.innerHTML = "<b>" + games.length + "</b> live · <b>" + inband.length +
        "</b> in band · <b class='" + (armed.length ? "sl-ok" : "") + "'>" + armed.length +
        "</b> armed";
    }
    var gate = $("slateGate");
    if (gate) {
      var any = inband[0] || vs[0] || {};
      gate.textContent = "pace > " + f(any.rung, 2) + " · band " +
        f((any.rung !== null ? (payload.games[0].under_alert_execution_window || {}).min : null), 0) +
        "–" + f(((payload.games[0] || {}).under_alert_execution_window || {}).max, 0) +
        " % · price ≥ " + f(any.floor, 2) + " · verdict = the executor's own";
    }

    var shown = armed.length ? armed.concat(inband.filter(function (v) { return !v.armed; }))
                             : inband;
    var html = shown.map(row).join("");
    if (approaching.length) {
      html += '<tr class="sl-sep"><td class="sl-l" colspan="8">approaching the band</td></tr>' +
              approaching.map(row).join("");
    }
    host.innerHTML = html ||
      '<tr><td class="sl-l muted" colspan="8" style="padding:18px 8px">' +
      "no game inside the band right now — the gate cannot fire</td></tr>";
  }

  function tick() {
    fetch("/api/v4/live", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function () {
        var host = $("slateRows");
        if (host) host.innerHTML =
          '<tr><td class="sl-l" colspan="8" style="padding:16px 8px">' +
          "live payload unavailable — the engine may be restarting</td></tr>";
      });
  }

  function show(on) {
    var el = $("viewSlate");
    if (!el) return;
    el.hidden = !on;
    var tab = $("tabSlate");
    if (tab) {
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", String(on));
    }
    if (on) { if (!timer) { tick(); timer = setInterval(tick, POLL_MS); } }
    else if (timer) { clearInterval(timer); timer = null; }
  }

  function wire() {
    var tab = $("tabSlate");
    if (!tab) return;
    tab.addEventListener("click", function (e) {
      e.preventDefault();
      ["viewLive", "viewStats", "viewExplorer"].forEach(function (id) {
        var el = $(id); if (el) el.hidden = true;
      });
      ["tabLive", "tabStats", "tabExplorer"].forEach(function (id) {
        var el = $(id);
        if (el) { el.classList.remove("active"); el.setAttribute("aria-selected", "false"); }
      });
      show(true);
    });
    // any OTHER tab click must hide the slate view again
    document.querySelectorAll(".tab").forEach(function (t) {
      if (t === tab) return;
      t.addEventListener("click", function () { show(false); });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else { wire(); }
})();
