/* Tape & Book — LIVE view. Polls the local feed and draws the book. */
'use strict';

const $ = (id) => document.getElementById(id);
const S = { symbol: 'btcusdt', heat: null, dom: null, foot: null, cvd: null, bot: null, tick: 0.1 };

/* Decimals that show one tick. A fixed 1 decimal rendered every DOGE or XRP
   price as the same number. */
const tickDp = (tick) => Math.min(8, Math.max(0, Math.ceil(-Math.log10(tick || 0.1) - 1e-9)));

const fmt = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n) ? '—'
    : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hms = (ms) => {
  const d = new Date(ms);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((x) => String(x).padStart(2, '0')).join(':');
};

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

function prep(canvas) {
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth, h = wrap.clientHeight;
  canvas.width = Math.max(1, Math.floor(w * dpr));
  canvas.height = Math.max(1, Math.floor(h * dpr));
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function note(ctx, w, h, text) {
  ctx.fillStyle = '#6b7a8f';
  ctx.font = '11px Consolas,monospace';
  ctx.textAlign = 'center';
  text.split('\n').forEach((l, i) => ctx.fillText(l, w / 2, h / 2 - 8 + i * 15));
  ctx.textAlign = 'left';
}

/* ---------- liquidity heatmap: the Bookmap view ---------- */
function drawHeat() {
  const { ctx, w, h } = prep($('heatC'));
  const d = S.heat;
  if (!d || !d.cells || !d.cells.length) {
    return note(ctx, w, h, 'collecting depth…\nthe heatmap builds from one book snapshot per second');
  }
  const nc = d.cols.length, nr = d.rows.length;
  const padR = 62;
  const cw = Math.max(1, (w - padR) / Math.max(nc, 1));
  const ch = Math.max(1, h / Math.max(nr, 1));

  let peak = 0;
  for (const c of d.cells) if (c[2] > peak) peak = c[2];
  const scale = peak > 0 ? 1 / Math.log1p(peak) : 0;

  for (const [col, row, size, side] of d.cells) {
    const t = Math.log1p(size) * scale;
    const x = col * cw;
    const y = h - (row + 1) * ch;
    // resting liquidity: bids warm green, asks warm red, intensity = size
    ctx.fillStyle = side === 0
      ? `rgba(38,166,154,${0.06 + t * 0.94})`
      : `rgba(239,83,80,${0.06 + t * 0.94})`;
    ctx.fillRect(x, y, Math.max(cw, 1), Math.max(ch, 1));
  }

  ctx.fillStyle = '#0e131c';
  ctx.fillRect(w - padR, 0, padR, h);
  ctx.fillStyle = '#6b7a8f';
  ctx.font = '9px Consolas,monospace';
  const step = Math.max(1, Math.floor(nr / 9));
  for (let r = 0; r < nr; r += step) {
    const y = h - (r + 0.5) * ch;
    if (y < 8 || y > h - 2) continue;
    ctx.fillText(fmt(d.rows[r], tickDp(S.tick)), w - padR + 4, y + 3);
  }
}

/* ---------- DOM ladder ---------- */
function drawDom() {
  const { ctx, w, h } = prep($('domC'));
  const d = S.dom;
  if (!d || (!d.bids.length && !d.asks.length)) return note(ctx, w, h, 'no book');

  const asks = d.asks.slice(0, 12).reverse();
  const bids = d.bids.slice(0, 12);
  const rows = asks.length + bids.length + 1;
  const rh = Math.min(16, (h - 4) / rows);
  const peak = Math.max(...asks.map((x) => x.size), ...bids.map((x) => x.size), 1);

  ctx.font = '10px Consolas,monospace';
  ctx.textBaseline = 'middle';
  let y = 2;

  const row = (item, colour) => {
    const bw = (item.size / peak) * (w - 84);
    ctx.fillStyle = colour.replace('ALPHA', '0.22');
    ctx.fillRect(w - 84 - bw, y, bw, rh - 1);
    ctx.fillStyle = colour.replace('ALPHA', '1');
    ctx.textAlign = 'left';
    ctx.fillText(fmt(item.price, tickDp(S.tick)), 4, y + rh / 2);
    ctx.fillStyle = '#93a3b8';
    ctx.textAlign = 'right';
    ctx.fillText(fmt(item.size, 2), w - 4, y + rh / 2);
    y += rh;
  };

  asks.forEach((a) => row(a, 'rgba(239,83,80,ALPHA)'));

  const spread = (bids[0] && asks.length) ? asks[asks.length - 1].price - bids[0].price : null;
  ctx.fillStyle = '#131a26';
  ctx.fillRect(0, y, w, rh);
  ctx.fillStyle = '#e0a33e';
  ctx.textAlign = 'center';
  ctx.fillText(spread !== null ? `spread ${fmt(spread, tickDp(S.tick))}` : '—', w / 2, y + rh / 2);
  y += rh;

  bids.forEach((b) => row(b, 'rgba(38,166,154,ALPHA)'));
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
}

/* ---------- live footprint ---------- */
function drawFoot() {
  const { ctx, w, h } = prep($('footC'));
  const d = S.foot;
  if (!d || !d.levels || !d.levels.length) return note(ctx, w, h, 'collecting trades…');

  const lv = d.levels.slice(-40);
  const rh = Math.max(7, Math.min(15, h / lv.length));
  const peak = Math.max(...lv.map((x) => Math.max(x.bid, x.ask)), 1e-9);
  const half = w / 2 - 22;

  ctx.font = '9px Consolas,monospace';
  ctx.textBaseline = 'middle';
  lv.forEach((L, i) => {
    const y = h - (i + 0.5) * rh;
    if (y < 2 || y > h - 1) return;
    const bl = (L.bid / peak) * half;
    const al = (L.ask / peak) * half;
    ctx.fillStyle = 'rgba(239,83,80,.30)';
    ctx.fillRect(half - bl, y - rh / 2 + 0.5, bl, rh - 1);
    ctx.fillStyle = 'rgba(38,166,154,.30)';
    ctx.fillRect(half + 2, y - rh / 2 + 0.5, al, rh - 1);
    if (L.poc) {
      ctx.strokeStyle = '#8b6fd4'; ctx.lineWidth = 1;
      ctx.strokeRect(0.5, y - rh / 2 + 0.5, w - 1, rh - 1);
    }
    ctx.fillStyle = '#93a3b8';
    ctx.textAlign = 'right';
    ctx.fillText(fmt(L.price, tickDp(d.tick_size)), w - 2, y);
  });
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
}

/* ---------- CVD curve ---------- */
function drawCvd() {
  const { ctx, w, h } = prep($('cvdC'));
  const d = S.cvd;
  if (!d || !d.points || d.points.length < 2) return note(ctx, w, h, 'building CVD curve…');

  const pts = d.points;
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1e-9) { y0 -= 1; y1 += 1; }
  const px = (x) => ((x - x0) / Math.max(x1 - x0, 1e-9)) * (w - 56);
  const py = (y) => h - 6 - ((y - y0) / (y1 - y0)) * (h - 14);

  // zero line
  if (y0 < 0 && y1 > 0) {
    ctx.strokeStyle = '#1e2836'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, py(0)); ctx.lineTo(w - 56, py(0)); ctx.stroke();
  }
  const last = ys[ys.length - 1];
  const col = last >= 0 ? '#26a69a' : '#ef5350';
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(px(p[0]), py(p[1])) : ctx.moveTo(px(p[0]), py(p[1]))));
  ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.stroke();
  ctx.lineTo(px(x1), py(y0)); ctx.lineTo(px(x0), py(y0)); ctx.closePath();
  ctx.fillStyle = last >= 0 ? 'rgba(38,166,154,.13)' : 'rgba(239,83,80,.13)';
  ctx.fill();

  ctx.fillStyle = '#0e131c'; ctx.fillRect(w - 56, 0, 56, h);
  ctx.fillStyle = col; ctx.font = '10px Consolas,monospace';
  ctx.fillText(fmt(last, 3), w - 52, py(last) + 3);
  ctx.fillStyle = '#6b7a8f'; ctx.font = '9px Consolas,monospace';
  ctx.fillText(fmt(y1, 2), w - 52, 10);
  ctx.fillText(fmt(y0, 2), w - 52, h - 4);
}

