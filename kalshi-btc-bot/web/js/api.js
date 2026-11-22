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

  /** Limit buy at the given price. Priced at the ask, so it fills immediately if liquidity is there. */
  Kalshi.prototype.buy = function (o) {
    var body = {
      ticker: o.ticker,
      client_order_id: o.clientOrderId,
      action: 'buy',
      side: o.side,
      count: o.count,
      type: 'limit'
    };
    body[o.side === 'yes' ? 'yes_price' : 'no_price'] = o.price;
    return this.call('POST', '/portfolio/orders', null, body, true).then(function (d) { return d.order; });
  };

  Kalshi.prototype.order = function (orderId) {
    return this.call('GET', '/portfolio/orders/' + encodeURIComponent(orderId), null, null, true)
      .then(function (d) { return d.order; });
  };

  Kalshi.prototype.cancel = function (orderId) {
    return this.call('DELETE', '/portfolio/orders/' + encodeURIComponent(orderId), null, null, true);
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
        candles: candles.map(function (c) { return { time: c[0], low: c[1], high: c[2], open: c[3], close: c[4] }; })
      };
    });
  };

  var Api = { Kalshi: Kalshi, Prices: Prices, defaultTransport: defaultTransport, FetchTransport: FetchTransport, KALSHI_BASE: KALSHI_BASE };
  if (typeof module !== 'undefined' && module.exports) module.exports = Api;
  else root.Api = Api;
})(this);
