const test = require('node:test');
const assert = require('node:assert');
const PaperPerps = require('../web/js/perps.js');
const Ind = require('../web/js/indicators.js');

const close = (a, b, tol) => assert.ok(Math.abs(a - b) < tol, `${a} != ${b}`);

test('paper long: pnl, fees and take-profit', () => {
  const pp = new PaperPerps(null);
  const p = pp.open({ ticker: 'X', dir: 1, margin: 100, leverage: 5, price: 6, maxLeverage: 6.9, takeProfit: 6.06 });
  assert.strictEqual(p.qty, 83);              // floor(500 / 6)
  close(pp.state.cash, 1000 - 100 - 83 * 6 * PaperPerps.FEE, 1e-9);
  const ev = pp.mark('X', 6.07);
  assert.strictEqual(ev[0].reason, 'take-profit');
  // pnl = 83 * 0.06 - entry fee - exit fee
  close(ev[0].pnl, 83 * 0.06 - 83 * 6 * 0.0012 - 83 * 6.06 * 0.0012, 1e-9);
  assert.deepStrictEqual(pp.state.positions, {});
});

test('paper short: stop-loss and liquidation can not lose more than margin', () => {
  const pp = new PaperPerps(null);
  pp.open({ ticker: 'X', dir: -1, margin: 50, leverage: 6, price: 6, maxLeverage: 6.9, stopLoss: 6.1 });
  assert.strictEqual(pp.mark('X', 6.11)[0].reason, 'stop-loss');
  const p = pp.open({ ticker: 'X', dir: -1, margin: 50, leverage: 6, price: 6, maxLeverage: 6.9 });
  assert.ok(p.liq > 6 && p.liq < 6.6);
  const ev = pp.mark('X', 7);
  assert.strictEqual(ev[0].reason, 'liquidated');
  assert.ok(ev[0].pnl >= -50 - 1 && ev[0].pnl < 0);
});

test('paper rejects leverage above market max and oversize orders', () => {
  const pp = new PaperPerps(null);
  assert.throws(() => pp.open({ ticker: 'X', dir: 1, margin: 10, leverage: 10, price: 6, maxLeverage: 6.9 }), /leverage/);
  assert.throws(() => pp.open({ ticker: 'X', dir: 1, margin: 5000, leverage: 2, price: 6, maxLeverage: 6.9 }), /cash/);
});

test('indicators: RSI/EMA/VWAP sanity', () => {
  const up = Array.from({ length: 50 }, (_, i) => 100 + i);
  assert.strictEqual(Ind.rsi(up, 14)[49], 100);
  const e = Ind.ema(Array(30).fill(5), 9);
  assert.strictEqual(e[29], 5);
  const candles = Array.from({ length: 300 }, (_, i) => ({ time: i * 60, open: 100, high: 101, low: 99, close: 100, volume: 1 }));
  close(Ind.vwap(candles, 240)[299], 100, 1e-9);
  const bb = Ind.bollinger(candles.map((c) => c.close), 20, 2);
  assert.strictEqual(bb.upper[299], 100);
});

test('setup detector: breakout and EMA cross fire on a clean trend change', () => {
  const candles = [];
  for (let i = 0; i < 400; i++) {
    const px = i < 300 ? 100 : 100 + (i - 300) * 0.3; // flat, then a clean uptrend
    candles.push({ time: i * 60, open: px, high: px + 0.05, low: px - 0.05, close: px, volume: 1 });
  }
  const ev = Ind.detect(candles);
  const kinds = new Set(ev.filter((e) => e.index >= 300).map((e) => e.setup + (e.dir > 0 ? '+' : '-')));
  assert.ok(kinds.has('breakout_60+'), [...kinds].join(','));
  assert.ok(kinds.has('ema_cross+'), [...kinds].join(','));
  // no lookahead: detecting on a prefix gives the same events up to that point
  const pre = Ind.detect(candles.slice(0, 350)).map((e) => e.setup + e.time);
  const full = ev.filter((e) => e.index < 350).map((e) => e.setup + e.time);
  assert.deepStrictEqual(pre, full);
});
