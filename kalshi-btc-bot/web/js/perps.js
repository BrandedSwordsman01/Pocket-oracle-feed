/*
 * Paper trading engine for perpetual futures (one-way positions, isolated margin per position).
 * Prices are dollars per contract (KXBTCPERP contract = 0.0001 BTC).
 * Fee assumption: 0.12% of notional per side (Kalshi's documented example taker rate);
 * the live account's real rate is shown in the Perps tab when a key is connected.
 */
(function (root) {
  'use strict';

  var FEE = 0.0012;
  var MAINT = 0.5; // liquidate when losses eat 50% of posted margin (conservative vs a real exchange)

  function PaperPerps(storage, key) {
    this.storage = storage;
    this.key = key || 'kbot_perps_paper';
    var s = storage && storage.get(this.key);
    this.state = s && s.positions ? s : { cash: 1000, positions: {}, history: [] };
  }

  PaperPerps.prototype.save = function () { if (this.storage) this.storage.set(this.key, this.state); };

  PaperPerps.prototype.reset = function (cash) {
    this.state = { cash: cash || 1000, positions: {}, history: [] };
    this.save();
  };

  /** Liquidation price for an isolated position. */
  function liqPrice(p) {
    var move = (p.margin * MAINT) / Math.abs(p.qty);
    return p.qty > 0 ? p.entry - move : p.entry + move;
  }

  /**
   * Open a position: dir +1 long / -1 short, margin in dollars, leverage x, fill at price.
   * One position per ticker; opening in the opposite direction closes the existing one first.
   */
  PaperPerps.prototype.open = function (o) {
    var price = Number(o.price), lev = Number(o.leverage), margin = Number(o.margin);
    if (!(price > 0 && lev >= 1 && margin > 0)) throw new Error('bad order');
    if (lev > o.maxLeverage) throw new Error('leverage above market max ' + o.maxLeverage.toFixed(1) + 'x');
    var existing = this.state.positions[o.ticker];
    if (existing && Math.sign(existing.qty) !== o.dir) this.close(o.ticker, price, 'flip');
    var qty = Math.floor((margin * lev) / price);
    if (qty < 1) throw new Error('size below 1 contract');
    var fee = qty * price * FEE;
    if (margin + fee > this.state.cash + 1e-9) throw new Error('not enough paper cash');
    this.state.cash -= margin + fee;
    var p = this.state.positions[o.ticker];
    if (p) {
      var tot = Math.abs(p.qty) + qty;
      p.entry = (p.entry * Math.abs(p.qty) + price * qty) / tot;
      p.qty = o.dir * tot;
      p.margin += margin;
      p.fees += fee;
    } else {
      p = this.state.positions[o.ticker] = { ticker: o.ticker, qty: o.dir * qty, entry: price, margin: margin, fees: fee, openedAt: Date.now() };
    }
    if (o.takeProfit) p.tp = Number(o.takeProfit);
    if (o.stopLoss) p.sl = Number(o.stopLoss);
    p.leverage = (Math.abs(p.qty) * p.entry) / p.margin;
    p.liq = liqPrice(p);
    this.save();
    return p;
  };

  PaperPerps.prototype.close = function (ticker, price, reason) {
    var p = this.state.positions[ticker];
    if (!p) return null;
    var fee = Math.abs(p.qty) * price * FEE;
    var pnl = p.qty * (price - p.entry);
    var back = Math.max(0, p.margin + pnl - fee); // isolated: can't lose more than margin
    this.state.cash += back;
    var realized = back - p.margin - p.fees;
    this.state.history.unshift({ ticker: ticker, qty: p.qty, entry: p.entry, exit: price, pnl: realized, reason: reason || 'manual', at: Date.now() });
    this.state.history = this.state.history.slice(0, 200);
    delete this.state.positions[ticker];
    this.save();
    return realized;
  };

  /** Mark to market; fires take-profit, stop-loss and liquidation. Returns list of {ticker, reason, pnl}. */
  PaperPerps.prototype.mark = function (ticker, price) {
    var p = this.state.positions[ticker];
    if (!p || !(price > 0)) return [];
    p.mark = price;
    p.upnl = p.qty * (price - p.entry);
    var long = p.qty > 0, reason = null;
    if (long ? price <= p.liq : price >= p.liq) reason = 'liquidated';
    else if (p.sl && (long ? price <= p.sl : price >= p.sl)) reason = 'stop-loss';
    else if (p.tp && (long ? price >= p.tp : price <= p.tp)) reason = 'take-profit';
    if (!reason) { this.save(); return []; }
    var fill = reason === 'liquidated' ? p.liq : reason === 'stop-loss' ? p.sl : p.tp;
    return [{ ticker: ticker, reason: reason, pnl: this.close(ticker, fill, reason) }];
  };

  PaperPerps.prototype.equity = function () {
    var e = this.state.cash, ps = this.state.positions;
    Object.keys(ps).forEach(function (k) { e += ps[k].margin + (ps[k].upnl || 0); });
    return e;
  };

  PaperPerps.FEE = FEE;
  PaperPerps.liqPrice = liqPrice;
  if (typeof module !== 'undefined' && module.exports) module.exports = PaperPerps;
  else root.PaperPerps = PaperPerps;
})(this);
