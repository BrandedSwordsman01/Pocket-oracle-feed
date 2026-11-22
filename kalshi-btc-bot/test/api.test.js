const test = require('node:test');
const assert = require('node:assert');
const Api = require('../web/js/api.js');

function fakeTransport(reply) {
  const sent = [];
  return { sent, request: async (req) => { sent.push(req); const r = reply(req); const st = r.__status || 201; delete r.__status; return { status: st, text: JSON.stringify(r) }; } };
}

test('hourly buy uses V2 /portfolio/events/orders with YES-book mapping', async () => {
  const t = fakeTransport(() => ({ order_id: 'o1', fill_count: '3.00', remaining_count: '0.00', ts_ms: 1 }));
  const k = new Api.Kalshi(t, 'prod');
  const yes = await k.buy({ ticker: 'T', side: 'yes', count: 3, price: 42, clientOrderId: 'c1' });
  let body = JSON.parse(t.sent[0].body);
  assert.strictEqual(t.sent[0].url, 'https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders');
  assert.deepStrictEqual(t.sent[0].auth, { env: 'prod', path: '/trade-api/v2/portfolio/events/orders' });
  assert.strictEqual(body.side, 'bid');
  assert.strictEqual(body.price, '0.4200');
  assert.strictEqual(body.count, '3.00');
  assert.strictEqual(body.time_in_force, 'good_till_canceled');
  assert.deepStrictEqual([yes.order_id, yes.fill_count, yes.status], ['o1', 3, 'executed']);

  await k.buy({ ticker: 'T', side: 'no', count: 1, price: 30, clientOrderId: 'c2' }); // buy NO @30c = sell YES @70c
  body = JSON.parse(t.sent[1].body);
  assert.strictEqual(body.side, 'ask');
  assert.strictEqual(body.price, '0.7000');
});

test('cancel uses V2 path; demo env signs for demo host', async () => {
  const t = fakeTransport(() => ({ order_id: 'o1', reduced_by: '1.00' }));
  await new Api.Kalshi(t, 'demo').cancel('o1');
  assert.strictEqual(t.sent[0].method, 'DELETE');
  assert.strictEqual(t.sent[0].url, 'https://demo-api.kalshi.co/trade-api/v2/portfolio/events/orders/o1');
  assert.strictEqual(t.sent[0].auth.env, 'demo');
});

test('perps order body matches the margin API', async () => {
  const t = fakeTransport(() => ({ order_id: 'p1', fill_count: '0', remaining_count: '1' }));
  await new Api.Perps(t, 'prod').createOrder({ ticker: 'KXBTCPERP', clientOrderId: 'x', side: 'bid', count: 2, price: 6.12345, postOnly: true, timeInForce: 'good_till_canceled' });
  const b = JSON.parse(t.sent[0].body);
  assert.strictEqual(t.sent[0].url, 'https://external-api.kalshi.com/trade-api/v2/margin/orders');
  assert.strictEqual(t.sent[0].auth.path, '/trade-api/v2/margin/orders');
  assert.deepStrictEqual([b.side, b.count, b.price, b.post_only, b.time_in_force], ['bid', '2', '6.1235', true, 'good_till_canceled']);
});

test('cancel routes to the market shard via market_ticker (query not signed)', async () => {
  const t = fakeTransport(() => ({}));
  await new Api.Kalshi(t, 'prod').cancel('o9', 'KXBTC15M-X');
  assert.strictEqual(t.sent[0].url, 'https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders/o9?market_ticker=KXBTC15M-X');
  assert.strictEqual(t.sent[0].auth.path, '/trade-api/v2/portfolio/events/orders/o9');
});

test('shard balances, transfer in centicents, and buy auto-funds the crypto shard first', async () => {
  let shard2 = '0.0000';
  const t = fakeTransport((req) => {
    if (req.url.includes('/portfolio/balance')) return { balance: 1961, balance_breakdown: [{ exchange_index: 0, balance: '19.6100' }, { exchange_index: 2, balance: shard2 }] };
    if (req.url.includes('intra_exchange_instance_transfer')) { shard2 = '1.0000'; return { transfer_id: 't1' }; }
    if (shard2 === '0.0000') return { __status: 400, error: { code: 'insufficient_balance', message: 'insufficient balance' } };
    return { order_id: 'o1', fill_count: '0.00', remaining_count: '2.00' };
  });
  const k = new Api.Kalshi(t, 'prod');
  const b = await k.balanceByShard();
  assert.deepStrictEqual(b, { total: 1961, shards: { 0: 1961, 2: 0 } });
  global.setTimeout = ((orig) => (fn, ms) => orig(fn, 0))(global.setTimeout); // skip the settle wait in tests
  await k.buy({ ticker: 'KXBTC15M-X', side: 'yes', count: 2, price: 40, clientOrderId: 'c', exchangeIndex: 2 });
  const tr = t.sent.find((r) => r.url.includes('intra_exchange_instance_transfer'));
  const body = JSON.parse(tr.body);
  assert.strictEqual(body.source_exchange_shard, 0);
  assert.strictEqual(body.destination_exchange_shard, 2);
  assert.strictEqual(body.source, 'event_contract');
  assert.strictEqual(body.amount % 100, 0);                 // centicents
  assert.strictEqual(body.client_transfer_id, undefined);   // must be a UUID if present; we omit it
  assert.ok(t.sent.findIndex((r) => r.url.endsWith('/portfolio/events/orders')) < t.sent.indexOf(tr)); // order tried first
  assert.ok(body.amount >= (2 * 40 + 2) * 100);              // covers cost + fee
  assert.ok(t.sent[t.sent.length - 1].url.endsWith('/portfolio/events/orders'));  // order sent after funding
});
