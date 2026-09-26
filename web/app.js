/* Tape & Book — terminal order flow. Frontend logic. */
'use strict';

const $ = (id) => document.getElementById(id);
const S = { file: null, tf: '15m', bars: null, heat: null, foot: null,
            profile: null, large: null, sweeps: null, showHeat: true };

const fmt = (n, d = 0) =>
  n === null || n === undefined || Number.isNaN(n) ? '—'
    : n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sign = (n) => (n > 0 ? '+' : '') + fmt(n);
const hhmm = (sec) => {
  const d = new Date(sec * 1000);
  return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
};
const dmy = (sec) => {
  const d = new Date(sec * 1000);
  return String(d.getUTCDate()).padStart(2, '0') + '.' + String(d.getUTCMonth() + 1).padStart(2, '0');
};

function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.style.display = 'none'; }, ms);
}

async function api(path, params = {}) {
  const q = new URLSearchParams({ file: S.file || '', ...params });
  const r = await fetch(`${path}?${q}`);
  const j = await r.json();
  if (j && j.error) throw new Error(j.error);
  return j;
}

/* ---------- charts ---------- */
const CHART_OPTS = {
  layout: { background: { color: '#0e131c' }, textColor: '#6b7a8f', fontSize: 10,
            fontFamily: 'Consolas,monospace' },
  grid: { vertLines: { color: '#141c28' }, horzLines: { color: '#141c28' } },
  rightPriceScale: { borderColor: '#1e2836' },
  timeScale: { borderColor: '#1e2836', timeVisible: true, secondsVisible: false },
  crosshair: { mode: 0, vertLine: { color: '#4a9eff', width: 1, style: 2, labelBackgroundColor: '#4a9eff' },
               horzLine: { color: '#4a9eff', width: 1, style: 2, labelBackgroundColor: '#4a9eff' } },
};

let priceChart, candleSeries, vwapSeries, cvdChart, deltaSeries, cvdSeries;

function initCharts() {
  priceChart = LightweightCharts.createChart($('chart'), CHART_OPTS);
  candleSeries = priceChart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  });
  vwapSeries = priceChart.addLineSeries({
    color: '#e0a33e', lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
  });

  cvdChart = LightweightCharts.createChart($('cvd'), {
    ...CHART_OPTS,
    timeScale: { ...CHART_OPTS.timeScale, visible: false },
  });
  deltaSeries = cvdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  cvdSeries = cvdChart.addLineSeries({
    color: '#4a9eff', lineWidth: 2, priceScaleId: 'cvd',
    priceLineVisible: false, lastValueVisible: false,
  });
  cvdChart.priceScale('cvd').applyOptions({ scaleMargins: { top: 0.1, bottom: 0.1 } });

  // keep the two time axes locked together
  priceChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (r) cvdChart.timeScale().setVisibleLogicalRange(r);
  });

  const ro = new ResizeObserver(() => {
    priceChart.applyOptions({ width: $('chart').clientWidth, height: $('chart').clientHeight });
    cvdChart.applyOptions({ width: $('cvd').clientWidth, height: $('cvd').clientHeight });
    drawAll();
  });
  ro.observe($('chart'));
  ro.observe($('cvd'));
  window.addEventListener('resize', drawAll);
}

/* ---------- canvas helpers ---------- */
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
  text.split('\n').forEach((line, i) => ctx.fillText(line, w / 2, h / 2 - 8 + i * 15));
  ctx.textAlign = 'left';
}

/* ---------- heatmap ---------- */
function drawHeat() {
  const { ctx, w, h } = prep($('heat'));
  const d = S.heat;
  if (!S.showHeat) return note(ctx, w, h, 'heatmap off');
  if (!d || !d.cells || !d.cells.length) return note(ctx, w, h, 'no data');

  const nc = d.cols.length, nr = d.rows.length;
  const padL = 0, padR = 56;
  const cw = Math.max(1, (w - padL - padR) / nc);
  const ch = Math.max(1, h / nr);

  let peak = 0;
  for (const c of d.cells) if (c[2] > peak) peak = c[2];
  const scale = peak > 0 ? 1 / Math.log1p(peak) : 0;

  for (const [col, row, vol, dlt] of d.cells) {
    const t = Math.log1p(vol) * scale;           // log keeps small prints visible
    const x = padL + col * cw;
    const y = h - (row + 1) * ch;
    // hue by aggressor: green when buyers dominated the cell, red when sellers
    if (dlt > 0) ctx.fillStyle = `rgba(38,166,154,${0.12 + t * 0.88})`;
    else if (dlt < 0) ctx.fillStyle = `rgba(239,83,80,${0.12 + t * 0.88})`;
    else ctx.fillStyle = `rgba(160,180,205,${0.10 + t * 0.7})`;
    ctx.fillRect(x, y, Math.max(cw, 1), Math.max(ch, 1));
  }

  // price axis
  ctx.fillStyle = '#0e131c';
  ctx.fillRect(w - padR, 0, padR, h);
  ctx.fillStyle = '#6b7a8f';
  ctx.font = '9px Consolas,monospace';
  const step = Math.max(1, Math.floor(nr / 7));
  for (let r = 0; r < nr; r += step) {
    const y = h - (r + 0.5) * ch;
    if (y < 8 || y > h - 2) continue;
    ctx.fillText(fmt(d.rows[r], d.tick_size < 1 ? 2 : 0), w - padR + 5, y + 3);
  }
  // time ticks
  const cstep = Math.max(1, Math.floor(nc / 6));
  for (let c = 0; c < nc; c += cstep) {
    ctx.fillText(dmy(d.cols[c]), padL + c * cw + 2, h - 3);
  }
}

