/* Tape & Book — learning report. */
'use strict';

const $ = (id) => document.getElementById(id);
const S = { symbol: 'btcusdt', horizon: '30' };

const fmt = (n, d = 1) =>
  n === null || n === undefined || Number.isNaN(n) ? '—'
    : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* --- session token: fetched once, echoed on every state-changing call --- */
let TOKEN = null;
async function ensureToken() {
  if (TOKEN) return TOKEN;
  try {
    const r = await fetch('/api/session');
    TOKEN = (await r.json()).token;
  } catch (e) { console.error('token', e); }
  return TOKEN;
}

/** POST a mutation. Reads go through api(); writes must come here. */
async function post(path, body = {}) {
  await ensureToken();
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN || '' },
    body: JSON.stringify({ symbol: S.symbol, ...body }),
  });
  const j = await r.json();
  if (j && j.error) throw new Error(j.error);
  return j;
}

async function api(path, params = {}) {
  const q = new URLSearchParams({ symbol: S.symbol, ...params });
  const r = await fetch(`${path}?${q}`);
  return r.json();
}

function renderCards(rep, props) {
  const usable = props.filter((p) => p.enough).length;
  const solid = props.filter((p) => p.enough && p.hit_lower > 50).length;
  const disagree = props.filter((p) => p.agrees === false).length;
  $('cards').innerHTML = `
    <div class="card"><div class="k">Observations recorded</div>
      <div class="v">${fmt(rep.rows, 0)}</div>
      <div class="d">train ${fmt(rep.train_rows, 0)} · test ${fmt(rep.test_rows, 0)}</div></div>
    <div class="card"><div class="k">Features with enough samples</div>
      <div class="v">${usable}<span class="dim" style="font-size:.5em"> / ${props.length}</span></div>
      <div class="d">the rest await more data</div></div>
    <div class="card"><div class="k">With measurable edge</div>
      <div class="v ${solid ? 'up' : 'dim'}">${solid}</div>
      <div class="d">lower bound of hit rate above 50%</div></div>
    <div class="card"><div class="k">Learning progress</div>
      <div class="v ${rep.rows >= 2500 ? 'up' : rep.rows >= 400 ? 'w' : 'dim'}">${Math.min(100, Math.round(rep.rows / 25))}<span class="dim" style="font-size:.5em">%</span></div>
      <div class="d">${rep.rows >= 2500 ? 'enough material for firm conclusions'
        : rep.rows >= 400 ? 'enough for strong signals, not weak ones'
        : `collecting — about ${Math.max(1, Math.round((2500 - rep.rows) / 3600 * 60))} min left`}</div></div>
    <div class="card"><div class="k">Contradicted by test</div>
      <div class="v ${disagree ? 'dn' : 'dim'}">${disagree}</div>
      <div class="d">weight cut — suspected noise</div></div>`;
}

function renderTable(props) {
  if (!props.length) {
    $('tableWrap').innerHTML = '<div class="empty">No observations to score yet.</div>';
    return;
  }
  const rows = props.map((p) => {
    const dir = p.hit_lower > 50 ? 'up' : p.hit_rate < 50 ? 'dn' : 'dim';
    const oos = p.oos_hit_rate === null ? '<span class="dim">—</span>'
      : `<span class="${p.agrees === false ? 'dn' : p.oos_hit_rate > 50 ? 'up' : 'dim'}">${fmt(p.oos_hit_rate)}%</span>`;
    const arrow = !p.changed ? '<span class="dim">unchanged</span>'
      : p.new > p.old ? `<span class="up">${fmt(p.old, 2)} → ${fmt(p.new, 2)} ↑</span>`
        : `<span class="dn">${fmt(p.old, 2)} → ${fmt(p.new, 2)} ↓</span>`;
    return `<tr>
      <td>${esc(p.name)} <span class="src ${p.source === 'book' ? 'book' : ''}">${esc(p.source)}</span></td>
      <td title="${fmt(p.decided, 0)} with a price move · ${fmt(p.flat, 0)} flat (not scored)">${fmt(p.samples, 0)}</td>
      <td class="${dir}"><span class="bar"><i style="width:${Math.max(0, Math.min(100, p.hit_rate))}%"></i></span>${fmt(p.hit_rate)}%</td>
      <td class="${dir}">${fmt(p.hit_lower)}%</td>
      <td>${oos}</td>
      <td class="${p.avg_bps >= 0 ? 'up' : 'dn'}">${p.avg_bps >= 0 ? '+' : ''}${fmt(p.avg_bps, 2)}</td>
      <td>${arrow}</td>
      <td class="dim" style="text-align:left;font-size:10.5px">${esc(p.reason)}</td></tr>`;
  }).join('');

  $('tableWrap').innerHTML = `<table><thead><tr>
    <th>feature</th><th>samples</th><th>hit rate</th><th>lower bd.</th>
    <th>OOS</th><th>avg move (bps)</th><th>weight</th><th>reason</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

async function refresh() {
  $('status').textContent = 'computing…';
  try {
    const d = await api('/api/learn/report', { horizon: S.horizon });
    const rep = d.report || {};
    const props = d.proposals || [];

    if (!rep.rows) {
      $('cards').innerHTML = '';
      $('tableWrap').innerHTML = `<div class="empty">
        <b>The journal is still empty.</b><br><br>
        The bot records every observation and waits 300 seconds to see
        what price did next.<br>
        First rows appear after about 5 minutes of running,<br>
        and meaningful conclusions after a few hours.<br><br>
        <span class="dim">Leave the LIVE terminal running and come back later.</span></div>`;
      $('status').textContent = 'no data';
      return;
    }
    renderCards(rep, props);
    renderTable(props);
    $('status').textContent = `${fmt(rep.rows, 0)} observations · horizon ${rep.horizon_s}s`
      + (rep.legacy_rows ? ` · ${fmt(rep.legacy_rows, 0)} rows predate event de-duplication` : '');
  } catch (e) {
    $('status').textContent = 'error: ' + e.message;
  }
}

async function loadSymbols() {
  try {
    const d = await api('/api/live/symbols');
    $('symSel').innerHTML = (d.symbols || []).map(
      (x) => `<option value="${esc(x.symbol)}">${esc(x.label)}</option>`).join('');
    $('symSel').value = S.symbol;
  } catch (e) { console.error(e); }
}

$('symSel').addEventListener('change', (e) => { S.symbol = e.target.value; refresh(); });
$('hSel').addEventListener('change', (e) => { S.horizon = e.target.value; refresh(); });
$('btnRefresh').addEventListener('click', refresh);
$('btnApply').addEventListener('click', async () => {
  $('status').textContent = 'applying…';
  try {
    // Send the horizon on screen, so what gets applied is what was shown.
    const r = await post('/api/learn/apply', { horizon: S.horizon });
    $('status').textContent = r.count
      ? `applied ${r.count} weights — the bot uses them immediately`
      : 'no weights to change (not enough data)';
    refresh();
  } catch (e) { $('status').textContent = 'error: ' + e.message; }
});

loadSymbols();
refresh();
setInterval(refresh, 30000);
