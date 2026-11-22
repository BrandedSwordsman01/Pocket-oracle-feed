/*
 * Network layer: Kalshi REST client + Coinbase BTC price feed.
 *
 * In the Android app every request goes through the native bridge
 * (window.Native), which avoids browser CORS limits and signs Kalshi requests
 * with your private key inside Java. The key never enters JavaScript.
 * In a desktop browser (for UI testing) it falls back to fetch(); only
 * public endpoints work there.
 */
(function (root) {
  'use strict';

  var KALSHI_BASE = {
    prod: 'https://api.elections.kalshi.com/trade-api/v2',
    demo: 'https://demo-api.kalshi.co/trade-api/v2'
  };
  var API_PREFIX = '/trade-api/v2';

  function NativeTransport(nativeObj) {
    var pending = {};
    var nextId = 1;
    root.__nativeResolve = function (id, status, text) {
      var p = pending[id];
      if (!p) return;
      delete pending[id];
      p.resolve({ status: status, text: text });
    };
    this.request = function (req) {
      var id = nextId++;
      return new Promise(function (resolve, reject) {
        pending[id] = { resolve: resolve, reject: reject };
        setTimeout(function () {
          if (pending[id]) { delete pending[id]; reject(new Error('timeout ' + req.url)); }
        }, 20000);
        nativeObj.request(id, req.method, req.url, req.body || '',
          req.auth ? req.auth.env : '', req.auth ? req.auth.path : '');
      });
    };
  }

  function FetchTransport(fetchFn) {
    this.request = function (req) {
      if (req.auth) return Promise.reject(new Error('Signed requests need the Android app (no key in browser).'));
      return fetchFn(req.url, {
        method: req.method,
        headers: req.body ? { 'Content-Type': 'application/json' } : {},
        body: req.body || undefined
      }).then(function (r) { return r.text().then(function (t) { return { status: r.status, text: t }; }); });
    };
  }

  function defaultTransport() {
    if (root.Native && typeof root.Native.request === 'function') return new NativeTransport(root.Native);
    return new FetchTransport(root.fetch.bind(root));
  }

  function parse(res, what) {
    var data = null;
    try { data = res.text ? JSON.parse(res.text) : null; } catch (e) { /* not json */ }
    if (res.status < 200 || res.status >= 300) {
      var msg = (data && (data.error && (data.error.message || data.error.code) || data.message)) || res.text || '';
      throw new Error(what + ' failed (HTTP ' + res.status + '): ' + String(msg).slice(0, 200));
    }
    return data;
  }

  function qs(query) {
    var parts = [];
    for (var k in query) {
      if (query[k] !== undefined && query[k] !== null && query[k] !== '') {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(query[k]));
      }
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  function Kalshi(transport, env) {
    this.t = transport;
    this.env = env === 'demo' ? 'demo' : 'prod';
    this.base = KALSHI_BASE[this.env];
  }

  Kalshi.prototype.call = function (method, path, query, body, auth) {
    var req = {
      method: method,
      url: this.base + path + qs(query || {}),
      body: body ? JSON.stringify(body) : null,
      // Kalshi signs timestamp + METHOD + path, where path excludes the query string.
      auth: auth ? { env: this.env, path: API_PREFIX + path } : null
    };
    return this.t.request(req).then(function (res) { return parse(res, method + ' ' + path); });
  };

  /** All open markets in a series (e.g. KXBTCD), following pagination. */
  Kalshi.prototype.openMarkets = function (series) {
    var self = this;
    var all = [];
    function page(cursor, n) {
      return self.call('GET', '/markets', { series_ticker: series, status: 'open', limit: 200, cursor: cursor })
        .then(function (d) {
          all = all.concat((d && d.markets) || []);
          if (d && d.cursor && n < 4) return page(d.cursor, n + 1);
          return all;
        });
    }
    return page('', 0);
  };

  /** Every market in one event (any status); used to read settlement results in bulk. */
  Kalshi.prototype.marketsByEvent = function (eventTicker) {
    return this.call('GET', '/markets', { event_ticker: eventTicker, limit: 200 })
      .then(function (d) { return (d && d.markets) || []; });
  };

  Kalshi.prototype.market = function (ticker) {
    return this.call('GET', '/markets/' + encodeURIComponent(ticker)).then(function (d) { return d.market; });
  };

  Kalshi.prototype.balanceCents = function () {
    return this.call('GET', '/portfolio/balance', null, null, true).then(function (d) { return d.balance; });
  };

  /**
   * Balance split by exchange shard. Kalshi runs markets on separate shards (crypto = shard 2) and API orders only
   * use the money already on that shard (docs.kalshi.com/getting_started/exchange_sharding). Returns cents.
   */
  Kalshi.prototype.balanceByShard = function () {
    return this.call('GET', '/portfolio/balance', null, null, true).then(function (d) {
      var shards = {};
      (d.balance_breakdown || []).forEach(function (b) { shards[b.exchange_index || 0] = Math.round(parseFloat(b.balance) * 100) || 0; });
      if (!Object.keys(shards).length) shards[0] = d.balance || 0;
      return { total: d.balance || 0, shards: shards };
    });
  };

  /** Move cash between exchange shards of the event-contract account (amount in cents; the API takes centicents). */
  Kalshi.prototype.moveBetweenShards = function (amountCents, fromShard, toShard) {
    return this.call('POST', '/portfolio/intra_exchange_instance_transfer', null, {
      source: 'event_contract', destination: 'event_contract',   // client_transfer_id must be a UUID if sent; omitted
      amount: Math.round(amountCents) * 100, source_exchange_shard: fromShard, destination_exchange_shard: toShard
    }, true);
  };

  /** Make sure the shard holding `marketExchangeIndex` has at least `needCents`; tops it up from other shards if not. */
  Kalshi.prototype.ensureShardFunds = async function (needCents, shard) {
    shard = shard || 0;
    var b = await this.balanceByShard();
    var have = b.shards[shard] || 0;
    if (have >= needCents) return { moved: 0, have: have };
    var short = needCents - have, moved = 0;
    var others = Object.keys(b.shards).map(Number).filter(function (k) { return k !== shard && b.shards[k] > 0; })
      .sort(function (a, c) { return b.shards[c] - b.shards[a]; });
    for (var i = 0; i < others.length && moved < short; i++) {
      var amt = Math.min(b.shards[others[i]], short - moved + 5); // +5c buffer for fee rounding
      await this.moveBetweenShards(amt, others[i], shard);
      moved += amt;
    }
    if (moved > 0) await new Promise(function (r) { setTimeout(r, 2500); }); // transfers settle asynchronously
    return { moved: moved, have: have + moved };
  };

  /**
   * Limit buy at the given price (cents), via Kalshi's V2 order endpoint (the legacy
   * /portfolio/orders writes now return 410 Gone). V2 quotes everything on the YES book:
   * buying YES at p = bid at p; buying NO at q = selling YES (ask) at 1 - q.
   */
  /** Try the order first; only if Kalshi says the shard is short of cash, top it up and retry once. */
  Kalshi.prototype.buy = async function (o) {
    try {
      return await this._buy(o);
    } catch (e) {
      var shard = o.exchangeIndex;
      if (!/insufficient/i.test(e.message) || shard === undefined || shard === null || this.autoFund === false) throw e;
      var need = o.count * o.price + Math.ceil(7 * o.count * o.price * (100 - o.price) / 10000) + 1;
      var f = await this.ensureShardFunds(need, shard);
      if (!f.moved) throw new Error('Not enough money on Kalshi for this order (' + e.message + ')');
      return this._buy(o);
    }
  };

  Kalshi.prototype._buy = function (o) {
    var yesPrice = o.side === 'yes' ? o.price : 100 - o.price;
    var body = {
      ticker: o.ticker,
      client_order_id: o.clientOrderId,
      side: o.side === 'yes' ? 'bid' : 'ask',
      count: Number(o.count).toFixed(2),
      price: (yesPrice / 100).toFixed(4),
      time_in_force: 'good_till_canceled',
      self_trade_prevention_type: 'taker_at_cross',
      cancel_order_on_pause: true
    };
    return this.call('POST', '/portfolio/events/orders', null, body, true).then(function (d) {
      var r = (d && d.order) || d || {};
      var remaining = parseFloat(r.remaining_count);
      return {
        order_id: r.order_id, fill_count: parseFloat(r.fill_count) || 0,
        status: remaining > 0 ? 'resting' : 'executed', raw: r
      };
    });
  };

  Kalshi.prototype.order = function (orderId) {
    return this.call('GET', '/portfolio/orders/' + encodeURIComponent(orderId), null, null, true)
      .then(function (d) { return d.order; });
  };

  /** Cancel by order ID. market_ticker routes the request to the market's exchange shard (crypto = shard 2). */
  Kalshi.prototype.cancel = function (orderId, ticker) {
    return this.call('DELETE', '/portfolio/events/orders/' + encodeURIComponent(orderId),
      ticker ? { market_ticker: ticker } : null, null, true);
  };

  /**
   * Kalshi Perps (margin) API: same auth as event contracts, endpoints under /margin.
   * Docs: https://docs.kalshi.com/margin.md. Prices/counts are fixed-point strings.
   */
  var PERPS_BASE = {
    prod: 'https://external-api.kalshi.com/trade-api/v2',
    demo: 'https://external-api.demo.kalshi.co/trade-api/v2'
  };

  function Perps(transport, env) {
    Kalshi.call(this, transport, env);
    this.base = PERPS_BASE[this.env];
  }
  Perps.prototype = Object.create(Kalshi.prototype);

  Perps.prototype.market = function (ticker) {
    return this.call('GET', '/margin/markets/' + encodeURIComponent(ticker)).then(function (d) { return d.market || d; });
  };
  Perps.prototype.orderbook = function (ticker, depth) {
    return this.call('GET', '/margin/markets/' + encodeURIComponent(ticker) + '/orderbook', { depth: depth || 8 })
      .then(function (d) { return d.orderbook || d; });
  };
  Perps.prototype.candles = function (ticker, minutes) {
    var now = Math.floor(Date.now() / 1000);
    return this.call('GET', '/margin/markets/' + encodeURIComponent(ticker) + '/candlesticks',
      { start_ts: now - minutes * 60, end_ts: now, period_interval: 1 }).then(function (d) { return d.candlesticks || []; });
  };
  Perps.prototype.funding = function (ticker) {
    return this.call('GET', '/margin/funding_rates/estimate', { ticker: ticker });
  };
  Perps.prototype.enabled = function () { return this.call('GET', '/margin/enabled', null, null, true); };
  Perps.prototype.balance = function () { return this.call('GET', '/margin/balance', null, null, true); };
  Perps.prototype.positions = function (ticker) {
    return this.call('GET', '/margin/positions', { ticker: ticker }, null, true).then(function (d) { return d.positions || []; });
  };
  Perps.prototype.orders = function (ticker) {
    return this.call('GET', '/margin/orders', { ticker: ticker, status: 'resting' }, null, true).then(function (d) { return d.orders || []; });
  };
  Perps.prototype.feeRates = function () { return this.call('GET', '/margin/fee_tier_rates', null, null, true); };
  /** side: 'bid' (buy / long) or 'ask' (sell / short). price in dollars per contract. */
  Perps.prototype.createOrder = function (o) {
    var body = {
      ticker: o.ticker,
      client_order_id: o.clientOrderId,
      side: o.side,
      count: String(o.count),
      price: Number(o.price).toFixed(4),
      time_in_force: o.timeInForce || 'immediate_or_cancel',
      self_trade_prevention_type: 'taker_at_cross',
      reduce_only: !!o.reduceOnly,
      post_only: !!o.postOnly,
      cancel_order_on_pause: true
    };
    return this.call('POST', '/margin/orders', null, body, true);
  };
  Perps.prototype.cancelOrder = function (orderId) {
    return this.call('DELETE', '/margin/orders/' + encodeURIComponent(orderId), null, null, true);
  };
  /** Bracket take-profit / stop-loss on an open position. mode: 'cross' or 'isolated'. */
  Perps.prototype.setBracket = function (ticker, mode, stopLoss, takeProfit) {
    var body = { kind: 'bracket' };
    if (stopLoss) body.stop_loss_price = Number(stopLoss).toFixed(4);
    if (takeProfit) body.take_profit_price = Number(takeProfit).toFixed(4);
    var path = '/margin/' + (mode === 'isolated' ? 'isolated' : 'cross') + '/positions/' + encodeURIComponent(ticker) + '/exit_trigger';
    return this.call('PUT', path, null, body, true);
  };

  /** Coinbase public BTC-USD data, a close proxy for the CF Benchmarks index Kalshi settles on. */
  function Prices(transport) { this.t = transport; }

  Prices.prototype.snapshot = function () {
    var t = this.t;
    var base = 'https://api.exchange.coinbase.com/products/BTC-USD';
    return Promise.all([
      t.request({ method: 'GET', url: base + '/candles?granularity=60' }).then(function (r) { return parse(r, 'Coinbase candles'); }),
      t.request({ method: 'GET', url: base + '/ticker' }).then(function (r) { return parse(r, 'Coinbase ticker'); })
    ]).then(function (res) {
      // Candles are [time, low, high, open, close, volume], newest first.
      var candles = res[0].slice().sort(function (a, b) { return a[0] - b[0]; });
      var closes = candles.map(function (c) { return c[4]; });
      var spot = Number(res[1].price);
      return {
        spot: spot, closes: closes, lastCandleTs: candles.length ? candles[candles.length - 1][0] : 0,
        candles: candles.map(function (c) { return { time: c[0], low: c[1], high: c[2], open: c[3], close: c[4], volume: c[5] }; })
      };
    });
  };

  var Api = { Kalshi: Kalshi, Perps: Perps, PERPS_BASE: PERPS_BASE, Prices: Prices, defaultTransport: defaultTransport, FetchTransport: FetchTransport, KALSHI_BASE: KALSHI_BASE };
  if (typeof module !== 'undefined' && module.exports) module.exports = Api;
  else root.Api = Api;
})(this);
