/* ═══════════════════════════════════════════════════════════════════════
   BLM V2 — UNDER Timing Dashboard  (Main Script)
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const API = '/api/v2';
  const POLL_MS = 5000;
  const ANALYSIS_POLL_MS = 10000;
  const MAX_CHART_POINTS = 400;

  // ── State ───────────────────────────────────────────────────
  let gameId = null;
  let lastUnderData = null;
  let lastLineAnalysis = null;
  let mainChart = null;
  let subChart = null;
  let alertTimers = new Map();

  // ── DOM Refs ───────────────────────────────────────────────

  const $ = id => document.getElementById(id);

  const dom = {
    // Header
    gameTeams: $('gameTeams'),
    gameScore: $('gameScore'),
    gameClock: $('gameClock'),
    gameLeague: $('gameLeague'),
    liveDot: $('liveDot'),
    lastUpdate: $('lastUpdate'),
    snapCount: $('snapCount'),
    statusBadge: $('statusBadge'),
    // Scoreboard
    homeTeam: $('homeTeam'),
    awayTeam: $('awayTeam'),
    homeScore: $('homeScore'),
    awayScore: $('awayScore'),
    totalScore: $('totalScore'),
    margin: $('margin'),
    // Market
    totalLine: $('totalLine'),
    spread: $('spread'),
    olv: $('olv'),
    excursion: $('excursion'),
    // State
    paceVal: $('paceVal'),
    possessions: $('possessions'),
    scoreDelta: $('scoreDelta'),
    lineDelta: $('lineDelta'),
    // UNDER
    underGauge: $('underGauge'),
    underStatus: $('underStatus'),
    underScore: $('underScore'),
    confidenceBar: $('confidenceBar'),
    confidenceVal: $('confidenceVal'),
    // Components
    comp_historical_inflation: $('comp_historical_inflation'),
    comp_freeze: $('comp_freeze'),
    comp_burst: $('comp_burst'),
    comp_excursion: $('comp_excursion'),
    comp_divergence: $('comp_divergence'),
    comp_regression: $('comp_regression'),
    comp_under_rate: $('comp_under_rate'),
    // Signals
    signalsMet: $('signalsMet'),
    signalsMissed: $('signalsMissed'),
    // Alerts
    alertBadge: $('alertBadge'),
    alertFeed: $('alertFeed'),
    // Historical
    histGames: $('histGames'),
    histSnapshots: $('histSnapshots'),
    histRegress: $('histRegress'),
    histUnderRate: $('histUnderRate'),
    histOlvMean: $('histOlvMean'),
    histExcP95: $('histExcP95'),
    // Chart controls
    chartRangeLabel: $('chartRangeLabel'),
    resetZoomBtn: $('resetZoomBtn'),
  };

  // ── Utilities ──────────────────────────────────────────────

  function fmt(n, d) {
    const digits = d !== undefined ? d : 1;
    return (n === null || n === undefined) ? '--' : Number(n).toFixed(digits);
  }

  function pct(n) {
    if (n === null || n === undefined) return '--';
    return (n * 100).toFixed(1) + '%';
  }

  function timeStr(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '--' : d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function clockStr(quarter, clock) {
    if (!clock && !quarter) return '--';
    return `Q${quarter || 1} ${clock || '--'}`;
  }

  // ── Status ────────────────────────────────────────────────

  function setConnected(ok) {
    dom.statusBadge.textContent = ok ? '● Connected' : '✕ Disconnected';
    dom.statusBadge.className = 'status-badge ' + (ok ? 'connected' : 'disconnected');
  }

  // ── Gauge Drawing ──────────────────────────────────────────

  function drawGauge(score) {
    const canvas = dom.underGauge;
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h - 12;
    const r = 90;

    ctx.clearRect(0, 0, w, h);

    const startAngle = Math.PI * 0.75;
    const endAngle = Math.PI * 2.25;

    // Background arc
    ctx.beginPath();
    ctx.arc(cx, cy, r, startAngle, endAngle);
    ctx.strokeStyle = '#1a2235';
    ctx.lineWidth = 14;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Score arc
    const clamped = Math.min(100, Math.max(0, score));
    const pctArc = clamped / 100;
    const scoreAngle = startAngle + pctArc * (endAngle - startAngle);

    let color;
    if (clamped < 15) color = '#4a5570';
    else if (clamped < 35) color = '#f59e0b';
    else if (clamped < 60) color = '#f97316';
    else color = '#22c55e';

    ctx.beginPath();
    ctx.arc(cx, cy, r, startAngle, scoreAngle);
    ctx.strokeStyle = color;
    ctx.lineWidth = 14;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Glow on READY
    if (clamped >= 60) {
      ctx.beginPath();
      ctx.arc(cx, cy, r, startAngle, scoreAngle);
      ctx.strokeStyle = 'rgba(34, 197, 94, 0.2)';
      ctx.lineWidth = 20;
      ctx.lineCap = 'round';
      ctx.stroke();
    }

    // Center number
    ctx.fillStyle = '#e2e8f0';
    ctx.font = 'bold 28px -apple-system, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(Math.round(clamped).toString(), cx, cy - 4);

    ctx.fillStyle = '#4a5570';
    ctx.font = '11px -apple-system, sans-serif';
    ctx.fillText('/ 100', cx, cy + 20);

    // Tick marks at thresholds
    const thresholds = [0, 15, 35, 60, 100];
    const tcolors = ['#4a5570', '#f59e0b', '#f97316', '#22c55e'];
    for (let i = 0; i < thresholds.length; i++) {
      const t = thresholds[i];
      const a = startAngle + (t / 100) * (endAngle - startAngle);
      const tx = cx + (r + 18) * Math.cos(a);
      const ty = cy + (r + 18) * Math.sin(a);
      ctx.fillStyle = i < thresholds.length - 1 ? tcolors[Math.min(i, tcolors.length - 1)] : tcolors[tcolors.length - 1];
      ctx.font = '8px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(t, tx, ty);
    }
  }

  // ── Dashboard Updates ──────────────────────────────────────

  function updateUnderSignals(under) {
    if (!under) return;
    lastUnderData = under;

    const status = under.status || 'PASS';
    dom.underStatus.textContent = status;
    dom.underStatus.className = 'under-status ' + status;

    dom.underScore.textContent = fmt(under.under_timing_score, 1);
    drawGauge(under.under_timing_score || 0);

    const conf = under.confidence || 0;
    dom.confidenceBar.style.width = (conf * 100) + '%';
    dom.confidenceVal.textContent = pct(conf);
    if (conf >= 0.6) dom.confidenceBar.style.background = '#22c55e';
    else if (conf >= 0.3) dom.confidenceBar.style.background = '#f59e0b';
    else dom.confidenceBar.style.background = '#3b82f6';

    // Components
    const comps = under.components || {};
    const compKeys = ['historical_inflation', 'freeze', 'burst', 'excursion', 'divergence', 'regression', 'under_rate'];
    const compMax = { historical_inflation: 25, freeze: 20, burst: 15, excursion: 15, divergence: 10, regression: 10, under_rate: 5 };

    compKeys.forEach(key => {
      const val = comps[key] || 0;
      const max = compMax[key];
      const ele = dom['comp_' + key];
      if (ele) {
        ele.textContent = fmt(val, 1);
        const bar = ele.closest('.comp-item').querySelector('.comp-bar');
        if (bar) bar.style.width = Math.min(100, (val / max) * 100) + '%';
      }
    });

    // Signals met/missed
    const met = under.signals_met || [];
    dom.signalsMet.innerHTML = met.length
      ? met.map(s => `<li>${s}</li>`).join('')
      : '<li class="signal-empty">None</li>';

    const missed = under.signals_missed || [];
    dom.signalsMissed.innerHTML = missed.length
      ? missed.map(s => `<li>${s}</li>`).join('')
      : '<li class="signal-empty">None</li>';

    // Card border color flash
    const underCard = document.getElementById('underCard');
    if (underCard) {
      if (status === 'UNDER READY') underCard.style.borderColor = '#22c55e';
      else if (status === 'WATCH') underCard.style.borderColor = '#f97316';
      else if (status === 'WAIT') underCard.style.borderColor = '#f59e0b';
      else underCard.style.borderColor = '';
    }
  }

  function parseGameId(gid) {
    // "East Cyber-vs-West Cyber-2026-07-21" => "East Cyber" vs "West Cyber"
    if (!gid) return ['--', '--'];
    const trimmed = gid.replace(/\s*\d{4}-\d{2}-\d{2}(T.*)?$/, '').trim();
    const parts = trimmed.split(/-vs-/i);
    if (parts.length >= 2) {
      return [parts[0].trim() || 'HOME', parts.slice(1).join(' vs ').trim() || 'AWAY'];
    }
    return [gid, ''];
  }

  function updateScoreboardFromAnalysis(entries) {
    if (!entries || !entries.length) return;

    // Parse teams from game_id
    if (lastUnderData && lastUnderData.game_id) {
      const [h, a] = parseGameId(lastUnderData.game_id);
      dom.homeTeam.textContent = h;
      dom.awayTeam.textContent = a;
      dom.gameTeams.textContent = `${h} vs ${a}`;
      dom.gameLeague.textContent = lastUnderData.league || 'Cyber 2K26';
    }

    const latest = entries[entries.length - 1];
    if (!latest) return;

    // Total score
    const total = latest.current_total ?? latest.current_score ?? 0;
    dom.homeScore.textContent = '0';  // line analysis doesn't have per-team scores
    dom.awayScore.textContent = '0';
    dom.totalScore.textContent = total;

    // Game score header
    dom.gameScore.textContent = `${total} - ?`;

    // Market
    dom.totalLine.textContent = latest.current_line != null ? fmt(latest.current_line, 1) : '--';
    if (latest.olv != null) dom.olv.textContent = fmt(latest.olv, 1);

    // Excursion
    if (latest.excursion != null) {
      dom.excursion.textContent = fmt(latest.excursion, 1);
      dom.excursion.style.color = latest.excursion > 3 ? '#22c55e'
        : latest.excursion > 0 ? '#f59e0b' : '#ef4444';
    }

    // Deltas
    dom.scoreDelta.textContent = latest.score_delta ?? 0;
    dom.lineDelta.textContent = fmt(latest.line_delta, 1);

    // Clock from timestamp
    if (latest.timestamp) {
      dom.lastUpdate.textContent = timeStr(latest.timestamp);
    }

    dom.snapCount.textContent = (lastLineAnalysis ? lastLineAnalysis.total_entries : entries.length) + ' entries';
  }

  function updateAlerts(alerts) {
    if (!alerts || !alerts.alerts || !alerts.alerts.length) {
      dom.alertBadge.textContent = '0';
      dom.alertBadge.className = 'alert-badge zero';
      dom.alertFeed.innerHTML = '<div class="alert-empty">No alerts</div>';
      return;
    }

    const items = alerts.alerts.slice(-20);
    dom.alertBadge.textContent = items.length;
    dom.alertBadge.className = 'alert-badge';

    dom.alertFeed.innerHTML = items.map(a => {
      const lvl = (a.level || 'info').toLowerCase();
      const cls = lvl === 'warning' ? 'warning' : (lvl === 'info' ? 'info' : '');
      const msg = a.message || a.type || a.text || 'Alert';
      return `<div class="alert-item ${cls}">
        <span>${msg}</span>
        <span class="alert-time">${timeStr(a.timestamp || a.time)}</span>
      </div>`;
    }).join('');
  }

  function updateHistorical(profile) {
    if (!profile) return;
    dom.histGames.textContent = profile.total_games ?? 0;
    dom.histSnapshots.textContent = profile.total_snapshots ?? 0;

    if (profile.regression) {
      dom.histRegress.textContent = pct(profile.regression.total_regression_rate);
      dom.histUnderRate.textContent = pct(profile.regression.under_rate);
    }
    if (profile.olv_distribution) {
      dom.histOlvMean.textContent = fmt(profile.olv_distribution.mean, 1);
    }
    if (profile.excursion_distribution) {
      dom.histExcP95.textContent = fmt(profile.excursion_distribution.p95, 1);
    }
  }

  // ── Chart Management ───────────────────────────────────────

  function getOrCreateMainChart() {
    if (mainChart) return mainChart;
    const ctx = document.getElementById('mainChart').getContext('2d');

    Chart.register(chartjsZoom);
    Chart.register(chartjsAnnotation);

    mainChart = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [
          {
            label: 'Total Score', data: [],
            borderColor: '#00d4ff', backgroundColor: 'rgba(0,212,255,0.08)',
            showLine: true, tension: 0.15, pointRadius: 1, pointHoverRadius: 4,
            borderWidth: 2, fill: true, yAxisID: 'y'
          },
          {
            label: 'Bookmaker Line', data: [],
            borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.06)',
            showLine: true, tension: 0.15, pointRadius: 0.5, pointHoverRadius: 3,
            borderWidth: 1.5, borderDash: [4, 3], fill: false, yAxisID: 'y'
          },
          {
            label: 'OLV', data: [],
            borderColor: '#7c3aed', backgroundColor: 'rgba(124,58,237,0.5)',
            showLine: true, tension: 0, pointRadius: 0,
            borderWidth: 1.5, borderDash: [8, 4], fill: false, yAxisID: 'y'
          },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 200 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            labels: { color: '#7a84a0', font: { size: 10 }, usePointStyle: true, padding: 8, boxWidth: 12 }
          },
          tooltip: {
            backgroundColor: 'rgba(13,17,28,0.95)', titleColor: '#e2e8f0', bodyColor: '#7a84a0',
            borderColor: '#1e2745', borderWidth: 1, padding: 8,
            callbacks: { title: items => items.length ? 'Tick ' + items[0].label : '' }
          },
          zoom: {
            pan: { enabled: true, mode: 'x', modifierKey: 'shift' },
            zoom: {
              wheel: { enabled: true, speed: 0.05 },
              pinch: { enabled: true },
              drag: { enabled: true, backgroundColor: 'rgba(0,212,255,0.08)', borderColor: '#00d4ff', borderWidth: 1 },
              mode: 'x'
            }
          },
          annotation: {
            annotations: {
              under_ready: {
                type: 'line', yMin: 60, yMax: 60,
                borderColor: 'rgba(34,197,94,0.3)', borderWidth: 1, borderDash: [4, 3],
                label: {
                  display: true, content: 'UNDER READY', position: 'end',
                  backgroundColor: 'rgba(13,17,28,0.8)', color: '#22c55e', font: { size: 9 }
                }
              }
            }
          }
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Tick #', color: '#4a5570' },
            ticks: { color: '#4a5570', maxTicksLimit: 20 },
            grid: { color: 'rgba(30,39,69,0.4)' }
          },
          y: {
            type: 'linear', position: 'left',
            title: { display: true, text: 'Points', color: '#4a5570' },
            ticks: { color: '#4a5570' },
            grid: { color: 'rgba(30,39,69,0.2)' }
          },
        }
      }
    });

    return mainChart;
  }

  function getOrCreateSubChart() {
    if (subChart) return subChart;
    const ctx = document.getElementById('subChart').getContext('2d');

    subChart = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [
          {
            label: 'Excursion from OLV', data: [],
            borderColor: '#f97316', backgroundColor: 'rgba(249,115,22,0.08)',
            showLine: true, tension: 0.15, pointRadius: 1, pointHoverRadius: 4,
            borderWidth: 1.5, fill: true, yAxisID: 'y'
          },
          {
            label: 'Freeze Ticks', data: [],
            borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.08)',
            showLine: true, tension: 0, pointRadius: 2, pointHoverRadius: 5,
            borderWidth: 1.5, fill: false, yAxisID: 'y1'
          },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 200 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            labels: { color: '#7a84a0', font: { size: 10 }, usePointStyle: true, padding: 8, boxWidth: 12 }
          },
          tooltip: {
            backgroundColor: 'rgba(13,17,28,0.95)', titleColor: '#e2e8f0', bodyColor: '#7a84a0',
            borderColor: '#1e2745', borderWidth: 1, padding: 8,
            callbacks: { title: items => items.length ? 'Tick ' + items[0].label : '' }
          },
          zoom: {
            pan: { enabled: true, mode: 'x', modifierKey: 'shift' },
            zoom: { wheel: { enabled: true, speed: 0.05 }, mode: 'x' }
          },
          annotation: {
            annotations: {
              zero_line: {
                type: 'line', yMin: 0, yMax: 0,
                borderColor: 'rgba(74,85,112,0.3)', borderWidth: 1, borderDash: [2, 2]
              }
            }
          }
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Tick #', color: '#4a5570' },
            ticks: { color: '#4a5570', maxTicksLimit: 15 },
            grid: { color: 'rgba(30,39,69,0.4)' }
          },
          y: {
            type: 'linear', position: 'left',
            title: { display: true, text: 'Excursion', color: '#4a5570' },
            ticks: { color: '#4a5570' },
            grid: { color: 'rgba(30,39,69,0.2)' }
          },
          y1: {
            type: 'linear', position: 'right', min: 0,
            title: { display: true, text: 'Freeze Ticks', color: '#4a5570' },
            ticks: { color: '#4a5570', stepSize: 1 },
            grid: { drawOnChartArea: false }
          }
        }
      }
    });

    return subChart;
  }

  function updateCharts(entries) {
    if (!entries || !entries.length) return;

    const chart = getOrCreateMainChart();
    const sub = getOrCreateSubChart();

    chart.data.datasets.forEach(ds => ds.data = []);
    sub.data.datasets.forEach(ds => ds.data = []);

    const points = entries.slice(-MAX_CHART_POINTS);

    points.forEach((e, i) => {
      chart.data.datasets[0].data.push({
        x: i,
        y: e.current_total ?? e.current_score ?? 0
      });
      if (e.current_line != null) {
        chart.data.datasets[1].data.push({ x: i, y: e.current_line });
      }
      if (e.olv != null) {
        chart.data.datasets[2].data.push({ x: i, y: e.olv });
      }
    });

    points.forEach((e, i) => {
      if (e.excursion != null) {
        sub.data.datasets[0].data.push({ x: i, y: e.excursion });
      }
      sub.data.datasets[1].data.push({ x: i, y: e.freeze_ticks || 0 });
    });

    chart.update('none');
    sub.update('none');

    dom.chartRangeLabel.textContent = `Last ${points.length} ticks`;
  }

  function resetZoom() {
    if (mainChart) mainChart.resetZoom();
    if (subChart) subChart.resetZoom();
  }

  // ── Polling ─────────────────────────────────────────────────

  let pollTimer = null;
  let analysisTimer = null;

  async function fetchJson(url) {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        if (res.status === 404) return null;
        throw new Error(`HTTP ${res.status}`);
      }
      return await res.json();
    } catch (err) {
      console.warn('Fetch error:', url, err.message);
      return null;
    }
  }

  async function pollMain() {
    const under = await fetchJson(`${API}/under-signals`);
    if (under && under.game_id) {
      gameId = under.game_id;
      updateUnderSignals(under);
    }

    const alerts = await fetchJson(`${API}/alerts`);
    if (alerts) updateAlerts(alerts);

    const profile = await fetchJson(`${API}/learned-ranges`);
    if (profile) updateHistorical(profile);

    dom.lastUpdate.textContent = timeStr(new Date().toISOString());
    setConnected(true);
  }

  async function pollAnalysis() {
    if (!gameId) return;

    const analysis = await fetchJson(`${API}/line-analysis/${encodeURIComponent(gameId)}`);
    if (analysis && analysis.entries && analysis.entries.length) {
      lastLineAnalysis = analysis;
      updateScoreboardFromAnalysis(analysis.entries);
      updateCharts(analysis.entries);
    }
  }

  async function pollAll() {
    await pollMain();
    await pollAnalysis();
  }

  // ── Init ───────────────────────────────────────────────────

  function init() {
    dom.resetZoomBtn.addEventListener('click', resetZoom);

    // Initial fetch
    pollAll();

    // Start polling
    pollTimer = setInterval(pollMain, POLL_MS);
    analysisTimer = setInterval(pollAnalysis, ANALYSIS_POLL_MS);

    window.addEventListener('online', () => { pollAll(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
