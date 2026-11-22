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
    // spot 100000, strike 98000, 45 min left: vol model ~99% but market at 50c -> blend still sees ~7c edge on YES
    { ticker: 'KXBTCD-A', event_ticker: 'EV1', strike_type: 'greater', floor_strike: 98000, yes_bid: 49, yes_ask: 51, no_bid: 49, no_ask: 51, close_time: close },
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

test('AEGIS strategy: trades a clearly mispriced 15-min contract, passes a fair one', async () => {
  global.Aegis = require('../web/js/aegis.js');
  let t = Date.parse('2026-11-22T15:05:00Z');
  const now = () => t;
  const nowS = Math.floor(t / 1000);
  const candles = Array.from({ length: 300 }, (_, i) => {
    const px = 60000 * Math.exp(((i % 2) - 0.5) * 0.0008);
    return { time: nowS - 60 * (300 - i), open: px, high: px, low: px, close: px, volume: 1 };
  });
  const prices = { snapshot: async () => ({ spot: 60000, closes: candles.map((c) => c.close), candles, lastCandleTs: nowS - 60 }) };
  const close = new Date(t + 6 * 60000).toISOString();
  const mk = (ticker, strike, bid, ask) => ({ ticker, event_ticker: 'E', strike_type: 'greater_or_equal', floor_strike: strike,
    yes_bid: bid, yes_ask: ask, no_bid: 100 - ask, no_ask: 100 - bid, close_time: close });
  const kalshi = fakeKalshi(now);
  kalshi.markets = [mk('CHEAP', 59700, 60, 62)];   // spot 0.5% above strike, 6 min left: fair ~ 0.97+, ask 62c
  const bot = new Bot({ kalshi, prices, mode: 'paper', storage: memStorage(), now, config: { series: 'KXBTC15M', strategy: 'aegis', minMinutes: 2, maxMinutes: 14 } });
  await bot.tick();
  assert.strictEqual(bot.state.positions.length, 1);
  assert.strictEqual(bot.state.positions[0].side, 'yes');

  const kalshi2 = fakeKalshi(now);
  kalshi2.markets = [mk('FAIR', 60000, 49, 51)];
  const bot2 = new Bot({ kalshi: kalshi2, prices, mode: 'paper', storage: memStorage(), now, config: { series: 'KXBTC15M', strategy: 'aegis', minMinutes: 2, maxMinutes: 14 } });
  await bot2.tick();
  assert.strictEqual(bot2.state.positions.length, 0);
});
