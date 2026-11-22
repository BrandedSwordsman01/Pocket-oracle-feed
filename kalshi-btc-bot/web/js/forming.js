/*
 * "Patterns forming": setups that are close to triggering (not just ones that already fired), what each means,
 * and what it implies for each market, together with the backtested verdict (research/SETUPS.md,
 * docs/backtest_results_15m.md). Purely informational: none of these is a validated edge on its own.
 */
(function (root) {
  'use strict';
  var Ind = root.Indicators || (typeof require === 'function' ? require('./indicators.js') : null);

  function last(a) { for (var i = a.length - 1; i >= 0; i--) if (a[i] !== null && isFinite(a[i])) return a[i]; return null; }

  /**
   * candles: 1m candles oldest->newest. Returns [{id, name, state:'forming'|'active', dir:+1/-1/0, progress 0..1,
   *   detail, meaning, markets:{m15, hourly, perps}, verdict}]
   */
  function detect(candles) {
    if (!candles || candles.length < 80) return [];
    var closes = candles.map(function (c) { return c.close; });
    var n = closes.length, px = closes[n - 1], out = [];
    var rsi = Ind.rsi(closes, 14), r = last(rsi);
    var e9 = Ind.ema(closes, 9), e21 = Ind.ema(closes, 21);
    var bb = Ind.bollinger(closes, 20, 2), vw = Ind.vwap(candles, Math.min(240, Math.floor(n * 0.8)));
    var rets = closes.map(function (c, i) { return i ? Math.log(c / closes[i - 1]) : 0; });
    var sd = Ind.stdev(rets, 60), s60 = last(sd) || 1e-4;

    // RSI approaching an extreme (trigger at 25/75)
    if (r !== null && (r < 35 || r > 65)) {
      var down = r < 50, dist = down ? (r - 25) / 10 : (75 - r) / 10;
      out.push({ id: 'rsi', name: down ? 'RSI oversold' : 'RSI overbought', state: (down ? r < 25 : r > 75) ? 'active' : 'forming',
        dir: down ? 1 : -1, progress: Math.max(0, Math.min(1, 1 - dist)), detail: 'RSI(14) ' + r.toFixed(0) + ' (trigger ' + (down ? '25' : '75') + ')',
        meaning: 'Short-term selling/buying exhaustion; BTC tends to bounce slightly (58% of the time over 15 min) but the move averages only ~1.5 bp.',
        markets: { m15: 'Too small to beat fees. Do not buy ' + (down ? 'YES' : 'NO') + ' on this alone.', hourly: 'No measurable effect.', perps: 'Edge smaller than fees.' },
        verdict: 'weak' });
    }
    // VWAP stretch building (trigger 2.5 sigma)
    var v = last(vw);
    if (v) {
      var z = Math.log(px / v) / (s60 * Math.sqrt(60));
      if (Math.abs(z) > 1.6) {
        out.push({ id: 'vwap', name: 'VWAP stretch', state: Math.abs(z) > 2.5 ? 'active' : 'forming', dir: z > 0 ? -1 : 1,
          progress: Math.min(1, (Math.abs(z) - 1.6) / 0.9), detail: 'Price ' + z.toFixed(1) + 'σ ' + (z > 0 ? 'above' : 'below') + ' 4h VWAP (trigger 2.5σ)',
          meaning: 'Price far from its volume-weighted average tends to revert (65% over 60 min, +8 bp in testing) — the best of our candidates, not yet proven.',
          markets: { m15: 'Reversion usually takes longer than one 15-min window; small tilt ' + (z > 0 ? 'toward NO' : 'toward YES') + ' at best.',
            hourly: 'Most relevant horizon: strikes ' + (z > 0 ? 'above' : 'below') + ' spot may be slightly over-priced.',
            perps: 'Watch for a ' + (z > 0 ? 'short' : 'long') + ' mean-reversion entry (paper trade first).' },
          verdict: 'watch' });
      }
    }
    // Bollinger squeeze (bandwidth in bottom 20% of the last 4h)
    var w = bb.width.filter(function (x) { return x !== null; });
    if (w.length > 60) {
      var cur = w[w.length - 1], hist = w.slice(-240).slice().sort(function (a, b) { return a - b; });
      var pct = hist.findIndex(function (x) { return x >= cur; }) / hist.length;
      if (pct <= 0.2) {
        out.push({ id: 'squeeze', name: 'Bollinger squeeze', state: 'forming', dir: 0, progress: 1 - pct / 0.2,
          detail: 'Band width at the ' + Math.round(pct * 100) + 'th percentile of 4h',
          meaning: 'Volatility is compressed; a larger move often follows, but the direction is a coin flip (47% in testing).',
          markets: { m15: 'Contracts near 50¢ are fairly priced: direction unknown. Expect wider swings late in the window.',
            hourly: 'Far-from-money strikes may be cheap if volatility expands; AEGIS volatility forecast already reflects recent quiet.',
            perps: 'Do not pick a side; tighten stops.' },
          verdict: 'none' });
      }
    }
    // EMA 9/21 cross imminent
    var a = last(e9), b = last(e21), a5 = e9[n - 6], b5 = e21[n - 6];
    if (a && b && a5 && b5) {
      var gap = (a - b) / px, gap5 = (a5 - b5) / px;
      if (Math.abs(gap) < 0.00025 && Math.abs(gap) < Math.abs(gap5) && Math.sign(gap) === Math.sign(gap5)) {
        out.push({ id: 'ema', name: 'EMA 9/21 cross', state: 'forming', dir: gap > 0 ? -1 : 1, progress: 1 - Math.abs(gap) / 0.00025,
          detail: 'EMAs ' + (Math.abs(gap) * 1e4).toFixed(1) + ' bp apart and converging',
          meaning: 'Trend-following signal. Hit rate 48% after costs in 6 months of BTC data: no edge.',
          markets: { m15: 'Ignore.', hourly: 'Ignore.', perps: 'Ignore as an entry signal.' }, verdict: 'none' });
      }
    }
    // 60-minute breakout pending
    var hi = -Infinity, lo = Infinity;
    for (var i = n - 61; i < n - 1; i++) { hi = Math.max(hi, candles[i].high); lo = Math.min(lo, candles[i].low); }
    var dh = (hi - px) / px, dl = (px - lo) / px;
    if (dh >= 0 && dh < 0.0005 || dl >= 0 && dl < 0.0005) {
      var up = dh < dl;
      out.push({ id: 'breakout', name: up ? 'Testing 60-min high' : 'Testing 60-min low', state: 'forming', dir: up ? 1 : -1,
        progress: 1 - Math.min(dh, dl) / 0.0005, detail: (Math.min(dh, dl) * 1e4).toFixed(1) + ' bp from the ' + (up ? 'high' : 'low') + ' ($' + Math.round(up ? hi : lo).toLocaleString() + ')',
        meaning: 'Breakouts of the 1-hour range did not continue reliably (45% hit rate).',
        markets: { m15: 'Kalshi prices already react to the level; no edge from chasing.', hourly: 'Strikes at the level are where AEGIS and the market disagree most often — check the table.', perps: 'No edge chasing breakouts.' },
        verdict: 'none' });
    }
    return out.sort(function (x, y) { return (y.state === 'active') - (x.state === 'active') || y.progress - x.progress; });
  }

  var Forming = { detect: detect };
  if (typeof module !== 'undefined' && module.exports) module.exports = Forming;
  else root.Forming = Forming;
})(this);
