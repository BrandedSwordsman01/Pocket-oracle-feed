/*
 * Pricing model for Kalshi BTC price contracts ("Will BTC be above $K at time T?").
 *
 * Idea (standard binary-option pricing, nothing proprietary):
 *   1. Estimate short-term BTC volatility from recent 1-minute candles (EWMA).
 *   2. Assume log-price moves until expiry are fat-tailed (Student-t, 4 dof,
 *      scaled to the same variance) with zero drift.
 *   3. P(BTC_T > K) = 1 - F( ln(K/S) / (sigma * sqrt(minutes)) ).
 *   4. Compare to the price on Kalshi. Only trade when the model's fair value
 *      beats the ask by more than fees + a safety margin.
 *
 * This is a reasonable baseline, not a money printer. Market makers on Kalshi
 * run this same math with better data. Use the calibration panel in the app to
 * see whether the model actually beats the market before going live.
 */
(function (root) {
  'use strict';

  // Abramowitz & Stegun 7.1.26 erf approximation (|error| < 1.5e-7).
  function erf(x) {
    var sign = x < 0 ? -1 : 1;
    x = Math.abs(x);
    var t = 1 / (1 + 0.3275911 * x);
    var y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return sign * y;
  }

  function normCdf(z) {
    return 0.5 * (1 + erf(z / Math.SQRT2));
  }

  // CDF of a Student-t with 4 dof, rescaled to unit variance (raw t4 has variance 2).
  function t4Cdf(z) {
    var t = z * Math.SQRT2;
    var s = t * t;
    return 0.5 + 0.75 * (t / Math.sqrt(4 + s)) * (1 - s / (3 * (4 + s)));
  }

  function cdf(z, dist) {
    return dist === 'normal' ? normCdf(z) : t4Cdf(z);
  }

  /**
   * EWMA volatility of 1-minute log returns.
   * @param {number[]} closes oldest -> newest
   * @param {number} halfLifeMin
   * @returns {number} sigma per sqrt(minute), as a fraction (0.0005 = 0.05%)
   */
  function ewmaVol(closes, halfLifeMin) {
    if (!closes || closes.length < 3) return NaN;
    var lambda = Math.pow(0.5, 1 / (halfLifeMin || 60));
    var variance = null;
    for (var i = 1; i < closes.length; i++) {
      if (!(closes[i] > 0) || !(closes[i - 1] > 0)) continue;
      var r = Math.log(closes[i] / closes[i - 1]);
      variance = variance === null ? r * r : lambda * variance + (1 - lambda) * r * r;
    }
    return variance === null ? NaN : Math.sqrt(variance);
  }

  /** P(S_T > strike). */
  function probAbove(spot, strike, sigmaPerMin, minutes, opts) {
    opts = opts || {};
    var volMult = opts.volMult || 1;
    if (minutes <= 0) return spot > strike ? 1 : 0;
    var sd = sigmaPerMin * volMult * Math.sqrt(minutes);
    if (!(sd > 0)) return spot > strike ? 1 : 0;
    var z = Math.log(strike / spot) / sd;
    return 1 - cdf(z, opts.dist);
  }

  /**
   * Model probability that a Kalshi market resolves YES. Returns null when the
   * market's structure isn't understood (better to skip than guess).
   */
  function marketYesProb(market, spot, sigmaPerMin, minutes, opts) {
    var type = String(market.strike_type || '').toLowerCase();
    var floor = num(market.floor_strike);
    var cap = num(market.cap_strike);
    if ((type === 'greater' || type === 'greater_or_equal') && isFinite(floor)) {
      return probAbove(spot, floor, sigmaPerMin, minutes, opts);
    }
    if ((type === 'less' || type === 'less_or_equal') && isFinite(cap)) {
      return 1 - probAbove(spot, cap, sigmaPerMin, minutes, opts);
    }
    if (type === 'between' && isFinite(floor) && isFinite(cap) && cap > floor) {
      return probAbove(spot, floor, sigmaPerMin, minutes, opts) - probAbove(spot, cap, sigmaPerMin, minutes, opts);
    }
    return null;
  }

  function num(v) {
    if (v === null || v === undefined || v === '') return NaN;
    return Number(v);
  }

  /** Read a price in cents from a market, supporting both cent and *_dollars fields. */
  function priceCents(market, field) {
    var c = market[field];
    if (typeof c === 'number' && isFinite(c)) return c;
    var d = market[field + '_dollars'];
    if (d !== undefined && d !== null && d !== '') return Math.round(Number(d) * 100);
    return NaN;
  }

  /** Kalshi taker fee for one order: ceil(0.07 * C * P * (1-P)) dollars, in cents. */
  function takerFeeCents(count, price) {
    if (count <= 0) return 0;
    var n = 7 * count * price * (100 - price); // fee in cents * 10000
    return Math.floor((n + 9999) / 10000);
  }

  /**
   * Find the better side to buy (YES or NO) and its edge after fees.
   * @returns {{side:'yes'|'no', price:number, fair:number, edge:number}|null}
   *          price/fair/edge are in cents per contract.
   */
  function bestTrade(market, pYes, cfg) {
    var yesAsk = priceCents(market, 'yes_ask');
    var noAsk = priceCents(market, 'no_ask');
    var options = [];
    if (isFinite(yesAsk)) options.push({ side: 'yes', price: yesAsk, fair: pYes * 100 });
    if (isFinite(noAsk)) options.push({ side: 'no', price: noAsk, fair: (1 - pYes) * 100 });
    var best = null;
    for (var i = 0; i < options.length; i++) {
      var o = options[i];
      if (o.price < cfg.minPrice || o.price > cfg.maxPrice) continue;
      // Fee per contract on a typical small order (fee rounding makes 1-lots expensive).
      o.edge = o.fair - o.price - takerFeeCents(10, o.price) / 10;
      if (!best || o.edge > best.edge) best = o;
    }
    return best;
  }

  /**
   * Fractional-Kelly position size, capped by max dollars per trade.
   * Kelly for a binary bought at price a with win prob p: f = (p - a) / (1 - a).
   */
  function sizeContracts(fairCents, priceCents, bankrollCents, cfg) {
    var p = fairCents / 100;
    var a = priceCents / 100;
    if (p <= a || a <= 0 || a >= 1) return 0;
    var f = ((p - a) / (1 - a)) * cfg.kellyFraction;
    var budget = Math.min(bankrollCents * f, cfg.maxTradeCents);
    var count = Math.floor(budget / priceCents + 1e-9);
    return Math.max(0, Math.min(count, cfg.maxContractsPerTrade));
  }

  /** Brier score of (prob, outcome 0/1) pairs; lower is better. */
  function brier(pairs) {
    if (!pairs.length) return NaN;
    var s = 0;
    for (var i = 0; i < pairs.length; i++) s += Math.pow(pairs[i][0] - pairs[i][1], 2);
    return s / pairs.length;
  }

  /**
   * Market-anchored blends, fitted by logistic regression per series:
   *   logit(p) = bias + wVol * logit(p_vol) + wMkt * logit(market_mid)
   * KXBTCD (hourly): 30 days, Oct 23 - Nov 11 2026 train. Test: +5.3c/contract, 67 trades.
   * KXBTC15M (15-min): 14 days, Nov 8 - Nov 17 2026 train. Test: +5.3c/contract, 108 trades, 72% win.
   *   UPDATE (60-day study, docs/backtest_results_15m.md): walk-forward +1.15c/contract, n=370, t=0.51.
   *   The 14-day result was noise; treat both blends as UNPROVEN and paper trade only.
   * (research/backtest.py, research/test15.py). Raw vol-model trading lost money on both.
   */
  var SERIES = {
    KXBTCD:   { bias: -0.068, wVol: 0.108, wMkt: 0.986, volMult: 1.15, avgAdj: false, minMinutes: 10, maxMinutes: 90, label: 'Hourly (KXBTCD)' },
    KXBTC15M: { bias: -0.009, wVol: 0.380, wMkt: 0.629, volMult: 1.0,  avgAdj: true,  minMinutes: 2,  maxMinutes: 14, label: '15-minute (KXBTC15M)' }
  };
  var BLEND = SERIES.KXBTCD;
  function logit(p) { p = Math.min(Math.max(p, 0.01), 0.99); return Math.log(p / (1 - p)); }
  function blend(pVol, mid, series) {
    var b = SERIES[series] || BLEND;
    var x = b.bias + b.wVol * logit(pVol) + b.wMkt * logit(mid);
    return 1 / (1 + Math.exp(-x));
  }

  var Model = {
    blend: blend, BLEND: BLEND, SERIES: SERIES,
    erf: erf, normCdf: normCdf, t4Cdf: t4Cdf, ewmaVol: ewmaVol, probAbove: probAbove,
    marketYesProb: marketYesProb, priceCents: priceCents, takerFeeCents: takerFeeCents,
    bestTrade: bestTrade, sizeContracts: sizeContracts, brier: brier
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = Model;
  else root.Model = Model;
})(this);
