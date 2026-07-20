/* ═══════════════════════════════════════════════════════════════════════
   BLM V2 Dashboard — Main Application Script
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ── Constants ──────────────────────────────────────────────────────

  const WS_URL = 'ws://localhost:8000/ws';
  const RECONNECT_BASE_DELAY = 1000;  // 1s
  const RECONNECT_MAX_DELAY = 30000;  // 30s
  const ALERT_DISMISS_AFTER_MS = 30000;
  const CHART_COLORS = {
    blmScore: '#00d4ff',
    trapMeter: '#ef4444',
    winProb: '#22c55e',
    pace: '#f59e0b',
    projectedTotal: '#ff6b6b',
    momentum: '#7c3aed',
    vegasLine: '#ff6b6b',
    confidence: '#3b82f6',
    marketMovement: '#06b6d4',
  };
  const CHART_LABELS = {
    blmScore: 'BLM Score',
    trapMeter: 'Trap Meter',
    winProb: 'Win Probability',
    pace: 'Pace',
    projectedTotal: 'Projected Total',
    momentum: 'Momentum',
    vegasLine: 'Vegas Line',
    confidence: 'Confidence',
    marketMovement: 'Market Movement',
  };

  // ── State ──────────────────────────────────────────────────────────

  let ws = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;
  let currentGameId = null;
  let lastSnapshotData = null;
  let chartHistory = [];        // {time, blmScore, trapMeter, winProb, pace, projectedTotal, momentum, vegasLine, confidence, marketMovement}
  let alertTimers = new Map();  // alertId -> setTimeout handle
  let mainChart = null;

  // ── DOM References ─────────────────────────────────────────────────

  const $ = (id) => document.getElementById(id);

  const dom = {
    // Header
    headerTeams: $('headerTeams'),
    headerScore: $('headerScore'),
    headerClock: $('headerClock'),
    liveDot: $('liveDot'),
    liveLabel: $('liveLabel'),
    lastUpdate: $('lastUpdate'),
    statusIndicator: $('statusIndicator'),
    statusLabel: $('statusLabel'),

    // Sidebar panels
    scoreValue: $('scoreValue'),
    scoreSub: $('scoreSub'),
    blmScoreValue: $('blmScoreValue'),
    blmScoreSub: $('blmScoreSub'),
    trapMeterValue: $('trapMeterValue'),
    trapMeterGauge: $('trapMeterGauge'),
    trapMeterFill: $('trapMeterFill'),
    confidenceGauge: $('confidenceGauge'),
    confidenceArc: $('confidenceArc'),
    confidenceValue: $('confidenceValue'),
    winProbValue: $('winProbValue'),
    winProbSub: $('winProbSub'),
    momentumArrow: $('momentumArrow'),
    momentumScore: $('momentumScore'),
    momentumVelocity: $('momentumVelocity'),
    momentumAccel: $('momentumAccel'),
    paceValue: $('paceValue'),
    paceSub: $('paceSub'),
    expectedTotal: $('expectedTotal'),
    expectedTotalSub: $('expectedTotalSub'),
    expectedMargin: $('expectedMargin'),
    expectedMarginSub: $('expectedMarginSub'),

    // Alerts
    alertCount: $('alertCount'),
    alertsFeed: $('alertsFeed'),

    // Game details
    detailGameId: $('detailGameId'),
    detailLeague: $('detailLeague'),
    detailSeason: $('detailSeason'),
    detailHomeTeam: $('detailHomeTeam'),
    detailAwayTeam: $('detailAwayTeam'),
    detailPossession: $('detailPossession'),
    detailQuarter: $('detailQuarter'),
    detailClock: $('detailClock'),
    detailSpread: $('detailSpread'),
    detailTotalLine: $('detailTotalLine'),
    detailLiveSpread: $('detailLiveSpread'),
    detailLiveTotal: $('detailLiveTotal'),
    detailMoneyline: $('detailMoneyline'),
    detailSteamMovement: $('detailSteamMovement'),
    detailReverseLine: $('detailReverseLine'),
    detailSnapshotVersion: $('detailSnapshotVersion'),
  };

  // ── Utility Functions ──────────────────────────────────────────────

  function safeGet(obj, path, fallback = null) {
    return path.split('.').reduce((acc, key) =>
      (acc && typeof acc === 'object' && key in acc) ? acc[key] : fallback, obj);
  }

  function toPct(val) {
    if (val === null || val === undefined) return '--';
    return (val * 100).toFixed(1) + '%';
  }

  function toFixed(val, digits = 1) {
    if (val === null || val === undefined) return '--';
    return Number(val).toFixed(digits);
  }

  function formatTime(isoString) {
    if (!isoString) return '--';
    const d = new Date(isoString);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function now() {
    return new Date().toISOString();
  }

  // ── Status Indicator ───────────────────────────────────────────────

  function setStatus(state) {
    const indicator = dom.statusIndicator;
    indicator.classList.remove('status-connected', 'status-disconnected', 'status-reconnecting');
    if (state === 'connected') {
      indicator.classList.add('status-connected');
      dom.statusLabel.textContent = 'Connected';
    } else if (state === 'disconnected') {
      indicator.classList.add('status-disconnected');
      dom.statusLabel.textContent = 'Disconnected';
    } else if (state === 'reconnecting') {
      indicator.classList.add('status-reconnecting');
      dom.statusLabel.textContent = `Reconnecting (${reconnectAttempts})`;
    }
  }

  function updateLastUpdate() {
    dom.lastUpdate.textContent = 'Last update: ' + formatTime(now());
  }

  // ── Dashboard Panel Updates ────────────────────────────────────────

  function animateValue(el, newValue) {
    if (!el) return;
    if (el.textContent !== String(newValue)) {
      el.textContent = newValue;
      el.classList.remove('updating');
      // Force reflow then add
      void el.offsetWidth;
      el.classList.add('updating');
    }
  }

  function updateAllPanels(data) {
    if (!data) return;

    lastSnapshotData = data;

    // ── Header ──────────────────────────────────────────────────
    const homeTeam = safeGet(data, 'game_state.home_team', '--');
    const awayTeam = safeGet(data, 'game_state.away_team', '--');
    const homeScore = safeGet(data, 'game_state.home_score', 0);
    const awayScore = safeGet(data, 'game_state.away_score', 0);
    const quarter = safeGet(data, 'metadata.quarter', 1);
    const clock = safeGet(data, 'metadata.clock', '--');

    dom.headerTeams.textContent = `${homeTeam} vs ${awayTeam}`;
    dom.headerScore.textContent = `${homeScore} - ${awayScore}`;
    dom.headerClock.textContent = `Q${quarter} ${clock}`;

    // Live indicator
    const status = safeGet(data, 'metadata.status', 'live');
    if (status === 'ended') {
      dom.liveDot.classList.add('ended');
      dom.liveLabel.textContent = 'ENDED';
    } else if (status === 'halftime') {
      dom.liveDot.style.animation = 'none';
      dom.liveDot.style.background = '#f59e0b';
      dom.liveLabel.textContent = 'HALFTIME';
    } else {
      dom.liveDot.classList.remove('ended');
      dom.liveDot.style.animation = '';
      dom.liveDot.style.background = '';
      dom.liveLabel.textContent = 'LIVE';
    }

    // ── Score Panel ─────────────────────────────────────────────
    animateValue(dom.scoreValue, `${homeScore} - ${awayScore}`);
    const margin = safeGet(data, 'game_state.margin', 0);
    dom.scoreSub.textContent = `Margin: ${margin > 0 ? '+' : ''}${margin}`;

    // ── BLM Score ───────────────────────────────────────────────
    const blmScore = safeGet(data, 'blm.blm_score') || safeGet(data, 'blm.expected_margin');
    animateValue(dom.blmScoreValue, toFixed(blmScore));
    const expectedWinner = safeGet(data, 'blm.expected_winner', '--');
    dom.blmScoreSub.textContent = `Expected Winner: ${expectedWinner}`;

    // ── Trap Meter ──────────────────────────────────────────────
    const trapMeter = safeGet(data, 'trap_detection.trap_meter', 0);
    const trapPct = Math.round(trapMeter * 100);
    animateValue(dom.trapMeterValue, trapPct + '%');
    dom.trapMeterFill.style.width = trapPct + '%';
    // Color the gauge
    dom.trapMeterGauge.className = 'gauge';
    if (trapPct >= 80) dom.trapMeterGauge.classList.add('gauge-high');
    else if (trapPct >= 50) dom.trapMeterGauge.classList.add('gauge-mid');
    else dom.trapMeterGauge.classList.add('gauge-low');

    // ── Confidence (Circular Gauge) ─────────────────────────────
    const confidence = safeGet(data, 'blm.confidence', 0);
    const confPct = Math.round(confidence * 100);
    animateValue(dom.confidenceValue, confPct + '%');
    const circumference = 2 * Math.PI * 34; // r=34
    const offset = circumference - (confidence * circumference);
    dom.confidenceArc.style.strokeDasharray = circumference;
    dom.confidenceArc.style.strokeDashoffset = offset;
    // Color
    if (confPct >= 70) dom.confidenceArc.style.stroke = '#22c55e';
    else if (confPct >= 40) dom.confidenceArc.style.stroke = '#f59e0b';
    else dom.confidenceArc.style.stroke = '#ef4444';

    // ── Win Probability ─────────────────────────────────────────
    const winProb = safeGet(data, 'blm.win_probability', null);
    animateValue(dom.winProbValue, toPct(winProb));
    dom.winProbSub.textContent = expectedWinner !== '--' ? `${expectedWinner} to win` : '--';

    // ── Momentum ────────────────────────────────────────────────
    const momentumDir = safeGet(data, 'momentum.momentum_direction', 'neutral');
    dom.momentumArrow.className = 'momentum-arrow ' + momentumDir;
    const arrowMap = { up: '↑', down: '↓', sideways: '→', neutral: '→' };
    dom.momentumArrow.textContent = arrowMap[momentumDir] || '→';
    animateValue(dom.momentumScore, toFixed(safeGet(data, 'momentum.momentum_score', null)));
    animateValue(dom.momentumVelocity, toFixed(safeGet(data, 'momentum.momentum_velocity', null)));
    animateValue(dom.momentumAccel, toFixed(safeGet(data, 'momentum.momentum_acceleration', null)));

    // ── Pace ────────────────────────────────────────────────────
    const pace = safeGet(data, 'pace.real_pace', null);
    animateValue(dom.paceValue, toFixed(pace));
    const possessions = safeGet(data, 'pace.possessions', '--');
    const remaining = safeGet(data, 'pace.remaining_possessions', '--');
    dom.paceSub.textContent = `Possessions: ${possessions} / ${remaining} remaining`;

    // ── Expected Total ──────────────────────────────────────────
    const expTotal = safeGet(data, 'blm.expected_total', null);
    animateValue(dom.expectedTotal, toFixed(expTotal));
    const currentTotal = safeGet(data, 'game_state.total', 0);
    dom.expectedTotalSub.textContent = `Current: ${currentTotal}`;

    // ── Expected Margin ─────────────────────────────────────────
    const expMargin = safeGet(data, 'blm.expected_margin', null);
    animateValue(dom.expectedMargin, toFixed(expMargin));
    dom.expectedMarginSub.textContent = `${homeTeam} - ${awayTeam}`;

    // ── Game Details (bottom panel) ─────────────────────────────
    dom.detailGameId.textContent = safeGet(data, 'metadata.game_id', '--');
    dom.detailLeague.textContent = safeGet(data, 'metadata.league', '--');
    dom.detailSeason.textContent = safeGet(data, 'metadata.season', '--');
    dom.detailHomeTeam.textContent = homeTeam;
    dom.detailAwayTeam.textContent = awayTeam;
    dom.detailPossession.textContent = safeGet(data, 'game_state.possession', '--');
    dom.detailQuarter.textContent = `Q${safeGet(data, 'metadata.quarter', '--')}`;
    dom.detailClock.textContent = clock;
    dom.detailSpread.textContent = toFixed(safeGet(data, 'betting_market.spread', null));
    dom.detailTotalLine.textContent = toFixed(safeGet(data, 'betting_market.total', null));
    dom.detailLiveSpread.textContent = toFixed(safeGet(data, 'betting_market.live_spread', null));
    dom.detailLiveTotal.textContent = toFixed(safeGet(data, 'betting_market.live_total', null));
    dom.detailMoneyline.textContent = safeGet(data, 'betting_market.moneyline', '--');
    dom.detailSteamMovement.textContent = toFixed(safeGet(data, 'betting_market.steam_movement', null), 2);
    dom.detailReverseLine.textContent = safeGet(data, 'betting_market.reverse_line_movement', '--');
    dom.detailSnapshotVersion.textContent = safeGet(data, 'metadata.snapshot_version', '--');

    // ── Update Chart ────────────────────────────────────────────
    pushChartDataPoint(data);
    updateLastUpdate();
  }

  // ── Chart Functions ────────────────────────────────────────────────

  function getOrCreateChart() {
    const ctx = document.getElementById('mainChart').getContext('2d');

    if (mainChart) return mainChart;

    // Register plugins
    Chart.register(chartjsZoom);
    Chart.register(chartjsAnnotation);

    mainChart = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [
          {
            label: 'BLM Score',
            data: [],
            borderColor: CHART_COLORS.blmScore,
            backgroundColor: CHART_COLORS.blmScore + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 2,
            pointHoverRadius: 5,
            borderWidth: 2,
            yAxisID: 'y',
          },
          {
            label: 'Trap Meter',
            data: [],
            borderColor: CHART_COLORS.trapMeter,
            backgroundColor: CHART_COLORS.trapMeter + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y1',
          },
          {
            label: 'Win Probability',
            data: [],
            borderColor: CHART_COLORS.winProb,
            backgroundColor: CHART_COLORS.winProb + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y1',
          },
          {
            label: 'Pace',
            data: [],
            borderColor: CHART_COLORS.pace,
            backgroundColor: CHART_COLORS.pace + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y',
          },
          {
            label: 'Projected Total',
            data: [],
            borderColor: CHART_COLORS.projectedTotal,
            backgroundColor: CHART_COLORS.projectedTotal + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y',
          },
          {
            label: 'Momentum',
            data: [],
            borderColor: CHART_COLORS.momentum,
            backgroundColor: CHART_COLORS.momentum + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y',
          },
          {
            label: 'Vegas Line',
            data: [],
            borderColor: CHART_COLORS.vegasLine,
            backgroundColor: CHART_COLORS.vegasLine + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y',
          },
          {
            label: 'Confidence',
            data: [],
            borderColor: CHART_COLORS.confidence,
            backgroundColor: CHART_COLORS.confidence + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y1',
          },
          {
            label: 'Market Movement',
            data: [],
            borderColor: CHART_COLORS.marketMovement,
            backgroundColor: CHART_COLORS.marketMovement + '33',
            showLine: true,
            tension: 0.2,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
            borderDash: [4, 3],
            hidden: true,
            yAxisID: 'y',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: 'index',
          intersect: false,
        },
        plugins: {
          legend: {
            display: false,
          },
          tooltip: {
            backgroundColor: 'rgba(13, 17, 28, 0.95)',
            titleColor: '#e8edf5',
            bodyColor: '#8892b0',
            borderColor: '#1e2745',
            borderWidth: 1,
            padding: 10,
            callbacks: {
              title: function (items) {
                if (items.length > 0) {
                  return 'Game Time: ' + items[0].label;
                }
                return '';
              },
            },
          },
          zoom: {
            pan: {
              enabled: true,
              mode: 'x',
              modifierKey: 'shift',
            },
            zoom: {
              wheel: {
                enabled: true,
                speed: 0.05,
              },
              pinch: {
                enabled: true,
              },
              drag: {
                enabled: true,
                backgroundColor: 'rgba(0, 212, 255, 0.1)',
                borderColor: '#00d4ff',
                borderWidth: 1,
              },
              mode: 'x',
            },
          },
          annotation: {
            annotations: {},
          },
        },
        scales: {
          x: {
            type: 'linear',
            title: {
              display: true,
              text: 'Game Time (minutes)',
              color: '#55607a',
            },
            ticks: {
              color: '#55607a',
              maxTicksLimit: 20,
            },
            grid: {
              color: 'rgba(30, 39, 69, 0.5)',
            },
          },
          y: {
            type: 'linear',
            position: 'left',
            title: {
              display: true,
              text: 'Score / Value',
              color: '#55607a',
            },
            ticks: { color: '#55607a' },
            grid: {
              color: 'rgba(30, 39, 69, 0.3)',
            },
          },
          y1: {
            type: 'linear',
            position: 'right',
            min: 0,
            max: 1,
            title: {
              display: true,
              text: 'Percentage',
              color: '#55607a',
            },
            ticks: {
              color: '#55607a',
              callback: function (value) {
                return (value * 100).toFixed(0) + '%';
              },
            },
            grid: {
              drawOnChartArea: false,
            },
          },
        },
      },
    });

    return mainChart;
  }

  function pushChartDataPoint(data) {
    if (!mainChart) mainChart = getOrCreateChart();

    const quarter = safeGet(data, 'metadata.quarter', 1);
    const clock = safeGet(data, 'metadata.clock', '12:00');
    // Compute game time in minutes
    let gameMinutes = (quarter - 1) * 12;
    if (clock && clock !== '--') {
      const parts = clock.split(':');
      const mins = parseInt(parts[0], 10);
      const secs = parseInt(parts[1], 10);
      gameMinutes += (12 - mins - 1) + (60 - secs) / 60;
    }

    // Push data to each dataset
    const blmScore = safeGet(data, 'blm.blm_score') || safeGet(data, 'blm.expected_margin') || 0;
    const trapMeter = safeGet(data, 'trap_detection.trap_meter', 0);
    const winProb = safeGet(data, 'blm.win_probability', 0);
    const pace = safeGet(data, 'pace.real_pace', 0);
    const projectedTotal = safeGet(data, 'blm.expected_total', 0);
    const momentum = safeGet(data, 'momentum.momentum_score', 0);
    const vegas = safeGet(data, 'betting_market.live_spread') || safeGet(data, 'betting_market.spread') || 0;
    const confidence = safeGet(data, 'blm.confidence', 0);
    const marketMove = safeGet(data, 'betting_market.steam_movement', 0);

    const datasets = mainChart.data.datasets;
    datasets[0].data.push({ x: gameMinutes, y: blmScore });
    datasets[1].data.push({ x: gameMinutes, y: trapMeter });
    datasets[2].data.push({ x: gameMinutes, y: winProb });
    datasets[3].data.push({ x: gameMinutes, y: pace });
    datasets[4].data.push({ x: gameMinutes, y: projectedTotal });
    datasets[5].data.push({ x: gameMinutes, y: momentum });
    datasets[6].data.push({ x: gameMinutes, y: vegas });
    datasets[7].data.push({ x: gameMinutes, y: confidence });
    datasets[8].data.push({ x: gameMinutes, y: marketMove });

    // Update annotations for quarter separators
    const annotations = mainChart.options.plugins.annotation.annotations;
    const qMinutes = [0, 12, 24, 36, 48];
    qMinutes.forEach((qm, i) => {
      annotations['q' + (i + 1) + '_end'] = {
        type: 'line',
        xMin: qm,
        xMax: qm,
        borderColor: 'rgba(85, 96, 122, 0.4)',
        borderWidth: 1,
        borderDash: [6, 4],
        label: {
          display: true,
          content: 'Q' + (i + 1) + ' End',
          position: 'start',
          backgroundColor: 'rgba(13, 17, 28, 0.8)',
          color: '#55607a',
          font: { size: 9 },
        },
      };
    });

    // Trim data to last 500 points to avoid memory issues
    if (datasets[0].data.length > 500) {
      datasets.forEach(ds => {
        ds.data = ds.data.slice(-500);
      });
    }

    mainChart.update('none');
  }

  // ── Chart Toggle Handlers ──────────────────────────────────────────

  function initChartToggles() {
    const toggles = document.querySelectorAll('.chart-toggle');
    toggles.forEach(btn => {
      btn.addEventListener('click', function () {
        const key = this.dataset.key;
        const datasetIndex = [
          'blmScore', 'trapMeter', 'winProb', 'pace', 'projectedTotal',
          'momentum', 'vegasLine', 'confidence', 'marketMovement'
        ].indexOf(key);

        if (datasetIndex === -1 || !mainChart) return;

        const meta = mainChart.getDatasetMeta(datasetIndex);
        meta.hidden = !meta.hidden;
        this.classList.toggle('active');
        mainChart.update();
      });
    });
  }

  // ── Alert Management ───────────────────────────────────────────────

  function addAlert(alert) {
    const feed = dom.alertsFeed;

    // Remove empty state if present
    const emptyState = feed.querySelector('.empty-state');
    if (emptyState) emptyState.remove();

    const severity = alert.severity || 'info';
    const typeLabel = alert.type ? alert.type.replace(/_/g, ' ') : severity;
    const alertId = alert.id || 'alert-' + Date.now();

    const el = document.createElement('div');
    el.className = 'alert-item';
    el.dataset.alertId = alertId;
    el.innerHTML = `
      <div class="alert-header">
        <span class="alert-type-badge ${severity}">${typeLabel}</span>
        <span class="alert-time">${formatTime(alert.timestamp)}</span>
      </div>
      <div class="alert-title">${alert.title || 'Alert'}</div>
      <div class="alert-message">${alert.message || ''}</div>
    `;

    // Click to dismiss
    el.addEventListener('click', function () {
      dismissAlert(alertId);
    });

    feed.insertBefore(el, feed.firstChild);

    // Update count
    const visible = feed.querySelectorAll('.alert-item:not(.dismissing)');
    dom.alertCount.textContent = visible.length;

    // Auto-dismiss after 30s
    const timer = setTimeout(function () {
      dismissAlert(alertId);
    }, ALERT_DISMISS_AFTER_MS);
    alertTimers.set(alertId, timer);
  }

  function dismissAlert(alertId) {
    const feed = dom.alertsFeed;
    const el = feed.querySelector(`[data-alert-id="${alertId}"]`);
    if (el) {
      el.classList.add('dismissing');
      setTimeout(function () {
        el.remove();
        // Show empty state if no alerts
        if (!feed.querySelector('.alert-item')) {
          feed.innerHTML = `
            <div class="empty-state">
              <div class="empty-state-icon">📡</div>
              <div class="empty-state-text">No alerts yet</div>
              <div class="empty-state-sub">Alerts will appear here as they trigger</div>
            </div>
          `;
        }
        dom.alertCount.textContent = feed.querySelectorAll('.alert-item:not(.dismissing)').length;
      }, 400);
    }
    // Clear timer
    const timer = alertTimers.get(alertId);
    if (timer) {
      clearTimeout(timer);
      alertTimers.delete(alertId);
    }
  }

  // ── WebSocket Connection ───────────────────────────────────────────

  function connectWebSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    setStatus('reconnecting');

    try {
      ws = new WebSocket(WS_URL);
    } catch (err) {
      console.error('WebSocket construction failed:', err);
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      console.log('[BLM Dashboard] WebSocket connected');
      reconnectAttempts = 0;
      setStatus('connected');
      // Subscribe to current game or wildcard
      if (currentGameId) {
        ws.send(JSON.stringify({ subscribe: currentGameId }));
      } else {
        ws.send(JSON.stringify({ subscribe: '*' }));
      }
    };

    ws.onmessage = function (event) {
      try {
        const msg = JSON.parse(event.data);

        if (msg.type === 'snapshot') {
          const data = msg.data || {};
          const gid = msg.game_id || data.game_id || '';
          if (!currentGameId && gid) {
            currentGameId = gid;
          }
          updateAllPanels(data);
        } else if (msg.type === 'subscribed') {
          console.log('[BLM Dashboard] Subscribed to game:', msg.game_id);
          currentGameId = msg.game_id;
        } else if (msg.type === 'pong') {
          // Keepalive response received
        } else if (msg.type === 'error') {
          console.warn('[BLM Dashboard] Server error:', msg.message);
        } else if (msg.type === 'alert') {
          // Direct alert push from server
          addAlert(msg.alert || msg);
        }
      } catch (err) {
        console.error('[BLM Dashboard] Failed to parse message:', err);
      }
    };

    ws.onclose = function () {
      console.log('[BLM Dashboard] WebSocket disconnected');
      setStatus('disconnected');
      scheduleReconnect();
    };

    ws.onerror = function (err) {
      console.error('[BLM Dashboard] WebSocket error:', err);
      // onclose will fire after onerror, triggering reconnect
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
    }
    reconnectAttempts++;
    const delay = Math.min(
      RECONNECT_BASE_DELAY * Math.pow(1.5, reconnectAttempts - 1),
      RECONNECT_MAX_DELAY
    );
    console.log(`[BLM Dashboard] Reconnecting in ${delay}ms (attempt ${reconnectAttempts})`);
    reconnectTimer = setTimeout(function () {
      connectWebSocket();
    }, delay);
  }

  function sendMessage(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }

  // ── Initialization ─────────────────────────────────────────────────

  function init() {
    console.log('[BLM Dashboard] Initializing...');

    // Initialize chart
    getOrCreateChart();

    // Initialize chart toggles
    initChartToggles();

    // Load game list from REST API to find current game
    fetch('/api/v2/live')
      .then(function (res) {
        if (!res.ok) throw new Error('No live game');
        return res.json();
      })
      .then(function (data) {
        if (data && data.game_id) {
          currentGameId = data.game_id;
          // Load initial chart history
          return fetch('/api/v2/chart/' + data.game_id);
        }
      })
      .then(function (res) {
        if (res && res.ok) return res.json();
        return null;
      })
      .then(function (chartData) {
        if (chartData && chartData.data_points) {
          // Pre-populate chart with historical data
          chartData.data_points.forEach(function (pt) {
            // Push as a pseudo-snapshot structure
            pushChartDataPoint(pt);
          });
        }
      })
      .catch(function (err) {
        console.log('[BLM Dashboard] No live game found, waiting for WebSocket data');
      })
      .finally(function () {
        // Connect WebSocket
        connectWebSocket();
      });

    // Handle visibility change — reconnect if needed
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && (!ws || ws.readyState !== WebSocket.OPEN)) {
        connectWebSocket();
      }
    });
  }

  // ── Start ──────────────────────────────────────────────────────────

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