/* ---------- decision ---------- */
function renderDecision(d) {
  if (!d) return;
  const act = $('act');
  const word = d.action > 0 ? 'BUY' : d.action < 0 ? 'SELL' : 'WAIT';
  act.textContent = word;
  act.className = 'act ' + (d.action > 0 ? 'buy' : d.action < 0 ? 'sell' : 'wait');
  $('sc').textContent = d.blocked_by
    ? d.blocked_by
    : `score ${d.score >= 0 ? '+' : ''}${fmt(d.score, 2)} · confidence ${Math.round((d.confidence || 0) * 100)}%`;

  const tot = (d.bull || 0) + (d.bear || 0) || 1;
  $('barB').style.width = `${((d.bull || 0) / tot) * 100}%`;
  $('barS').style.width = `${((d.bear || 0) / tot) * 100}%`;

  const obs = d.observations || [];
  $('obsList').innerHTML = obs.length
    ? obs.slice().sort((a, b) => Math.abs(b.score) - Math.abs(a.score)).map((o) => `
      <div class="obs">
        <span class="w ${o.score > 0 ? 'buy' : o.score < 0 ? 'sell' : ''}">${o.score >= 0 ? '+' : ''}${fmt(o.score, 2)}</span>
        <span class="n">${esc(o.name)}</span>
        <span class="src ${o.source === 'book' ? 'book' : ''}">${esc(o.source)}</span>
        <span class="d">${esc(o.detail)}</span>
      </div>`).join('')
    : '<div class="empty">No observations yet.<br>Waiting for book data.</div>';

  $('deciMode').textContent = d.has_book ? 'reading book' : 'tape only';
  $('deciNote').textContent = d.has_book
    ? `Note: this is ${S.symbol.toUpperCase()} on Binance, not Nasdaq. The same engine handles NQ once CME data is connected.`
    : 'Book unavailable — decision rests on the tape alone.';
}

