/* ═══════════ TRADE COHORTS page + Auto-Bet cohort selector ═════════════════
   Contract (docs/AGENT_WORKFLOW_TC_COHORT_STATS_AND_UI.md §6.2/§6.5):

     * every cohort number is computed SERVER-SIDE by the frozen evaluator and
       read from /api/v4/cohorts/{spec,summary,comparisons,ledger,quality};
       there is no threshold and no cohort arithmetic in this file;
     * every KPI shows its denominator, and a bucket smaller than SMALL_N is
       labelled rather than presented as evidence;
     * firing-time evidence (trigger line/price/inputs) is rendered verbatim,
       and UNKNOWN / checkpoint rows are visually distinct from a fired alert;
     * the Auto-Bet selector persists ONE validated cohort code and renders the
       backend's fail-closed lock state.  It never posts an enable, a stake, a
       limit, a kill-switch or a submission — and it never calls the executor.

   Loaded AFTER dashboard.js so it can reuse nothing it does not own: this
   module is self-contained on purpose (the cohort page must keep working even
   if the live board is polling slowly). */
(function () {
  "use strict";

  const API_COHORTS = "/api/v4/cohorts";
  const API_COHORT_SELECTION = "/api/v4/betting/cohort-selection";
  const PAGE_SIZE = 50;
  const SMALL_N = 10;                 // below this a bucket is labelled "small n"

  const $ = (id) => document.getElementById(id);

  const num = (v, d = 2) =>
    (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : Number(v).toFixed(d);
  const pct = (v, d = 1) =>
    (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : (100 * Number(v)).toFixed(d) + "%";
  const esc = (s) => String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`${url} → ${res.status}${body ? " · " + body.slice(0, 160) : ""}`);
    }
    return res.json();
  }

  // ── state ───────────────────────────────────────────────────────────────
  const S = {
    spec: null,
    summary: null,
    cohort: null,
    source: "reconstructed_firing",
    loaded: false,
    inflight: false,
    ledger: { offset: 0, total: 0, rows: [], cohort: "", source: "reconstructed_firing",
              status: "", competition: "" },
    compareBy: "competition",
    compareSource: "reconstructed_firing",
    selection: null,
    selecting: false,
  };

  // ── Trade Cohorts page ──────────────────────────────────────────────────

  function ruleLine(rule) {
    return `pace &gt; ${rule.pace_ratio_gt} · shortfall ≥ ${rule.shortfall_gte} · price ≥ ${rule.min_under_price_gte}`;
  }

  function renderCards() {
    const wrap = $("cohortCards");
    if (!wrap || !S.summary) return;
    wrap.innerHTML = "";
    for (const c of S.summary.cohorts) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tc-card" + (c.code === S.cohort ? " active" : "");
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(c.code === S.cohort));
      b.dataset.code = c.code;
      const rate = c.under_rate === null || c.under_rate === undefined
        ? "–" : pct(c.under_rate);
      b.innerHTML = `
        <span class="tc-card-head"><strong>${esc(c.code)}</strong> <em>${esc(c.name)}</em></span>
        <span class="tc-card-v">${rate}</span>
        <span class="tc-card-sub">${c.unique_games} games · ${c.firings} firings · n=${c.settled} settled</span>
        <span class="tc-card-rule">${ruleLine(c.rule)}</span>`;
      b.addEventListener("click", () => {
        S.cohort = c.code;
        renderCards();
        renderScorecard();
        S.ledger.offset = 0;
        S.ledger.cohort = c.code;
        const sel = $("tcLedgerCohort");
        if (sel) sel.value = c.code;
        loadLedger();
      });
      wrap.appendChild(b);
    }
  }

  function renderScorecard() {
    const wrap = $("cohortScorecard");
    if (!wrap || !S.summary) return;
    const c = S.summary.cohorts.find((x) => x.code === S.cohort);
    if (!c) { wrap.innerHTML = '<div class="empty">no cohort selected</div>'; return; }
    const m = S.summary.read_model || {};
    const wilson = c.wilson95 ? `${pct(c.wilson95[0])}–${pct(c.wilson95[1])}` : "–";
    const settledPct = c.unique_games ? c.settled / c.unique_games : null;
    const firingCov = c.firing_coverage || {};
    const obsCov = c.observation_coverage || {};
    const kpi = (k, v, sub, cls) => `
      <div class="tc-kpi ${cls || ""}">
        <div class="tc-kpi-k">${k}</div>
        <div class="tc-kpi-v">${v}</div>
        <div class="tc-kpi-n">${sub || ""}</div>
      </div>`;
    wrap.innerHTML = `
      <div class="tc-kpi-head">
        <strong>${esc(c.code)} ${esc(c.name)}</strong>
        <span class="muted">${esc(c.blurb || "")}</span>
      </div>
      <div class="tc-kpi-grid">
        ${kpi("UNIQUE GAMES", c.unique_games, "first firing per game")}
        ${kpi("ACTUAL FIRINGS", c.firings, "ledger rows of this cohort")}
        ${kpi("SETTLED n", c.settled, `UNDER+OVER · ${pct(settledPct)} of games resolved`)}
        ${kpi("UNDER / OVER", `${c.under} / ${c.over}`, "settled outcomes", "under-over")}
        ${kpi("PUSH", c.push, "excluded from the rate")}
        ${kpi("PENDING", c.pending, "never counted as a loss")}
        ${kpi("UNDER RATE", pct(c.under_rate), `denominator = ${c.settled} settled (UNDER+OVER)`)}
        ${kpi("WILSON 95%", wilson, "binomial interval on the same denominator")}
        ${kpi("FIRING COVERAGE", firingCov.pct === null || firingCov.pct === undefined ? "–" : firingCov.pct + "%",
              `${firingCov.rows_with_market || 0}/${firingCov.rows_total || 0} firings with price+line — SAME population as the rate`)}
        ${kpi("OBS COVERAGE", obsCov.pct === null || obsCov.pct === undefined ? "–" : obsCov.pct + "%",
              `${obsCov.rows_with_market || 0}/${obsCov.rows_total || 0} ledger rows — data quality, NOT the rate`)}
        ${kpi("AVG TRIGGER PRICE", num(c.avg_trigger_price), "settled firings only")}
        ${kpi("BREAK-EVEN", pct(c.break_even_rate_at_avg_price), "1/price at the avg trigger")}
      </div>
      <p class="tc-caveat muted">
        Rule ${esc(S.summary.rule_version)} · window ${esc((m.window_start || "–").slice(0, 16))} → ${esc((m.window_end || "–").slice(0, 16))} UTC ·
        league reference <strong>${esc(m.league_reference || "–")}</strong> ·
        ${esc(S.source === "checkpoint" ? "hypothetical checkpoints (not alerts)" : "actual firings")} ·
        reconstructed from observations, descriptive only.
      </p>`;
  }

  function renderComparison() {
    const table = $("cohortCompare");
    if (!table || !S.summary) return;
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = S.summary.cohorts.map((c) => {
      const wilson = c.wilson95 ? `${pct(c.wilson95[0])}–${pct(c.wilson95[1])}` : "–";
      const small = c.settled > 0 && c.settled < SMALL_N ? ' <span class="small-n">small n</span>' : "";
      return `<tr class="${c.code === S.cohort ? "active" : ""}">
        <td><strong>${esc(c.code)}</strong> ${esc(c.name)}${small}</td>
        <td class="num">${c.unique_games}</td>
        <td class="num">${c.firings}</td>
        <td class="num">${c.settled}</td>
        <td class="num under">${c.under}</td>
        <td class="num over">${c.over}</td>
        <td class="num">${c.push}</td>
        <td class="num">${c.pending}</td>
        <td class="num">${pct(c.under_rate)}</td>
        <td class="mono">${wilson}</td>
        <td class="num">${c.firing_coverage && c.firing_coverage.pct != null ? c.firing_coverage.pct + "%" : "–"}</td>
        <td class="num">${pct(c.break_even_rate_at_avg_price)}</td>
      </tr>`;
    }).join("");
    const note = $("cohortCompareNote");
    if (note) {
      note.textContent = `denominator = settled (UNDER+OVER) · cohorts overlap, so rows are not independent samples · ` +
        `published rule ${S.summary.rule_version}`;
    }
  }

  async function loadBreakdown() {
    const table = $("cohortBreakdown");
    if (!table) return;
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = '<tr><td colspan="9" class="empty">loading…</td></tr>';
    try {
      const data = await getJSON(`${API_COHORTS}/comparisons?by=${encodeURIComponent(S.compareBy)}` +
        `&source_type=${encodeURIComponent(S.compareSource)}`);
      if (!data.buckets.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">no rows for this grouping</td></tr>';
        return;
      }
      tbody.innerHTML = data.buckets.map((b) => {
        const wilson = b.wilson95 ? `${pct(b.wilson95[0])}–${pct(b.wilson95[1])}` : "–";
        const small = b.rows > 0 && b.rows < SMALL_N ? ' <span class="small-n">small n</span>' : "";
        return `<tr>
          <td>${esc(b.bucket)}${small}</td>
          <td class="num">${b.rows}</td>
          <td class="num">${b.settled}</td>
          <td class="num under">${b.under}</td>
          <td class="num over">${b.over}</td>
          <td class="num">${b.push}</td>
          <td class="num">${pct(b.under_rate)}</td>
          <td class="mono">${wilson}</td>
          <td class="num">${num(b.avg_price)}</td>
        </tr>`;
      }).join("");
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty">breakdowns unavailable: ${esc(err.message)}</td></tr>`;
    }
  }

  async function loadLedger() {
    const table = $("cohortLedger");
    if (!table) return;
    const tbody = table.querySelector("tbody");
    const L = S.ledger;
    const params = new URLSearchParams();
    if (L.cohort) params.set("cohort", L.cohort);
    if (L.source) params.set("source_type", L.source);
    if (L.status) params.set("status", L.status);
    if (L.competition) params.set("competition", L.competition);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(L.offset));
    tbody.innerHTML = '<tr><td colspan="17" class="empty">loading…</td></tr>';
    try {
      const data = await getJSON(`${API_COHORTS}/ledger?${params.toString()}`);
      L.total = data.total;
      L.rows = data.rows;
      const count = $("cohortLedgerCount");
      if (count) {
        count.textContent = `${data.total} rows · showing ${data.offset + 1}–` +
          `${Math.min(data.offset + data.rows.length, data.total)} · order ${data.ordering}`;
      }
      const page = $("tcLedgerPage");
      if (page) page.textContent = data.total ? `page ${Math.floor(data.offset / PAGE_SIZE) + 1} of ${Math.max(1, Math.ceil(data.total / PAGE_SIZE))}` : "–";
      const prev = $("tcLedgerPrev"); const next = $("tcLedgerNext");
      if (prev) prev.disabled = data.offset <= 0;
      if (next) next.disabled = data.offset + data.rows.length >= data.total;
      if (!data.rows.length) {
        tbody.innerHTML = '<tr><td colspan="17" class="empty">no ledger rows match these filters</td></tr>';
        return;
      }
      tbody.innerHTML = data.rows.map((r) => {
        const outcome = (r.outcome || "UNKNOWN").toUpperCase();
        const cls = outcome === "UNDER" ? "under" : outcome === "OVER" ? "over"
          : outcome === "PUSH" ? "push" : "unk";
        const srcCls = r.source_type === "reconstructed_firing" ? "src-firing" : "src-checkpoint";
        const statusCls = String(r.status || "").toLowerCase();
        const reasons = (r.reason_codes || []).join(", ");
        return `<tr class="tc-row ${srcCls} st-${esc(statusCls)}" tabindex="0"
                    title="${esc(reasons ? "reason codes: " + reasons : "")}">
          <td class="mono">${esc((r.observed_at || "").slice(0, 19).replace("T", " "))}</td>
          <td>${esc(r.competition_slug)}</td>
          <td class="mono">${esc(r.source_game_id)}</td>
          <td><strong>${esc(r.tightest_cohort || "–")}</strong></td>
          <td class="mono">${esc((r.qualifying_cohorts || []).join(", ") || "–")}</td>
          <td class="num">${num(r.progress_pct, 1)}</td>
          <td class="num">${r.scored_points ?? "–"}</td>
          <td class="num">${num(r.required_ppm)}</td>
          <td class="num">${num(r.actual_ppm)}</td>
          <td class="num">${num(r.league_ppm)}</td>
          <td class="num">${num(r.pace_ratio, 3)}</td>
          <td class="num">${num(r.shortfall)}</td>
          <td class="num">${num(r.under_price)}</td>
          <td class="num">${num(r.trigger_line, 1)}</td>
          <td class="${cls}">${esc(outcome)}</td>
          <td class="mono">${esc(r.status)}</td>
          <td class="${srcCls}">${r.source_type === "reconstructed_firing" ? "FIRING" : "checkpoint"}</td>
        </tr>`;
      }).join("");
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="17" class="empty">ledger unavailable: ${esc(err.message)}</td></tr>`;
    }
  }

  function renderQuality(q) {
    const wrap = $("tcQuality");
    if (!wrap) return;
    const reasons = Object.entries(q.unknown_reasons || {})
      .map(([k, v]) => `<li><span class="mono">${esc(k)}</span><span class="num">${v}</span></li>`)
      .join("") || '<li class="muted">none</li>';
    const comps = Object.entries(q.competitions || {})
      .map(([k, v]) => `<li><span class="mono">${esc(k)}</span><span class="num">${v}</span></li>`)
      .join("") || '<li class="muted">none</li>';
    const run = q.run || {};
    wrap.innerHTML = `
      <div class="tc-q-grid">
        <div class="tc-q-card">
          <h4>UNKNOWN reasons</h4>
          <p class="muted">${q.unknown_rows} rows could not be evaluated; each carries a reason code and is never a pass.</p>
          <ul class="tc-q-list">${reasons}</ul>
        </div>
        <div class="tc-q-card">
          <h4>Sources</h4>
          <ul class="tc-q-list">
            ${Object.entries(q.sources || {}).map(([k, v]) =>
              `<li><span class="mono">${esc(k)}</span><span class="num">${v}</span></li>`).join("")}
            ${Object.entries(q.league_sources || {}).map(([k, v]) =>
              `<li><span class="mono">league: ${esc(k)}</span><span class="num">${v}</span></li>`).join("")}
          </ul>
          <p class="muted">rows with price+line: <strong>${q.market_rows_with_price_and_line}</strong> of ${q.ledger_rows}</p>
        </div>
        <div class="tc-q-card">
          <h4>Competitions in the ledger</h4>
          <ul class="tc-q-list">${comps}</ul>
        </div>
        <div class="tc-q-card">
          <h4>Rescore run</h4>
          <p class="mono">${esc(run.run_at || "–")}</p>
          <p class="muted">window ${esc((run.window_start || "–").slice(0, 16))} → ${esc((run.window_end || "–").slice(0, 16))} UTC</p>
          <ul class="tc-q-list">
            ${Object.entries(q.caveats || []).map((c) => `<li class="muted">${esc(c)}</li>`).join("")}
          </ul>
        </div>
      </div>`;
  }

  function fillFilters() {
    const cohortSel = $("tcLedgerCohort");
    if (cohortSel && S.spec) {
      const current = cohortSel.value;
      cohortSel.innerHTML = '<option value="">ALL</option>' + S.spec.cohorts
        .map((c) => `<option value="${esc(c.code)}">${esc(c.code)} · ${esc(c.name)}</option>`).join("");
      cohortSel.value = current || "";
    }
    const compSel = $("tcLedgerCompetition");
    const quality = S.quality;
    if (compSel && quality) {
      const current = compSel.value;
      compSel.innerHTML = '<option value="">ALL</option>' + Object.keys(quality.competitions || {})
        .sort().map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
      compSel.value = current || "";
    }
  }

  async function load({ force = false } = {}) {
    if (S.inflight && !force) return;
    S.inflight = true;
    const err = $("cohortCaveat");
    try {
      const [spec, summary, quality] = await Promise.all([
        getJSON(`${API_COHORTS}/spec`),
        getJSON(`${API_COHORTS}/summary`),
        getJSON(`${API_COHORTS}/quality`),
      ]);
      S.spec = spec;
      S.summary = summary;
      S.quality = quality;
      if (!S.cohort) S.cohort = summary.cohorts[0] && summary.cohorts[0].code || null;
      const m = summary.read_model || {};
      $("cohortRuleVersion").textContent = "rule " + summary.rule_version;
      $("cohortWindow").textContent = "window " + (m.window_start || "–").slice(0, 10) +
        " → " + (m.window_end || "–").slice(0, 10);
      $("cohortLeague").textContent = "league ref " + (m.league_reference || "–");
      renderCards();
      renderScorecard();
      renderComparison();
      renderQuality(quality);
      fillFilters();
      await Promise.all([loadBreakdown(), loadLedger()]);
      S.loaded = true;
    } catch (e) {
      if (err) {
        err.textContent = "cohort read-model unavailable: " + e.message;
        err.classList.add("err");
      }
      const wrap = $("cohortScorecard");
      if (wrap) wrap.innerHTML = `<div class="empty">cohort read-model unavailable: ${esc(e.message)}<br>
        build it with <code>python -m blm_v4.cohorts.rescore</code> against the test data root.</div>`;
    } finally {
      S.inflight = false;
    }
  }

  // ── Auto-Bet cohort selector ────────────────────────────────────────────

  function renderSelection() {
    const s = S.selection;
    const wrap = $("abCohortOptions");
    if (!wrap || !s) return;
    const selected = s.selected || {};
    wrap.innerHTML = "";
    for (const opt of s.options || []) {
      const on = opt.code === selected.cohort;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ab-cohort-opt" + (on ? " active" : "");
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(on));
      b.dataset.code = opt.code;
      b.disabled = S.selecting;
      b.innerHTML = `<strong>${esc(opt.code)}</strong><span>${esc(opt.name)}</span>
        <em>pace &gt; ${opt.rule.pace_ratio_gt} · short ≥ ${opt.rule.shortfall_gte} · price ≥ ${opt.rule.min_under_price_gte}</em>`;
      b.addEventListener("click", () => select(opt.code));
      wrap.appendChild(b);
    }
    const sel = $("abCohortSelected");
    if (sel) {
      sel.textContent = `SELECTED — ${selected.cohort || "–"} ${selected.name || ""}`;
      sel.title = selected.source === "persisted"
        ? "persisted in the TEST configuration"
        : "showing the default; nothing has been persisted yet";
    }
    const lock = $("abCohortLock");
    const ex = s.execution || {};
    if (lock) {
      lock.textContent = ex.locked ? "EXECUTION LOCKED" : "EXECUTION UNLOCKED";
      lock.classList.toggle("warn", !!ex.locked);
      lock.title = ex.locked
        ? "locked by: " + (ex.reasons || []).join(", ")
        : "every prerequisite is present (the server still gates each bet)";
    }
    const note = $("abCohortNote");
    if (note) {
      const missing = (ex.missing_limits || []).length
        ? ` · missing limits: ${ex.missing_limits.join(", ")}` : "";
      note.textContent = `configuration only · persisted in the TEST environment · ` +
        `rule ${s.rule_version}${missing}` +
        (ex.reasons && ex.reasons.length ? ` · locked: ${ex.reasons.join(", ")}` : "");
    }
  }

  async function loadSelection() {
    try {
      S.selection = await getJSON(API_COHORT_SELECTION);
      renderSelection();
    } catch (e) {
      const note = $("abCohortNote");
      if (note) note.textContent = "cohort selection unavailable: " + e.message;
    }
  }

  async function select(code) {
    if (S.selecting) return;                       // one action at a time
    S.selecting = true;
    renderSelection();
    try {
      const res = await fetch(API_COHORT_SELECTION, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ cohort: code }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.detail || `HTTP ${res.status}`);
      S.selection = payload;
    } catch (e) {
      const note = $("abCohortNote");
      if (note) {
        note.textContent = `selection rejected: ${e.message} — the previous selection stands`;
        note.classList.add("err");
      }
    } finally {
      S.selecting = false;
      renderSelection();
      // the lock state is read back from the server, never inferred here
      loadSelection();
    }
  }

  // ── wiring ──────────────────────────────────────────────────────────────

  function wire() {
    const bySel = $("tcCompareBy");
    if (bySel) bySel.addEventListener("change", (ev) => {
      S.compareBy = ev.target.value; loadBreakdown();
    });
    const srcSel = $("tcCompareSource");
    if (srcSel) srcSel.addEventListener("change", (ev) => {
      S.compareSource = ev.target.value; loadBreakdown();
    });

    const L = S.ledger;
    const bind = (id, key, reloadAll) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", (ev) => {
        L[key] = ev.target.value;
        L.offset = 0;
        if (reloadAll) load({ force: true }); else loadLedger();
      });
    };
    bind("tcLedgerCohort", "cohort", false);
    bind("tcLedgerSource", "source", false);
    bind("tcLedgerStatus", "status", false);
    bind("tcLedgerCompetition", "competition", false);

    const prev = $("tcLedgerPrev");
    if (prev) prev.addEventListener("click", () => {
      L.offset = Math.max(0, L.offset - PAGE_SIZE); loadLedger();
    });
    const next = $("tcLedgerNext");
    if (next) next.addEventListener("click", () => { L.offset += PAGE_SIZE; loadLedger(); });

    const reload = $("abCohortReload");
    if (reload) reload.addEventListener("click", loadSelection);

    // The page loads its data when the destination is opened (not on every
    // page load): the read model is static between rescores, and a hidden
    // page must not poll the API.
    document.addEventListener("blm:view", (ev) => {
      if (ev.detail && ev.detail.view === "cohorts") load();
      if (ev.detail && ev.detail.view === "settings") loadSelection();
    });
    if (window.blmNav && window.blmNav.current === "cohorts") load();
    if (window.blmNav && window.blmNav.current === "settings") loadSelection();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }

  // Exposed for the UI contract tests and manual console use.
  window.__tcPage = { load, loadLedger, loadSelection, select, state: S };
})();
