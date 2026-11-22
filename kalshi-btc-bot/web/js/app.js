/* UI: chart, strike ladder, positions, stats, settings. Wires the bot to the page and the Android bridge. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var Native = window.Native || null;
  var LWC = window.LightweightCharts;
  var fmt = Bot.fmt;
  var TZ = -new Date().getTimezoneOffset() * 60; // chart times are shown in local time

  var storage = {
    get: function (k) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : null; } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage full or blocked */ } }
  };

  var settings = storage.get('kbot_settings') || { mode: 'paper', config: {} };
  var transport = Api.defaultTransport();
  var prices = new Api.Prices(transport);
  var bot = null, timer = null, scanTimer = null;
  var ui = { tf: 1, event: null };
  var logLines = storage.get('kbot_log') || [];

  function envFor(mode) { return mode === 'demo' ? 'demo' : 'prod'; }
  function running() { return timer !== null; }
  function money(c) { return (c < 0 ? '-$' : '$') + (Math.abs(c) / 100).toFixed(2); }

  function log(line) {
    logLines.unshift(line);
    logLines = logLines.slice(0, 200);
    storage.set('kbot_log', logLines);
    $('log').textContent = logLines.join('\n');
  }

  function makeBot() {
    return new Bot({
      kalshi: new Api.Kalshi(transport, envFor(settings.mode)),
      prices: prices, mode: settings.mode, storage: storage, config: settings.config, onLog: log
    });
  }

  // ---------- charts
  var chartOpts = {
    autoSize: true,
    layout: { background: { color: '#131722' }, textColor: '#787b86', fontSize: 11 },
    grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
    rightPriceScale: { borderColor: '#2a2e39' },
    timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 }
  };
  var chart = LWC.createChart($('chart'), chartOpts);
  var candleSeries = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    priceFormat: { type: 'price', precision: 0, minMove: 1 }
  });
  var strikeLines = [];
  var eqChart = LWC.createChart($('equity'), Object.assign({}, chartOpts, {
    layout: { background: { color: '#1e222d' }, textColor: '#787b86', fontSize: 11 },
    grid: { vertLines: { visible: false }, horzLines: { color: '#2a2e39' } }
  }));
  var eqSeries = eqChart.addAreaSeries({
    lineColor: '#2962ff', topColor: 'rgba(41,98,255,.35)', bottomColor: 'rgba(41,98,255,0)', lineWidth: 2,
    priceFormat: { type: 'custom', formatter: function (v) { return '$' + v.toFixed(2); } }
  });

  function aggregate(candles, n) {
    if (n === 1) return candles;
    var out = [], cur = null;
    candles.forEach(function (c) {
      var b = Math.floor(c.time / (60 * n)) * 60 * n;
      if (!cur || cur.time !== b) { cur = { time: b, open: c.open, high: c.high, low: c.low, close: c.close }; out.push(cur); }
      else { cur.high = Math.max(cur.high, c.high); cur.low = Math.min(cur.low, c.low); cur.close = c.close; }
    });
    return out;
  }

  var lastCandleKey = '';
  function drawChart(l, rows) {
    if (l.candles && l.candles.length) {
      var data = aggregate(l.candles, ui.tf).map(function (c) {
        return { time: c.time + TZ, open: c.open, high: c.high, low: c.low, close: c.close };
      });
      var last = data[data.length - 1];
      if (l.spot) { last.close = l.spot; last.high = Math.max(last.high, l.spot); last.low = Math.min(last.low, l.spot); }
      var key = ui.tf + ':' + data[0].time;
      if (key !== lastCandleKey) { candleSeries.setData(data); lastCandleKey = key; chart.timeScale().scrollToRealTime(); }
      else { candleSeries.setData(data); }
    }
    strikeLines.forEach(function (pl) { candleSeries.removePriceLine(pl); });
    strikeLines = [];
    if (!l.spot) return;
    rows.filter(function (r) { return Math.abs(r.strike / l.spot - 1) < 0.012; })
      .sort(function (a, b) { return Math.abs(a.strike - l.spot) - Math.abs(b.strike - l.spot); })
      .slice(0, 7)
      .forEach(function (r) {
        var sig = signal(r);
        strikeLines.push(candleSeries.createPriceLine({
          price: Math.round(r.strike), lineWidth: sig ? 2 : 1, lineStyle: sig ? 0 : 2, axisLabelVisible: true,
          color: sig === 'yes' ? '#26a69a' : sig === 'no' ? '#ef5350' : '#4a4e59',
          title: Math.round(r.p * 100) + '%'
        }));
      });
  }

  function signal(r) {
    return r.t && r.t.edge >= bot.cfg.minEdgeCents && r.t.edge <= bot.cfg.maxEdgeCents ? r.t.side : null;
  }

  // ---------- markets tab
  function eventsOf(rows) {
    var by = {};
    rows.forEach(function (r) { (by[r.event] = by[r.event] || { event: r.event, close: r.close, rows: [] }).rows.push(r); });
    return Object.keys(by).map(function (k) { return by[k]; })
      .filter(function (e) { return e.close - Date.now() > 30000; })
      .sort(function (a, b) { return a.close - b.close; });
  }

  function timeLabel(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function drawMarkets(l, evs) {
    var chips = $('events');
    chips.innerHTML = '';
    evs.slice(0, 6).forEach(function (e) {
      var b = document.createElement('button');
      b.textContent = timeLabel(e.close) + ' · ' + Math.round((e.close - Date.now()) / 60000) + 'm';
      if (e.event === ui.event) b.className = 'on';
      b.onclick = function () { ui.event = e.event; render(); };
      chips.appendChild(b);
    });

    var lad = $('ladder');
    lad.innerHTML = '';
    var ev = evs.filter(function (e) { return e.event === ui.event; })[0];
    if (!ev || !l.spot) { lad.innerHTML = '<div class="empty">Loading markets…</div>'; return; }
    var rows = ev.rows.slice()
      .sort(function (a, b) { return Math.abs(a.strike - l.spot) - Math.abs(b.strike - l.spot); })
      .slice(0, 14)
      .sort(function (a, b) { return b.strike - a.strike; });
    var spotDone = false;
    rows.forEach(function (r) {
      if (!spotDone && r.strike < l.spot) { lad.appendChild(spotRow(l.spot)); spotDone = true; }
      lad.appendChild(ladderRow(r));
    });
    if (!spotDone) lad.appendChild(spotRow(l.spot));
  }

  function spotRow(spot) {
    var d = document.createElement('div');
    d.className = 'spotrow';
    d.textContent = 'BTC $' + Math.round(spot).toLocaleString();
    return d;
  }

  function ladderRow(r) {
    var d = document.createElement('div');
    d.className = 'lrow';
    var below = r.strikeType === 'less' || r.strikeType === 'less_or_equal';
    var bid = isFinite(r.bid) ? r.bid : 0, ask = isFinite(r.ask) ? r.ask : 100;
    var sig = signal(r);
    var edge = r.t ? r.t.edge : NaN;
    d.innerHTML =
      '<div class="k">' + (below ? '<' : '>') + '$' + Math.round(r.strike).toLocaleString() + '<small>' + Math.round(r.minutes) + 'm left</small></div>' +
      '<div class="bar"><div class="rng" style="left:' + bid + '%;width:' + Math.max(ask - bid, 1) + '%"></div>' +
      '<div class="mdl" style="left:' + (r.p * 100).toFixed(1) + '%"></div>' +
      '<span class="lbl">' + bid + '–' + ask + '¢ · model ' + Math.round(r.p * 100) + '%</span></div>' +
      '<div class="edge ' + (sig === 'yes' ? 'up' : sig === 'no' ? 'down' : '') + '">' +
      (isFinite(edge) ? (edge >= 0 ? '+' : '') + edge.toFixed(1) + '¢' : '–') +
      '<small>' + (sig ? 'BUY ' + sig.toUpperCase() : r.inWindow ? 'no edge' : 'too early/late') + '</small></div>';
    return d;
  }

  // ---------- positions + stats
  function drawPositions() {
    var open = $('open-list'), hist = $('hist-list');
    open.innerHTML = ''; hist.innerHTML = '';
    var ps = bot.state.positions.slice().reverse();
    ps.forEach(function (p) {
      var c = document.createElement('div');
      c.className = 'card';
      var settled = p.status === 'settled';
      c.innerHTML = '<div class="t"><span class="side ' + p.side + '">' + p.side.toUpperCase() + '</span>' + p.ticker + '</div>' +
        '<div class="v ' + (settled ? (p.pnl >= 0 ? 'up' : 'down') : '') + '">' + (settled ? fmt(p.pnl) : p.status) + '</div>' +
        '<div class="s">' + p.count + ' @ ' + p.price + '¢ · fair ' + p.fair + '¢</div>' +
        '<div class="s">' + (settled ? 'settled ' + p.result.toUpperCase() : 'closes ' + timeLabel(p.closeTime)) + '</div>';
      (settled ? hist : open).appendChild(c);
    });
    if (!open.children.length) open.innerHTML = '<div class="empty">No open positions</div>';
    if (!hist.children.length) hist.innerHTML = '<div class="empty">Nothing settled yet</div>';
  }

  function drawStats(s) {
    var settled = bot.state.positions.filter(function (p) { return p.status === 'settled'; })
      .sort(function (a, b) { return a.settledAt - b.settledAt; });
    var eq = 0, lastT = 0, pts = [];
    settled.forEach(function (p) {
      eq += p.pnl / 100;
      var t = Math.max(Math.floor(p.settledAt / 1000) + TZ, lastT + 1);
      lastT = t;
      pts.push({ time: t, value: eq });
    });
    eqSeries.setData(pts);
    if (pts.length) eqChart.timeScale().fitContent();

    var n = s.wins + s.losses;
    var avg = n ? s.realized / n : 0;
    $('perf').innerHTML =
      '<div><small>Trades settled</small><b>' + n + '</b></div>' +
      '<div><small>Win rate</small><b>' + (n ? Math.round(100 * s.wins / n) + '%' : '–') + '</b></div>' +
      '<div><small>Avg P&amp;L / trade</small><b class="' + (avg >= 0 ? 'up' : 'down') + '">' + money(avg) + '</b></div>' +
      '<div><small>Total P&amp;L</small><b class="' + (s.realized >= 0 ? 'up' : 'down') + '">' + money(s.realized) + '</b></div>';

    var cal = $('calib');
    if (s.calibN < 30) {
      cal.innerHTML = '<b>' + s.calibN + '</b> markets scored. Needs a few hundred settled markets before this means anything.';
    } else {
      var better = s.brierModel < s.brierMarket;
      cal.innerHTML = '<b>' + s.calibN + '</b> markets scored<br>Model error <b>' + s.brierModel.toFixed(4) +
        '</b> vs market <b>' + s.brierMarket.toFixed(4) + '</b> (lower wins)<br>' +
        (s.calibN < 300 ? 'Too few to trust yet.' : better
          ? '<span class="up">Model is out-predicting market prices.</span>'
          : '<span class="down">Market beats the model. Don\'t go live.</span>');
    }
  }

  // ---------- main render
  function render() {
    var mode = settings.mode;
    var l = bot.last || {};
    var rows = l.markets || [];
    var evs = eventsOf(rows);
    if (!evs.some(function (e) { return e.event === ui.event; })) ui.event = evs.length ? evs[0].event : null;
    var evRows = (evs.filter(function (e) { return e.event === ui.event; })[0] || { rows: [] }).rows;

    // header
    $('price').textContent = l.spot ? '$' + l.spot.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '–';
    if (l.candles && l.candles.length > 60 && l.spot) {
      var ref = l.candles[l.candles.length - 61].close, ch = (l.spot / ref - 1) * 100;
      $('chg').textContent = (ch >= 0 ? '+' : '') + ch.toFixed(2) + '% 1h';
      $('chg').className = 'chg ' + (ch >= 0 ? 'up' : 'down');
    }
    $('mode-pill').textContent = mode.toUpperCase();
    $('mode-pill').className = 'pill ' + mode;
    $('run').className = 'run' + (running() ? ' on' : '');
    $('run-text').textContent = running() ? 'Trading' : 'Stopped';

    var t = $('toggle');
    t.textContent = running() ? 'Stop + cancel orders' : 'Start ' + (mode === 'live' ? 'LIVE trading' : mode + ' trading');
    t.className = 'go' + (running() ? ' stop' : mode === 'live' ? ' live' : '');

    document.querySelectorAll('#mode-seg button').forEach(function (b) {
      b.className = b.dataset.mode === mode ? 'on' : '';
      b.disabled = running();
    });
    $('mode-hint').textContent = {
      paper: 'Real Kalshi + BTC prices, simulated fills, $100 fake bankroll. No keys needed.',
      demo: 'Real orders on Kalshi\'s demo exchange (fake money). Needs demo API keys.',
      live: 'Real orders with REAL money, within your risk limits below.'
    }[mode];

    var s = bot.stats();
    setPnl($('k-today'), s.today);
    setPnl($('k-total'), s.realized);
    $('k-risk').textContent = money(s.openCost);
    var n = s.wins + s.losses;
    $('k-win').textContent = n ? Math.round(100 * s.wins / n) + '%' : '–';

    drawChart(l, evRows);
    drawMarkets(l, evs);
    drawPositions();
    drawStats(s);
  }

  function setPnl(el, cents) {
    el.textContent = (cents > 0 ? '+' : '') + money(cents);
    el.className = cents > 0 ? 'up' : cents < 0 ? 'down' : '';
  }

  function tickCountdown() {
    var evs = eventsOf((bot.last && bot.last.markets) || []);
    var ev = evs.filter(function (e) { return e.event === ui.event; })[0];
    if (!ev) { $('next-close').textContent = '–'; return; }
    var s = Math.max(0, Math.round((ev.close - Date.now()) / 1000));
    var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
    $('next-close').textContent = timeLabel(ev.close) + ' · ' + (h ? h + ':' : '') + String(m).padStart(h ? 2 : 1, '0') + ':' + String(sec).padStart(2, '0');
  }

  // ---------- data refresh when not trading
  function scan() {
    if (running()) return;
    bot.scan().then(render).catch(function (e) { log('Data error: ' + e.message); });
  }

  // ---------- start / stop
  function start() {
    var mode = settings.mode;
    if (mode !== 'paper') {
      if (!Native) { alert('Demo and live trading only work inside the Android app.'); return; }
      if (!Native.hasKey(envFor(mode))) { alert('Save your ' + (mode === 'demo' ? 'demo' : 'production') + ' API key first (Settings → API keys).'); return; }
    }
    if (mode === 'live') {
      var c = bot.cfg;
      var answer = prompt('REAL MONEY. Limits: $' + c.maxTradeCents / 100 + '/trade, $' + c.maxOpenExposureCents / 100 +
        ' at risk, stop after -$' + c.dailyLossLimitCents / 100 + '/day.\nType LIVE to start.');
      if (answer !== 'LIVE') return;
    }
    log('Started ' + mode.toUpperCase() + ' trading on ' + bot.cfg.series);
    if (Native) Native.setKeepAlive(true);
    timer = setInterval(function () { bot.tick().then(render); }, bot.cfg.intervalSec * 1000);
    bot.tick().then(render);
    render();
  }

  function stop() {
    clearInterval(timer);
    timer = null;
    if (Native) Native.setKeepAlive(false);
    bot.cancelAll().then(function () { log('Stopped. Resting orders canceled.'); render(); });
    render();
  }

  $('toggle').onclick = function () { running() ? stop() : start(); };

  document.querySelectorAll('.tabs button').forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll('.tabs button').forEach(function (x) { x.classList.toggle('on', x === b); });
      document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('on', x.id === 'tab-' + b.dataset.tab); });
      document.body.dataset.tab = b.dataset.tab;
      if (b.dataset.tab === 'stats') setTimeout(function () { eqChart.timeScale().fitContent(); }, 0);
      window.scrollTo(0, 0);
    };
  });

  document.querySelectorAll('#tf button').forEach(function (b) {
    b.onclick = function () {
      ui.tf = Number(b.dataset.tf);
      document.querySelectorAll('#tf button').forEach(function (x) { x.classList.toggle('on', x === b); });
      render();
    };
  });

  document.querySelectorAll('#mode-seg button').forEach(function (b) {
    b.onclick = function () {
      if (running()) return;
      settings.mode = b.dataset.mode;
      storage.set('kbot_settings', settings);
      var last = bot.last;
      bot = makeBot();
      bot.last = last;
      render();
    };
  });

  // ---------- settings
  function loadCfgForm() {
    var c = bot.cfg;
    $('cfg-series').value = c.series;
    $('cfg-maxTrade').value = c.maxTradeCents / 100;
    $('cfg-maxExposure').value = c.maxOpenExposureCents / 100;
    $('cfg-dailyLoss').value = c.dailyLossLimitCents / 100;
    $('cfg-minEdge').value = c.minEdgeCents;
    $('cfg-minMinutes').value = c.minMinutes;
    $('cfg-maxMinutes').value = c.maxMinutes;
  }

  $('save-cfg').onclick = function () {
    var n = function (id) { return Number($(id).value); };
    var cfg = {
      series: $('cfg-series').value.trim().toUpperCase() || 'KXBTCD',
      maxTradeCents: Math.round(n('cfg-maxTrade') * 100),
      maxOpenExposureCents: Math.round(n('cfg-maxExposure') * 100),
      dailyLossLimitCents: Math.round(n('cfg-dailyLoss') * 100),
      minEdgeCents: n('cfg-minEdge'),
      minMinutes: n('cfg-minMinutes'),
      maxMinutes: n('cfg-maxMinutes')
    };
    for (var k in cfg) if (typeof cfg[k] === 'number' && !(cfg[k] > 0)) { alert('Invalid value for ' + k); return; }
    if (cfg.minMinutes >= cfg.maxMinutes) { alert('"Trade until" must be less than "Trade from".'); return; }
    settings.config = cfg;
    storage.set('kbot_settings', settings);
    Object.assign(bot.cfg, cfg);
    log('Settings saved.');
    render();
  };

  function keyStatus() {
    if (!Native) { $('key-status').textContent = 'Key storage is only available in the Android app.'; return; }
    $('key-status').textContent = 'Demo key: ' + (Native.hasKey('demo') ? 'saved' : 'none') +
      ' · Production key: ' + (Native.hasKey('prod') ? 'saved' : 'none');
  }

  $('key-save').onclick = function () {
    if (!Native) return keyStatus();
    var res = Native.saveKey($('key-env').value, $('key-id').value.trim(), $('key-pem').value);
    $('key-pem').value = '';
    if (res !== 'ok') alert('Could not save key: ' + res);
    keyStatus();
  };

  $('key-clear').onclick = function () {
    if (!Native) return keyStatus();
    if (confirm('Delete the saved ' + $('key-env').value + ' key?')) Native.clearKey($('key-env').value);
    keyStatus();
  };

  $('key-test').onclick = function () {
    var env = $('key-env').value;
    $('key-status').textContent = 'Checking…';
    new Api.Kalshi(transport, env).balanceCents()
      .then(function (b) { $('key-status').textContent = 'Connected to ' + env + '. Balance: $' + (b / 100).toFixed(2); })
      .catch(function (e) { $('key-status').textContent = e.message; });
  };

  // ---------- boot
  bot = makeBot();
  $('log').textContent = logLines.join('\n');
  loadCfgForm();
  keyStatus();
  render();
  scan();
  scanTimer = setInterval(scan, 15000);
  setInterval(tickCountdown, 1000);
})();
