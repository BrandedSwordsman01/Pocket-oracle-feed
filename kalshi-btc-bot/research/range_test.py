"""Does Kalshi overprice Bitcoin movement on the hourly ladder (variance risk premium)?

For each settled KXBTCD event and decision time (10-60 min before close):
  - market-implied probability per strike = mid price
  - forecast probability = GARCH x time-of-day vol (frozen, fit before Oct 23), exact 60-s averaging, zero drift
  - implied vol per strike (invert the Gaussian digital) vs forecast vol
Strategies (one trade per strike per event, taker at the ask, official fee, hold to settlement):
  TAIL  : buy the 'unlikely-to-happen' side's opposite when the market prices a far strike richer than the forecast
          (YES price <= 25c and forecast < market - theta  -> buy NO;  YES >= 75c and forecast > market + theta -> buy YES)
  ALL   : any strike where |forecast - market| beats fees by theta
Splits by day: train first 50%, validation next 25%, test last 25%.
Usage: python3 range_test.py DATA_DIR(KXBTCD + coinbase) BTC_180D.json
"""
import json, math, sys, collections, datetime, os
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from aegis import settlement as SM, volmodels as V

d, btc_path = sys.argv[1], sys.argv[2]
R = json.load(open(os.path.join(os.path.dirname(__file__), "aegis_results.json")))
g = R["vol_models"]["garch"]; diur = np.array(R["vol_models"]["diurnal"])
garch = V.GarchT(); garch.w, garch.a, garch.b, garch.nu = g["w"], g["a"], g["b"], g["nu"]; garch.uncond = g["w"] / (1 - g["a"] - g["b"])
raw = sorted(json.load(open(btc_path)), key=lambda c: c[0])
ct = np.array([c[0] for c in raw]); cc = np.array([c[4] for c in raw], float)
gt = np.arange(ct[0], ct[-1] + 60, 60); gc = cc[np.searchsorted(ct, gt, side="right") - 1]
r = np.r_[0.0, np.diff(np.log(gc))]; hrs = (gt // 3600) % 24
h = garch.filter(r / np.sqrt(diur[hrs])); gidx = {int(t): i for i, t in enumerate(gt)}
F_LAST = SM.last_minute_factor()
from scipy.special import ndtri as _ndtri
_NI = lambda p: float(_ndtri(p))
ncdf = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
iso = lambda s: int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
mk = {m["ticker"]: m for m in json.load(open(f"{d}/KXBTCD_markets.json"))}
cand = json.load(open(f"{d}/KXBTCD_candles.json"))

def quote(cs, t):
    q = None
    for e, b, a in cs:
        if e <= t: q = (e, b, a)
        else: break
    if not q or q[1] is None or q[2] is None or t - q[0] > 180: return None
    b, a = float(q[1]), float(q[2])
    return (b, a) if 0 < b < a < 1 else None

def fee(P): return math.ceil(round(0.07 * 10 * P * (1 - P) * 100, 6)) / 1000
rows = []
for ev, by in cand.items():
    for tk, cs in by.items():
        m = mk.get(tk)
        if not m or m.get("strike_type") != "greater" or m.get("result") not in ("yes", "no"): continue
        close = iso(m["close_time"]); K = float(m["floor_strike"]); cs = sorted(cs)
        for off in (60, 45, 30, 20, 15, 10):
            t = close - 60 * off; i = gidx.get(t - 60)
            if i is None: continue
            q = quote(cs, t)
            if not q: continue
            fut = ((t + 60 * np.arange(off)) // 3600) % 24
            sd = np.sqrt(garch.forecast(h[i + 1], off) * diur[fut]); w = sd.copy(); w[-1] *= math.sqrt(F_LAST)
            v = float(np.sum(w ** 2)) + (2.9e-4) ** 2         # + Coinbase-vs-BRTI proxy error
            S = gc[i]; zf = (math.log(K / S) + 0.5 * v) / math.sqrt(v)
            pf = 1 - ncdf(zf); mid = (q[0] + q[1]) / 2
            # implied vol of the market for this strike (Gaussian digital inversion), relative to forecast
            try:
                zm = -_NI(mid) if 0.02 < mid < 0.98 else None
            except ValueError:
                zm = None
            ivr = (math.log(K / S) / zm) / math.sqrt(v) if zm and abs(zm) > 0.3 and abs(math.log(K / S)) > 1e-5 and (math.log(K / S) / zm) > 0 else None
            rows.append(dict(tk=tk, close=close, off=off, y=int(m["result"] == "yes"), bid=q[0], ask=q[1], mid=mid, pf=pf, ivr=ivr))
days = sorted({datetime.date.fromtimestamp(x["close"]) for x in rows})
a, b = int(len(days) * .5), int(len(days) * .75)
part = lambda x: "train" if datetime.date.fromtimestamp(x["close"]) < days[a] else ("val" if datetime.date.fromtimestamp(x["close"]) < days[b] else "test")
for x in rows: x["part"] = part(x)
print(f"{len(rows)} rows, {len({x['tk'] for x in rows})} strikes, {days[0]}..{days[-1]}")
ivs = [x["ivr"] for x in rows if x["ivr"]]
print(f"market implied vol / forecast vol: median {np.median(ivs):.2f}, mean {np.mean(ivs):.2f}, IQR {np.percentile(ivs,25):.2f}-{np.percentile(ivs,75):.2f} (n={len(ivs)})")
for off in (60, 30, 10):
    s = [x["ivr"] for x in rows if x["ivr"] and x["off"] == off]
    print(f"  {off:2d} min out: median ratio {np.median(s):.2f}")

def sim(xs, mode, th):
    done, out = set(), []
    for x in sorted(xs, key=lambda x: (x["close"], -x["off"])):
        if x["tk"] in done: continue
        ya, na = x["ask"], 1 - x["bid"]
        ey, en = x["pf"] - ya - fee(ya), (1 - x["pf"]) - na - fee(na)
        side = None
        if mode == "TAIL":
            if x["mid"] <= 0.25 and en > th: side, px = "no", na
            elif x["mid"] >= 0.75 and ey > th: side, px = "yes", ya
        else:
            if max(ey, en) > th: side, px = ("yes", ya) if ey >= en else ("no", na)
        if not side or not (0.02 <= px <= 0.98): continue
        win = (x["y"] == 1) == (side == "yes")
        out.append(((1 - px) if win else -px) - fee(px)); done.add(x["tk"])
    return out
def summ(t):
    if len(t) < 5: return f"n={len(t)}"
    a = np.array(t); return f"n={len(a):4d} {100*a.mean():+5.2f}c/contract win {np.mean(a>0):4.0%} t={a.mean()/(a.std(ddof=1)/math.sqrt(len(a))):+.2f} net ${a.sum()*10:+.0f}/10-lots"
print("\nStrategy results (threshold chosen on validation, test reported once):")
for mode in ("TAIL", "ALL"):
    best = max((0.0, 0.01, 0.02, 0.04), key=lambda th: sum(sim([x for x in rows if x["part"] == "val"], mode, th)))
    print(f"  {mode} (theta={best:.2f})")
    for p in ("train", "val", "test"):
        print(f"     {p:5s}: {summ(sim([x for x in rows if x['part'] == p], mode, best))}")
