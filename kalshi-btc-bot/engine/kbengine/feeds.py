"""Live data feeds. Exchange websockets (no keys) + Kalshi REST polling (public market data).

Exchanges are BRTI constituents, so their median mid tracks the settlement index much better than Coinbase alone.
Every update stores local receipt time; exchange timestamps are kept where the feed provides them.
"""
import asyncio, collections, json, logging, ssl, time, urllib.request, urllib.parse

log = logging.getLogger("feeds")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PERPS = "https://external-api.kalshi.com/trade-api/v2/margin"


class Book:
    """Top of book + taker trade flow for one exchange."""

    def __init__(self, name):
        self.name = name
        self.bid = self.ask = self.bid_sz = self.ask_sz = None
        self.updated = 0.0
        self.trades = collections.deque(maxlen=20000)   # (local_ts, signed_qty): + taker buy, - taker sell
        self.connected = False

    def mid(self, max_age=10):
        if self.bid and self.ask and time.time() - self.updated < max_age:
            return (self.bid + self.ask) / 2
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


class State:
    def __init__(self):
        self.books = {n: Book(n) for n in ("coinbase", "kraken", "bitstamp")}
        self.comp_hist = collections.deque(maxlen=4000)   # (second, composite mid)
        self.kalshi = {}            # ticker -> dict(market fields, bid, ask, bid_sz, ask_sz, updated)
        self.m15 = None             # current 15-minute market dict
        self.hourly = []            # next-hour KXBTCD ladder (nearest strikes)
        self.perp = {}              # mid, funding, updated
        self.errors = collections.deque(maxlen=50)

    def composite(self):
        mids = sorted(m for m in (b.mid() for b in self.books.values()) if m)
        if not mids:
            return None
        n = len(mids)
        return mids[n // 2] if n % 2 else (mids[n // 2 - 1] + mids[n // 2]) / 2

    def tick_second(self):
        c = self.composite()
        if c:
            s = int(time.time())
            if not self.comp_hist or self.comp_hist[-1][0] != s:
                self.comp_hist.append((s, c))
        return c


def _ssl():
    return ssl.create_default_context()


async def _ws_loop(state, name, url, subscribe, handle):
    import websockets
    book = state.books[name]
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ssl=_ssl(), ping_interval=20, max_size=8_000_000) as ws:
                for msg in subscribe:
                    await ws.send(json.dumps(msg))
                book.connected = True; backoff = 1
                log.info("%s connected", name)
                async for raw in ws:
                    try:
                        handle(book, json.loads(raw))
                    except Exception as e:  # malformed message: skip
                        state.errors.append(f"{name} parse: {e}")
        except Exception as e:
            book.connected = False
            state.errors.append(f"{name}: {e}")
            log.warning("%s disconnected: %s", name, e)
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)


def _coinbase(book, m):
    t = m.get("type")
    if t == "ticker":
        book.bid = float(m["best_bid"]); book.ask = float(m["best_ask"])
        book.bid_sz = float(m.get("best_bid_size") or 0); book.ask_sz = float(m.get("best_ask_size") or 0)
        book.updated = time.time()
    elif t in ("match", "last_match"):
        q = float(m["size"])
        book.trades.append((time.time(), q if m["side"] == "sell" else -q))  # side = maker side


def _kraken(book, m):
    ch = m.get("channel")
    if ch == "ticker":
        for d in m.get("data", []):
            book.bid = float(d["bid"]); book.ask = float(d["ask"])
            book.bid_sz = float(d.get("bid_qty") or 0); book.ask_sz = float(d.get("ask_qty") or 0)
            book.updated = time.time()
    elif ch == "trade":
        for d in m.get("data", []):
            q = float(d["qty"])
            book.trades.append((time.time(), q if d["side"] == "buy" else -q))


def _bitstamp(book, m):
    ev, d = m.get("event"), m.get("data") or {}
    if ev == "data" and d.get("bids") and d.get("asks"):
        book.bid = float(d["bids"][0][0]); book.bid_sz = float(d["bids"][0][1])
        book.ask = float(d["asks"][0][0]); book.ask_sz = float(d["asks"][0][1])
        book.updated = time.time()
    elif ev == "trade":
        q = float(d["amount"])
        book.trades.append((time.time(), q if d.get("type") == 0 else -q))


def exchange_tasks(state):
    return [
        _ws_loop(state, "coinbase", "wss://ws-feed.exchange.coinbase.com",
                 [{"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker", "matches"]}], _coinbase),
        _ws_loop(state, "kraken", "wss://ws.kraken.com/v2",
                 [{"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}},
                  {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}}], _kraken),
        _ws_loop(state, "bitstamp", "wss://ws.bitstamp.net",
                 [{"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}},
                  {"event": "bts:subscribe", "data": {"channel": "live_trades_btcusd"}}], _bitstamp),
    ]


