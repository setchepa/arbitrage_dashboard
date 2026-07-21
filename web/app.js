/* ============================================================
   CLP <-> USD Arbitrage Dashboard — client logic
   - fetches live rates + Buda order book from /api/rates
   - runs a slippage-aware greedy optimizer in the browser
   - recomputes instantly when any sidebar control changes
   ============================================================ */

// ---- state ----
// Base scenario: every new session starts here (budget 5,000,000 CLP, Buda fee
// 0.30%, USDC->USD at the 1.0 peg -> sell venue reads Robinhood).
const state = {
  theme: 'light',
  budget: 5000000,
  budaFee: 0.30,
  usdcRate: 1.0,
  openCard: null,
  showFlow: true,
  paramsOpen: false,  // mobile-only: parameters sheet open?
  cards: [
    { name: 'Fidelity',   network: 'Visa',       cashback: 2.0, capOn: true,  cap: 5000000, dot: '#1F7B2C' },
    { name: 'CapitalOne', network: 'Mastercard', cashback: 1.5, capOn: false, cap: 5000000, dot: '#CF2128' },
    { name: 'Chase',      network: 'Visa',       cashback: 1.0, capOn: false, cap: 5000000, dot: '#0052AC' },
  ],
};

let market = null; // { visa_fx, mc_fx, buda_best_ask, buda_asks, ... }
let flowCardsSig = null; // guards the Step-1 rebuild so logos don't flicker
let lastResult = null;   // current on-screen figures, for the Executed button

// ---- logos (drop matching PNGs into web/logos/; missing files fall back to
//      the empty "logo" placeholder automatically) ----
const CARD_LOGOS = {
  Fidelity: 'logos/fidelity.png',
  CapitalOne: 'logos/capitalone.png',
  Chase: 'logos/chase.png',
};
const CARD_DOTS = { Fidelity: '#1F7B2C', CapitalOne: '#CF2128', Chase: '#0052AC' };
// Sell venue (Step 3) depends on the peg: exactly $1.00 -> Robinhood (sells at
// peg); any other USDC->USD rate implies selling via Binance instead.
function sellVenue() {
  return Math.abs(state.usdcRate - 1) < 1e-9
    ? { name: 'Robinhood', logo: 'logos/robinhood.png' }
    : { name: 'Binance', logo: 'logos/binance.png' };
}
// Exchange node — swap to Binance later by changing this one object.
const EXCHANGE = { name: 'Buda', logo: 'logos/buda.png' };
// const EXCHANGE = { name: 'Binance', logo: 'logos/binance.png' };  // future

function setLogo(imgId, slotId, src) {
  const img = $(imgId), slot = $(slotId);
  if (!src) { slot.classList.add('empty'); img.removeAttribute('src'); return; }
  img.onload = () => slot.classList.remove('empty');
  img.onerror = () => slot.classList.add('empty');
  img.src = src;
}

// ---- helpers ----
const $ = (id) => document.getElementById(id);
const clp = (n) => Math.round(n).toLocaleString('en-US');
const usd = (n) => (n < 0 ? '−$' : '$') + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const rate = (n, d = 2) => n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const DASH = '—';

