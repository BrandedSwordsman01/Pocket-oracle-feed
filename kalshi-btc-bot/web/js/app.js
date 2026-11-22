/* UI glue: wires the bot to the page and the Android bridge. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var Native = window.Native || null;
  var fmt = Bot.fmt;

  var storage = {
    get: function (k) { try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : null; } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage full or blocked */ } }
  };

  var settings = storage.get('kbot_settings') || { mode: 'paper', config: {} };
  var transport = Api.defaultTransport();
  var prices = new Api.Prices(transport);
  var bot = null;
  var timer = null;
  var logLines = storage.get('kbot_log') || [];

  function envFor(mode) { return mode === 'demo' ? 'demo' : 'prod'; }

  function log(line) {
    logLines.unshift(line);
    logLines = logLines.slice(0, 200);
    storage.set('kbot_log', logLines);
    $('log').textContent = logLines.join('\n');
  }

  function makeBot() {
    return new Bot({
      kalshi: new Api.Kalshi(transport, envFor(settings.mode)),
      prices: prices,
      mode: settings.mode,
      storage: storage,
      config: settings.config,
      onLog: log
    });
  }

  function running() { return timer !== null; }

  function start() {
    var mode = settings.mode;
    if (mode !== 'paper') {
      if (!Native) { alert('Demo and live trading only work inside the Android app.'); return; }
      if (!Native.hasKey(envFor(mode))) { alert('Save your ' + (mode === 'demo' ? 'demo' : 'production') + ' API key first (API keys section).'); return; }
    }
    if (mode === 'live') {
      var c = bot.cfg;
      var answer = prompt('REAL MONEY. Limits: $' + c.maxTradeCents / 100 + '/trade, $' + c.maxOpenExposureCents / 100 +
        ' at risk, stop after -$' + c.dailyLossLimitCents / 100 + '/day.\nType LIVE to start.');
      if (answer !== 'LIVE') return;
    }
    log('Started in ' + mode.toUpperCase() + ' mode, series ' + bot.cfg.series);
    if (Native) Native.setKeepAlive(true);
    bot.tick().then(render);
    timer = setInterval(function () { bot.tick().then(render); }, bot.cfg.intervalSec * 1000);
    render();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    if (Native) Native.setKeepAlive(false);
    bot.cancelAll().then(function () { log('Stopped. Resting orders canceled.'); render(); });
    render();
  }

  function render() {
    var mode = settings.mode;
    var badge = $('badge');
    badge.textContent = running() ? mode.toUpperCase() + ' · RUNNING' : 'STOPPED';
    badge.className = 'badge ' + (running() ? mode : '');
    $('mode').value = mode;
    $('mode').disabled = running();
    $('start').disabled = running();

    var l = bot.last || {};
    $('spot').textContent = l.spot ? '$' + Math.round(l.spot).toLocaleString() : '–';
    $('vol').textContent = l.sigma ? (l.sigma * Math.sqrt(60) * 100).toFixed(2) + '%' : '–';
    var s = bot.stats();
    setPnl($('today'), s.today);
    setPnl($('total'), s.realized);
    $('open').textContent = s.open + ' / $' + (s.openCost / 100).toFixed(2);
    $('wl').textContent = s.wins + ' / ' + s.losses;

    var cal = $('calib');
    if (s.calibN < 30) {
      cal.textContent = 'Scored markets: ' + s.calibN + '. Need a few hundred settled markets before this means anything.';
    } else {
      var better = s.brierModel < s.brierMarket;
      cal.innerHTML = 'Scored markets: <b>' + s.calibN + '</b><br>Model Brier: <b>' + s.brierModel.toFixed(4) +
        '</b> vs market: <b>' + s.brierMarket.toFixed(4) + '</b> (lower is better)<br>' +
        (s.calibN < 300 ? 'Still too few to trust.' : better
          ? '<span class="pos">Model is out-predicting market prices so far.</span>'
          : '<span class="neg">Market prices beat the model. Do not go live.</span>');
    }

    var edges = $('edges');
    edges.innerHTML = '';
    (l.markets || []).forEach(function (m) {
      edges.appendChild(li(m.ticker + ' · ' + m.side.toUpperCase() + ' @' + m.price + '¢ · ' + Math.round(m.minutes) + 'm',
        (m.edge >= 0 ? '+' : '') + m.edge.toFixed(1) + '¢', m.edge >= bot.cfg.minEdgeCents ? 'pos' : ''));
    });
    if (!edges.children.length) edges.appendChild(li(running() ? 'Waiting for data…' : 'Start the bot to scan markets.', '', ''));

    var pos = $('positions');
    pos.innerHTML = '';
    var list = bot.state.positions.slice().reverse().slice(0, 25);
    list.forEach(function (p) {
      var right = p.status === 'settled' ? fmt(p.pnl) : p.status;
      pos.appendChild(li(p.count + '× ' + p.side.toUpperCase() + ' ' + p.ticker + ' @' + p.price + '¢', right,
        p.status === 'settled' ? (p.pnl >= 0 ? 'pos' : 'neg') : ''));
    });
    if (!list.length) pos.appendChild(li('No positions yet.', '', ''));
  }

  function setPnl(el, cents) {
    el.textContent = fmt(cents);
    el.className = cents > 0 ? 'pos' : cents < 0 ? 'neg' : '';
  }

  function li(text, right, cls) {
    var e = document.createElement('li');
    var a = document.createElement('span'); a.className = 't'; a.textContent = text;
    var b = document.createElement('span'); b.textContent = right; if (cls) b.className = cls;
    e.appendChild(a); e.appendChild(b);
    return e;
  }

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
    if (cfg.minMinutes >= cfg.maxMinutes) { alert('Min minutes must be below max minutes.'); return; }
    settings.config = cfg;
    storage.set('kbot_settings', settings);
    Object.assign(bot.cfg, cfg);
    log('Settings saved.');
    render();
  };

  $('mode').onchange = function () {
    settings.mode = $('mode').value;
    storage.set('kbot_settings', settings);
    bot = makeBot();
    render();
  };

  $('start').onclick = start;
  $('stop').onclick = stop;

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

  bot = makeBot();
  $('log').textContent = logLines.join('\n');
  loadCfgForm();
  keyStatus();
  render();
  setInterval(render, 5000);
})();
