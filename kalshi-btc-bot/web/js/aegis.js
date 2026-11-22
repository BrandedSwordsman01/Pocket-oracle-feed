/*
 * AEGIS 2.0 in the app: settlement-distribution pricing for Kalshi BTC 15-minute and hourly contracts.
 * Mirrors research/aegis/live.py with frozen parameters exported to js/aegis_params.js by research/export_app_params.py.
 *
 *   log(A_T / C) ~ N(-v/2, v),  v = sum_k sd_k^2 with the final minute weighted by the discrete 60-sample factor (0.3417)
 *   sd_k = sqrt(GARCH forecast_k * diurnal(hour_k)),  C = Coinbase * (1 + basis at the 15M window open)
 *   p_cal = Platt(p_raw); p_final = log-pool(p_cal, market mid, omega)
 *   p_low per side from bootstrap Platt draws (10th percentile), pooled with the market the same way
 *   EDGE_LOW per contract = p_low - (cost incl. fee & rounding)/Q - latency;  action if > theta and depth >= Q
 * (Research model E also includes empirical jumps; the app uses the Gaussian core. See docs/aegis2_mathematics.md.)
 */
(function (root) {
  'use strict';
  var P = root.AEGIS_PARAMS || (typeof require === 'function' ? require('./aegis_params.js') : null);
  var F_LAST = 60 * 61 * 121 / 6 / 3600 / 60; // sum min(j,k)/60^2/60 for 60 one-second samples

  function erf(x) {
    var s = x < 0 ? -1 : 1; x = Math.abs(x);
    var t = 1 / (1 + 0.3275911 * x);
    return s * (1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x));
  }
  function ncdf(z) { return 0.5 * (1 + erf(z / Math.SQRT2)); }
  function logit(p) { p = Math.min(Math.max(p, 1e-4), 1 - 1e-4); return Math.log(p / (1 - p)); }
  function sigmoid(x) { return 1 / (1 + Math.exp(-x)); }
  function ceilTo(x, q) { return Math.ceil(Math.round(x / q * 1e9) / 1e9) * q; }

  /** Kalshi taker cost for Q contracts at price P (dollars): ceil_cent(Q*P + ceil_6dp(0.07 Q P (1-P))). */
  function buyCost(Q, P, mult) {
    var fee = ceilTo((mult || 1) * 0.07 * Q * P * (1 - P), 1e-6);
    return ceilTo(Q * P + fee, 0.01);
  }

  /** GARCH variance filter over recent 1m candles (de-seasonalised), then multi-step forecasts for `minutes` ahead. */
  function sdForecast(candles, tDecision, minutes) {
    var g = P.garch, d = P.diurnal, unc = g.w / (1 - g.a - g.b), h = unc;
    for (var i = 1; i < candles.length; i++) {
      var r = Math.log(candles[i].close / candles[i - 1].close);
      var hr = Math.floor(candles[i].time / 3600) % 24;
      var rd = r / Math.sqrt(d[hr]);
      if (isFinite(rd)) h = g.w + g.a * rd * rd + g.b * h;
    }
    var out = [], p = g.a + g.b;
    for (var k = 0; k < minutes; k++) {
      var hk = unc + Math.pow(p, k) * (h - unc);
      var hour = Math.floor((tDecision + 60 * k) / 3600) % 24;
      out.push(Math.sqrt(hk * d[hour]));
    }
    return out;
  }

  /** Total log-variance of the settlement average with m minutes left (last minute = averaging window). */
  function settlementVar(sd) {
    var v = 0;
    for (var i = 0; i < sd.length; i++) v += sd[i] * sd[i] * (i === sd.length - 1 ? F_LAST : 1);
    return v;
  }

  /** P(A_T >= K) or P(A_T > K) (identical for a continuous distribution; boundaries matter only in the scanner). */
  function probAbove(centre, v, K) {
    var sd = Math.sqrt(Math.max(v, 1e-14));
    return 1 - ncdf((Math.log(K / centre) + 0.5 * v) / sd);
  }

  /** Quantiles of the settlement average (and of price at any intermediate minute, for the chart cone). */
  function quantile(centre, v, q) {
    // inverse normal via Acklam-style rational approximation
    var a = [-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.3577518672690, -30.66479806614716, 2.506628277459239];
    var b = [-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572];
    var c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
    var dd = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416];
    var z, pl = 0.02425, t;
    if (q < pl) { t = Math.sqrt(-2 * Math.log(q)); z = (((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / ((((dd[0] * t + dd[1]) * t + dd[2]) * t + dd[3]) * t + 1); }
    else if (q > 1 - pl) { t = Math.sqrt(-2 * Math.log(1 - q)); z = -(((((c[0] * t + c[1]) * t + c[2]) * t + c[3]) * t + c[4]) * t + c[5]) / ((((dd[0] * t + dd[1]) * t + dd[2]) * t + dd[3]) * t + 1); }
    else { t = q - 0.5; var r2 = t * t; z = (((((a[0] * r2 + a[1]) * r2 + a[2]) * r2 + a[3]) * r2 + a[4]) * r2 + a[5]) * t / (((((b[0] * r2 + b[1]) * r2 + b[2]) * r2 + b[3]) * r2 + b[4]) * r2 + 1); }
    return centre * Math.exp(-0.5 * v + Math.sqrt(v) * z);
  }

  function pool(pm, pk, w) {
    var e = 1e-4, a = Math.pow(pm + e, w) * Math.pow(pk + e, 1 - w), b = Math.pow(1 - pm + e, w) * Math.pow(1 - pk + e, 1 - w);
    return a / (a + b);
  }

  function boundsRaw(pRaw) {
    var ys = P.draws.map(function (ab) { return sigmoid(ab[0] + ab[1] * logit(pRaw)); }).sort(function (x, y) { return x - y; });
    var k = Math.floor(0.10 * ys.length), kh = Math.ceil(0.90 * ys.length) - 1;
    return { lowYes: ys[k], lowNo: 1 - ys[kh] };  // NO bound from the YES upper tail, not 1 - lowYes
  }

  /**
   * Evaluate one contract. q = {bid, ask, askSizeYes, askSizeNo} in dollars; returns probabilities, edges, action.
   */
  function evaluate(centre, v, contract, q, opts) {
    opts = opts || {};
    var Q = opts.Q || 10, lat = opts.latency === undefined ? 0.01 : opts.latency, theta = opts.theta === undefined ? P.theta : opts.theta;
    var pRaw = probAbove(centre, v, contract.strike);
    var res = { strike: contract.strike, series: contract.series, ticker: contract.ticker, pRaw: pRaw };
    if (!q || !(q.bid > 0 && q.ask < 1 && q.ask > q.bid)) { res.action = 'INSUFFICIENT_DATA'; return res; }
    var mid = (q.bid + q.ask) / 2;
    var pCal = sigmoid(P.platt.a + P.platt.b * logit(pRaw));
    var pFinal = pool(pCal, mid, P.omega);
    var bd = boundsRaw(pRaw);
    var lowYes = pool(bd.lowYes, mid, P.omega), lowNo = 1 - pool(1 - bd.lowNo, mid, P.omega);
    var eYes = lowYes - buyCost(Q, q.ask) / Q - lat, eNo = lowNo - buyCost(Q, 1 - q.bid) / Q - lat;
    var side = eYes >= eNo ? 'yes' : 'no', e = Math.max(eYes, eNo);
    var size = side === 'yes' ? q.askSizeYes : q.askSizeNo;
    res.mid = mid; res.pCal = pCal; res.pFinal = pFinal; res.lowYes = lowYes; res.lowNo = lowNo;
    res.edgeLow = e; res.side = side; res.bid = q.bid; res.ask = q.ask;
    res.action = e > theta && (size === undefined || size >= Q) ? (side === 'yes' ? 'BUY_YES' : 'BUY_NO') : 'PASS';
    return res;
  }

  /** Two-leg dominance check for contracts sharing one settlement (min payout over states, after fees). */
  function dominance(c15, hourly, quotes, Q) {
    Q = Q || 10;
    var out = [];
    var all = [c15].concat(hourly).filter(Boolean);
    function pays(c, side, A) {
      var y = c.kind === 'at_least' ? A >= c.strike : A > c.strike;
      return side === 'yes' ? (y ? 1 : 0) : (y ? 0 : 1);
    }
    for (var i = 0; i < all.length; i++) for (var j = i + 1; j < all.length; j++) {
      ['yes', 'no'].forEach(function (sa) { ['yes', 'no'].forEach(function (sb) {
        var a = all[i], b = all[j], qa = quotes[a.ticker], qb = quotes[b.ticker];
        if (!qa || !qb) return;
        var pa = sa === 'yes' ? qa.ask : 1 - qa.bid, pb = sb === 'yes' ? qb.ask : 1 - qb.bid;
        if (!(pa > 0 && pa < 1 && pb > 0 && pb < 1)) return;
        var pts = [a.strike - 0.001, a.strike, a.strike + 0.001, b.strike - 0.001, b.strike, b.strike + 0.001, 0, 1e9];
        var mn = Math.min.apply(null, pts.map(function (A) { return pays(a, sa, A) + pays(b, sb, A); }));
        if (mn < 1) return;
        var floor = Q * mn - buyCost(Q, pa) - buyCost(Q, pb);
        var outage = (a.series === 'KXBTC15M' || b.series === 'KXBTC15M') && a.series !== b.series;
        if (floor > 0) out.push({ legs: [[a.ticker, sa, pa], [b.ticker, sb, pb]], floor: floor, outageRisk: outage });
      }); });
    }
    return out;
  }

  var Aegis = { buyCost: buyCost, sdForecast: sdForecast, settlementVar: settlementVar, probAbove: probAbove, quantile: quantile,
    evaluate: evaluate, dominance: dominance, pool: pool, F_LAST: F_LAST, params: function () { return P; } };
  if (typeof module !== 'undefined' && module.exports) module.exports = Aegis;
  else root.Aegis = Aegis;
})(this);