// ============================================================
//  OPTIMIZER  (1:1 port of optimizer.py optimize())
// ============================================================
function optimize() {
  if (!market) return null;
  const { visa_fx, mc_fx, buda_asks } = market;
  const feeMult = 1 - state.budaFee / 100;
  const usdcUsd = state.usdcRate;

  const cards = state.cards.map((c) => {
    const fx = c.network === 'Visa' ? visa_fx : mc_fx;
    const cb = c.cashback / 100;
    return {
      name: c.name, network: c.network, fx, cashback: cb,
      cap: c.capOn ? c.cap : Infinity,
      costCoeff: (1 - cb) / fx,
    };
  });

  const ordered = [...cards].sort((a, b) => a.costCoeff - b.costCoeff);
  const allocs = {};
  cards.forEach((c) => { allocs[c.name] = { card: c.name, clp: 0, usdc: 0, usdcGross: 0, usdCharged: 0, cashbackUsd: 0 }; });

  const book = buda_asks.map(([p, s]) => [p, s]);
  let lvl = 0;
  let budgetLeft = state.budget;

  for (const card of ordered) {
    let capLeft = card.cap;
    let unprofitable = false;
    while (budgetLeft > 1e-9 && capLeft > 1e-9 && lvl < book.length) {
      const [price, size] = book[lvl];
      if (size <= 1e-12) { lvl++; continue; }
      const revCoeff = (feeMult / price) * usdcUsd;
      if (revCoeff <= card.costCoeff) { unprofitable = true; break; }
      const levelClpCap = price * size;
      const take = Math.min(levelClpCap, capLeft, budgetLeft);
      const takeUsdcGross = take / price;              // before the Buda fee
      const takeUsdc = takeUsdcGross * feeMult;        // after it
      const a = allocs[card.name];
      a.clp += take; a.usdc += takeUsdc; a.usdcGross += takeUsdcGross;
      a.usdCharged += take / card.fx;
      a.cashbackUsd += (take / card.fx) * card.cashback;
      book[lvl][1] -= take / price;
      capLeft -= take; budgetLeft -= take;
    }
    if (unprofitable) break; // best remaining card unprofitable -> stop
  }

  // finalize
  const tot = { clp: 0, usdCharged: 0, cashback: 0, usdc: 0, usdcGross: 0, sale: 0, profit: 0 };
  const rows = cards.map((c) => {
    const a = allocs[c.name];
    const sale = a.usdc * usdcUsd;
    const profit = sale + a.cashbackUsd - a.usdCharged;
    tot.clp += a.clp; tot.usdCharged += a.usdCharged; tot.cashback += a.cashbackUsd;
    tot.usdc += a.usdc; tot.usdcGross += a.usdcGross; tot.sale += sale; tot.profit += profit;
    return { ...c, ...a, sale, profit };
  });

  const premium = tot.usdc * (usdcUsd - 1);
  return {
    rows,
    totals: tot,
    premium,
    base: tot.usdc * 1.0,
    roi: tot.usdCharged > 1e-9 ? (tot.profit / tot.usdCharged) * 100 : 0,
    profitable: tot.profit > 0,
    // VWAP excludes the Buda fee — it's the pure order-book average price. The
    // fee is shown as its own line in the income breakdown.
    vwap: tot.usdcGross > 1e-9 ? tot.clp / tot.usdcGross : null,
    feeUsdc: tot.usdcGross - tot.usdc,
  };
}

/**
 * Volume-weighted average price for buying `clpAmount` of CLP on Buda, walking
 * the live ask book — the pure order-book average CLP/USDC for that size.
 *
 * Excludes the Buda fee (that's a separate line in the income breakdown), so
 * this matches the buy-leg rate shown there. Used as the rate-card fallback
 * when the optimizer deploys nothing, so the card still shows a live number.
 */
function vwapForBudget(clpAmount) {
  if (!market || !(clpAmount > 0)) return null;
  let left = clpAmount, usdc = 0;
  for (const [price, size] of market.buda_asks) {
    if (left <= 1e-9) break;
    const take = Math.min(price * size, left);
    usdc += take / price;
    left -= take;
  }
  const spent = clpAmount - left;
  return usdc > 1e-9 ? spent / usdc : null;
}

