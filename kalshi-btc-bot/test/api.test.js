const test = require('node:test');
const assert = require('node:assert');
const Api = require('../web/js/api.js');

function fakeTransport(reply) {
  const sent = [];
  return { sent, request: async (req) => { sent.push(req); return { status: 201, text: JSON.stringify(reply(req)) }; } };
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
