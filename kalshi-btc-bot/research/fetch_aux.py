"""Auxiliary history for the 15-minute research.

- Deribit DVOL (BTC 30-day implied volatility index, annualised %), 1-minute.
- Bitstamp BTC/USD 1-minute candles (a BRTI constituent exchange; second spot source).

Usage: python3 fetch_aux.py OUT_DIR DAYS
"""
import json, os, sys, time, urllib.request, urllib.parse

out, days = sys.argv[1], int(sys.argv[2])
os.makedirs(out, exist_ok=True)
now = int(time.time()) // 60 * 60
start = now - days * 86400


def get(url, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kbot-research/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


# Deribit DVOL: max ~1000 points per call at 1-minute resolution
dvol = {}
t = start
while t < now:
    end = min(t + 1000 * 60, now)
    q = urllib.parse.urlencode({"currency": "BTC", "start_timestamp": t * 1000, "end_timestamp": end * 1000, "resolution": 60})
    d = get("https://www.deribit.com/api/v2/public/get_volatility_index_data?" + q)
    for row in d.get("result", {}).get("data", []):
        dvol[int(row[0] // 1000)] = float(row[4])
    t = end
    time.sleep(0.1)
json.dump(sorted(dvol.items()), open(f"{out}/deribit_dvol_1m.json", "w"))
print("dvol points", len(dvol), file=sys.stderr)

# Bitstamp 1-minute OHLC, 1000 per call
bs = {}
t = start
while t < now:
    d = get(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=60&limit=1000&start={t}")
    rows = d.get("data", {}).get("ohlc", [])
    if not rows:
        t += 1000 * 60
        continue
    for r in rows:
        bs[int(r["timestamp"])] = float(r["close"])
    t = max(int(rows[-1]["timestamp"]) + 60, t + 60)
    time.sleep(0.15)
json.dump(sorted(bs.items()), open(f"{out}/bitstamp_1m.json", "w"))
print("bitstamp candles", len(bs), file=sys.stderr)
