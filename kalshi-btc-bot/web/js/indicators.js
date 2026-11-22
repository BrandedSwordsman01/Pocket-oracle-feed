/*
 * Technical indicators + setup detectors on 1-minute candles {time, open, high, low, close, volume}.
 * Definitions are mirrored in research/setups.py, which backtests every setup on
 * 6 months of BTC data and on settled Kalshi markets. See research/SETUPS.md.
 */
(function (root) {
  'use strict';

  function ema(values, n) {
    var k = 2 / (n + 1), out = new Array(values.length), e = null;
    for (var i = 0; i < values.length; i++) {
      e = e === null ? values[i] : values[i] * k + e * (1 - k);
      out[i] = i >= n - 1 ? e : null;
    }
    return out;
  }

  function sma(values, n) {
    var out = new Array(values.length), s = 0;
    for (var i = 0; i < values.length; i++) {
      s += values[i];
      if (i >= n) s -= values[i - n];
      out[i] = i >= n - 1 ? s / n : null;
    }
    return out;
  }

  function stdev(values, n) {
    var m = sma(values, n), out = new Array(values.length);
    for (var i = 0; i < values.length; i++) {
      if (m[i] === null) { out[i] = null; continue; }
      var s = 0;
      for (var j = i - n + 1; j <= i; j++) s += (values[j] - m[i]) * (values[j] - m[i]);
      out[i] = Math.sqrt(s / n);
    }
    return out;
  }

  /** Wilder RSI. */
  function rsi(closes, n) {
    n = n || 14;
    var out = new Array(closes.length).fill(null), gain = 0, loss = 0;
    for (var i = 1; i < closes.length; i++) {
      var d = closes[i] - closes[i - 1], g = Math.max(d, 0), l = Math.max(-d, 0);
      if (i <= n) { gain += g / n; loss += l / n; if (i < n) continue; }
      else { gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n; }
      out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    }
    return out;
  }

  function macd(closes) {
    var f = ema(closes, 12), s = ema(closes, 26);
    var line = closes.map(function (_, i) { return f[i] !== null && s[i] !== null ? f[i] - s[i] : null; });
    var start = line.findIndex(function (v) { return v !== null; });
    var sig = new Array(closes.length).fill(null);
    if (start >= 0) {
      var e = ema(line.slice(start), 9);
      for (var i = 0; i < e.length; i++) sig[start + i] = e[i];
    }
    var hist = line.map(function (v, i) { return v !== null && sig[i] !== null ? v - sig[i] : null; });
    return { line: line, signal: sig, hist: hist };
  }

  function bollinger(closes, n, k) {
    n = n || 20; k = k || 2;
    var m = sma(closes, n), sd = stdev(closes, n);
    return {
      mid: m,
      upper: m.map(function (v, i) { return v === null ? null : v + k * sd[i]; }),
      lower: m.map(function (v, i) { return v === null ? null : v - k * sd[i]; }),
      width: m.map(function (v, i) { return v === null ? null : (2 * k * sd[i]) / v; })
    };
  }

  /** Rolling VWAP over the last n minutes (BTC trades 24/7, so no session reset). */
  function vwap(candles, n) {
    n = n || 240;
    var out = new Array(candles.length), pv = 0, v = 0;
    for (var i = 0; i < candles.length; i++) {
      var c = candles[i], tp = (c.high + c.low + c.close) / 3, vol = c.volume || 0;
      pv += tp * vol; v += vol;
      if (i >= n) { var o = candles[i - n], otp = (o.high + o.low + o.close) / 3; pv -= otp * (o.volume || 0); v -= o.volume || 0; }
      out[i] = i >= n - 1 && v > 0 ? pv / v : null;
    }
    return out;
  }

  /** Swing-pivot support/resistance: local extremes that held for `w` bars each side, clustered. */
  function levels(candles, w, maxLevels) {
    w = w || 10; maxLevels = maxLevels || 4;
    var pts = [];
    for (var i = w; i < candles.length - w; i++) {
      var hi = true, lo = true;
      for (var j = i - w; j <= i + w; j++) {
        if (candles[j].high > candles[i].high) hi = false;
        if (candles[j].low < candles[i].low) lo = false;
      }
      if (hi) pts.push({ price: candles[i].high, kind: 'resistance', time: candles[i].time });
      if (lo) pts.push({ price: candles[i].low, kind: 'support', time: candles[i].time });
    }
    var last = candles.length ? candles[candles.length - 1].close : 0;
    pts.forEach(function (p) { p.kind = p.price >= last ? 'resistance' : 'support'; });
    // merge levels within 0.08% of each other, count touches
    var merged = [];
    pts.sort(function (a, b) { return a.price - b.price; }).forEach(function (p) {
      var m = merged[merged.length - 1];
      if (m && Math.abs(p.price / m.price - 1) < 0.0008) { m.price = (m.price * m.touches + p.price) / (m.touches + 1); m.touches++; }
      else merged.push({ price: p.price, kind: p.kind, touches: 1 });
    });
    return merged.sort(function (a, b) { return Math.abs(a.price - last) - Math.abs(b.price - last); }).slice(0, maxLevels);
  }

  /**
   * Setup detectors. Each returns events {time, index, setup, dir (+1 bullish / -1 bearish), text}.
   * Fired on candle close, using only data up to that candle (no lookahead).
   */
  var SETUPS = {
    ema_cross: { name: 'EMA 9/21 cross', family: 'trend' },
    breakout_60: { name: '60-min breakout', family: 'trend' },
    momentum_60: { name: '1h momentum', family: 'trend' },
    rsi_extreme: { name: 'RSI(14) extreme', family: 'reversion' },
    vwap_stretch: { name: 'VWAP stretch (2.5σ)', family: 'reversion' },
    shock_reversal: { name: 'Shock candle (5σ)', family: 'reversion' },
    bb_squeeze: { name: 'Bollinger squeeze break', family: 'volatility' }
  };

  function detect(candles) {
    var n = candles.length, closes = candles.map(function (c) { return c.close; });
    var e9 = ema(closes, 9), e21 = ema(closes, 21), r = rsi(closes, 14), bb = bollinger(closes, 20, 2), vw = vwap(candles, 240);
    var rets = closes.map(function (c, i) { return i ? Math.log(c / closes[i - 1]) : 0; });
    var sd60 = stdev(rets, 60);
    var ev = [];
    function push(i, setup, dir, text) { ev.push({ time: candles[i].time, index: i, setup: setup, dir: dir, text: text }); }
    var lastFire = {};
    function cool(setup, i, gap) { if (lastFire[setup] !== undefined && i - lastFire[setup] < gap) return false; lastFire[setup] = i; return true; }

    for (var i = 61; i < n; i++) {
      var c = candles[i];
      if (e9[i] !== null && e21[i] !== null && e9[i - 1] !== null && e21[i - 1] !== null) {
        var d0 = e9[i - 1] - e21[i - 1], d1 = e9[i] - e21[i];
        if (d0 <= 0 && d1 > 0 && cool('ema_cross', i, 15)) push(i, 'ema_cross', 1, 'EMA9 crossed above EMA21');
        if (d0 >= 0 && d1 < 0 && cool('ema_cross', i, 15)) push(i, 'ema_cross', -1, 'EMA9 crossed below EMA21');
      }
      var hi = -Infinity, lo = Infinity;
      for (var j = i - 60; j < i; j++) { hi = Math.max(hi, candles[j].high); lo = Math.min(lo, candles[j].low); }
      if (c.close > hi && cool('breakout_60', i, 30)) push(i, 'breakout_60', 1, 'Closed above 60-min high');
      if (c.close < lo && cool('breakout_60', i, 30)) push(i, 'breakout_60', -1, 'Closed below 60-min low');

      if (sd60[i]) {
        var m60 = Math.log(c.close / closes[i - 60]) / (sd60[i] * Math.sqrt(60));
        if (Math.abs(m60) > 2 && cool('momentum_60', i, 60)) push(i, 'momentum_60', m60 > 0 ? 1 : -1, '1h move ' + m60.toFixed(1) + 'σ');
        if (Math.abs(rets[i]) > 5 * sd60[i] && cool('shock_reversal', i, 15)) push(i, 'shock_reversal', rets[i] > 0 ? -1 : 1, '1-min shock ' + (rets[i] / sd60[i]).toFixed(1) + 'σ');
      }
      if (r[i] !== null && r[i - 1] !== null) {
        if (r[i - 1] >= 25 && r[i] < 25 && cool('rsi_extreme', i, 30)) push(i, 'rsi_extreme', 1, 'RSI oversold (' + r[i].toFixed(0) + ')');
        if (r[i - 1] <= 75 && r[i] > 75 && cool('rsi_extreme', i, 30)) push(i, 'rsi_extreme', -1, 'RSI overbought (' + r[i].toFixed(0) + ')');
      }
      if (vw[i] && sd60[i]) {
        var z = Math.log(c.close / vw[i]) / (sd60[i] * Math.sqrt(60));
        if (Math.abs(z) > 2.5 && cool('vwap_stretch', i, 60)) push(i, 'vwap_stretch', z > 0 ? -1 : 1, 'Price ' + z.toFixed(1) + 'σ from VWAP');
      }
      if (bb.width[i] !== null && i > 240) {
        var ws = bb.width.slice(i - 240, i).filter(function (v) { return v !== null; }).sort(function (a, b) { return a - b; });
        var squeezed = bb.width[i - 1] <= ws[Math.floor(ws.length * 0.2)];
        if (squeezed && c.close > bb.upper[i] && cool('bb_squeeze', i, 60)) push(i, 'bb_squeeze', 1, 'Squeeze broke up');
        if (squeezed && c.close < bb.lower[i] && cool('bb_squeeze', i, 60)) push(i, 'bb_squeeze', -1, 'Squeeze broke down');
      }
    }
    return ev;
  }

  var Indicators = { ema: ema, sma: sma, stdev: stdev, rsi: rsi, macd: macd, bollinger: bollinger, vwap: vwap, levels: levels, detect: detect, SETUPS: SETUPS };
  if (typeof module !== 'undefined' && module.exports) module.exports = Indicators;
  else root.Indicators = Indicators;
})(this);
