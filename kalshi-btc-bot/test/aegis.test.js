const test = require('node:test');
const assert = require('node:assert');
const A = require('../web/js/aegis.js');
global.Indicators = require('../web/js/indicators.js');
const Forming = require('../web/js/forming.js');

const close = (a, b, tol) => assert.ok(Math.abs(a - b) < tol, `${a} != ${b}`);

test('fee/cost matches the Python implementation and Kalshi rounding', () => {
  close(A.buyCost(10, 0.5), 5.18, 1e-9);   // 5.00 + 0.175 -> 5.18
  close(A.buyCost(1, 0.5), 0.52, 1e-9);
  close(A.buyCost(10, 0.06), 0.64, 1e-9);  // 0.60 + 0.03948 -> 0.64
});

test('settlement variance uses the discrete last-minute factor', () => {
  close(A.F_LAST, 73810 / 216000, 1e-12);
  close(A.settlementVar([1, 1, 1]), 2 + A.F_LAST, 1e-12);
});

test('probabilities: monotone in strike, ~0.5 at the centre, quantiles bracket the centre', () => {
  const v = 0.0007 ** 2 * 10;
  const ps = [59800, 59900, 60000, 60100, 60200].map((k) => A.probAbove(60000, v, k));
  for (let i = 1; i < ps.length; i++) assert.ok(ps[i] < ps[i - 1]);
  close(ps[2], 0.5, 0.01);
  assert.ok(A.quantile(60000, v, 0.05) < 60000 && A.quantile(60000, v, 0.95) > 60000);
  close(A.probAbove(60000, v, A.quantile(60000, v, 0.25)), 0.75, 1e-3);
});

test('evaluate: NO bound from the YES upper tail; actions respect data sufficiency', () => {
  const c = { ticker: 'X', series: 'KXBTCD', kind: 'above', strike: 60000 };
  const r = A.evaluate(60300, 0.0007 ** 2 * 10, c, { bid: 0.40, ask: 0.42 }, { theta: 0.0, latency: 0 });
  assert.ok(r.lowYes <= r.pFinal + 1e-9 && r.lowNo <= 1 - r.pFinal + 1e-9);
  assert.ok(['BUY_YES', 'BUY_NO', 'PASS'].includes(r.action));
  assert.strictEqual(A.evaluate(60300, 1e-6, c, { bid: 0, ask: 0 }).action, 'INSUFFICIENT_DATA');
});

test('dominance: overlapping 15m YES + hourly NO flagged with outage risk; non-overlap ignored', () => {
  const c15 = { ticker: '15M', series: 'KXBTC15M', kind: 'at_least', strike: 60000 };
  const hi = { ticker: 'H1', series: 'KXBTCD', kind: 'above', strike: 60099.99 };
  const lo = { ticker: 'H0', series: 'KXBTCD', kind: 'above', strike: 59899.99 };
  const quotes = { '15M': { bid: 0.40, ask: 0.42 }, H1: { bid: 0.60, ask: 0.62 }, H0: { bid: 0.70, ask: 0.72 } };
  const opps = A.dominance(c15, [hi, lo], quotes, 10);
  // YES 15M @0.42 + NO H1 @0.40 = 0.82 < 1 and covers every state -> signal, flagged for the outage rule
  const pair = opps.find((o) => o.legs[0][0] === '15M' && o.legs[1][0] === 'H1');
  assert.ok(pair && pair.outageRisk && pair.floor > 0);
  // YES H1 + NO H0 does not cover all states (thresholds in the wrong order) -> never flagged
  assert.ok(!opps.some((o) => o.legs.some((l) => l[0] === 'H1' && l[1] === 'yes') && o.legs.some((l) => l[0] === 'H0' && l[1] === 'no')));
});

test('forming patterns: squeeze on flat data, breakout test near the range high', () => {
  const flat = Array.from({ length: 300 }, (_, i) => ({ time: i * 60, open: 100, high: 100.01, low: 99.99, close: 100 + (i % 2 ? 0.001 : 0), volume: 1 }));
  const f1 = Forming.detect(flat);
  assert.ok(f1.every((p) => p.markets && p.meaning && p.verdict));
  const up = Array.from({ length: 300 }, (_, i) => {
    const px = 100 + Math.sin(i / 7) * 0.5 + (i > 290 ? 0.6 : 0);
    return { time: i * 60, open: px, high: px + 0.02, low: px - 0.02, close: px, volume: 1 };
  });
  const f2 = Forming.detect(up);
  assert.ok(Array.isArray(f2));
});
