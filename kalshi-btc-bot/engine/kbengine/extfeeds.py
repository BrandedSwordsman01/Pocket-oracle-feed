"""Derivatives feeds: futures prices, taker flow, liquidations, funding, open interest, implied volatility. No keys needed.

Futures usually react to news before spot. Forced selling and buying (liquidations) push price for minutes.
Any venue your region blocks just shows as disconnected; the model treats its features as missing.
  Deribit, Hyperliquid, Kraken Futures (work from the US) + Binance, Bybit, OKX (blocked in the US)  (websockets)
  Binance open interest, Deribit DVOL (30-day BTC implied volatility)  (REST)
"""
import asyncio, collections, json, logging, time
from .feeds import http_json, _ssl

log = logging.getLogger("extfeeds")


class Deriv:
    def __init__(self, name):
        self.name = name
        self.bid = self.ask = None
        self.mark = None
        self.funding = None
        self.updated = 0.0
        self.connected = False
        self.hist = collections.deque(maxlen=4000)      # (second, mid)
        self.trades = collections.deque(maxlen=30000)   # (ts, signed BTC): + taker buy
        self.liqs = collections.deque(maxlen=5000)      # (ts, signed USD): + forced BUY (shorts wiped), - forced SELL (longs wiped)
        self.oi = collections.deque(maxlen=500)         # (ts, open interest)

    def mid(self, max_age=10):
        if self.bid and self.ask and time.time() - self.updated < max_age:
            return (self.bid + self.ask) / 2
        return None

    def set_quote(self, bid, ask):
        self.bid, self.ask, self.updated = float(bid), float(ask), time.time()
        s = int(self.updated)
        if not self.hist or self.hist[-1][0] != s:
            self.hist.append((s, (self.bid + self.ask) / 2))

    def ret(self, secs):
        if len(self.hist) < 2 or time.time() - self.updated > 10:
            return None
        s1, p1 = self.hist[-1]
        for s, p in reversed(self.hist):
            if s1 - s >= secs:
                return (p1 / p - 1) * 1e4
        return None

    def flow(self, secs):
        cut = time.time() - secs
        buy = sell = 0.0
        for t, q in reversed(self.trades):
            if t < cut:
                break
            if q > 0: buy += q
            else: sell -= q
        return buy, sell

    def liq(self, secs):
        cut = time.time() - secs
        up = down = 0.0
        for t, v in reversed(self.liqs):
            if t < cut:
                break
            if v > 0: up += v
            else: down -= v
        return up, down

    def oi_change(self, secs):
        if len(self.oi) < 2:
            return None
        t1, v1 = self.oi[-1]
        for t, v in reversed(self.oi):
            if t1 - t >= secs and v:
                return (v1 / v - 1) * 100
        return None


class Ext:
    def __init__(self):
        self.d = {n: Deriv(n) for n in ("deribit", "hyperliquid", "krakenf", "binance", "bybit", "okx")}
        self.dvol = None
        self.dvol_hist = collections.deque(maxlen=500)  # (ts, dvol)


async def _ws(ext, name, url, subs, handle, ping=None):
    import websockets
    dv = ext.d[name]
    backoff = 2
    while True:
        try:
            async with websockets.connect(url, ssl=_ssl(), ping_interval=20, max_size=8_000_000) as ws:
                for m in subs:
                    await ws.send(json.dumps(m))
                dv.connected = True; backoff = 2
                log.info("%s futures connected", name)
                last_ping = time.time()
                async for raw in ws:
                    try:
                        handle(dv, json.loads(raw))
                    except Exception:
                        pass
                    if ping and time.time() - last_ping > 20:
                        await ws.send(ping if isinstance(ping, str) else json.dumps(ping)); last_ping = time.time()
        except Exception as e:
            dv.connected = False
            log.warning("%s futures disconnected: %s", name, e)
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 120)