/* ---------- tables ---------- */
function renderTrades(list) {
  const el = $('tradeBody');
  if (!list || !list.length) { el.innerHTML = '<div class="empty">waiting for trades…</div>'; return; }
  const buys = list.filter((t) => t.side === 'BUY').length;
  $('tapeInfo').textContent = `${buys}B / ${list.length - buys}S`;
  el.innerHTML = `<table><thead><tr><th>time</th><th>side</th><th>price</th><th>size</th></tr></thead><tbody>${
    list.slice(0, 50).map((t) => `<tr>
      <td>${hms(t.ts)}</td>
      <td class="${t.side === 'BUY' ? 'buy' : 'sell'}">${t.side}</td>
      <td>${fmt(t.price, tickDp(S.tick))}</td><td>${fmt(t.qty, 3)}</td></tr>`).join('')}</tbody></table>`;
}

const EV_LABEL = { wall: 'wall', pulled: 'pulled', stacked: 'stacked', iceberg: 'iceberg', void: 'gap' };

function renderBook(events, large, sweeps) {
  const el = $('evBody');
  const rows = [];
  const dp = tickDp(S.tick);

  for (const s of (sweeps || []).slice(0, 8)) {
    rows.push(`<tr><td class="${s.side === 'BUY' ? 'buy' : 'sell'}">SWEEP ${s.side}</td>
      <td>${s.levels} lvls</td><td>${fmt(s.price_to, dp)}</td><td>${fmt(s.volume, 3)}</td></tr>`);
  }
  for (const t of (large || []).slice(0, 12)) {
    rows.push(`<tr><td class="${t.side === 'BUY' ? 'buy' : 'sell'}">LARGE ${t.side}</td>
      <td>x${t.multiple}</td><td>${fmt(t.price, dp)}</td><td>${fmt(t.qty, 3)}</td></tr>`);
  }
  for (const e of (events || []).slice(0, 30)) {
    rows.push(`<tr><td>${esc(EV_LABEL[e.kind] || e.kind)}</td>
      <td class="${e.side === 0 ? 'buy' : 'sell'}">${e.side === 0 ? 'BID' : 'ASK'}</td>
      <td>${fmt(e.price, dp)}</td><td>${fmt(e.size, 2)}</td></tr>`);
  }

  const counts = {};
  for (const e of (events || [])) counts[e.kind] = (counts[e.kind] || 0) + 1;
  $('evInfo').textContent = [
    (sweeps || []).length ? `sweeps:${sweeps.length}` : '',
    (large || []).length ? `large:${large.length}` : '',
    ...Object.entries(counts).map(([k, v]) => `${EV_LABEL[k] || k}:${v}`),
  ].filter(Boolean).join(' ');

  el.innerHTML = rows.length
    ? `<table><thead><tr><th>event</th><th>side</th><th>price</th><th>size</th></tr></thead><tbody>${rows.join('')}</tbody></table>`
    : '<div class="empty">waiting for book events…</div>';
}


