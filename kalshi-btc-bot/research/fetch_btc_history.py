"""Coinbase BTC-USD 1-minute OHLCV for the last N days. Usage: fetch_btc_history.py OUT.json DAYS"""
import json, os, sys, time, datetime, urllib.request, urllib.parse
out, days = sys.argv[1], int(sys.argv[2])
have = {c[0]: c for c in json.load(open(out))} if os.path.exists(out) else {}
now = int(time.time()) // 60 * 60; t = now - days * 86400
while t < now:
    if t in have and t + 17940 in have: t += 18000; continue
    q = urllib.parse.urlencode({"granularity": 60,
        "start": datetime.datetime.fromtimestamp(t, datetime.UTC).replace(tzinfo=None).isoformat(),
        "end": datetime.datetime.fromtimestamp(t + 17940, datetime.UTC).replace(tzinfo=None).isoformat()})
    for i in range(5):
        try:
            req = urllib.request.Request("https://api.exchange.coinbase.com/products/BTC-USD/candles?" + q, headers={"User-Agent": "kbot-research"})
            for c in json.load(urllib.request.urlopen(req, timeout=30)): have[c[0]] = c
            break
        except Exception as e:
            time.sleep(2 * (i + 1))
    t += 18000; time.sleep(0.12)
json.dump(sorted(have.values()), open(out, "w"))
print("candles", len(have), file=sys.stderr)
