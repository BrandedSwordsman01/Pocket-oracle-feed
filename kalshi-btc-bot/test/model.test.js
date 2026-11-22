const test = require('node:test');
const assert = require('node:assert');
const M = require('../web/js/model.js');

const close = (a, b, tol) => assert.ok(Math.abs(a - b) < tol, `${a} != ${b}`);

test('normal and t4 CDFs are sane', () => {
  close(M.normCdf(0), 0.5, 1e-9);
  close(M.normCdf(1.96), 0.975, 1e-4);
  close(M.t4Cdf(0), 0.5, 1e-12);
  assert.ok(M.t4Cdf(10) > 0.999 && M.t4Cdf(-10) < 0.001);
  // monotone
  let prev = 0;
  for (let z = -6; z <= 6; z += 0.1) { const v = M.t4Cdf(z); assert.ok(v >= prev); prev = v; }
  // unit-variance t4 has fatter tails than the normal
  assert.ok(1 - M.t4Cdf(3) > 1 - M.normCdf(3));
});

test('t4Cdf matches numeric integral of the scaled t4 density', () => {
  // raw t4 pdf: 3/8 * (1 + t^2/4)^(-5/2); scaled z = t / sqrt(2)
  const pdf = (z) => Math.SQRT2 * 0.375 * Math.pow(1 + (2 * z * z) / 4, -2.5);
  let acc = 0.5; const h = 1e-4;
  for (let i = 0; i < 20000; i++) acc += pdf((i + 0.5) * h) * h;
  close(M.t4Cdf(2), acc, 1e-6);
});

test('probAbove behaves', () => {
  close(M.probAbove(100000, 100000, 0.0007, 60), 0.5, 1e-9);
  assert.ok(M.probAbove(100000, 99000, 0.0007, 60) > 0.8);
  assert.ok(M.probAbove(100000, 101000, 0.0007, 60) < 0.2);
  assert.strictEqual(M.probAbove(100, 90, 0.001, 0), 1);
  // more time -> closer to 50/50
  assert.ok(M.probAbove(100000, 99500, 0.0007, 120) < M.probAbove(100000, 99500, 0.0007, 10));
});

test('marketYesProb handles strike types', () => {
  const S = 100000, v = 0.0007, t = 30;
  const above = M.marketYesProb({ strike_type: 'greater', floor_strike: 99800 }, S, v, t);
  const below = M.marketYesProb({ strike_type: 'less', cap_strike: 99800 }, S, v, t);
  close(above + below, 1, 1e-12);
  const between = M.marketYesProb({ strike_type: 'between', floor_strike: 99750, cap_strike: 100249.99 }, S, v, t);
  assert.ok(between > 0 && between < 1);
  assert.strictEqual(M.marketYesProb({ strike_type: 'custom' }, S, v, t), null);
});

test('ewmaVol recovers a known volatility', () => {
  // deterministic +/- sigma returns => realized vol = sigma
  const s = 0.0008; const closes = [100000];
  for (let i = 0; i < 500; i++) closes.push(closes[i] * Math.exp(i % 2 ? s : -s));
  close(M.ewmaVol(closes, 60), s, 1e-9);
});

test('taker fee matches Kalshi formula', () => {
  assert.strictEqual(M.takerFeeCents(1, 50), 2);    // 0.07*0.25 = $0.0175 -> 2c
  assert.strictEqual(M.takerFeeCents(100, 50), 175); // $1.75
  assert.strictEqual(M.takerFeeCents(10, 10), 7);    // 0.07*10*0.09 = 0.063 -> 7c
  assert.strictEqual(M.takerFeeCents(0, 50), 0);
});

test('bestTrade picks the side with edge and respects price bounds', () => {
  const cfg = { minPrice: 8, maxPrice: 92 };
  const m = { yes_ask: 40, no_ask: 62 };
  const t = M.bestTrade(m, 0.55, cfg);
  assert.strictEqual(t.side, 'yes');
  assert.ok(t.edge > 10 && t.edge < 15);
  const n = M.bestTrade(m, 0.2, cfg);
  assert.strictEqual(n.side, 'no');
  assert.strictEqual(M.bestTrade({ yes_ask: 97, no_ask: 4 }, 0.5, cfg), null);
  // dollar-string fields
  assert.strictEqual(M.priceCents({ yes_ask_dollars: '0.4300' }, 'yes_ask'), 43);
});

test('sizeContracts is fractional Kelly and capped', () => {
  const cfg = { kellyFraction: 0.15, maxTradeCents: 500, maxContractsPerTrade: 50 };
  assert.strictEqual(M.sizeContracts(40, 50, 10000, cfg), 0); // no edge
  const n = M.sizeContracts(60, 50, 10000, cfg); // f = 0.2*0.15 = 3% of $100 = $3 -> 6 contracts
  assert.strictEqual(n, 6);
  assert.strictEqual(M.sizeContracts(90, 50, 1e9, cfg), 10); // capped at $5
});

test('blend is anchored on the market price', () => {
  close(M.blend(0.5, 0.5), 1 / (1 + Math.exp(0.068)), 1e-12);
  // model disagreeing hard only nudges the market a few cents
  const p = M.blend(0.99, 0.5);
  assert.ok(p > 0.55 && p < 0.65, String(p));
  assert.ok(M.blend(0.3, 0.8) < 0.8 && M.blend(0.3, 0.8) > 0.7);
});

test('per-series blends: 15-min weights the vol model more than hourly', () => {
  const h = M.blend(0.9, 0.5, 'KXBTCD'), q = M.blend(0.9, 0.5, 'KXBTC15M');
  assert.ok(q > h, `${q} <= ${h}`);
  assert.strictEqual(M.blend(0.9, 0.5), h); // default = hourly
  assert.strictEqual(M.SERIES.KXBTC15M.maxMinutes, 14);
});
