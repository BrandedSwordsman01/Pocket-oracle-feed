"""Paper trader for Kalshi KXBTC15M. Research use only: this file contains NO order-placement code.

Every minute (aligned to the minute boundary, like the backtest) it:
  1. discovers the open 15-minute market and reads its order book (top 5 levels each side)
  2. pulls Coinbase 1m candles (+ Bitstamp spot and Deribit DVOL when reachable)
  3. builds the same causal feature row as the backtest (research/kbt/data.py)
  4. evaluates every configured strategy and journals each decision, traded or not
  5. simulates fills conservatively: entry at the ask + slippage, only if the best level has enough size;
     exits at the bid - slippage; Kalshi taker fees on every fill
  6. settles positions from Kalshi's official result and journals P&L

Journal (JSON lines, append-only) in --journal DIR: signals.jsonl, trades.jsonl, snapshots.jsonl, aegis.jsonl (model
probabilities/actions), scanner.jsonl (shared-settlement hourly ladder + dominance scan), prints.jsonl (Kalshi trades with
exchange timestamps; every record also carries the local ingest `ts`).
Report: python3 research/paper_report.py DIR  ->  DIR/report.html

Usage:
  python3 research/paper_trader.py --journal paper_journal [--minutes 120] [--fit-data DATA_DIR]
"""
import argparse, json, math, os, sys, time, urllib.request, urllib.parse, datetime
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from kbt import data as D, strategies as S, models as M

K_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CB = "https://api.exchange.coinbase.com/products/BTC-USD"


def get(url, params=None, timeout=15):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kbot-paper/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def try_get(url, params=None):
    try:
        return get(url, params)
    except Exception as e:  # network errors are journaled, never fatal
        return {"_error": str(e)}