// ============================================================
//  RENDER
// ============================================================
function render() {
  const r = optimize();

  // rate cards
  if (market) {
    $('visaFig').textContent = rate(market.visa_fx);
    $('mcFig').textContent = rate(market.mc_fx);
    // VWAP for the deployed size — the same figure the income breakdown shows.
    // Falls back to the VWAP over the full budget when nothing is deployed.
    const vwap = (r && r.vwap) || vwapForBudget(state.budget);
    $('budaFig').textContent = vwap ? rate(vwap) : DASH;
    $('budaUnit').textContent = `CLP / USDC · ${market.buda_levels} levels`;
    $('asOf').innerHTML = `Visa as of ${market.visa_date} · Mastercard as of ${market.mc_date}`;
  }
  if (!r) return;

  // loop-flow strip
  $('flowCard').style.display = state.showFlow ? '' : 'none';

  // Step 1 — show EVERY card the optimizer uses (Fidelity first; others appear
  // once the budget exceeds Fidelity's cap), each with its own logo + spend.
  const usedCards = r.rows.filter((x) => x.clp > 0)
    .sort((a, b) => (b.clp - a.clp) || (a.costCoeff - b.costCoeff));

  // render() runs every second for live data. Rebuilding this block's innerHTML
  // would recreate the <img> tags each tick, so the logos would visibly flicker
  // back to their placeholder while reloading. Only rebuild when the allocation
  // actually changes.
  const cardsSig = usedCards.map((c) => `${c.name}:${Math.round(c.clp)}`).join('|') || 'none';
  if (cardsSig !== flowCardsSig) {
    flowCardsSig = cardsSig;
    $('flowCards').innerHTML = usedCards.length
    ? usedCards.map((c) => {
      const src = CARD_LOGOS[c.name];
      const img = src
        ? `<img alt="" src="${src}" onload="this.parentElement.classList.remove('empty')" onerror="this.parentElement.classList.add('empty')" />`
        : '';
      const dot = CARD_DOTS[c.name]
        ? `<span class="brand-dot" style="background:${CARD_DOTS[c.name]}"></span>` : '';
      return `<div class="card-item">
          <div class="logo-slot empty">${img}</div>
          <div class="brand">${c.name}</div>${dot}
          <div class="fig mono">${clp(c.clp)} CLP</div>
        </div>`;
    }).join('')
    : `<div class="card-item">
          <div class="logo-slot empty"><img alt="" src="logos/sadface.png" onload="this.parentElement.classList.remove('empty')" onerror="this.parentElement.classList.add('empty')" /></div>
          <div class="brand">Unprofitable</div>
          <div class="fig mono">${DASH}</div>
        </div>`;
  }

  $('flowExchName').textContent = EXCHANGE.name;
  // Step 2 shows no rate — the Buda VWAP lives in its own rate card.
  $('flowRhFig').textContent = `@ $${rate(state.usdcRate, 4)}`;
  // sell venue depends on the peg (Robinhood @ exactly $1, else Binance)
  const venue = sellVenue();
  $('flowSellName').textContent = venue.name;
  $('subtitleVenue').textContent = venue.name;
  $('rateVenue').textContent = venue.name;
  // logos (fall back to placeholder if file missing)
  setLogo('logoExch', 'logoSlotExch', EXCHANGE.logo);
  setLogo('logoSell', 'logoSlotSell', venue.logo);

  // banner
  const banner = $('banner');
  const badge = $('bannerBadge');
  if (r.profitable) {
    banner.className = 'banner good';
    badge.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    $('bannerMsg').innerHTML = `Profitable — deploy <span class="mono">${clp(r.totals.clp)}</span> CLP for <span class="mono">${usd(r.totals.profit)}</span> profit (<span class="mono">${rate(r.roi, 3)}%</span> ROI)`;
  } else {
    banner.className = 'banner bad';
    badge.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
    $('bannerMsg').textContent = 'Not profitable right now — best action is to deploy nothing.';
  }

  // income breakdown
  const premClass = r.premium > 0 ? 'pos' : (r.premium < 0 ? 'neg' : '');
  const qty = (n) => n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // One row per card actually charged, so it's clear which cards are being paid.
  const chargeRows = r.rows.filter((c) => c.clp > 0.5)
    .sort((a, b) => b.usdCharged - a.usdCharged)
    .map((c) => `<tr><td><span class="mute">— Less: USD charged to ${c.name} (${c.network})</span></td><td class="num">${usd(-c.usdCharged)}</td></tr>`)
    .join('')
    || `<tr><td><span class="mute">— Less: USD charged to cards</span></td><td class="num">${usd(0)}</td></tr>`;

  $('incomeBody').innerHTML = `
    <tr><td>Buy USDC on ${EXCHANGE.name} — ${clp(r.totals.clp)} CLP${r.vwap ? ` @ ${rate(r.vwap)}` : ''}</td><td class="num mute">${qty(r.totals.usdcGross)} USDC</td></tr>
    <tr><td><span class="mute">— Less: ${EXCHANGE.name} fee (${state.budaFee.toFixed(2)}%)</span></td><td class="num neg">−${qty(r.feeUsdc)} USDC</td></tr>
    <tr><td>Sell ${qty(r.totals.usdc)} USDC on ${venue.name} @ $1.00 (base)</td><td class="num">${usd(r.base)}</td></tr>
    <tr><td>USDC premium (USDC @ $${rate(state.usdcRate, 4)} vs $1.00)</td><td class="num ${premClass}">${usd(r.premium)}</td></tr>
    <tr><td>Credit-card cashback</td><td class="num pos">${usd(r.totals.cashback)}</td></tr>
    ${chargeRows}
    <tr class="net-row"><td>= Net profit</td><td class="num">${usd(r.totals.profit)}</td></tr>`;
  if (r.premium > 0) $('incomeNote').innerHTML = `USDC is trading <b>above</b> peg ($${rate(state.usdcRate, 4)}), adding <span class="mono">${usd(r.premium)}</span> of premium income on <span class="mono">${r.totals.usdc.toFixed(2)}</span> USDC.`;
  else if (r.premium < 0) $('incomeNote').innerHTML = `USDC is trading <b>below</b> peg ($${rate(state.usdcRate, 4)}), costing <span class="mono">${usd(-r.premium)}</span> vs a $1.00 sale.`;
  else $('incomeNote').textContent = 'USDC assumed exactly at the $1.00 peg — no premium income.';

  // loop totals
  const cells = [
    { lbl: 'CLP deployed', val: clp(r.totals.clp), cls: '' },
    { lbl: 'USD charged to cards', val: usd(r.totals.usdCharged), cls: '' },
    { lbl: 'Cashback earned', val: usd(r.totals.cashback), cls: 'pos' },
    { lbl: 'USDC premium income', val: usd(r.premium), cls: premClass },
    { lbl: 'Net profit', val: usd(r.totals.profit), cls: 'accent', roi: r.profitable ? `↑ ${rate(r.roi, 3)}% ROI` : null },
  ];
  $('totals').innerHTML = cells.map((c) => `
    <div class="total-cell">
      <div class="lbl">${c.lbl}</div>
      <div class="val ${c.cls}">${c.val}</div>
      ${c.roi ? `<span class="roi-pill">${c.roi}</span>` : ''}
    </div>`).join('');

  $('budaEffNote').textContent = r.vwap
    ? `Buda VWAP for ${clp(r.totals.clp)} CLP: ${rate(r.vwap)} CLP/USDC — the order-book average, before the ${state.budaFee.toFixed(2)}% fee (top-of-book ask ${rate(market.buda_best_ask)}).`
    : '';

  // Snapshot the current on-screen figures for the Executed button.
  lastResult = {
    visa: market.visa_fx,
    mc: market.mc_fx,
    buda: market.buda_best_ask,
    net_profit: r.totals.profit,
    roi: r.roi,
  };
}

