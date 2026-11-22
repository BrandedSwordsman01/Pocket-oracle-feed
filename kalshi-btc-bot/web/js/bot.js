/*
 * The trading loop. Each tick it:
 *   1. pulls BTC spot + 1-minute candles, estimates volatility
 *   2. pulls open Kalshi markets in the series (default KXBTCD, hourly/daily BTC price)
 *   3. prices each market with the model, records calibration samples
 *   4. buys the best few edges that pass every risk check
 *   5. settles finished positions and tracks P&L
 *
 * Modes:
 *   paper - real prod market data, simulated fills, fake $100 bankroll. No keys needed.
 *   demo  - real orders on Kalshi's demo exchange (fake money, demo API keys).
 *   live  - real orders with real money.
 */
(function (root) {
  'use strict';

  var Model = root.Model || (typeof require === 'function' ? require('./model.js') : null);

  var DEFAULTS = {
    series: 'KXBTCD',
    intervalSec: 30,
    minMinutes: 10,          // don't trade the last minutes: settlement averaging + latency risk
    maxMinutes: 90,
    minEdgeCents: 4,         // edge after fees, per contract
    maxEdgeCents: 25,        // an "edge" this big is almost always bad data; skip it
    minPrice: 8,             // avoid lottery tickets and near-certainties
    maxPrice: 92,
    kellyFraction: 0.15,
    maxTradeCents: 500,      // $5 per trade
    maxContractsPerTrade: 50,
    maxOpenExposureCents: 2000,  // $20 at risk at once
    dailyLossLimitCents: 1000,   // stop for the day after -$10
    maxOrdersPerTick: 2,
    volHalfLifeMin: 60,
    volMult: 1.15,           // be a bit more uncertain than history says
    minVolPerMin: 0.0003,
    dist: 't4',
    maxPriceStaleSec: 240,
    paperBankrollCents: 10000,
    cancelRestingAfterSec: 20
  };

  function Bot(deps) {
    this.kalshi = deps.kalshi;          // Api.Kalshi for the mode's environment
    this.prices = deps.prices;          // Api.Prices
    this.mode = deps.mode || 'paper';
    this.storage = deps.storage;        // {get(key), set(key, value)}
    this.now = deps.now || function () { return Date.now(); };
    this.onLog = deps.onLog || function () {};
    this.cfg = Object.assign({}, DEFAULTS, deps.config || {});
    this.state = this.loadState();
    this.last = {};                     // last tick snapshot for the UI
    this.busy = false;
  }

  Bot.DEFAULTS = DEFAULTS;

  Bot.prototype.key = function () { return 'kbot_state_' + this.mode; };

  Bot.prototype.loadState = function () {
    var s = this.storage && this.storage.get(this.key());
    return s && s.positions ? s : { positions: [], calib: {}, haltedDay: null };
  };

  Bot.prototype.save = function () { if (this.storage) this.storage.set(this.key(), this.state); };

  Bot.prototype.log = function (msg) { this.onLog(new Date(this.now()).toLocaleTimeString() + '  ' + msg); };

  Bot.prototype.dayKey = function () {
    var d = new Date(this.now());
    return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
  };

  Bot.prototype.stats = function () {
    var startOfDay = new Date(this.now()); startOfDay.setHours(0, 0, 0, 0);
    var s = { realized: 0, today: 0, open: 0, openCost: 0, wins: 0, losses: 0 };
    this.state.positions.forEach(function (p) {
      if (p.status === 'settled') {
        s.realized += p.pnl;
        if (p.settledAt >= startOfDay.getTime()) s.today += p.pnl;
        if (p.pnl > 0) s.wins++; else s.losses++;
      } else {
        s.open++;
        s.openCost += p.price * p.count + p.fee;
      }
    });
    var pm = [], pk = [];
    var calib = this.state.calib;
    Object.keys(calib).forEach(function (t) {
      var c = calib[t];
      if (c.outcome === 0 || c.outcome === 1) { pm.push([c.pModel, c.outcome]); pk.push([c.pMkt, c.outcome]); }
    });
    s.calibN = pm.length;
    s.brierModel = Model.brier(pm);
    s.brierMarket = Model.brier(pk);
    return s;
  };

  Bot.prototype.tick = function () {
    var self = this;
    if (self.busy) return Promise.resolve();
    self.busy = true;
    return self._tick().catch(function (e) {
      self.log('ERROR ' + (e && e.message || e));
    }).then(function () { self.busy = false; self.save(); });
  };

  Bot.prototype._tick = async function () {
    var cfg = this.cfg;
    var now = this.now();

    await this.settle();
    if (this.mode !== 'paper') await this.reconcile();

    var snap = await this.prices.snapshot();
    var ageSec = now / 1000 - snap.lastCandleTs;
    var sigma = Math.max(Model.ewmaVol(snap.closes.slice(-240), cfg.volHalfLifeMin), cfg.minVolPerMin);
    this.last = { spot: snap.spot, sigma: sigma, at: now, markets: [] };
    if (!(snap.spot > 0) || ageSec > cfg.maxPriceStaleSec) {
      this.log('Price feed stale (' + Math.round(ageSec) + 's), not trading.');
      return;
    }

    var markets = await this.kalshi.openMarkets(cfg.series);
    var stats = this.stats();

    var halted = this.state.haltedDay === this.dayKey();
    if (!halted && stats.today <= -cfg.dailyLossLimitCents) {
      this.state.haltedDay = this.dayKey();
      halted = true;
      this.log('Daily loss limit hit (' + fmt(stats.today) + '). Done for today.');
    }

    var held = {};
    this.state.positions.forEach(function (p) { if (p.status !== 'settled') held[p.ticker] = true; });

    var candidates = [];
    var opts = { volMult: cfg.volMult, dist: cfg.dist };
    for (var i = 0; i < markets.length; i++) {
      var m = markets[i];
      var minutes = (Date.parse(m.close_time) - now) / 60000;
      if (!(minutes >= cfg.minMinutes && minutes <= cfg.maxMinutes)) continue;
      var p = Model.marketYesProb(m, snap.spot, sigma, minutes, opts);
      if (p === null || !isFinite(p)) continue;

      this.recordCalibration(m, p);

      var t = Model.bestTrade(m, p, cfg);
      if (t) this.last.markets.push({ ticker: m.ticker, p: p, side: t.side, price: t.price, edge: t.edge, minutes: minutes });
      if (held[m.ticker] || !t) continue;
      if (t.edge >= cfg.minEdgeCents && t.edge <= cfg.maxEdgeCents) {
        candidates.push({ m: m, t: t, p: p, minutes: minutes });
      }
    }
    this.last.markets.sort(function (a, b) { return b.edge - a.edge; });
    this.last.markets = this.last.markets.slice(0, 8);

    if (halted || !candidates.length) return;

    candidates.sort(function (a, b) { return b.t.edge - a.t.edge; });
    var bankroll = this.mode === 'paper'
      ? cfg.paperBankrollCents + stats.realized - stats.openCost
      : await this.kalshi.balanceCents();
    var exposure = stats.openCost;

    for (var j = 0; j < Math.min(cfg.maxOrdersPerTick, candidates.length); j++) {
      var c = candidates[j];
      var count = Model.sizeContracts(c.t.fair, c.t.price, bankroll, cfg);
      var room = cfg.maxOpenExposureCents - exposure;
      count = Math.min(count, Math.floor(room / (c.t.price + 2)));
      if (count < 1) continue;
      var fee = Model.takerFeeCents(count, c.t.price);
      var pos = {
        id: c.m.ticker + '-' + now,
        ticker: c.m.ticker, event: c.m.event_ticker, side: c.t.side,
        count: count, price: c.t.price, fee: fee, fair: Math.round(c.t.fair),
        openedAt: now, closeTime: Date.parse(c.m.close_time), status: 'open'
      };
      if (this.mode === 'paper') {
        this.log('PAPER BUY ' + count + ' ' + pos.side.toUpperCase() + ' ' + pos.ticker + ' @' + pos.price + '¢ (fair ' + pos.fair + '¢, edge ' + c.t.edge.toFixed(1) + '¢)');
      } else {
        var order = await this.kalshi.buy({
          ticker: pos.ticker, side: pos.side, count: count, price: pos.price,
          clientOrderId: 'kbot-' + now + '-' + j
        });
        pos.orderId = order && order.order_id;
        pos.requested = count;
        pos.status = 'pending';
        this.log((this.mode === 'live' ? 'LIVE' : 'DEMO') + ' ORDER ' + count + ' ' + pos.side.toUpperCase() + ' ' + pos.ticker + ' @' + pos.price + '¢ (fair ' + pos.fair + '¢)');
      }
      this.state.positions.push(pos);
      exposure += count * pos.price + fee;
    }
  };

  Bot.prototype.recordCalibration = function (m, pModel) {
    var calib = this.state.calib;
    if (calib[m.ticker]) return;
    var bid = Model.priceCents(m, 'yes_bid');
    var ask = Model.priceCents(m, 'yes_ask');
    if (!(bid > 0 && ask > 0 && ask < 100 && ask >= bid)) return;
    var pMkt = (bid + ask) / 200;
    if (pMkt < 0.05 || pMkt > 0.95) return; // only score informative markets
    calib[m.ticker] = { pModel: pModel, pMkt: pMkt, event: m.event_ticker, closeTime: Date.parse(m.close_time), outcome: null };
  };

  /** Resolve finished positions and calibration samples, a few events per tick. */
  Bot.prototype.settle = async function () {
    var now = this.now();
    var self = this;
    var events = {};
    this.state.positions.forEach(function (p) {
      if (p.status === 'open' && now > p.closeTime + 60000 && p.event) events[p.event] = true;
    });
    var calib = this.state.calib;
    Object.keys(calib).forEach(function (t) {
      var c = calib[t];
      if (c.outcome !== null) return;
      if (now - c.closeTime > 3 * 86400000) { delete calib[t]; return; }
      if (now > c.closeTime + 60000 && c.event) events[c.event] = true;
    });
    var list = Object.keys(events).slice(0, 5);
    for (var i = 0; i < list.length; i++) {
      var ms;
      try { ms = await this.kalshi.marketsByEvent(list[i]); } catch (e) { this.log('settle: ' + e.message); continue; }
      var results = {};
      ms.forEach(function (m) { if (m.result === 'yes' || m.result === 'no') results[m.ticker] = m.result; });
      this.state.positions.forEach(function (p) {
        if (p.status !== 'open' || !results[p.ticker]) return;
        var payout = results[p.ticker] === p.side ? 100 * p.count : 0;
        p.pnl = payout - p.price * p.count - p.fee;
        p.result = results[p.ticker];
        p.status = 'settled';
        p.settledAt = now;
        self.log('SETTLED ' + p.ticker + ' ' + p.side.toUpperCase() + ' -> ' + p.result.toUpperCase() + ' ' + fmt(p.pnl));
      });
      Object.keys(results).forEach(function (t) {
        if (calib[t] && calib[t].outcome === null) calib[t].outcome = results[t] === 'yes' ? 1 : 0;
      });
    }
    // keep storage bounded
    var keys = Object.keys(calib);
    if (keys.length > 3000) {
      keys.sort(function (a, b) { return calib[a].closeTime - calib[b].closeTime; });
      keys.slice(0, keys.length - 3000).forEach(function (k) { delete calib[k]; });
    }
    var settled = this.state.positions.filter(function (p) { return p.status === 'settled'; });
    if (settled.length > 500) {
      var drop = settled.slice(0, settled.length - 500);
      this.state.positions = this.state.positions.filter(function (p) { return drop.indexOf(p) < 0; });
    }
  };

  /** Demo/live: update fills on our orders, cancel anything left resting. */
  Bot.prototype.reconcile = async function () {
    var now = this.now();
    var pending = this.state.positions.filter(function (p) { return p.status === 'pending' && p.orderId; });
    for (var i = 0; i < pending.length; i++) {
      var p = pending[i];
      var o;
      try { o = await this.kalshi.order(p.orderId); } catch (e) { this.log('order check: ' + e.message); continue; }
      var status = String(o && o.status || '');
      if (status === 'resting' && now - p.openedAt > this.cfg.cancelRestingAfterSec * 1000) {
        try { await this.kalshi.cancel(p.orderId); this.log('Canceled unfilled rest of ' + p.ticker); } catch (e) { this.log('cancel: ' + e.message); }
        continue;
      }
      if (status === 'resting') continue;
      var filled = fillCount(o, p.requested);
      if (filled <= 0) {
        p.status = 'void';
        this.log('No fill on ' + p.ticker + ', dropped.');
      } else {
        p.count = filled;
        p.fee = typeof o.taker_fees === 'number' ? o.taker_fees : Model.takerFeeCents(filled, p.price);
        p.status = 'open';
        this.log('Filled ' + filled + '/' + p.requested + ' ' + p.ticker);
      }
    }
    this.state.positions = this.state.positions.filter(function (p) { return p.status !== 'void'; });
  };

  function fillCount(o, requested) {
    if (typeof o.fill_count === 'number') return o.fill_count;
    if (o.fill_count_fp !== undefined) return Math.floor(Number(o.fill_count_fp));
    var initial = typeof o.initial_count === 'number' ? o.initial_count : requested;
    if (typeof o.remaining_count === 'number') return initial - o.remaining_count;
    return o.status === 'executed' ? requested : 0;
  }

  /** Kill switch: cancel every order the bot still has resting. */
  Bot.prototype.cancelAll = async function () {
    if (this.mode === 'paper') return;
    var pending = this.state.positions.filter(function (p) { return p.status === 'pending' && p.orderId; });
    for (var i = 0; i < pending.length; i++) {
      try { await this.kalshi.cancel(pending[i].orderId); } catch (e) { /* already filled or gone */ }
    }
  };

  function fmt(cents) {
    var s = cents < 0 ? '-' : '+';
    return s + '$' + (Math.abs(cents) / 100).toFixed(2);
  }

  Bot.fmt = fmt;
  if (typeof module !== 'undefined' && module.exports) module.exports = Bot;
  else root.Bot = Bot;
})(this);
