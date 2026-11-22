/* Perps tab: BTC perpetual (KXBTCPERP) market data, order book, order ticket, positions. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var K = window.KBot;
  var TICKER = 'KXBTCPERP';
  var f = function (v) { var n = parseFloat(v); return isFinite(n) ? n : NaN; };

  var pmode = K.storage.get('kbot_pmode') || 'paper';
  var paper = new PaperPerps(K.storage);
  var st = { dir: 1, market: null, book: null, funding: null, fundingAt: 0, live: null, liveErr: '', enabledChecked: {}, tab: 'markets' };

  function env() { return pmode === 'demo' ? 'demo' : 'prod'; }
  function client() { return new Api.Perps(K.transport, env()); }
  function contractSize() { return st.market ? f(st.market.contract_size) || 0.0001 : 0.0001; }
  function toBtc(px) { return px / contractSize(); }
  function bestAsk() { return st.book && st.book.asks.length ? st.book.asks[0][0] : st.market ? f(st.market.ask) : NaN; }
  function bestBid() { return st.book && st.book.bids.length ? st.book.bids[0][0] : st.market ? f(st.market.bid) : NaN; }
  function mid() { var a = bestAsk(), b = bestBid(); return isFinite(a) && isFinite(b) ? (a + b) / 2 : f(st.market && st.market.price); }
  function maxLev(dir) {
    if (!st.market) return 5;
    var m = dir > 0 ? st.market.long_leverage_estimates : st.market.short_leverage_estimates;
    return (m && f(m['1000'])) || f(st.market.leverage_estimate) || 5;
  }
  function usd(v, d) { return isFinite(v) ? (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d }) : '–'; }

  // ---------- data
  async function refreshMarket() {
    var c = new Api.Perps(K.transport, pmode === 'demo' ? 'demo' : 'prod');
    var res = await Promise.all([c.market(TICKER), c.orderbook(TICKER, 6)]);
    st.market = res[0];
    var ob = res[1] || {};
    st.book = {
      asks: (ob.asks || []).map(function (r) { return [f(r[0]), f(r[1])]; }).sort(function (a, b) { return a[0] - b[0]; }),
      bids: (ob.bids || []).map(function (r) { return [f(r[0]), f(r[1])]; }).sort(function (a, b) { return b[0] - a[0]; })
    };
    if (Date.now() - st.fundingAt > 60000) { st.funding = await c.funding(TICKER); st.fundingAt = Date.now(); }
    var m = mid();
    paper.mark(TICKER, m).forEach(function (e) {
      K.log('PERP PAPER ' + e.reason.toUpperCase() + ' ' + TICKER + ' ' + (e.pnl >= 0 ? '+' : '') + usd(e.pnl));
    });
  }

  async function refreshAccount() {
    if (pmode === 'paper') { st.live = null; return; }
    if (!K.hasKey(env())) { st.live = null; st.liveErr = 'No ' + env() + ' API key saved (Settings → API keys).'; return; }
    try {
      var c = client();
      if (!st.enabledChecked[env()]) { st.enabled = await c.enabled(); st.enabledChecked[env()] = true; }
      var res = await Promise.all([c.balance(), c.positions(), c.orders(TICKER)]);
      st.live = { balance: res[0], positions: res[1], orders: res[2] };
      st.liveErr = '';
    } catch (e) { st.liveErr = e.message; st.live = null; }
  }

  var busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try { await refreshMarket(); if (st.tab === 'perps') await refreshAccount(); }
    catch (e) { K.log('Perps data: ' + e.message); }
    busy = false;
    render();
  }

  // ---------- render
  function render() {
    document.querySelectorAll('#perps-mode-seg button').forEach(function (b) { b.classList.toggle('on', b.dataset.pmode === pmode); });
    var m = st.market;
    if (m) {
      var px = mid();
      $('pp-price').textContent = usd(px, 4);
      $('pp-btc').textContent = '≈ ' + usd(toBtc(px), 0) + ' / BTC';
      $('pp-contract').textContent = m.title || '';
      $('pp-lev').textContent = maxLev(1).toFixed(1) + 'x / ' + maxLev(-1).toFixed(1) + 'x';
      $('pp-vol').textContent = '$' + (f(m.volume_24h_notional_value_dollars) / 1e6).toFixed(0) + 'M';
    }
    if (st.funding) {
      var fr = f(st.funding.funding_rate) * 100;
      $('pp-funding').textContent = (fr >= 0 ? '+' : '') + fr.toFixed(4) + '%';
      $('pp-funding').className = fr > 0 ? 'down' : fr < 0 ? 'up' : '';
      var s = Math.max(0, Math.round((Date.parse(st.funding.next_funding_time) - Date.now()) / 1000));
      $('pp-next').textContent = Math.floor(s / 3600) + 'h ' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + 'm';
    }
    renderBook();
    renderTicket();
    renderPositions();
    renderSignal();
  }

  function renderBook() {
    if (!st.book) return;
    var max = 1;
    st.book.asks.concat(st.book.bids).forEach(function (r) { max = Math.max(max, r[1]); });
    var row = function (r, cls) {
      return '<div class="brow ' + cls + '"><i style="width:' + (100 * r[1] / max).toFixed(0) + '%"></i><span>' + r[0].toFixed(4) +
        '</span><span>' + r[1].toLocaleString() + '</span></div>';
    };
    $('book-asks').innerHTML = st.book.asks.slice(0, 6).reverse().map(function (r) { return row(r, 'ask'); }).join('');
    $('book-bids').innerHTML = st.book.bids.slice(0, 6).map(function (r) { return row(r, 'bid'); }).join('');
    var a = bestAsk(), b = bestBid();
    $('book-mid').textContent = isFinite(a) && isFinite(b) ? usd(toBtc((a + b) / 2), 0) + ' · spread ' + ((a - b) / ((a + b) / 2) * 1e4).toFixed(1) + 'bp' : '–';
  }

  function ticket() {
    var price = st.dir > 0 ? bestAsk() : bestBid();
    var margin = f($('t-margin').value), lev = f($('t-lev').value);
    var qty = price > 0 ? Math.floor((margin * lev) / price) : 0;
    var tp = f($('t-tp').value), sl = f($('t-sl').value);
    var cs = contractSize();
    var probe = qty > 0 ? PaperPerps.liqPrice({ qty: st.dir * qty, entry: price, margin: margin }) : NaN;
    return {
      price: price, margin: margin, lev: lev, qty: qty, notional: qty * price, fee: qty * price * PaperPerps.FEE,
      tp: isFinite(tp) ? tp * cs : null, sl: isFinite(sl) ? sl * cs : null, liq: probe
    };
  }

  function renderTicket() {
    var lev = $('t-lev'), mx = Math.floor(maxLev(st.dir) * 10) / 10;
    lev.max = String(mx);
    if (f(lev.value) > mx) lev.value = String(mx);
    $('t-lev-val').textContent = f(lev.value).toFixed(1) + 'x';
    $('t-long').classList.toggle('on', st.dir > 0);
    $('t-short').classList.toggle('on', st.dir < 0);
    var t = ticket();
    var move = t.qty ? Math.abs(t.liq / t.price - 1) * 100 : NaN;
    $('t-sum').innerHTML =
      'Contracts <b>' + (t.qty || 0).toLocaleString() + '</b><br>Position size <b>' + usd(t.notional) + '</b><br>' +
      'Entry ≈ <b>' + usd(toBtc(t.price), 0) + '</b><br>' +
      (pmode === 'paper' ? 'Liquidation ≈ <b>' + usd(toBtc(t.liq), 0) + ' (' + (isFinite(move) ? move.toFixed(2) : '–') + '%)</b><br>' : '') +
      'Est. fee <b>' + usd(t.fee) + '</b>';
    var go = $('t-go');
    go.textContent = (st.dir > 0 ? 'Long ' : 'Short ') + (t.qty || 0) + ' @ ' + usd(toBtc(t.price), 0) + (pmode === 'paper' ? ' (paper)' : pmode === 'demo' ? ' (demo)' : ' (REAL $)');
    go.className = 'btn ' + (st.dir > 0 ? 'go-long' : 'go-short');
    go.disabled = !(t.qty >= 1);
  }

  function renderPositions() {
    var acct = $('pp-account'), pos = $('pp-positions'), hist = $('pp-history');
    if (pmode === 'paper') {
      var realized = paper.state.history.reduce(function (s, h) { return s + h.pnl; }, 0);
      acct.innerHTML = cell('Equity', usd(paper.equity())) + cell('Cash', usd(paper.state.cash)) +
        cell('Realized P&L', usd(realized), realized >= 0 ? 'up' : 'down') + cell('Trades', paper.state.history.length);
      var ps = Object.keys(paper.state.positions).map(function (k) { return paper.state.positions[k]; });
      pos.innerHTML = ps.length ? ps.map(function (p) {
        var roe = (p.upnl || 0) / p.margin * 100;
        return card(p.qty > 0, Math.abs(p.qty) + ' × ' + TICKER, p.upnl || 0, roe,
          'Entry ' + usd(toBtc(p.entry), 0) + ' · mark ' + usd(toBtc(p.mark || p.entry), 0) + ' · ' + p.leverage.toFixed(1) + 'x',
          'Liq ' + usd(toBtc(p.liq), 0) + (p.tp ? ' · TP ' + usd(toBtc(p.tp), 0) : '') + (p.sl ? ' · SL ' + usd(toBtc(p.sl), 0) : ''), p.ticker);
      }).join('') : '<div class="empty">No open perp positions</div>';
      hist.innerHTML = paper.state.history.length ? paper.state.history.slice(0, 15).map(function (h) {
        return '<div class="card"><div class="t"><span class="side ' + (h.qty > 0 ? 'yes">LONG' : 'no">SHORT') + '</span>' + Math.abs(h.qty) + ' × ' + h.ticker +
          '</div><div class="v ' + (h.pnl >= 0 ? 'up' : 'down') + '">' + usd(h.pnl) + '</div><div class="s">' + usd(toBtc(h.entry), 0) + ' → ' + usd(toBtc(h.exit), 0) +
          '</div><div class="s">' + h.reason + '</div></div>';
      }).join('') : '<div class="empty">No closed trades yet</div>';
    } else {
      if (!st.live) {
        acct.innerHTML = '<div style="grid-column:1/-1">' + (st.liveErr || 'Loading account…') + '</div>';
        pos.innerHTML = ''; hist.innerHTML = '<div class="empty">See trade history in the Kalshi app</div>';
        return;
      }
      var b = st.live.balance || {};
      var sb = (b.subaccount_balances && b.subaccount_balances[0]) || b;
      acct.innerHTML = cell('Equity', usd(f(sb.account_equity))) + cell('Available', usd(f(sb.available_balance))) +
        cell('Initial margin', usd(f(sb.initial_margin))) + cell('Maint. margin', usd(f(sb.maintenance_margin)));
      var lp = st.live.positions.filter(function (p) { return f(p.position) !== 0; });
      pos.innerHTML = lp.length ? lp.map(function (p) {
        var q = f(p.position);
        return card(q > 0, Math.abs(q) + ' × ' + p.market_ticker, f(p.unrealized_pnl), f(p.roe) * 100,
          'Entry ' + usd(toBtc(f(p.entry_price)), 0) + ' · margin ' + usd(f(p.margin_used)), (p.subaccount_type || '') + ' margin', p.market_ticker);
      }).join('') : '<div class="empty">No open perp positions</div>';
      hist.innerHTML = st.live.orders.length ? st.live.orders.map(function (o) {
        return '<div class="card"><div class="t">Resting ' + o.side + ' ' + (o.remaining_count || o.count || '') + '</div><div class="v">' + usd(f(o.price), 4) +
          '</div><button class="act" data-cancel="' + o.order_id + '">Cancel</button></div>';
      }).join('') : '<div class="empty">No resting orders</div>';
    }
    pos.querySelectorAll('[data-close]').forEach(function (b) { b.onclick = function () { closePos(b.dataset.close); }; });
    hist.querySelectorAll('[data-cancel]').forEach(function (b) {
      b.onclick = function () { client().cancelOrder(b.dataset.cancel).then(refresh).catch(function (e) { alert(e.message); }); };
    });
  }

  function cell(k, v, cls) { return '<div><small>' + k + '</small><b class="' + (cls || '') + '">' + v + '</b></div>'; }
  function card(long, title, upnl, roe, s1, s2, ticker) {
    return '<div class="card"><div class="t"><span class="side ' + (long ? 'yes">LONG' : 'no">SHORT') + '</span>' + title + '</div>' +
      '<div class="v ' + (upnl >= 0 ? 'up' : 'down') + '">' + usd(upnl) + ' <small>(' + (isFinite(roe) ? roe.toFixed(1) : '–') + '%)</small></div>' +
      '<div class="s">' + s1 + '</div><div class="s">' + s2 + '</div><button class="act" data-close="' + ticker + '">Close</button></div>';
  }

  function renderSignal() {
    var cutoff = Date.now() / 1000 - 3600;
    var ev = K.events().filter(function (e) { return e.time >= cutoff && (e.setup === 'vwap_stretch' || e.setup === 'rsi_extreme'); }).pop();
    $('perp-signal').innerHTML = ev
      ? '<b>' + (ev.dir > 0 ? '▲ ' : '▼ ') + Indicators.SETUPS[ev.setup].name + '</b>: ' + ev.text + '. ' + SETUP_STATS[ev.setup].note
      : 'No mean-reversion setup in the last hour. Trend setups had no edge in the backtest (Stats tab).';
  }

  // ---------- actions
  async function go() {
    var t = ticket();
    if (!(t.qty >= 1)) return;
    if (t.tp && (st.dir > 0 ? t.tp <= t.price : t.tp >= t.price)) return alert('Take profit must be ' + (st.dir > 0 ? 'above' : 'below') + ' entry.');
    if (t.sl && (st.dir > 0 ? t.sl >= t.price : t.sl <= t.price)) return alert('Stop loss must be ' + (st.dir > 0 ? 'below' : 'above') + ' entry.');
    if (pmode === 'paper') {
      try {
        paper.open({ ticker: TICKER, dir: st.dir, margin: t.margin, leverage: t.lev, price: t.price, maxLeverage: maxLev(st.dir), takeProfit: t.tp, stopLoss: t.sl });
        K.log('PERP PAPER ' + (st.dir > 0 ? 'LONG ' : 'SHORT ') + t.qty + ' @ ' + usd(toBtc(t.price), 0) + ' (' + t.lev.toFixed(1) + 'x)');
      } catch (e) { alert(e.message); }
      return render();
    }
    if (!K.hasKey(env())) return alert('Save your ' + env() + ' API key first (Settings → API keys).');
    var ok = confirm((pmode === 'live' ? 'REAL MONEY\n' : 'DEMO\n') + (st.dir > 0 ? 'LONG ' : 'SHORT ') + t.qty + ' BTC perp contracts\n' +
      'Size ' + usd(t.notional) + ' · ~' + t.lev.toFixed(1) + 'x on ' + usd(t.margin) + ' margin\nEntry ≈ ' + usd(toBtc(t.price), 0) +
      (t.tp ? '\nTake profit ' + usd(toBtc(t.tp), 0) : '') + (t.sl ? '\nStop loss ' + usd(toBtc(t.sl), 0) : '') + '\n\nSend order?');
    if (!ok) return;
    try {
      var c = client();
      var limit = st.dir > 0 ? t.price * 1.0005 : t.price * 0.9995; // marketable limit, 5bp slippage cap
      var res = await c.createOrder({ ticker: TICKER, clientOrderId: 'kbot-p-' + Date.now(), side: st.dir > 0 ? 'bid' : 'ask', count: t.qty, price: limit });
      var filled = f(res.fill_count);
      K.log('PERP ' + pmode.toUpperCase() + ' ' + (st.dir > 0 ? 'LONG ' : 'SHORT ') + 'filled ' + filled + '/' + t.qty + (res.average_fill_price ? ' @ ' + res.average_fill_price : ''));
      if (filled > 0 && (t.tp || t.sl)) {
        var ps = await c.positions(TICKER);
        var mode = ps[0] && String(ps[0].subaccount_type || '').indexOf('isolated') >= 0 ? 'isolated' : 'cross';
        await c.setBracket(TICKER, mode, t.sl, t.tp);
        K.log('PERP bracket set (' + mode + '): ' + (t.tp ? 'TP ' + usd(toBtc(t.tp), 0) + ' ' : '') + (t.sl ? 'SL ' + usd(toBtc(t.sl), 0) : ''));
      }
      if (!(filled > 0)) alert('Not filled (price moved). Nothing was opened.');
    } catch (e) { alert('Order failed: ' + e.message); K.log('PERP order error: ' + e.message); }
    refresh();
  }

  async function closePos(ticker) {
    if (pmode === 'paper') {
      var side = paper.state.positions[ticker] && paper.state.positions[ticker].qty > 0 ? bestBid() : bestAsk();
      var pnl = paper.close(ticker, side, 'manual');
      K.log('PERP PAPER CLOSE ' + ticker + ' ' + (pnl >= 0 ? '+' : '') + usd(pnl));
      return render();
    }
    var p = (st.live && st.live.positions || []).filter(function (x) { return x.market_ticker === ticker; })[0];
    if (!p) return;
    var q = f(p.position);
    if (!confirm('Close ' + Math.abs(q) + ' contracts at market (' + pmode.toUpperCase() + ')?')) return;
    try {
      var px = q > 0 ? bestBid() * 0.999 : bestAsk() * 1.001;
      var res = await client().createOrder({ ticker: ticker, clientOrderId: 'kbot-pc-' + Date.now(), side: q > 0 ? 'ask' : 'bid', count: Math.abs(q), price: px, reduceOnly: true });
      K.log('PERP close filled ' + res.fill_count + '/' + Math.abs(q));
    } catch (e) { alert('Close failed: ' + e.message); }
    refresh();
  }

  // ---------- events
  $('t-long').onclick = function () { st.dir = 1; renderTicket(); };
  $('t-short').onclick = function () { st.dir = -1; renderTicket(); };
  ['t-margin', 't-lev', 't-tp', 't-sl'].forEach(function (id) { $(id).oninput = renderTicket; });
  $('t-go').onclick = go;
  $('pp-reset').onclick = function () { if (confirm('Reset paper perps to $1,000?')) { paper.reset(1000); render(); } };
  document.querySelectorAll('#perps-mode-seg button').forEach(function (b) {
    b.onclick = function () {
      if (b.dataset.pmode === 'live' && !confirm('Live perps place REAL leveraged orders on your Kalshi account. Switch to live?')) return;
      pmode = b.dataset.pmode; K.storage.set('kbot_pmode', pmode); st.live = null; refresh();
    };
  });
  window.addEventListener('kbot:tab', function (e) { st.tab = e.detail; if (st.tab === 'perps') refresh(); });

  // Fast refresh while the Perps tab is open; slow refresh otherwise (paper TP/SL/liquidation checks).
  setInterval(function () {
    if (st.tab === 'perps' || Object.keys(paper.state.positions).length) refresh();
  }, 5000);
  setInterval(render, 1000);
  refresh();
})();