// ============================================================
//  SIDEBAR : accordion + inputs
// ============================================================
function buildAccordion() {
  $('accordion').innerHTML = state.cards.map((c, i) => `
    <div class="acc-item ${state.openCard === i ? 'open' : ''}" data-idx="${i}">
      <button class="acc-head">
        <span class="brand-dot" style="background:${c.dot}"></span>
        ${c.name} <span class="mute" style="font-weight:400">(${c.network})</span>
        <span class="chev">›</span>
      </button>
      <div class="acc-body">
        <div class="acc-row"><span>Network</span><span>${c.network}</span></div>
        <div class="acc-row"><span>Cashback %</span><input class="mini mono" data-card="${i}" data-k="cashback" value="${c.cashback.toFixed(1)}" /></div>
        <div class="acc-row"><span>Cap (CLP)</span><input class="mini mono" data-card="${i}" data-k="cap" value="${c.capOn ? c.cap : ''}" placeholder="none" /></div>
      </div>
    </div>`).join('');

  $('accordion').querySelectorAll('.acc-head').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = +btn.closest('.acc-item').dataset.idx;
      state.openCard = state.openCard === idx ? null : idx;
      buildAccordion();
    });
  });
  $('accordion').querySelectorAll('input[data-card]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const c = state.cards[+inp.dataset.card];
      const k = inp.dataset.k;
      if (k === 'cashback') c.cashback = parseFloat(inp.value) || 0;
      if (k === 'cap') {
        const v = parseFloat(inp.value.replace(/[, ]/g, ''));
        if (isNaN(v) || inp.value.trim() === '') { c.capOn = false; }
        else { c.capOn = true; c.cap = v; }
      }
      render();
    });
    inp.addEventListener('click', (e) => e.stopPropagation());
  });
}

const STEPS = { budget: 1000000, budaFee: 0.05, usdcRate: 0.001 };
function fmtField(k) {
  if (k === 'budget') return clp(state.budget);
  if (k === 'budaFee') return state.budaFee.toFixed(2);
  if (k === 'usdcRate') return state.usdcRate.toFixed(4);
}
function syncFields() { ['budget', 'budaFee', 'usdcRate'].forEach((k) => { $(k).value = fmtField(k); }); }

