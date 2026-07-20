/**
 * BLM Research Console — Live Updater + SVG Chart
 * Polls /api/live every 1s and renders score + chart.
 */

const API_BASE = '/api';
let snapshots = [];
let chartSvg = null;

// ── DOM refs ──────────────────────────────────────────────────

const $ = id => document.getElementById(id);

// ── Fetch & Render ─────────────────────────────────────────────

async function fetchLive() {
  try {
    const res = await fetch(`${API_BASE}/live`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.game_id) {
      renderScore(data);
    }
    $('status').textContent = data.game_id ? '● Live' : '○ Waiting';
    $('snapshot-count').textContent = `${data.snapshot_count || 0} snapshots`;
    $('last-update').textContent = new Date().toLocaleTimeString();
  } catch (err) {
    $('status').textContent = '✕ Error';
    $('status').style.color = 'var(--accent-red)';
  }
}

function renderScore(data) {
  $('home-team').textContent = data.home_team || 'HOME';
  $('away-team').textContent = data.away_team || 'AWAY';
  $('home-score').textContent = data.home_score ?? 0;
  $('away-score').textContent = data.away_score ?? 0;
  $('quarter').textContent = `Q${data.quarter || 1}`;
  $('clock').textContent = data.clock || '00:00';
  $('total-line').textContent = data.total_line ?? '--';
  $('spread').textContent = data.spread ?? '--';

  const margin = (data.home_score ?? 0) - (data.away_score ?? 0);
  $('margin').textContent = margin > 0 ? `+${margin}` : margin;

  // Derived metrics
  $('pace').textContent = data.pace ?? '--';
  $('expected-total').textContent = data.expected_total ?? '--';
  $('possessions').textContent = data.possessions ?? '--';
  $('home-projection').textContent = data.home_projection ?? '--';
  $('away-projection').textContent = data.away_projection ?? '--';
  $('inflation').textContent = data.inflation ?? '--';

  // Update chart if we have a timestamp
  if (data.timestamp) {
    snapshots.push({
      ts: data.timestamp,
      total: (data.home_score ?? 0) + (data.away_score ?? 0),
      line: data.total_line,
    });
    if (snapshots.length > 200) snapshots = snapshots.slice(-200);
    renderChart();
  }
}

// ── SVG Chart ──────────────────────────────────────────────────

function renderChart() {
  if (snapshots.length < 2) return;

  const svg = document.getElementById('line-chart');
  const W = 800, H = 300;
  const pad = { top: 20, right: 20, bottom: 30, left: 40 };
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;

  const totals = snapshots.map(s => s.total);
  const lines = snapshots.map(s => s.line).filter(l => l != null);
  const allVals = [...totals, ...lines];
  const yMin = Math.min(...allVals) - 5;
  const yMax = Math.max(...allVals) + 5;
  const yRange = yMax - yMin || 1;

  const xScale = i => pad.left + (i / (snapshots.length - 1)) * cw;
  const yScale = v => pad.top + ch - ((v - yMin) / yRange) * ch;

  const totalPath = snapshots.map((s, i) =>
    `${i === 0 ? 'M' : 'L'}${xScale(i)},${yScale(s.total)}`
  ).join(' ');

  const linePath = snapshots
    .filter(s => s.line != null)
    .map((s, i) => {
      const idx = snapshots.indexOf(s);
      return `${i === 0 ? 'M' : 'L'}${xScale(idx)},${yScale(s.line)}`;
    }).join(' ');

  // Grid lines
  const yTicks = 5;
  let html = '';
  for (let i = 0; i <= yTicks; i++) {
    const y = pad.top + (ch / yTicks) * i;
    const val = yMax - (yRange / yTicks) * i;
    html += `<line x1="${pad.left}" y1="${y}" x2="${W - pad.right}" y2="${y}" stroke="var(--chart-grid)" stroke-width="0.5"/>`;
    html += `<text x="${pad.left - 4}" y="${y + 4}" text-anchor="end" fill="var(--text-secondary)" font-size="10">${Math.round(val)}</text>`;
  }

  // Data paths
  html += `<path d="${totalPath}" fill="none" stroke="var(--chart-line)" stroke-width="2"/>`;
  if (linePath) {
    html += `<path d="${linePath}" fill="none" stroke="var(--accent-yellow)" stroke-width="1.5" stroke-dasharray="4,3"/>`;
  }

  // Latest value dot
  const last = snapshots[snapshots.length - 1];
  const lx = xScale(snapshots.length - 1);
  const ly = yScale(last.total);
  html += `<circle cx="${lx}" cy="${ly}" r="4" fill="var(--chart-line)"/>`;
  html += `<text x="${lx + 8}" y="${ly + 4}" fill="var(--chart-line)" font-size="12" font-weight="600">${last.total}</text>`;

  svg.innerHTML = html;
}

// ── Start ──────────────────────────────────────────────────────

setInterval(fetchLive, 1000);
fetchLive();