# ---------------------------------------------------------------- Kalshi (public REST)
def http_json(url, params=None, timeout=15):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-engine/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as r:
        return json.load(r)


def parse_book(ob):
    b = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes = [(float(p), float(q)) for p, q in (b.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (b.get("no_dollars") or [])]
    if not yes or not no:
        return None
    yb, nb = max(yes), max(no)
    return dict(bid=yb[0], bid_sz=yb[1], ask=round(1 - nb[0], 4), ask_sz=nb[1])


def iso_ts(s):
    import datetime
    return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


async def kalshi_loop(state, db):
    """15-minute market + order book every 3 s; next-hour ladder every 15 s; anchors for the basis."""
    loop = asyncio.get_running_loop()
    last_h = 0
    while True:
        try:
            now = time.time()
            r = await loop.run_in_executor(None, http_json, KALSHI + "/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 10})
            ms = [m for m in r.get("markets", []) if m.get("floor_strike") and iso_ts(m["close_time"]) > now + 20]
            m15 = min(ms, key=lambda m: m["close_time"]) if ms else None
            if m15:
                ob = await loop.run_in_executor(None, http_json, KALSHI + f"/markets/{m15['ticker']}/orderbook", {"depth": 5})
                b = parse_book(ob)
                if b:
                    state.kalshi[m15["ticker"]] = dict(m15, **b, updated=time.time(), series="KXBTC15M",
                                                       close_ts=iso_ts(m15["close_time"]), strike=float(m15["floor_strike"]), kind="at_least")
                state.m15 = state.kalshi.get(m15["ticker"])
                # anchor: strike is the BRTI 60s average at the window open; remember our composite then (for the basis)
                open_ts = iso_ts(m15["close_time"]) - 900
                comp_open = _comp_at(state, open_ts - 30, open_ts)
                if comp_open:
                    db.set_anchor(open_ts, float(m15["floor_strike"]), comp_open)
            if now - last_h > 30:
                last_h = now
                nowS = int(now)
                rh = await loop.run_in_executor(None, http_json, KALSHI + "/markets",
                                                {"series_ticker": "KXBTCD", "status": "open", "limit": 200,
                                                 "min_close_ts": nowS + 60, "max_close_ts": nowS + 3720})
                hs = [m for m in rh.get("markets", []) if m.get("strike_type") == "greater" and m.get("floor_strike")]
                if hs:
                    nxt = min(iso_ts(m["close_time"]) for m in hs)
                    comp = state.composite() or 0
                    near = sorted([m for m in hs if iso_ts(m["close_time"]) == nxt], key=lambda m: abs(float(m["floor_strike"]) - comp))[:6]
                    ladder = []
                    for m in near:
                        ob = await loop.run_in_executor(None, http_json, KALSHI + f"/markets/{m['ticker']}/orderbook", {"depth": 3})
                        b = parse_book(ob)
                        if b:
                            d = dict(m, **b, updated=time.time(), series="KXBTCD", close_ts=nxt, strike=float(m["floor_strike"]), kind="above")
                            state.kalshi[m["ticker"]] = d; ladder.append(d)
                    state.hourly = ladder
        except Exception as e:
            state.errors.append(f"kalshi: {e}")
            if "429" in str(e):
                await asyncio.sleep(20)      # Kalshi rate limit: back off
        await asyncio.sleep(4)


def _comp_at(state, a, b):
    xs = [c for s, c in state.comp_hist if a <= s <= b]
    return sum(xs) / len(xs) if xs else None


async def perp_loop(state):
    loop = asyncio.get_running_loop()
    while True:
        try:
            m = await loop.run_in_executor(None, http_json, PERPS + "/markets/KXBTCPERP")
            m = m.get("market", m)
            bid, ask = float(m.get("bid") or 0), float(m.get("ask") or 0)
            f = await loop.run_in_executor(None, http_json, PERPS + "/funding_rates/estimate", {"ticker": "KXBTCPERP"})
            if bid and ask:
                state.perp = dict(mid=(bid + ask) / 2 / float(m.get("contract_size") or 0.0001), funding=float(f.get("funding_rate") or 0),
                                  premium_index=float(f.get("premium_index") or 0), updated=time.time())
        except Exception as e:
            state.errors.append(f"perp: {e}")
        await asyncio.sleep(10)


async def settle_loop(state, db):
    """Label training rows with official outcomes once markets settle."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            for tk in db.pending_tickers(time.time() - 90)[:30]:
                m = (await loop.run_in_executor(None, http_json, KALSHI + f"/markets/{tk}")).get("market", {})
                if m.get("result") in ("yes", "no"):
                    ev = m.get("expiration_value")
                    db.set_outcome(tk, m["result"], float(ev) if ev not in (None, "") else None)
        except Exception as e:
            state.errors.append(f"settle: {e}")
        await asyncio.sleep(60)