def _binance(dv, m):
    st, d = m.get("stream", ""), m.get("data") or {}
    if st.endswith("@bookTicker"):
        dv.set_quote(d["b"], d["a"])
    elif st.endswith("@aggTrade"):
        q = float(d["q"]); dv.trades.append((time.time(), -q if d.get("m") else q))   # m: buyer is maker -> taker sold
    elif st.endswith("@forceOrder"):
        o = d.get("o") or {}
        usd = float(o.get("q", 0)) * float(o.get("ap") or o.get("p") or 0)
        dv.liqs.append((time.time(), usd if o.get("S") == "BUY" else -usd))
    elif "@markPrice" in st:
        dv.mark = float(d["p"]); dv.funding = float(d.get("r") or 0)


def _bybit(dv, m):
    topic, d = m.get("topic", ""), m.get("data")
    if topic.startswith("tickers") and isinstance(d, dict):
        if d.get("bid1Price") and d.get("ask1Price"):
            dv.set_quote(d["bid1Price"], d["ask1Price"])
        if d.get("markPrice"): dv.mark = float(d["markPrice"])
        if d.get("fundingRate"): dv.funding = float(d["fundingRate"])
        if d.get("openInterest"): dv.oi.append((time.time(), float(d["openInterest"])))
    elif topic.startswith("publicTrade"):
        for t in d or []:
            q = float(t["v"]); dv.trades.append((time.time(), q if t["S"] == "Buy" else -q))
    elif topic.startswith("allLiquidation"):
        for t in d or []:
            usd = float(t["v"]) * float(t["p"])
            dv.liqs.append((time.time(), -usd if t["S"] == "Buy" else usd))   # S = liquidated position side: Buy = long wiped = forced sell


def _okx(dv, m):
    ch = (m.get("arg") or {}).get("channel")
    for d in m.get("data") or []:
        if ch == "tickers":
            if d.get("bidPx") and d.get("askPx"): dv.set_quote(d["bidPx"], d["askPx"])
        elif ch == "trades":
            q = float(d["sz"]) * 0.01                                         # contracts of 0.01 BTC
            dv.trades.append((time.time(), q if d["side"] == "buy" else -q))
        elif ch == "funding-rate":
            dv.funding = float(d.get("fundingRate") or 0)
        elif ch == "mark-price":
            dv.mark = float(d["markPx"])
        elif ch == "liquidation-orders" and d.get("instId") == "BTC-USDT-SWAP":
            for x in d.get("details", []):
                usd = float(x["sz"]) * 0.01 * float(x["bkPx"])
                dv.liqs.append((time.time(), usd if x["side"] == "buy" else -usd))
        elif ch == "open-interest":
            dv.oi.append((time.time(), float(d["oiCcy"])))


def _deribit(dv, m):
    p = m.get("params") or {}
    ch, d = p.get("channel", ""), p.get("data")
    if ch.startswith("ticker") and d:
        if d.get("best_bid_price") and d.get("best_ask_price"):
            dv.set_quote(d["best_bid_price"], d["best_ask_price"])
        dv.mark = d.get("mark_price"); dv.funding = d.get("current_funding")
        if d.get("open_interest") and (not dv.oi or time.time() - dv.oi[-1][0] > 5):
            dv.oi.append((time.time(), float(d["open_interest"])))
    elif ch.startswith("trades") and d:
        for t in d:
            btc = float(t["amount"]) / float(t["price"])            # amount is USD
            sgn = 1 if t["direction"] == "buy" else -1
            dv.trades.append((time.time(), sgn * btc))
            if t.get("liquidation"):
                dv.liqs.append((time.time(), sgn * float(t["amount"])))