function bindInputs() {
  ['budget', 'budaFee', 'usdcRate'].forEach((k) => {
    const el = $(k);
    el.addEventListener('input', () => {
      const v = parseFloat(el.value.replace(/[, ]/g, ''));
      if (!isNaN(v)) { state[k] = v; render(); }
    });
    el.addEventListener('blur', () => { syncFields(); });
  });
  document.querySelectorAll('button[data-step]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const k = btn.dataset.step; const dir = +btn.dataset.dir;
      state[k] = Math.max(0, state[k] + dir * STEPS[k]);
      if (k === 'budaFee') state[k] = Math.round(state[k] * 100) / 100;
      if (k === 'usdcRate') state[k] = Math.round(state[k] * 10000) / 10000;
      syncFields(); render();
    });
  });
}

// ---- theme ----
const MOON_ICON = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
const SUN_ICON = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';

function applyTheme() {
  document.documentElement.setAttribute('data-theme', state.theme);
  $('themeLabel').textContent = state.theme === 'light' ? 'Dark mode' : 'Light mode';
  // mobile header: icon-only toggle (moon = switch to dark, sun = switch to light)
  $('mThemeToggle').innerHTML = state.theme === 'light' ? MOON_ICON : SUN_ICON;
}
function toggleTheme() {
  state.theme = state.theme === 'light' ? 'dark' : 'light';
  applyTheme();
}
$('themeToggle').addEventListener('click', toggleTheme);
$('mThemeToggle').addEventListener('click', toggleTheme);

// ---- mobile: parameters sheet (gear) ----
$('gearBtn').addEventListener('click', () => {
  state.paramsOpen = !state.paramsOpen;
  document.querySelector('.app').classList.toggle('params-open', state.paramsOpen);
});

// ---- Executed button: click -> "Sure?" Yes/No -> POST executed=1 ----
function showExecConfirm(show) {
  $('execBtn').hidden = show;
  $('execConfirm').hidden = !show;
}
function execMsg(text, cls) {
  const el = $('execMsg');
  el.textContent = text; el.className = 'exec-msg ' + cls; el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 4000);
}

$('execBtn').addEventListener('click', () => showExecConfirm(true));
$('execNo').addEventListener('click', () => showExecConfirm(false));  // No -> nothing happens
$('execYes').addEventListener('click', async () => {
  if (!lastResult) return;
  showExecConfirm(false);
  $('execBtn').disabled = true;
  try {
    const res = await fetch('/api/executed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(lastResult),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'save failed');
    execMsg('✓ Executed trade saved', 'ok');
  } catch (e) {
    execMsg('✗ ' + e.message, 'err');
  } finally {
    $('execBtn').disabled = false;
  }
});

// ============================================================
//  LIVE DATA
//  Buda is the only source that moves intraday, so it's polled ~1/second.
//  Visa/Mastercard publish daily rates — polling those per second would be
//  pointless and would get us rate-limited by Cloudflare/Akamai, so they're
//  refreshed on load and then every CARD_RATE_INTERVAL.
// ============================================================
const BUDA_POLL_MS = 1000;
const CARD_RATE_INTERVAL_MS = 10 * 60 * 1000;   // 10 min

let liveTimer = null;

async function fetchRates() {
  try {
    const res = await fetch('/api/rates');
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'fetch failed');
    market = data;
    render();
  } catch (e) {
    if (!market) {            // only surface if we have nothing to show
      $('bannerMsg').textContent = 'Failed to fetch live rates: ' + e.message;
      $('banner').className = 'banner bad';
    }
  }
}

async function pollBuda() {
  if (!market) return;                       // wait for the first full load
  if (document.hidden) return;               // don't poll a background tab
  try {
    const res = await fetch('/api/buda');
    const data = await res.json();
    if (!data.ok) return;                    // keep last good data on failure
    market.buda_asks = data.buda_asks;
    market.buda_best_ask = data.buda_best_ask;
    market.buda_levels = data.buda_levels;
    render();
  } catch (e) {
    /* transient — keep showing the last good values */
  }
}

function startLive() {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = setInterval(pollBuda, BUDA_POLL_MS);
  setInterval(fetchRates, CARD_RATE_INTERVAL_MS);
  // Catch up immediately when the tab becomes visible again.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pollBuda(); });
}

// ---- init ----
applyTheme();
syncFields();
buildAccordion();
bindInputs();
fetchRates().then(startLive);