def parse_book(ob):
    """Kalshi event order book -> best YES bid/ask (dollars) and size at each."""
    b = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes = [(float(p), float(q)) for p, q in (b.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (b.get("no_dollars") or [])]
    if not yes or not no:
        return None
    yb = max(yes); nb = max(no)
    return dict(bid=yb[0], bid_size=yb[1], ask=round(1 - nb[0], 4), ask_size=nb[1], levels=dict(yes=yes, no=no))


def default_strategies(fit_rows=None):
    """Strategies under paper test. Parameters are the pre-registered defaults, not tuned on recent data."""
    out = [S.RSIMomentum(), S.MomentumLag(), S.FairValue(), S.FlipGuard(S.FairValue(), flip_exit=0.5),
           S.LeadingSide(at_min=5)]
    if fit_rows:
        out += [S.Blend().fit(fit_rows), S.Ensemble().fit(fit_rows)]
    return out


class Journal:
    def __init__(self, d):
        os.makedirs(d, exist_ok=True); self.d = d

    def write(self, kind, rec):
        rec = dict(rec, ts=int(time.time()))
        with open(os.path.join(self.d, kind + ".jsonl"), "a") as f:
            f.write(json.dumps(rec, default=float) + "\n")


class PaperTrader:
    def __init__(self, journal, strategies, contracts=10, slippage=0.01):
        self.j, self.strats, self.C, self.slip = journal, strategies, contracts, slippage
        self.pos = {}       # (strategy, ticker) -> position
        self.mids = {}      # ticker -> [(mins, mid)]
        self.dvol = (float("nan"), 0)

    # ---------------------------------------------------------------- features
    def build_row(self, t, m, book):
        candles = get(CB + "/candles", {"granularity": 60})
        candles = sorted(candles, key=lambda c: c[0])
        closes = np.array([c[4] for c in candles], float)
        start = {c[0]: i for i, c in enumerate(candles)}
        i = start.get(t - 60)
        S_source = "candle"
        if i is None:
            # Coinbase's public candle feed can lag by minutes; fall back to the live ticker for spot and keep the
            # last published candles for volatility/RSI. The source is journaled so these rows can be analysed separately.
            if getattr(self, "_retry", 0) < 2:
                return None, "coinbase candle for the last minute not available yet"
            i = len(candles) - 1
            S_source = "ticker"
        close_t = D.iso_ts(m["close_time"]); mins = round((close_t - t) / 60)
        rsi1 = D.wilder_rsi(closes, 14); sig = D.ewma_sigma(closes, 60)
        five = [k for k, c in enumerate(candles) if (c[0] + 60) % 300 == 0 and k <= i]
        rsi5 = D.wilder_rsi(closes[five], 14) if len(five) > 15 else [float("nan")]
        K = float(m["floor_strike"])
        j_open = start.get(close_t - 900 - 60)
        basis = K / closes[j_open] - 1 if j_open is not None else float("nan")
        bs = try_get("https://www.bitstamp.net/api/v2/ticker/btcusd/")
        if time.time() - self.dvol[1] > 300:
            dv = try_get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
                         {"currency": "BTC", "start_timestamp": (t - 600) * 1000, "end_timestamp": t * 1000, "resolution": 60})
            data_ = (dv.get("result") or {}).get("data") or []
            self.dvol = (float(data_[-1][4]) if data_ else float("nan"), time.time())
        mid = (book["bid"] + book["ask"]) / 2
        hist = self.mids.setdefault(m["ticker"], [])
        prev = [x for x in hist if x[0] >= mins + 5]
        hist.append((mins, mid))
        S_ = closes[i]
        if S_source == "ticker":
            tk = try_get(CB + "/ticker")
            if "price" in tk:
                S_ = float(tk["price"])
        row = dict(S_source=S_source, candle_lag_s=int(t - 60 - candles[-1][0]),
            ticker=m["ticker"], close=close_t, t=t, mins=mins, y=None, K=K, bid=book["bid"], ask=book["ask"], mid=mid,
            spread=book["ask"] - book["bid"], quote_age=0, fresh=0 < book["bid"] < book["ask"] < 1,
            S=S_, S_bs=float(bs["last"]) if "last" in bs else float("nan"), basis=basis, sigma=float(sig[i]),
            rsi1=float(rsi1[i]), rsi5=float(rsi5[-1]), mom5=math.log(S_ / closes[i - 5]), mom15=math.log(S_ / closes[i - 15]),
            dvol=self.dvol[0], hour=datetime.datetime.fromtimestamp(t, datetime.UTC).hour,
            dow=datetime.datetime.fromtimestamp(t, datetime.UTC).weekday(), dmid5=(mid - prev[-1][1]) if prev else float("nan"),
        )
        return row, None

    # ---------------------------------------------------------------- one decision minute
    def step(self, t):
        self.settle()
        ms = try_get(K_BASE + "/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 10})
        if "_error" in ms:
            self.j.write("signals", dict(t=t, event="error", detail=ms["_error"])); return
        live = [m for m in ms.get("markets", []) if m.get("floor_strike") is not None and D.iso_ts(m["close_time"]) > t]
        if not live:
            self.j.write("signals", dict(t=t, event="no_market")); return
        m = min(live, key=lambda x: x["close_time"])
        ob = try_get(K_BASE + f"/markets/{m['ticker']}/orderbook", {"depth": 5})
        book = parse_book(ob) if "_error" not in ob else None
        if not book:
            self.j.write("signals", dict(t=t, event="no_book", ticker=m["ticker"])); return
        row, err = self.build_row(t, m, book)
        self._retry = 0
        for _ in range(3):  # Coinbase can publish the just-closed candle late; after 2 retries use the ticker fallback
            if row is not None:
                break
            time.sleep(4)
            self._retry += 1
            row, err = self.build_row(t, m, book)
        if row is None:
            self.j.write("signals", dict(t=t, event="skip", ticker=m["ticker"], detail=err)); return
        self.j.write("snapshots", dict(t=t, ticker=m["ticker"], mins=row["mins"], K=row["K"], S=row["S"], S_bs=row["S_bs"],
                                       bid=book["bid"], ask=book["ask"], bid_size=book["bid_size"], ask_size=book["ask_size"],
                                       levels=book["levels"], sigma=row["sigma"], rsi1=row["rsi1"], dvol=row["dvol"]))
        try:
            self.collect_cross_market(t, m, book, row)
            self.collect_trades(m["ticker"])
        except Exception as e:
            self.j.write("signals", dict(t=t, event="collector_error", detail=repr(e)))
        for s in self.strats:
            key = (s.label(), m["ticker"])
            prob = s.prob(row)
            if key in self.pos:
                pos = self.pos[key]
                if pos.get("closed"):
                    continue
                if s.exit(pos, row):
                    px = (book["bid"] if pos["side"] == "yes" else 1 - book["ask"]) - self.slip
                    size = book["bid_size"] if pos["side"] == "yes" else book["ask_size"]
                    if size < self.C:
                        self.j.write("signals", dict(t=t, strategy=s.label(), ticker=m["ticker"], mins=row["mins"], event="exit_rejected", reason="depth"))
                        continue
                    pos.update(exit_px=px, exit_min=row["mins"], fee_out=M.fee_cents(self.C, px), how="exit", closed=True)
                    self.finish(pos)
                continue
            side = s.entry(row)
            self.j.write("signals", dict(t=t, strategy=s.label(), ticker=m["ticker"], mins=row["mins"], prob=prob,
                                         mid=row["mid"], bid=row["bid"], ask=row["ask"], decision=side or "none"))
            if not side:
                continue
            px = (book["ask"] if side == "yes" else 1 - book["bid"]) + self.slip
            size = book["ask_size"] if side == "yes" else book["bid_size"]
            if size < self.C or px >= 1:
                self.j.write("signals", dict(t=t, strategy=s.label(), ticker=m["ticker"], event="entry_rejected", reason="depth/price"))
                continue
            self.pos[key] = dict(strategy=s.label(), ticker=m["ticker"], side=side, entry_px=px, entry_min=row["mins"],
                                 entry_t=t, close=row["close"], fee_in=M.fee_cents(self.C, px), prob=prob)
            self.j.write("trades", dict(event="open", **self.pos[key]))

    # ---------------------------------------------------------------- AEGIS + hourly ladder + scanner + trade prints
    def collect_cross_market(self, t, m15, book15, row):
        """Journal the hourly ladder settling at the same instant, the dominance scan and AEGIS decisions."""
        try:
            from aegis import settlement as SM, scanner as SC, live as LV
        except Exception as e:
            return
        close = D.iso_ts(m15["close_time"])
        hs = try_get(K_BASE + "/markets", {"series_ticker": "KXBTCD", "status": "open", "limit": 200})
        same = [m for m in hs.get("markets", []) if m.get("close_time") and D.iso_ts(m["close_time"]) == close
                and m.get("strike_type") == "greater" and m.get("floor_strike")]
        same = sorted(same, key=lambda m: abs(float(m["floor_strike"]) - row["S"]))[:6]
        quotes = {m15["ticker"]: (book15["bid"], book15["ask"], book15["ask_size"], book15["bid_size"])}
        cons = [SM.contract_from_market(m15, "KXBTC15M")]
        for m in same:
            ob = try_get(K_BASE + f"/markets/{m['ticker']}/orderbook", {"depth": 3})
            b = parse_book(ob) if "_error" not in ob else None
            if b:
                quotes[m["ticker"]] = (b["bid"], b["ask"], b["ask_size"], b["bid_size"])
                cons.append(SM.contract_from_market(m, "KXBTCD"))
        if len(cons) > 1:
            opps = SC.scan_shared(cons, {k: (v[0], v[1]) for k, v in quotes.items()}, Q=self.C)
            self.j.write("scanner", dict(t=t, close=close, n_hourly=len(cons) - 1, quotes=quotes, opportunities=opps))
        if getattr(self, "aegis", None) is None:
            try:
                self.aegis = LV.LiveAegis()
            except Exception:
                self.aegis = False
        if self.aegis:
            cb = get(CB + "/candles", {"granularity": 60})
            cb = sorted(cb, key=lambda c: c[0])
            sd = self.aegis.sd_forecast([c[0] for c in cb], [c[4] for c in cb], t, row["mins"])
            centre = row["S"] * (1 + row["basis"]) if row["basis"] == row["basis"] else row["S"]
            dec, qs = self.aegis.evaluate(centre, sd, cons, quotes)
            self.j.write("aegis", dict(t=t, close=close, mins=row["mins"], centre=centre, quantiles=list(qs), decisions=dec))

    def collect_trades(self, ticker):
        """Kalshi trade prints with exchange timestamps (journal adds the local ingest ts)."""
        tr = try_get(K_BASE + "/markets/trades", {"ticker": ticker, "limit": 100})
        seen = self.__dict__.setdefault("_seen_trades", set())
        new = [x for x in tr.get("trades", []) if x.get("trade_id") not in seen]
        for x in new:
            seen.add(x.get("trade_id"))
        if new:
            self.j.write("prints", dict(ticker=ticker, trades=new))

    def settle(self):
        now = time.time()
        for key, pos in list(self.pos.items()):
            if pos.get("closed") and "pnl_c" in pos:
                del self.pos[key]; continue
            if pos.get("closed") or now < pos["close"] + 30:
                continue
            m = try_get(K_BASE + f"/markets/{pos['ticker']}")
            res = (m.get("market") or {}).get("result")
            if res in ("yes", "no"):
                win = (res == "yes") == (pos["side"] == "yes")
                pos.update(exit_px=1.0 if win else 0.0, exit_min=0, fee_out=0, how="settle", result=res, closed=True)
                self.finish(pos)

    def finish(self, pos):
        pos["pnl_c"] = (pos["exit_px"] - pos["entry_px"]) * self.C * 100 - pos["fee_in"] - pos["fee_out"]
        self.j.write("trades", dict(event="close", **pos))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="paper_journal")
    ap.add_argument("--minutes", type=int, default=0, help="stop after N decision minutes (0 = run forever)")
    ap.add_argument("--fit-data", default=None, help="history dir to fit blend/ensemble before starting")
    ap.add_argument("--contracts", type=int, default=10)
    ap.add_argument("--slippage", type=float, default=0.01)
    a = ap.parse_args()
    fit_rows = D.load(a.fit_data).rows if a.fit_data else None
    pt = PaperTrader(Journal(a.journal), default_strategies(fit_rows), a.contracts, a.slippage)
    print("Paper trading KXBTC15M (no real orders). Strategies:", ", ".join(s.label() for s in pt.strats), file=sys.stderr)
    n = 0
    while a.minutes == 0 or n < a.minutes:
        t = (int(time.time()) // 60 + 1) * 60
        time.sleep(max(0, t + 4 - time.time()))  # 4s after the boundary so the closed candle is published
        try:
            pt.step(t)
        except Exception as e:
            pt.j.write("signals", dict(t=t, event="error", detail=repr(e)))
        n += 1
    # wait for open positions to settle before exiting
    deadline = time.time() + 20 * 60
    while any(not p.get("closed") for p in pt.pos.values()) and time.time() < deadline:
        time.sleep(30); pt.settle()


if __name__ == "__main__":
    main()
