const test = require('node:test');
const assert = require('node:assert');
global.Model = require('../web/js/model.js');
const Bot = require('../web/js/bot.js');

function memStorage() {
  const s = {};
  return { get: (k) => (s[k] ? JSON.parse(s[k]) : null), set: (k, v) => { s[k] = JSON.stringify(v); } };
}

function fakePrices(spot, nowMs) {
  const closes = [];
  for (let i = 0; i < 300; i++) closes.push(spot * Math.exp((i % 2) * 0.0007)); // 1-min returns of +/-0.07%
  return { snapshot: async () => ({ spot, closes, lastCandleTs: Math.floor(nowMs() / 1000) - 30 }) };
}

function fakeKalshi(nowMs) {
  const k = {
    markets: [],
    settled: {},
    orders: [],
    openMarkets: async () => k.markets,
    marketsByEvent: async (e) => k.markets.filter((m) => m.event_ticker === e)
      .map((m) => Object.assign({}, m, { result: k.settled[m.ticker] || '' })),
    balanceCents: async () => 10000,
    buy: async (o) => { k.orders.push(o); return { order_id: 'ord' + k.orders.length }; },
    order: async (id) => ({ order_id: id, status: 'executed', fill_count: k.orders[0].count }),
    cancel: async () => ({})
  };
  return k;
}

function setup(mode) {
  let t = Date.parse('2026-11-22T15:00:00Z');
  const now = () => t;
  const kalshi = fakeKalshi(now);
  const close = new Date(t + 45 * 60000).toISOString();
  kalshi.markets = [
    // spot 100000, strike 99700, 45 min left: model fair ~70c vs 62c ask -> edge
    { ticker: 'KXBTCD-A', event_ticker: 'EV1', strike_type: 'greater', floor_strike: 99700, yes_bid: 60, yes_ask: 62, no_bid: 37, no_ask: 39, close_time: close },
    // fairly priced market: no trade
    { ticker: 'KXBTCD-B', event_ticker: 'EV1', strike_type: 'greater', floor_strike: 100000, yes_bid: 48, yes_ask: 51, no_bid: 49, no_ask: 52, close_time: close }
  ];
  const logs = [];
  const bot = new Bot({ kalshi, prices: fakePrices(100000, now), mode, storage: memStorage(), now, onLog: (l) => logs.push(l) });
  return { bot, kalshi, logs, advance: (ms) => { t += ms; } };
}

test('paper mode buys the mispriced market only, then settles it', async () => {
  const { bot, kalshi, logs, advance } = setup('paper');
  await bot.tick();
  const open = bot.state.positions;
  assert.strictEqual(open.length, 1, logs.join('\n'));
  assert.strictEqual(open[0].ticker, 'KXBTCD-A');
  assert.strictEqual(open[0].side, 'yes');
  assert.ok(open[0].count >= 1);
  assert.ok(bot.stats().openCost <= bot.cfg.maxOpenExposureCents);

  // second tick: must not double up on the same market
  await bot.tick();
  assert.strictEqual(bot.state.positions.length, 1);

  // expiry passes, market settles YES
  advance(50 * 60000);
  kalshi.settled['KXBTCD-A'] = 'yes';
  kalshi.settled['KXBTCD-B'] = 'no';
  await bot.tick();
  const p = bot.state.positions[0];
  assert.strictEqual(p.status, 'settled');
  assert.strictEqual(p.pnl, 100 * p.count - p.price * p.count - p.fee);
  const s = bot.stats();
  assert.strictEqual(s.calibN, 2);
  assert.ok(isFinite(s.brierModel) && isFinite(s.brierMarket));
});

test('daily loss limit halts trading', async () => {
  const { bot, kalshi, logs } = setup('paper');
  bot.state.positions.push({ ticker: 'OLD', status: 'settled', pnl: -1500, settledAt: Date.parse('2026-11-22T15:00:00Z'), count: 1, price: 1, fee: 0 });
  await bot.tick();
  assert.strictEqual(bot.state.positions.filter((p) => p.status !== 'settled').length, 0);
  assert.ok(logs.some((l) => l.includes('Daily loss limit')));
});

test('stale price feed blocks trading', async () => {
  const { bot } = setup('paper');
  bot.prices = { snapshot: async () => ({ spot: 100000, closes: [1, 1, 1], lastCandleTs: 0 }) };
  await bot.tick();
  assert.strictEqual(bot.state.positions.length, 0);
});

test('demo mode places a real order and reconciles the fill', async () => {
  const { bot, kalshi } = setup('demo');
  await bot.tick();
  assert.strictEqual(kalshi.orders.length, 1);
  const o = kalshi.orders[0];
  assert.deepStrictEqual(Object.keys(o).sort(), ['clientOrderId', 'count', 'price', 'side', 'ticker']);
  assert.strictEqual(bot.state.positions[0].status, 'pending');
  await bot.tick();
  assert.strictEqual(bot.state.positions[0].status, 'open');
  assert.strictEqual(bot.state.positions[0].count, o.count);
});
