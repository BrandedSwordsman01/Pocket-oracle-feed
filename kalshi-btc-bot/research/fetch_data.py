"""Download history for backtesting: settled Kalshi BTC markets, their minute
bid/ask candles before close, and Coinbase 1-minute BTC candles.

Usage: python3 fetch_data.py OUT_DIR [days] [series]
"""
import json, os, sys, time, datetime, urllib.request, urllib.parse

K = "https://api.elections.kalshi.com/trade-api/v2"
CB = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
out = sys.argv[1]; days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
series = sys.argv[3] if len(sys.argv) > 3 else "KXBTCD"
os.makedirs(out, exist_ok=True)

def get(url, params=None, tries=5):
    if params: url += "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kbot-research/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(1.5 * (i + 1))

def ts(iso): return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())

now = int(time.time()); start = now - days * 86400

# 1. settled markets
mpath = f"{out}/{series}_markets.json"
if not os.path.exists(mpath):
    markets, cursor = [], ""
    while True:
        d = get(f"{K}/markets", {"series_ticker": series, "status": "settled", "limit": 1000,
                                  "min_close_ts": start, "cursor": cursor})
        markets += d.get("markets", []); cursor = d.get("cursor")
        print("markets", len(markets), file=sys.stderr)
        if not cursor or not d.get("markets"): break
    keep = ["ticker", "event_ticker", "strike_type", "floor_strike", "cap_strike", "close_time", "result", "expiration_value", "volume_fp"]
    markets = [{k: m.get(k) for k in keep} for m in markets]
    json.dump(markets, open(mpath, "w"))
markets = json.load(open(mpath))

# 2. candles for strikes near the settlement price (the only informative ones)
events = {}
for m in markets:
    try: ev = float(m["expiration_value"])
    except (TypeError, ValueError): continue
    k = m.get("floor_strike") or m.get("cap_strike")
    if k is None or abs(float(k) / ev - 1) > 0.02: continue
    events.setdefault(m["event_ticker"], []).append(m)
cpath = f"{out}/{series}_candles.json"
candles = json.load(open(cpath)) if os.path.exists(cpath) else {}
for i, (ev, ms) in enumerate(sorted(events.items())):
    if ev in candles: continue
    close = ts(ms[0]["close_time"])
    tickers = [m["ticker"] for m in ms][:100]
    rows = {}
    for j in range(0, len(tickers), 25):
        d = get(f"{K}/markets/candlesticks", {"market_tickers": ",".join(tickers[j:j+25]),
                 "start_ts": close - 125 * 60, "end_ts": close, "period_interval": 1})
        for mk in d.get("markets", []):
            rows[mk.get("market_ticker") or mk.get("ticker")] = [
                [c["end_period_ts"], c["yes_bid"].get("close_dollars"), c["yes_ask"].get("close_dollars")]
                for c in mk.get("candlesticks", [])]
    candles[ev] = rows
    if i % 20 == 0:
        print("events", i, len(events), file=sys.stderr); json.dump(candles, open(cpath, "w"))
json.dump(candles, open(cpath, "w"))

# 3. coinbase 1m candles
bpath = f"{out}/coinbase_1m.json"
have = {c[0]: c for c in json.load(open(bpath))} if os.path.exists(bpath) else {}
t = start - 3 * 3600
while t < now:
    if t + 60 in have and t + 17940 in have: t += 18000; continue
    d = get(CB, {"granularity": 60, "start": datetime.datetime.fromtimestamp(t, datetime.UTC).replace(tzinfo=None).isoformat(),
                 "end": datetime.datetime.fromtimestamp(t + 18000, datetime.UTC).replace(tzinfo=None).isoformat()})
    for c in d: have[c[0]] = c
    t += 18000; time.sleep(0.15)
json.dump(sorted(have.values()), open(bpath, "w"))
print("done", len(markets), "markets,", len(candles), "events,", len(have), "btc candles", file=sys.stderr)