/* ---------- footprint ---------- */
function drawFoot() {
  const { ctx, w, h } = prep($('foot'));
  const d = S.foot;
  if (!d || !d.bars || !d.bars.length) return note(ctx, w, h, 'no data');

  const bars = d.bars.slice(-26);
  const bw = Math.max(46, w / bars.length);
  let lo = Infinity, hi = -Infinity;
  for (const b of bars) for (const L of b.levels) { lo = Math.min(lo, L.price); hi = Math.max(hi, L.price); }
  if (!isFinite(lo)) return note(ctx, w, h, 'no levels');

  const tick = d.tick_size || 1;
  const rows = Math.max(1, Math.round((hi - lo) / tick) + 1);
  const rh = Math.min(15, Math.max(8, (h - 16) / rows));
  const yOf = (p) => h - 14 - (Math.round((p - lo) / tick) + 0.5) * rh;

  let peak = 1;
  for (const b of bars) for (const L of b.levels) peak = Math.max(peak, L.bid, L.ask);

  ctx.font = '9px Consolas,monospace';
  ctx.textBaseline = 'middle';

  bars.forEach((b, i) => {
    const x = i * bw;
    ctx.fillStyle = i % 2 ? '#0e131c' : '#101722';
    ctx.fillRect(x, 0, bw, h);

    for (const L of b.levels) {
      const y = yOf(L.price);
      if (y < 2 || y > h - 12) continue;
      const half = bw / 2 - 2;
      const bl = (L.bid / peak) * half;
      const al = (L.ask / peak) * half;

      ctx.fillStyle = 'rgba(239,83,80,.26)';
      ctx.fillRect(x + half - bl + 1, y - rh / 2 + 1, bl, rh - 2);
      ctx.fillStyle = 'rgba(38,166,154,.26)';
      ctx.fillRect(x + half + 2, y - rh / 2 + 1, al, rh - 2);

      if (L.poc) {
        ctx.strokeStyle = '#8b6fd4';
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 1.5, y - rh / 2 + 0.5, bw - 3, rh - 1);
      }
      if (rh >= 9) {
        ctx.fillStyle = '#93a3b8';
        ctx.textAlign = 'right';
        ctx.fillText(L.bid, x + half - 1, y);
        ctx.fillStyle = '#93a3b8';
        ctx.textAlign = 'left';
        ctx.fillText(L.ask, x + half + 4, y);
      }
      if (L.buy_imb) { ctx.fillStyle = '#26a69a'; ctx.textAlign = 'left'; ctx.fillText('▲', x + bw - 9, y); }
      if (L.sell_imb) { ctx.fillStyle = '#ef5350'; ctx.textAlign = 'left'; ctx.fillText('▼', x + bw - 9, y); }
    }

    ctx.textAlign = 'center';
    ctx.fillStyle = b.delta > 0 ? '#26a69a' : b.delta < 0 ? '#ef5350' : '#6b7a8f';
    ctx.fillText(sign(b.delta), x + bw / 2, h - 5);
  });
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
}

