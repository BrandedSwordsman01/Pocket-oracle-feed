/*
 * Chart patterns drawn on the candles, in plain English.
 *   Bart (pump → flat → dump) and inverse Bart (dump → flat → pump), double top / double bottom, range breakout.
 * Works on whatever candles the chart shows. Not backtested yet: shown for reference, not as a trade signal.
 */
(function (root) {
  'use strict';

  function sd(xs) {
    var m = xs.reduce(function (a, b) { return a + b; }, 0) / xs.length;
    return Math.sqrt(xs.reduce(function (a, b) { return a + (b - m) * (b - m); }, 0) / xs.length);
  }

  function bart(c, sig, dir) {
    // dir +1: up-flat-down (Bart); dir -1: down-flat-up (inverse Bart). Search the most recent ~90 bars.
    var n = c.length, start = Math.max(1, n - 90), best = null;
    var px = function (i) { return c[i].close; };
    for (var a = start; a < n - 4; a++) {
      for (var L1 = 2; L1 <= 10 && a + L1 < n; L1++) {
        var b = a + L1, move = dir * Math.log(px(b) / px(a));
        var need = Math.max(0.0015, 3 * sig * Math.sqrt(L1));
        if (move < need) continue;
        // flat top: at least 3 bars, range small relative to the move
        var hi = -Infinity, lo = Infinity, cEnd = b;
        for (var k = b; k < n && k <= b + 20; k++) {
          hi = Math.max(hi, px(k)); lo = Math.min(lo, px(k));
          if (Math.log(hi / lo) > 0.35 * move) break;
          cEnd = k;
        }
        if (cEnd - b < 3) continue;
        // third leg: reversal of >= 70% of the move within 10 bars
        var d = null;
        for (var j = cEnd + 1; j < n && j <= cEnd + 10; j++) {
          if (-dir * Math.log(px(j) / px(cEnd)) >= 0.7 * move) { d = j; break; }
        }
        var cand = { a: a, b: b, c: cEnd, d: d, move: move, state: d !== null ? 'done' : (cEnd >= n - 2 ? 'forming' : null) };
        if (!cand.state) continue;
        if (!best || cand.c > best.c || (cand.c === best.c && cand.move > best.move)) best = cand;
      }
    }
    if (!best) return null;
    var pts = [best.a, best.b, best.c].concat(best.d !== null ? [best.d] : []).map(function (i) { return { i: i, price: px(i) }; });
    var up = dir > 0;
    return {
      type: up ? 'bart' : 'ibart', name: up ? 'Bart pattern' : 'Inverse Bart', state: best.state, dir: up ? -1 : 1, points: pts,
      label: up ? (best.state === 'done' ? 'BART ↓' : 'BART?') : (best.state === 'done' ? 'iBART ↑' : 'iBART?'),
      meaning: up
        ? (best.state === 'done'
          ? 'Price shot up, went flat, then fell back down — a classic "Bart" (looks like Bart Simpson\'s hair). Usually a sign a big buyer pushed price and then left; it often stays near the lower level.'
          : 'Price shot up and is now going flat. If it drops back down this becomes a "Bart" — watch for the fall.')
        : (best.state === 'done'
          ? 'Price dropped sharply, went flat, then jumped back up — an inverse "Bart". Often a forced seller that ran out; price often holds near the upper level.'
          : 'Price dropped sharply and is now going flat. If it jumps back this becomes an inverse "Bart".'),
      bet: up ? 'For the 15-minute bet this slightly favours NO (price ending lower), if the line is near the current price.'
        : 'For the 15-minute bet this slightly favours YES (price ending higher), if the line is near the current price.'
    };
  }

  function pivots(c, w, hiLo) {
    var out = [];
    for (var i = w; i < c.length - w; i++) {
      var ok = true;
      for (var j = i - w; j <= i + w; j++) {
        if (hiLo > 0 ? c[j].high > c[i].high : c[j].low < c[i].low) { ok = false; break; }
      }
      if (ok) out.push(i);
    }
    return out;
  }

  function doubleTB(c, top) {
    var n = c.length, ps = pivots(c, 3, top ? 1 : -1).filter(function (i) { return i > n - 120; });
    for (var x = ps.length - 1; x >= 1; x--) {
      for (var y = x - 1; y >= 0; y--) {
        var i1 = ps[y], i2 = ps[x];
        if (i2 - i1 < 8 || i2 - i1 > 70) continue;
        var p1 = top ? c[i1].high : c[i1].low, p2 = top ? c[i2].high : c[i2].low;
        if (Math.abs(p2 / p1 - 1) > 0.0012) continue;
        var mid = i1, mp = p1;
        for (var k = i1; k <= i2; k++) {
          var v = top ? c[k].low : c[k].high;
          if (top ? v < mp : v > mp) { mp = v; mid = k; }
        }
        if (Math.abs(Math.log(mp / p1)) < 0.0025) continue;
        var last = c[n - 1].close, confirmed = top ? last < mp : last > mp;
        return {
          type: top ? 'dtop' : 'dbot', name: top ? 'Double top' : 'Double bottom', state: confirmed ? 'done' : 'forming',
          dir: top ? -1 : 1, points: [{ i: i1, price: p1 }, { i: mid, price: mp }, { i: i2, price: p2 }].concat(confirmed ? [{ i: n - 1, price: last }] : []),
          label: top ? (confirmed ? 'DOUBLE TOP ↓' : 'M?') : (confirmed ? 'DOUBLE BOTTOM ↑' : 'W?'),
          meaning: top ? 'Price hit the same high twice and failed both times (an "M" shape). ' + (confirmed ? 'It has now broken below the dip between them — sellers are in control.' : 'If it breaks below the dip in the middle, it confirms.')
            : 'Price hit the same low twice and bounced both times (a "W" shape). ' + (confirmed ? 'It has now broken above the bump between them — buyers are in control.' : 'If it breaks above the bump in the middle, it confirms.'),
          bet: top ? 'Leans toward NO on bets whose line is near or above the current price.' : 'Leans toward YES on bets whose line is near or below the current price.'
        };
      }
    }
    return null;
  }

  function breakout(c) {
    var n = c.length;
    if (n < 70) return null;
    var hi = -Infinity, lo = Infinity;
    for (var i = n - 61; i < n - 1; i++) { hi = Math.max(hi, c[i].high); lo = Math.min(lo, c[i].low); }
    var last = c[n - 1].close;
    if (last <= hi && last >= lo) return null;
    var up = last > hi, lvl = up ? hi : lo;
    return {
      type: 'brk', name: up ? 'Breakout up' : 'Breakdown', state: 'done', dir: up ? 1 : -1,
      points: [{ i: n - 61, price: lvl }, { i: n - 1, price: lvl }], label: up ? 'BREAKOUT ↑' : 'BREAKDOWN ↓', level: true,
      meaning: 'Price just left the range it held for the last hour (' + (up ? 'above' : 'below') + ' the line). In our 6-month test these did NOT reliably keep going — half the time price came back.',
      bet: 'No reliable lean for the 15-minute bet; Kalshi prices react to this already.'
    };
  }

  function detect(candles) {
    if (!candles || candles.length < 40) return [];
    var rets = [];
    for (var i = Math.max(1, candles.length - 120); i < candles.length; i++) rets.push(Math.log(candles[i].close / candles[i - 1].close));
    var sig = sd(rets) || 1e-4;
    return [bart(candles, sig, 1), bart(candles, sig, -1), doubleTB(candles, true), doubleTB(candles, false), breakout(candles)]
      .filter(Boolean)
      .sort(function (a, b) { return b.points[b.points.length - 1].i - a.points[a.points.length - 1].i; })
      .slice(0, 3);
  }

  var Patterns = { detect: detect };
  if (typeof module !== 'undefined' && module.exports) module.exports = Patterns;
  else root.Patterns = Patterns;
})(this);
