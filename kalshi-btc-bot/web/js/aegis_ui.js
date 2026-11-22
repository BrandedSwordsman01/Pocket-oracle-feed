/* AEGIS panel: settlement cone on the chart, per-market decisions (15-min + hourly), dominance check, forming patterns. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var K = window.KBot;
  if (!K || !window.Aegis || !window.AEGIS_PARAMS) return;
  var kalshi = new Api.Kalshi(K.transport, 'prod');
  var st = { which: K.storage.get('kbot_aegis_which') || '15m', m15: null, hourly: [], last: null };
  var num = function (v) { var n = parseFloat(v); return isFinite(n) ? n : NaN; };
  var usd0 = function (v) { return '$' + Math.round(v).toLocaleString(); };

  // ---------- cone series on the main chart
  var coneColors = ['rgba(41,98,255,.35)', 'rgba(41,98,255,.7)', '#f7a600', 'rgba(41,98,255,.7)', 'rgba(41,98,255,.35)'];
  var cone = coneColors.map(function (c, i) {
    return K.chart.addLineSeries({ color: c, lineWidth: i === 2 ? 2 : 1, lineStyle: i === 2 ? 0 : 2, priceLineVisible: false,
      lastValueVisible: i === 0 || i === 4 || i === 2, crosshairMarkerVisible: false, title: ['5%', '25%', 'median', '75%', '95%'][i] });
  });

  function quoteOf(m) {
    var b = num(m.yes_bid_dollars), a = num(m.yes_ask_dollars);
    return { bid: b, ask: a, askSizeYes: num(m.yes_ask_size_fp), askSizeNo: num(m.yes_bid_size_fp) };
  }

  async function refresh() {
    try {
      var res = await Promise.all([
        kalshi.call('GET', '/markets', { series_ticker: 'KXBTC15M', status: 'open', limit: 10 }),
        kalshi.call('GET', '/markets', { series_ticker: 'KXBTCD', status: 'open', limit: 400 })
      ]);
      var now = Date.now();
      var m15 = (res[0].markets || []).filter(function (m) { return Date.parse(m.close_time) > now && m.floor_strike; })
        .sort(function (a, b) { return Date.parse(a.close_time) - Date.parse(b.close_time); })[0];
      var hs = (res[1].markets || []).filter(function (m) { return Date.parse(m.close_time) > now && m.strike_type === 'greater'; });
      var nextH = hs.reduce(function (mn, m) { return Math.min(mn, Date.parse(m.close_time)); }, Infinity);
      st.m15 = m15 || null;
      st.hourly = hs.filter(function (m) { return Date.parse(m.close_time) === nextH; });
      render();
    } catch (e) { $('aegis-summary').textContent = 'AEGIS data error: ' + e.message; }
  }

  function render() {
    var l = K.last();
    var candles = l.candles || [];
    if (!candles.length || !l.spot) { $('aegis-summary').textContent = 'Waiting for price data…'; return; }
    document.querySelectorAll('#aegis-seg button').forEach(function (b) { b.classList.toggle('on', b.dataset.which === st.which); });
    var now = Math.floor(Date.now() / 1000);
    // benchmark basis: 15M strike = BRTI 60s average at the window open, vs the Coinbase close of that minute
    var basis = 0, basisOk = false;
    var lagS = now - (candles[candles.length - 1].time + 60);
    if (st.m15) {
      var openT = Date.parse(st.m15.close_time) / 1000 - 900;
      var c = candles.filter(function (x) { return x.time === openT - 60; })[0];
      if (c) { basis = num(st.m15.floor_strike) / c.close - 1; basisOk = true; }
      if (!isFinite(basis) || Math.abs(basis) > 0.003) { basis = 0; basisOk = false; }
    }
    var centre = l.spot * (1 + basis);
    var target = st.which === '15m' ? (st.m15 && Date.parse(st.m15.close_time) / 1000) : (st.hourly[0] && Date.parse(st.hourly[0].close_time) / 1000);
    if (!target) { $('aegis-summary').textContent = 'No open ' + (st.which === '15m' ? '15-minute' : 'hourly') + ' market right now.'; clearCone(); return; }
    var m = Math.max(1, Math.ceil((target - now) / 60));
    var sd = Aegis.sdForecast(candles, now, m);
    var v = Aegis.settlementVar(sd);

    // cone: price quantiles at each future minute, settlement-average quantiles at the end
    var TZ = K.TZ, pts = [[], [], [], [], []], qs = [0.05, 0.25, 0.5, 0.75, 0.95], acc = 0;
    var t0 = Math.floor(now / 60) * 60;
    for (var u = 0; u <= m; u++) {
      var vu = u === m ? v : acc;
      qs.forEach(function (q, j) { pts[j].push({ time: t0 + 60 * u + TZ, value: u === 0 ? centre : Aegis.quantile(centre, vu, q) }); });
      if (u < m) acc += sd[u] * sd[u];
    }
    cone.forEach(function (s, j) { s.setData(pts[j]); });
    var q05 = pts[0][m].value, q95 = pts[4][m].value;
    var ann = Math.sqrt(v / m * 525600) * 100;
    $('aegis-summary').innerHTML = '<b>' + (st.which === '15m' ? '15-min' : 'Hourly') + ' settlement ' +
      new Date(target * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) + '</b> · ' + m + ' min · centre ' + usd0(centre) +
      ' (basis ' + (basisOk ? (basis * 1e4).toFixed(1) + ' bp' : 'n/a') + ')' +
      (lagS > 120 ? ' · <span class="down">Coinbase candles ' + Math.round(lagS / 60) + ' min delayed</span>' : '') + '<br>90% range of the 60-s settlement average: <b>' + usd0(q05) + ' – ' + usd0(q95) +
      '</b> · forecast vol ' + ann.toFixed(0) + '% ann. (GARCH × time-of-day)';

    // decisions: the 15-min contract + nearby hourly strikes
    var rows = [];
    if (st.m15) {
      var c15 = { ticker: st.m15.ticker, series: 'KXBTC15M', kind: 'at_least', strike: num(st.m15.floor_strike) };
      var t15 = Date.parse(st.m15.close_time) / 1000, m15m = Math.max(1, Math.ceil((t15 - now) / 60));
      var v15 = Aegis.settlementVar(Aegis.sdForecast(candles, now, m15m));
      rows.push(Object.assign(Aegis.evaluate(centre, v15, c15, quoteOf(st.m15)), { label: '15m ≥ ' + usd0(c15.strike), mins: m15m }));
    }
    var hrs = st.hourly.slice().sort(function (a, b) { return Math.abs(num(a.floor_strike) - centre) - Math.abs(num(b.floor_strike) - centre); }).slice(0, 7)
      .sort(function (a, b) { return num(b.floor_strike) - num(a.floor_strike); });
    var hContracts = [], quotes = {};
    if (hrs.length) {
      var th = Date.parse(hrs[0].close_time) / 1000, mh = Math.max(1, Math.ceil((th - now) / 60));
      var vh = Aegis.settlementVar(Aegis.sdForecast(candles, now, mh));
      hrs.forEach(function (hm) {
        var c = { ticker: hm.ticker, series: 'KXBTCD', kind: 'above', strike: num(hm.floor_strike) };
        hContracts.push(c); quotes[c.ticker] = quoteOf(hm);
        rows.push(Object.assign(Aegis.evaluate(centre, vh, c, quotes[c.ticker]), { label: 'Hourly > ' + usd0(c.strike), mins: mh }));
      });
    }
    $('aegis-table').innerHTML = rows.map(function (r) {
      var act = r.action, cls = act === 'BUY_YES' ? 'up' : act === 'BUY_NO' ? 'down' : '';
      var mkt = r.bid !== undefined ? Math.round(r.bid * 100) + '–' + Math.round(r.ask * 100) + '¢' : '–';
      return '<div class="lrow"><div class="k">' + r.label + '<small>' + r.mins + 'm · ' + (r.series === 'KXBTC15M' ? '15-min' : 'hourly') + '</small></div>' +
        '<div class="bar">' + (r.bid !== undefined ? '<div class="rng" style="left:' + (r.bid * 100) + '%;width:' + Math.max((r.ask - r.bid) * 100, 1) + '%"></div>' : '') +
        '<div class="mdl" style="left:' + ((r.pFinal !== undefined ? r.pFinal : r.pRaw) * 100).toFixed(1) + '%"></div>' +
        '<span class="lbl">' + mkt + ' · AEGIS ' + Math.round((r.pFinal !== undefined ? r.pFinal : r.pRaw) * 100) + '%</span></div>' +
        '<div class="edge ' + cls + '">' + (r.edgeLow !== undefined ? (r.edgeLow >= 0 ? '+' : '') + (r.edgeLow * 100).toFixed(1) + '¢' : '–') +
        '<small>' + act.replace('_', ' ') + '</small></div></div>';
    }).join('') || '<div class="empty">No contracts</div>';

    // dominance: only when the 15-min window and the hourly ladder settle at the same instant
    var dom = $('aegis-dom');
    if (st.m15 && hrs.length && Date.parse(st.m15.close_time) === Date.parse(hrs[0].close_time)) {
      quotes[st.m15.ticker] = quoteOf(st.m15);
      var c15b = { ticker: st.m15.ticker, series: 'KXBTC15M', kind: 'at_least', strike: num(st.m15.floor_strike) };
      var opps = Aegis.dominance(c15b, hContracts, quotes, 10);
      dom.innerHTML = opps.length ? opps.map(function (o) {
        return '<div class="sig bull"><div class="sig-top"><span>Pair: ' + o.legs.map(function (x) { return x[1].toUpperCase() + ' ' + x[0].split('-').pop() + ' @' + Math.round(x[2] * 100) + '¢'; }).join(' + ') +
          '</span><span class="verdict watch">+$' + o.floor.toFixed(2) + '/10</span></div><div class="sig-sub">Minimum payout ≥ cost after fees' +
          (o.outageRisk ? ' — NOT risk-free: if index data is missing the 15-min leg resolves NO while the hourly settles later.' : '') + ' Locked only if both legs fill.</div></div>';
      }).join('') : '<div class="empty">15-min and hourly settle together: no after-fee dominance pairs right now.</div>';
    } else {
      dom.innerHTML = '<div class="empty">Dominance check runs when a 15-min window closes on the hour (shares the hourly settlement).</div>';
    }

    // forming patterns
    var f = Forming.detect(candles);
    $('forming').innerHTML = f.length ? f.map(function (p) {
      var lbl = p.verdict === 'watch' ? 'WATCH' : p.verdict === 'weak' ? 'WEAK EDGE' : 'NO EDGE';
      return '<div class="sig ' + (p.dir > 0 ? 'bull' : p.dir < 0 ? 'bear' : '') + '"><div class="sig-top"><span>' + (p.dir > 0 ? '▲ ' : p.dir < 0 ? '▼ ' : '◆ ') + p.name +
        ' <small class="muted">' + (p.state === 'active' ? 'ACTIVE' : 'forming ' + Math.round(p.progress * 100) + '%') + '</small></span><span class="verdict ' + p.verdict + '">' + lbl + '</span></div>' +
        '<div class="prog"><i style="width:' + Math.round(p.progress * 100) + '%"></i></div>' +
        '<div class="sig-sub">' + p.detail + '. ' + p.meaning + '</div>' +
        '<div class="sig-sub"><b>15-min:</b> ' + p.markets.m15 + '<br><b>Hourly:</b> ' + p.markets.hourly + '<br><b>Perps:</b> ' + p.markets.perps + '</div></div>';
    }).join('') : '<div class="empty">No patterns forming right now.</div>';
  }

  function clearCone() { cone.forEach(function (s) { s.setData([]); }); }

  document.querySelectorAll('#aegis-seg button').forEach(function (b) {
    b.onclick = function () { st.which = b.dataset.which; K.storage.set('kbot_aegis_which', st.which); render(); };
  });
  refresh();
  setInterval(refresh, 20000);
  setInterval(render, 5000);
})();