/* ---------- bot demo ---------- */
function renderBot(st) {
  if (!st) return;
  const dp = tickDp(S.tick);
  $('botEq').textContent = fmt(st.equity_live, 2);
  $('botEq').className = st.pnl >= 0 ? 'up' : 'dn';
  $('botPnl').innerHTML = `<span class="${st.pnl >= 0 ? 'up' : 'dn'}">${st.pnl >= 0 ? '+' : ''}${fmt(st.pnl, 2)} USD (${st.pnl_pct >= 0 ? '+' : ''}${fmt(st.pnl_pct, 2)}%)</span>`;
  $('botLeft').textContent = `${st.trades_left} entries today`;
  $('botMode').textContent = `${st.config.leverage}x · ${st.compound ? 'compound' : 'fixed size'}`;

  // Size of the next entry: with compounding it follows equity up and down.
  $('botNext').textContent = fmt(st.next_notional, 0) + ' USD';
  $('botPeak').textContent = fmt(st.peak_equity, 2);
  const dd = st.max_drawdown || 0;
  $('botDd').textContent = dd ? `${fmt(dd, 2)} (${fmt(st.max_drawdown_pct, 1)}%)` : '—';
  $('botDd').className = dd < 0 ? 'dn' : 'dim';
  $('botCompound').textContent = st.compound ? 'fixed size' : 'compound';

  const ratio = Math.max(0, Math.min(1, st.equity_live / Math.max(st.start_equity * 2, 1e-9)));
  $('botBar').style.width = `${ratio * 100}%`;
  $('botBar').className = st.pnl >= 0 ? 'b' : 's';

  const pos = st.position;
  const el = $('botPos');
  if (pos) {
    el.style.display = 'block';
    el.innerHTML = `
      <div class="act ${pos.side === 'LONG' ? 'buy' : 'sell'}" style="font-size:14px">${pos.side} ${fmt(pos.notional, 0)} USD</div>
      <div class="sc">entry ${fmt(pos.entry, dp)} · now ${fmt(pos.price, dp)}</div>
      <div class="sc"><span class="${pos.unreal >= 0 ? 'up' : 'dn'}">${pos.unreal >= 0 ? '+' : ''}${fmt(pos.unreal, 2)} USD (${fmt(pos.pct, 2)}%)</span> · ${pos.held_s}s</div>
      <div class="sc" style="color:var(--ask)">liquidation ${fmt(pos.liq, dp)}</div>`;
  } else {
    el.style.display = 'none';
  }

  $('botToggle').textContent = st.running ? 'pause' : 'resume';
  $('botCosts').textContent = st.costs_on ? 'disable fees' : 'enable fees';
  $('botNote').textContent = st.error
    ? `Bot error: ${st.error}`
    : st.costs_on
      ? `Fees on (${st.config.fee_bps} bps per side) — P&L is realistic.`
      : 'Fees OFF — P&L is flattered; this tests the mechanics only.';
}

function renderBotTrades(list) {
  const el = $('botTrades');
  if (!list || !list.length) {
    el.innerHTML = '<div class="empty">no closed positions</div>';
    return;
  }
  el.innerHTML = `<table><thead><tr><th>side</th><th>%</th><th>net</th><th>reason</th></tr></thead><tbody>${
    list.slice(0, 40).map((t) => `<tr>
      <td class="${t.side === 'LONG' ? 'buy' : 'sell'}">${t.side}</td>
      <td class="${t.pct >= 0 ? 'buy' : 'sell'}">${fmt(t.pct, 2)}</td>
      <td class="${t.net >= 0 ? 'buy' : 'sell'}">${t.net >= 0 ? '+' : ''}${fmt(t.net, 2)}</td>
      <td style="font-size:10px">${esc(t.reason)}</td></tr>`).join('')}</tbody></table>`;
}