/* ---------- volume profile ---------- */
function drawProfilee() {
  const { ctx, w, h } = prep($('profile'));
  const d = S.profile;
  if (!d || !d.levels || !d.levels.length) return note(ctx, w, h, 'no data');

  const lv = d.levels;
  const peak = Math.max(...lv.map((x) => x.total)) || 1;
  const lo = lv[0].price, hi = lv[lv.length - 1].price;
  const rh = Math.max(1, (h - 10) / lv.length);
  const yOf = (p) => h - 5 - ((p - lo) / Math.max(hi - lo, 1e-9)) * (h - 10);

  for (const L of lv) {
    const y = yOf(L.price);
    const inVA = L.price >= d.val && L.price <= d.vah;
    const bw = (L.bid / peak) * (w - 42);
    const aw = (L.ask / peak) * (w - 42);
    ctx.fillStyle = inVA ? 'rgba(239,83,80,.65)' : 'rgba(239,83,80,.28)';
    ctx.fillRect(0, y - rh / 2, bw, Math.max(rh - 0.5, 1));
    ctx.fillStyle = inVA ? 'rgba(38,166,154,.65)' : 'rgba(38,166,154,.28)';
    ctx.fillRect(bw, y - rh / 2, aw, Math.max(rh - 0.5, 1));
  }

  ctx.font = '9px Consolas,monospace';
  for (const [price, color, label] of [[d.poc, '#8b6fd4', 'POC'], [d.vah, '#e0a33e', 'VAH'], [d.val, '#e0a33e', 'VAL']]) {
    if (price == null) continue;
    const y = yOf(price);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w - 40, y); ctx.stroke();
    ctx.fillStyle = color;
    ctx.fillText(`${label} ${fmt(price, d.tick_size < 1 ? 2 : 0)}`, 2, y - 2);
  }
}

function drawAll() { drawHeat(); drawFoot(); drawProfilee(); }

