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
  ui.view = storage.get('kbot_view') || 'simple';            // 'simple' (default, plain English) or 'pro'
  function isPro() { return ui.view === 'pro'; }
  function applyView() {
    document.body.classList.toggle('pro', isPro());
    $('ui-btn').textContent = isPro() ? 'Simple view' : 'Pro view';
  }
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
  var strikeLines = [], levelLines = [];
  ui.ind = Object.assign({ ema: true, vwap: false, bb: false, levels: true, setups: true, strikes: true, rsi: true, macd: false },
    storage.get('kbot_ind') || {});
  function line(color, width, style) {
    return chart.addLineSeries({ color: color, lineWidth: width || 1, lineStyle: style || 0, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false });
  }
  var ov = {
    ema9: line('#f7a600', 1), ema21: line('#2962ff', 1), vwap: line('#e040fb', 2),
    bbU: line('#787b86', 1, 2), bbM: line('#4a4e59', 1, 2), bbL: line('#787b86', 1, 2)
  };
  var sub = LWC.createChart($('subchart'), Object.assign({}, chartOpts, { timeScale: { visible: false }, rightPriceScale: { borderColor: '#2a2e39', scaleMargins: { top: 0.1, bottom: 0.1 } } }));
  var subRsi = sub.addLineSeries({ color: '#b39ddb', lineWidth: 1, priceLineVisible: false, lastValueVisible: true });
  subRsi.createPriceLine({ price: 70, color: '#4a4e59', lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
  subRsi.createPriceLine({ price: 30, color: '#4a4e59', lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
  var subHist = sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  var subMacd = sub.addLineSeries({ color: '#2962ff', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  var subSig = sub.addLineSeries({ color: '#f7a600', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  var syncing = false;
  // sync by time (not bar index): the AEGIS cone adds future points to the main chart only
  function syncTo(target, r) {
    if (syncing || !r) return;
    syncing = true;
    try { target.timeScale().setVisibleRange(r); } catch (e) { /* range outside the other pane's data */ }
    syncing = false;
  }
  chart.timeScale().subscribeVisibleTimeRangeChange(function (r) { syncTo(sub, r); });
  sub.timeScale().subscribeVisibleTimeRangeChange(function (r) { syncTo(chart, r); });
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
      if (!cur || cur.time !== b) { cur = { time: b, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 0 }; out.push(cur); }
      else { cur.high = Math.max(cur.high, c.high); cur.low = Math.min(cur.low, c.low); cur.close = c.close; cur.volume += c.volume || 0; }
    });
    return out;
  }

  var lastCandleKey = '';
  function series(data, vals) {
    var out = [];
    for (var i = 0; i < data.length; i++) if (vals[i] !== null && vals[i] !== undefined && isFinite(vals[i])) out.push({ time: data[i].time, value: vals[i] });
    return out;
  }

  function drawIndicators(data) {
    var closes = data.map(function (c) { return c.close; });
    var I = isPro() ? ui.ind : {};   // Simple view: clean chart (price + likely range only)
    if (I.ema) { ov.ema9.setData(series(data, Indicators.ema(closes, 9))); ov.ema21.setData(series(data, Indicators.ema(closes, 21))); }
    else { ov.ema9.setData([]); ov.ema21.setData([]); }
    ov.vwap.setData(I.vwap ? series(data, Indicators.vwap(data, Math.min(240, Math.floor(data.length * 0.8)))) : []);
    if (I.bb) {
      var bb = Indicators.bollinger(closes, 20, 2);
      ov.bbU.setData(series(data, bb.upper)); ov.bbM.setData(series(data, bb.mid)); ov.bbL.setData(series(data, bb.lower));
    } else { ov.bbU.setData([]); ov.bbM.setData([]); ov.bbL.setData([]); }

    levelLines.forEach(function (pl) { candleSeries.removePriceLine(pl); });
    levelLines = [];
    if (I.levels) {
      Indicators.levels(data, ui.tf === 1 ? 10 : 4, 4).forEach(function (lv) {
        levelLines.push(candleSeries.createPriceLine({
          price: lv.price, color: lv.kind === 'support' ? 'rgba(38,166,154,.55)' : 'rgba(239,83,80,.55)',
          lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: (lv.kind === 'support' ? 'S' : 'R') + (lv.touches > 1 ? '×' + lv.touches : '')
        }));
      });
    }

    var subOn = I.rsi || I.macd;
    $('subchart').classList.toggle('on', subOn);
    subRsi.setData(I.rsi ? series(data, Indicators.rsi(closes, 14)) : []);
    if (I.macd && !I.rsi) {
      var m = Indicators.macd(closes);
      subHist.setData(series(data, m.hist).map(function (p) { return { time: p.time, value: p.value, color: p.value >= 0 ? 'rgba(38,166,154,.6)' : 'rgba(239,83,80,.6)' }; }));
      subMacd.setData(series(data, m.line)); subSig.setData(series(data, m.signal));
    } else { subHist.setData([]); subMacd.setData([]); subSig.setData([]); }
    if (subOn) { var vr = chart.timeScale().getVisibleRange(); if (vr) syncTo(sub, vr); }

    var leg = [];
    if (I.ema) leg.push(['#f7a600', 'EMA9'], ['#2962ff', 'EMA21']);
    if (I.vwap) leg.push(['#e040fb', 'VWAP']);
    if (I.bb) leg.push(['#787b86', 'BB(20,2)']);
    if (I.strikes) leg.push(['#26a69a', 'buy YES'], ['#ef5350', 'buy NO']);
    if (I.rsi) leg.push(['#b39ddb', 'RSI 14']); else if (I.macd) leg.push(['#2962ff', 'MACD']);
    $('legend').innerHTML = leg.map(function (x) { return '<span class="legend-item"><i style="background:' + x[0] + '"></i>' + x[1] + '</span>'; }).join('');
  }

  var lastEvents = [];
  function drawSetupMarkers(l) {
    lastEvents = l.candles && l.candles.length > 100 ? Indicators.detect(l.candles) : [];
    if (!isPro() || !ui.ind.setups) { candleSeries.setMarkers([]); return; }
    var bucket = 60 * ui.tf, seen = {};
    var marks = lastEvents.map(function (e) {
      var t = Math.floor(e.time / bucket) * bucket + TZ;
      return {
        time: t, position: e.dir > 0 ? 'belowBar' : 'aboveBar', shape: e.dir > 0 ? 'arrowUp' : 'arrowDown',
        color: SETUP_STATS[e.setup].verdict === 'none' ? (e.dir > 0 ? 'rgba(38,166,154,.55)' : 'rgba(239,83,80,.55)') : (e.dir > 0 ? '#26a69a' : '#ef5350'),
        text: shortName(e.setup)
      };
    }).filter(function (m) { var k = m.time + m.position; if (seen[k]) return false; seen[k] = 1; return true; })
      .sort(function (a, b) { return a.time - b.time; });
    candleSeries.setMarkers(marks);
  }

  function shortName(s) {
    return { ema_cross: 'EMA', breakout_60: 'BRK', momentum_60: 'MOM', rsi_extreme: 'RSI', vwap_stretch: 'VWAP', shock_reversal: 'SHK', bb_squeeze: 'BB' }[s] || s;
  }

  function drawSignals() {
    var box = $('signals');
    var cutoff = Date.now() / 1000 - 7200;
    var recent = lastEvents.filter(function (e) { return e.time >= cutoff; }).reverse().slice(0, 6);
    if (!recent.length) { box.innerHTML = '<div class="empty">No setups in the last 2 hours</div>'; return; }
    box.innerHTML = recent.map(function (e) {
      var st = SETUP_STATS[e.setup], name = Indicators.SETUPS[e.setup].name;
      var ago = Math.round((Date.now() / 1000 - e.time) / 60);
      var label = st.verdict === 'watch' ? 'WATCH' : st.verdict === 'weak' ? 'WEAK EDGE' : 'NO EDGE';
      return '<div class="sig ' + (e.dir > 0 ? 'bull' : 'bear') + '"><div class="sig-top"><span>' + (e.dir > 0 ? '▲ ' : '▼ ') + name +
        '</span><span class="verdict ' + st.verdict + '">' + label + '</span></div><div class="sig-sub">' + e.text + ' · ' + ago +
        'm ago · backtest: ' + (st.testHit * 100).toFixed(0) + '% hit, ' + (st.testMeanBp >= 0 ? '+' : '') + st.testMeanBp + 'bp/' + st.horizon + '</div></div>';
    }).join('');
  }

  function drawSetupTable() {
    $('setup-table').innerHTML = Object.keys(SETUP_STATS).map(function (k) {
      var st = SETUP_STATS[k];
      return '<div class="card"><div class="t">' + Indicators.SETUPS[k].name + '</div><div class="v"><span class="verdict ' + st.verdict + '">' +
        (st.verdict === 'watch' ? 'WATCH' : st.verdict === 'weak' ? 'WEAK' : 'NO EDGE') + '</span></div><div class="s">' + st.note +
        '</div><div class="s">' + (st.testHit * 100).toFixed(0) + '% · ' + (st.testMeanBp >= 0 ? '+' : '') + st.testMeanBp + 'bp · t=' + st.t + '</div></div>';
    }).join('');
  }

  function drawChart(l, rows) {
    if (l.candles && l.candles.length) {
      var data = aggregate(l.candles, ui.tf).map(function (c) {
        return { time: c.time + TZ, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume };
      });
      var last = data[data.length - 1];
      if (l.spot) { last.close = l.spot; last.high = Math.max(last.high, l.spot); last.low = Math.min(last.low, l.spot); }
      var key = ui.tf + ':' + data[0].time;
      if (key !== lastCandleKey) { candleSeries.setData(data); lastCandleKey = key; chart.timeScale().scrollToRealTime(); }
      else { candleSeries.setData(data); }
      drawIndicators(data);
      drawSetupMarkers(l);
    }
    strikeLines.forEach(function (pl) { candleSeries.removePriceLine(pl); });
    strikeLines = [];
    if (!l.spot || !isPro() || !ui.ind.strikes) return;
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
        '<div class="s">' + p.count + ' @ ' + p.price + '¢ · ' + (p.manual ? 'manual' : 'fair ' + p.fair + '¢') + '</div>' +
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
    t.textContent = running() ? 'Stop auto-bot' : (isPro() ? 'Start ' + (mode === 'live' ? 'LIVE trading' : mode + ' trading')
      : 'Start auto-bot ' + (mode === 'live' ? '(real money)' : mode === 'demo' ? '(demo money)' : '(practice money)'));
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
    drawSignals();
    drawMarkets(l, evs);
    drawPositions();
    drawStats(s);
  }

  function setPnl(el, cents) {
    el.textContent = (cents > 0 ? '+' : '') + money(cents);
    el.className = cents > 0 ? 'up' : cents < 0 ? 'down' : '';
  }

  function tickCountdown() {
    if (!isPro() && window.KBot && window.KBot.next15) {   // Simple view: count down to the 15-minute bet shown on the card
      var s15 = Math.max(0, Math.round((window.KBot.next15 - Date.now()) / 1000));
      $('next-close').textContent = timeLabel(window.KBot.next15) + ' · ' + Math.floor(s15 / 60) + ':' + String(s15 % 60).padStart(2, '0');
      return;
    }
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
    // keep manual positions settled / reconciled while the bot is stopped
    if (bot.state.positions.some(function (p) { return p.status !== 'settled'; })) bot.housekeep().then(render);
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
      window.dispatchEvent(new CustomEvent('kbot:tab', { detail: b.dataset.tab }));
      setTimeout(function () { chart.timeScale().scrollToRealTime(); }, 0);
      if (b.dataset.tab === 'stats') setTimeout(function () { eqChart.timeScale().fitContent(); }, 0);
      window.scrollTo(0, 0);
    };
  });

  function syncChips() {
    document.querySelectorAll('#ind-chips button').forEach(function (b) { b.classList.toggle('on', !!ui.ind[b.dataset.ind]); });
  }
  document.querySelectorAll('#ind-chips button').forEach(function (b) {
    b.onclick = function () {
      var k = b.dataset.ind;
      ui.ind[k] = !ui.ind[k];
      if (k === 'rsi' && ui.ind.rsi) ui.ind.macd = false;   // one sub-pane at a time
      if (k === 'macd' && ui.ind.macd) ui.ind.rsi = false;
      storage.set('kbot_ind', ui.ind);
      syncChips();
      lastCandleKey = '';
      render();
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
    $('cfg-strategy').value = c.strategy || 'blend';
    $('cfg-maxTrade').value = c.maxTradeCents / 100;
    $('cfg-maxExposure').value = c.maxOpenExposureCents / 100;
    $('cfg-dailyLoss').value = c.dailyLossLimitCents / 100;
    $('cfg-minEdge').value = c.minEdgeCents;
    $('cfg-minMinutes').value = c.minMinutes;
    $('cfg-maxMinutes').value = c.maxMinutes;
  }

  $('cfg-series').onchange = function () {
    var pr = Model.SERIES[$('cfg-series').value];
    if (pr) { $('cfg-minMinutes').value = pr.minMinutes; $('cfg-maxMinutes').value = pr.maxMinutes; }
  };

  $('save-cfg').onclick = function () {
    var n = function (id) { return Number($(id).value); };
    var cfg = {
      series: $('cfg-series').value || 'KXBTCD',
      strategy: $('cfg-strategy').value || 'blend',
      maxTradeCents: Math.round(n('cfg-maxTrade') * 100),
      maxOpenExposureCents: Math.round(n('cfg-maxExposure') * 100),
      dailyLossLimitCents: Math.round(n('cfg-dailyLoss') * 100),
      minEdgeCents: n('cfg-minEdge'),
      minMinutes: n('cfg-minMinutes'),
      maxMinutes: n('cfg-maxMinutes')
    };
    for (var k in cfg) if (typeof cfg[k] === 'number' && !(cfg[k] > 0)) { alert('Invalid value for ' + k); return; }
    if (cfg.strategy === 'aegis' && cfg.series !== 'KXBTC15M') { if (!confirm('AEGIS was tested on the 15-minute market. Use it on hourly anyway?')) return; }
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

  // ---------- live connectivity test: real order that can't fill, then cancel
  function testOut(line) {
    var o = $('test-out'); o.style.display = 'block';
    o.textContent += line + '\n';
  }
  function testReady(env) {
    $('test-out').textContent = '';
    if (!Native) { testOut('Only works inside the Android app.'); return false; }
    if (!Native.hasKey(env)) { testOut('No ' + env + ' key saved. Save it above first.'); return false; }
    return true;
  }

  $('test-order').onclick = async function () {
    var env = $('key-env').value;
    if (!testReady(env)) return;
    var k = new Api.Kalshi(transport, env);
    try {
      testOut('1/4 Balance: $' + ((await k.balanceCents()) / 100).toFixed(2));
      var ms = await k.openMarkets('KXBTCD');
      var m = ms.filter(function (x) { return Model.priceCents(x, 'yes_ask') >= 10 && Model.priceCents(x, 'yes_ask') <= 90; })[0];
      if (!m) { testOut('No suitable open KXBTCD market found right now.'); return; }
      testOut('2/4 Market ' + m.ticker + ' (YES ask ' + Model.priceCents(m, 'yes_ask') + '¢)');
      var o = await k.buy({ ticker: m.ticker, side: 'yes', count: 1, price: 1, clientOrderId: 'kbot-test-' + Date.now() });
      testOut('3/4 Order accepted: ' + o.order_id + ' (status ' + o.status + ', 1 YES @ 1¢, will not fill)');
      await k.cancel(o.order_id);
      testOut('4/4 Canceled. Hourly-market trading works on ' + env.toUpperCase() + '.');
      log('Test order OK on ' + env + ' (' + m.ticker + ')');
    } catch (e) { testOut('FAILED: ' + e.message); }
  };

  $('test-perp').onclick = async function () {
    var env = $('key-env').value;
    if (!testReady(env)) return;
    var p = new Api.Perps(transport, env);
    try {
      var en = await p.enabled();
      testOut('1/4 Perps enabled check: ' + JSON.stringify(en));
      var bal = await p.balance();
      var sb = (bal.subaccount_balances && bal.subaccount_balances[0]) || bal;
      testOut('2/4 Perps balance: available $' + Number(sb.available_balance || 0).toFixed(2));
      var ob = await p.orderbook('KXBTCPERP', 1);
      var bid = Math.max.apply(null, (ob.bids || []).map(function (r) { return Number(r[0]); }));
      var px = bid * 0.97;
      var o = await p.createOrder({ ticker: 'KXBTCPERP', clientOrderId: 'kbot-ptest-' + Date.now(), side: 'bid', count: 1,
        price: px, timeInForce: 'good_till_canceled', postOnly: true });
      testOut('3/4 Order accepted: ' + o.order_id + ' (post-only bid 1 @ $' + px.toFixed(4) + ', 3% below market)');
      await p.cancelOrder(o.order_id);
      testOut('4/4 Canceled. Perps trading works on ' + env.toUpperCase() + '.');
      log('Perps test order OK on ' + env);
    } catch (e) { testOut('FAILED: ' + e.message + (/enabled|permission|403|404/i.test(e.message) ? '\nPerps API may not be enabled for your account yet (Kalshi is rolling it out per member).' : '')); }
  };

  // ---------- Connect Kalshi wizard
  var cEnv = 'prod';
  function cOut(line, reset) {
    var o = $('c-status'); o.style.display = 'block';
    o.textContent = (reset ? '' : o.textContent) + line + '\n';
  }
  document.querySelectorAll('#c-env button').forEach(function (b) {
    b.onclick = function () {
      cEnv = b.dataset.env;
      document.querySelectorAll('#c-env button').forEach(function (x) { x.classList.toggle('on', x === b); });
    };
  });
  $('c-open').onclick = function () {
    var url = cEnv === 'prod' ? 'https://kalshi.com/account/profile' : 'https://demo.kalshi.co/account/profile';
    if (Native && Native.openUrl) Native.openUrl(url); else window.open(url, '_blank');
  };
  $('c-paste-id').onclick = function () {
    if (!Native || !Native.readClipboard) return cOut('Paste only works in the Android app.', true);
    var txt = Native.readClipboard() || '';
    var m = txt.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (m) { $('c-keyid').value = m[0]; cOut('✓ Key ID pasted', true); }
    else cOut('Clipboard does not contain a Key ID (looks like xxxxxxxx-xxxx-...). Copy it on Kalshi first.', true);
  };
  function needKeyId() {
    var id = $('c-keyid').value.trim();
    if (!id) { cOut('Enter or paste the Key ID first (step 2).', true); return null; }
    return id;
  }
  async function afterSave(res) {
    if (res !== 'ok') { cOut('✗ Could not save key: ' + res); return; }
    cOut('✓ Key saved and encrypted on this phone');
    keyStatus();
    var k = new Api.Kalshi(transport, cEnv);
    try {
      cOut('✓ Connected. Balance: $' + ((await k.balanceCents()) / 100).toFixed(2));
      var ms = await k.openMarkets('KXBTC15M');
      var m = ms.filter(function (x) { var a = Model.priceCents(x, 'yes_ask'); return a >= 10 && a <= 90; })[0];
      if (m) {
        var o = await k.buy({ ticker: m.ticker, side: 'yes', count: 1, price: 1, clientOrderId: 'kbot-conn-' + Date.now() });
        await k.cancel(o.order_id);
        cOut('✓ Test order placed and canceled (1 YES @ 1¢, could not fill). Trading works.');
      }
      $('c-quick').style.display = 'block';
      log('Kalshi connected (' + cEnv + ')');
    } catch (e) { cOut('✗ ' + e.message + '\n  Check the Key ID matches the key file, and that the key was created for ' + (cEnv === 'prod' ? 'kalshi.com' : 'demo.kalshi.co') + '.'); }
  }
  window.__keyImported = function (res) { afterSave(res); };
  $('c-import').onclick = function () {
    var id = needKeyId(); if (!id) return;
    if (!Native || !Native.importKeyFile) return cOut('Key import only works in the Android app.', true);
    cOut('Choose the key file Kalshi downloaded (usually in Downloads)…', true);
    Native.importKeyFile(cEnv, id);
  };
  $('c-paste-key').onclick = function () {
    var id = needKeyId(); if (!id) return;
    if (!Native || !Native.saveKeyFromClipboard) return cOut('Only works in the Android app.', true);
    cOut('Reading key from clipboard…', true);
    afterSave(Native.saveKeyFromClipboard(cEnv, id));
  };
  $('c-quick').onclick = function () {
    var cfg = Object.assign({}, settings.config, {
      series: 'KXBTC15M', strategy: 'aegis', minMinutes: 2, maxMinutes: 14,
      maxTradeCents: 200, maxOpenExposureCents: 600, dailyLossLimitCents: 1000
    });
    settings.config = cfg;
    settings.mode = cEnv === 'prod' ? 'live' : 'demo';
    storage.set('kbot_settings', settings);
    bot = makeBot();
    loadCfgForm();
    render();
    alert('Set: 15-minute market, AEGIS strategy, $2 per trade, $6 at risk max, stop after -$10/day, mode ' +
      settings.mode.toUpperCase() + '.\nGo to Markets and tap Start' + (settings.mode === 'live' ? ' (you will type LIVE to confirm).' : '.'));
    document.querySelector('.tabs button[data-tab="markets"]').click();
  };

  // ---------- view switch + help
  $('ui-btn').onclick = function () {
    ui.view = isPro() ? 'simple' : 'pro'; storage.set('kbot_view', ui.view);
    applyView(); lastCandleKey = ''; render();
    window.dispatchEvent(new CustomEvent('kbot:view'));
  };
  $('help-btn').onclick = function () { $('help').style.display = 'flex'; };
  $('help-close').onclick = function () { $('help').style.display = 'none'; };
  $('help').onclick = function (e) { if (e.target.id === 'help') $('help').style.display = 'none'; };

  // ---------- boot
  applyView();
  bot = makeBot();
  syncChips();
  drawSetupTable();
  // Shared with perps_ui.js
  window.KBot = {
    transport: transport, storage: storage, log: log, money: money,
    last: function () { return bot.last || {}; },
    events: function () { return lastEvents; },
    hasKey: function (env) { return !!(Native && Native.hasKey(env)); },
    chart: chart, candleSeries: candleSeries, TZ: TZ,
    mode: function () { return settings.mode; },
    botInfo: function () { return { running: running(), mode: settings.mode, cfg: bot.cfg, stats: bot.stats() }; },
    toggleBot: function () { $('toggle').click(); },
    manualBuy: function (m, side, n, priceC) { return bot.manualBuy(m, side, n, priceC).then(function (p) { render(); return p; }); },
    native: Native
  };
  $('log').textContent = logLines.join('\n');
  loadCfgForm();
  keyStatus();
  render();
  scan();
  scanTimer = setInterval(scan, 15000);
  setInterval(tickCountdown, 1000);
})();