async function botAction(what, params) {
  try { await post(`/api/trader/${what}`, params || {}); tickBot(); } catch (e) { console.error(e); }
}

async function tickBot() {
  try {
    const [st, hist] = await Promise.all([
      api('/api/trader/state'), api('/api/trader/history', { n: 40 }),
    ]);
    S.bot = st;
    renderBot(st); renderBotTrades(hist.trades);
  } catch (e) { console.error(e); }
}

/* ---------- loop ---------- */
async function tick() {
  try {
    // One request carries the whole live snapshot; nine separate polls were
    // slower and tripped the server's own rate limiter.
    const all = await api('/api/live/all', { levels: 14 });
    const st = all.status, dom = all.dom, dec = all.decision, tape = all.tape;
    const foot = all.footprint;
    // /api/live/all carries large prints and sweeps as plain arrays. Reading
    // them as {trades}/{sweeps} objects left both panels permanently empty.
    const tr = { trades: all.trades }, ev = { events: all.events };
    const large = { trades: all.large }, sw = { sweeps: all.sweeps };

    $('dot').className = 'dot' + (st.connected ? ' on' : '');
    $('conn').textContent = st.connected ? 'connected' : (st.error ? 'error' : 'connecting…');
    S.tick = st.tick_size || 0.1;
    const dp = tickDp(S.tick);
    $('sBid').textContent = fmt(st.best_bid, dp);
    $('sAsk').textContent = fmt(st.best_ask, dp);
    $('sMicro').textContent = fmt(st.microprice, dp + 1);   // sub-tick by construction
    $('sImb').textContent = (st.imbalance >= 0 ? '+' : '') + fmt(st.imbalance, 3);
    $('sImb').className = st.imbalance >= 0 ? 'up' : 'dn';
    $('sTick').textContent = S.tick;
    $('sMsg').textContent = st.messages;

    if (tape && tape.delta !== undefined) {
      $('sDelta').textContent = (tape.delta >= 0 ? '+' : '') + fmt(tape.delta, 3);
      $('sDelta').className = tape.delta >= 0 ? 'up' : 'dn';
      $('tapeInfo').textContent = `${fmt(tape.buy_volume, 2)}B / ${fmt(tape.sell_volume, 2)}S`;
    }

    S.dom = dom; S.foot = foot;
    renderTrades(tr.trades);
    renderBook(ev.events, large.trades, sw.sweeps);
    renderDecision(dec);
    drawDom(); drawFoot();
  } catch (e) { console.error(e); }
}

async function tickSlow() {
  try {
    const [heat, cvd] = await Promise.all([
      api('/api/live/heatmap'), api('/api/live/cvd'),
    ]);
    S.heat = heat; S.cvd = cvd;
    $('sCvd').textContent = (cvd.cvd >= 0 ? '+' : '') + fmt(cvd.cvd, 3);
    $('sCvd').className = cvd.cvd >= 0 ? 'up' : 'dn';
    $('cvdInfo').textContent = `${(cvd.points || []).length} pts`;
    drawHeat(); drawCvd();
  } catch (e) { console.error(e); }
}

async function loadSymbols() {
  try {
    const d = await api('/api/live/symbols');
    $('symSel').innerHTML = (d.symbols || []).map(
      (x) => `<option value="${esc(x.symbol)}">${esc(x.label)}</option>`).join('');
    $('symSel').value = S.symbol;
  } catch (e) { console.error(e); }
}

$('symSel').addEventListener('change', (e) => {
  S.symbol = e.target.value;
  S.heat = null; S.dom = null; S.foot = null; S.cvd = null;
  tick(); tickSlow();
});
window.addEventListener('resize', () => { drawHeat(); drawDom(); drawFoot(); drawCvd(); });

$('botToggle').addEventListener('click', () => botAction('toggle'));
$('botReset').addEventListener('click', () => botAction('reset'));
$('botCosts').addEventListener('click', () => botAction('config', {
  fee_bps: (S.bot && S.bot.costs_on) ? 0 : 10,
}));
$('botCompound').addEventListener('click', () => botAction('config', {
  compound: (S.bot && S.bot.compound) ? 0 : 1,
}));

loadSymbols();
tick(); tickSlow(); tickBot();
setInterval(tick, 1000);
setInterval(tickBot, 1000);
setInterval(tickSlow, 2500);