/* ---------- tables ---------- */
function renderLarge() {
  const d = S.large;
  const el = $('largeBody');
  if (!d || !d.trades || !d.trades.length) {
    el.innerHTML = '<div class="empty">No trades above threshold.</div>';
    return;
  }
  $('hdLarge').textContent = `threshold ${fmt(d.threshold)} (median ${fmt(d.median, 0)})`;
  const rows = d.trades.slice(0, 200).map((t) => `
    <tr><td>${dmy(t.ts)} ${hhmm(t.ts)}</td>
    <td class="${t.side === 'BUY' ? 'buy' : t.side === 'SELL' ? 'sell' : ''}">${t.side}</td>
    <td>${fmt(t.price, 2)}</td><td>${fmt(t.volume)}</td><td>×${t.multiple}</td></tr>`).join('');
  el.innerHTML = `<table><thead><tr><th>time</th><th>side</th><th>price</th><th>vol</th><th>mult</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderSweeps() {
  const d = S.sweeps;
  const el = $('sweepBody');
  if (!d || !d.sweeps || !d.sweeps.length) {
    el.innerHTML = '<div class="empty">No sweeps in this data.<br>A sweep needs a run of trades<br>in one direction.</div>';
    return;
  }
  $('hdSweep').textContent = `window ${d.window_ms}ms`;
  const rows = d.sweeps.slice(0, 150).map((s) => `
    <tr><td>${dmy(s.ts)} ${hhmm(s.ts)}</td>
    <td class="${s.side === 'BUY' ? 'buy' : 'sell'}">${s.side}</td>
    <td>${s.levels}</td><td>${fmt(s.volume)}</td><td>${sign(s.delta)}</td></tr>`).join('');
  el.innerHTML = `<table><thead><tr><th>time</th><th>side</th><th>lvls</th><th>vol</th><th>delta</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderSignals() {
  const b = S.bars;
  const el = $('sigList');
  if (!b || !b.ts) { el.innerHTML = '<div class="empty">—</div>'; return; }
  const n = b.ts.length;
  const absorption = (b.absorption || []).reduce((a, x) => a + x, 0);
  const ddiv = (b.delta_div || []).filter((x) => x !== 0).length;
  const cdiv = (b.cvd_div || []).filter((x) => x !== 0).length;
  const bear = (b.cvd_div || []).filter((x) => x === 1).length;
  const bull = (b.cvd_div || []).filter((x) => x === -1).length;
  const items = [
    ['bars', fmt(n), ''],
    ['absorption', fmt(absorption), 'w'],
    ['delta-div', fmt(ddiv), 'w'],
    ['CVD-div bear', fmt(bear), 'dn'],
    ['CVD-div bull', fmt(bull), 'up'],
    ['large ord.', S.large ? fmt(S.large.trades.length) : '—', ''],
    ['sweeps', S.sweeps ? fmt(S.sweeps.sweeps.length) : '—', ''],
  ];
  el.innerHTML = items.map(([k, v, c]) =>
    `<div class="sig"><span class="k">${k}</span><span class="v ${c}">${v}</span></div>`).join('');
  void cdiv;
}

/* ---------- load ---------- */
async function loadFiles() {
  const d = await api('/api/files');
  const list = $('fileList'), sel = $('fileSel');
  $('fileCount').textContent = d.files.length;
  if (!d.files.length) {
    list.innerHTML = '<div class="empty">No .scid files with data.<br><br>Put files in<br>D:\\tape-and-book\\data</div>';
    return;
  }
  list.innerHTML = d.files.map((f) => `
    <div class="file" data-n="${esc(f.name)}">
      <div class="n">${esc(f.name.replace(/\.scid$/i, ''))}</div>
      <div class="m">${fmt(f.records)} rek. · ${f.size_mb} MB</div>
    </div>`).join('');
  sel.innerHTML = d.files.map((f) => `<option>${esc(f.name)}</option>`).join('');
  list.querySelectorAll('.file').forEach((el) =>
    el.addEventListener('click', () => { sel.value = el.dataset.n; select(el.dataset.n); }));
  select(d.files[0].name);
}

function markSelected() {
  document.querySelectorAll('.file').forEach((el) =>
    el.classList.toggle('sel', el.dataset.n === S.file));
}

async function select(name) {
  S.file = name;
  markSelected();
  await refresh();
}

async function refresh() {
  if (!S.file) return;
  S.tf = $('tfSel').value;
  $('hdChart').textContent = `${S.file.replace(/\.scid$/i, '')} · ${S.tf}`;
  toast('computing order flow…', 60000);

  try {
    const [bars, heat, foot, prof, large, sw] = await Promise.all([
      api('/api/bars', { tf: S.tf }),
      api('/api/heatmap', { tf: S.tf }),
      api('/api/footprint', { tf: S.tf, bars: 40 }),
      api('/api/profile'),
      api('/api/large', { top: 200 }),
      api('/api/sweeps', { top: 150 }),
    ]);
    S.bars = bars; S.heat = heat; S.foot = foot;
    S.profile = prof; S.large = large; S.sweeps = sw;

    const cd = bars.ts.map((t, i) => ({
      time: t, open: bars.open[i], high: bars.high[i], low: bars.low[i], close: bars.close[i],
    }));
    candleSeries.setData(cd);
    vwapSeries.setData(bars.ts.map((t, i) => ({ time: t, value: bars.vwap[i] })));
    deltaSeries.setData(bars.ts.map((t, i) => ({
      time: t, value: bars.delta[i],
      color: bars.delta[i] >= 0 ? 'rgba(38,166,154,.55)' : 'rgba(239,83,80,.55)',
    })));
    cvdSeries.setData(bars.ts.map((t, i) => ({ time: t, value: bars.cvd[i] })));

    // mark the reads worth looking at directly on the price chart
    const marks = [];
    bars.ts.forEach((t, i) => {
      if (bars.absorption[i]) marks.push({ time: t, position: 'aboveBar', color: '#e0a33e', shape: 'circle', text: 'A' });
      if (bars.cvd_div[i] === 1) marks.push({ time: t, position: 'aboveBar', color: '#ef5350', shape: 'arrowDown', text: 'div' });
      if (bars.cvd_div[i] === -1) marks.push({ time: t, position: 'belowBar', color: '#26a69a', shape: 'arrowUp', text: 'div' });
    });
    candleSeries.setMarkers(marks.slice(-260));
    priceChart.timeScale().fitContent();

    const last = bars.close[bars.close.length - 1];
    const cvd = bars.cvd[bars.cvd.length - 1];
    const dl = bars.delta[bars.delta.length - 1];
    $('sLast').textContent = fmt(last, 2);
    $('sCvd').textContent = sign(cvd);
    $('sCvd').className = cvd >= 0 ? 'up' : 'dn';
    $('sDelta').textContent = sign(dl);
    $('sDelta').className = dl >= 0 ? 'up' : 'dn';
    $('sPoc').textContent = prof.poc != null ? fmt(prof.poc, 2) : '—';
    $('sTick').textContent = foot.tick_size ?? '—';
    $('hdFoot').textContent = `${foot.bars.length} bars · tick ${foot.tick_size}`;
    $('hdHeat').textContent = heat.kind === 'liquidity'
      ? 'resting book liquidity' : 'traded volume';

    renderLarge(); renderSweeps(); renderSignals(); drawAll();
    toast('done', 900);
  } catch (e) {
    toast('error: ' + e.message, 6000);
    console.error(e);
  }
}

/* ---------- boot ---------- */
initCharts();
$('tfSel').addEventListener('change', refresh);
$('fileSel').addEventListener('change', (e) => select(e.target.value));
$('btnReload').addEventListener('click', refresh);
$('btnHeat').addEventListener('click', (e) => {
  S.showHeat = !S.showHeat;
  e.target.classList.toggle('on', S.showHeat);
  drawHeat();
});
loadFiles().catch((e) => toast('error: ' + e.message, 8000));