def _hyperliquid(dv, m):
    ch, d = m.get("channel"), m.get("data")
    if ch == "bbo" and d:
        b, a = d["bbo"][0], d["bbo"][1]
        if b and a: dv.set_quote(b["px"], a["px"])
    elif ch == "trades":
        for t in d or []:
            q = float(t["sz"]); dv.trades.append((time.time(), q if t["side"] == "B" else -q))
    elif ch == "activeAssetCtx" and d:
        c = d.get("ctx") or {}
        if c.get("funding") is not None: dv.funding = float(c["funding"])
        if c.get("markPx"): dv.mark = float(c["markPx"])
        if c.get("openInterest") and (not dv.oi or time.time() - dv.oi[-1][0] > 5):
            dv.oi.append((time.time(), float(c["openInterest"])))


def _krakenf(dv, m):
    f = m.get("feed")
    if f == "ticker":
        if m.get("bid") and m.get("ask"): dv.set_quote(m["bid"], m["ask"])
        if m.get("markPrice"): dv.mark = float(m["markPrice"])
        if m.get("relative_funding_rate") is not None: dv.funding = float(m["relative_funding_rate"])
        if m.get("openInterest") and (not dv.oi or time.time() - dv.oi[-1][0] > 5):
            dv.oi.append((time.time(), float(m["openInterest"])))
    elif f == "trade":
        q = float(m["qty"]); sgn = 1 if m["side"] == "buy" else -1
        dv.trades.append((time.time(), sgn * q))
        if m.get("type") == "liquidation":
            dv.liqs.append((time.time(), sgn * q * float(m["price"])))


async def _rest_loop(ext):
    loop = asyncio.get_running_loop()
    n = 0
    while True:
        try:
            r = await loop.run_in_executor(None, http_json, "https://fapi.binance.com/fapi/v1/openInterest", {"symbol": "BTCUSDT"})
            ext.d["binance"].oi.append((time.time(), float(r["openInterest"])))
        except Exception:
            pass
        if n % 4 == 0:
            try:
                now = int(time.time() * 1000)
                r = await loop.run_in_executor(None, http_json, "https://www.deribit.com/api/v2/public/get_volatility_index_data",
                                               {"currency": "BTC", "resolution": "60", "start_timestamp": now - 600_000, "end_timestamp": now})
                rows = (r.get("result") or {}).get("data") or []
                if rows:
                    ext.dvol = float(rows[-1][4]); ext.dvol_hist.append((time.time(), ext.dvol))
            except Exception:
                pass
        n += 1
        await asyncio.sleep(30)


def tasks(ext):
    bn = "wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade/btcusdt@forceOrder/btcusdt@markPrice@1s"
    return [
        _ws(ext, "deribit", "wss://www.deribit.com/ws/api/v2",
            [{"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
              "params": {"channels": ["ticker.BTC-PERPETUAL.100ms", "trades.BTC-PERPETUAL.100ms"]}}], _deribit,
            ping={"jsonrpc": "2.0", "id": 9, "method": "public/test"}),
        _ws(ext, "hyperliquid", "wss://api.hyperliquid.xyz/ws",
            [{"method": "subscribe", "subscription": {"type": t, "coin": "BTC"}} for t in ("bbo", "trades", "activeAssetCtx")],
            _hyperliquid, ping={"method": "ping"}),
        _ws(ext, "krakenf", "wss://futures.kraken.com/ws/v1",
            [{"event": "subscribe", "feed": f, "product_ids": ["PF_XBTUSD"]} for f in ("ticker", "trade")], _krakenf),
        _ws(ext, "binance", bn, [], _binance),
        _ws(ext, "bybit", "wss://stream.bybit.com/v5/public/linear",
            [{"op": "subscribe", "args": ["tickers.BTCUSDT", "publicTrade.BTCUSDT", "allLiquidation.BTCUSDT"]}], _bybit, ping={"op": "ping"}),
        _ws(ext, "okx", "wss://ws.okx.com:8443/ws/v5/public",
            [{"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT-SWAP"}, {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                                          {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}, {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
                                          {"channel": "liquidation-orders", "instType": "SWAP"}]}], _okx, ping="ping"),
        _rest_loop(ext),
    ]
