"""Backtest strategies on Kalshi KXBTC15M (15-minute BTC up/down).
Train on the first 2/3 of days, report the rest once.
Usage: python3 test15.py DATA15_DIR
"""
import json, math, sys, collections, datetime
import numpy as np

d = sys.argv[1]
markets = json.load(open(f"{d}/KXBTC15M_markets.json"))
candles = json.load(open(f"{d}/KXBTC15M_candles.json"))
btc = {int(c[0]): float(c[4]) for c in json.load(open(f"{d}/coinbase_1m.json"))}
FEE = lambda p: 0.07 * p * (1 - p) + 0.001
ts = lambda iso: int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
day = lambda t: datetime.datetime.fromtimestamp(t, datetime.UTC).date()

def spot(t):
    m = (t // 60) * 60 - 60
    for k in range(4):
        if m - 60 * k in btc: return btc[m - 60 * k]
def sigma(t, n=240, hl=60):
    lam = 0.5 ** (1 / hl); v = None; prev = None
    for i in range(n, 0, -1):
        p = btc.get((t // 60) * 60 - 60 * i)
        if p and prev: r = math.log(p / prev); v = r * r if v is None else lam * v + (1 - lam) * r * r
        if p: prev = p
    return math.sqrt(v) if v else None
def quote(rows, t):
    q = None
    for end, b, a in rows:
        if end <= t: q = (b, a)
        else: break
    if not q or q[0] is None or q[1] is None: return None
    b, a = float(q[0]), float(q[1])
    return (b, a) if 0 < b < a < 1 else None
def t4(z): t = z * math.sqrt(2); s = t * t; return 0.5 + 0.75 * (t / math.sqrt(4 + s)) * (1 - s / (3 * (4 + s)))
def ncdf(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
def logit(p): p = min(max(p, .01), .99); return math.log(p / (1 - p))

OFFS = [14, 12, 10, 8, 6, 5, 4, 3, 2]
mk = {m["ticker"]: m for m in markets}
rows = []
for ev, by in candles.items():
    for tick, rs in by.items():
        m = mk.get(tick)
        if not m or m["result"] not in ("yes", "no") or m.get("floor_strike") is None: continue
        close = ts(m["close_time"]); K = float(m["floor_strike"]); rs = sorted(rs)
        for off in OFFS:
            t = close - off * 60; q = quote(rs, t); S = spot(t); s = sigma(t)
            if q and S and s:
                rows.append(dict(tick=tick, close=close, off=off, y=int(m["result"] == "yes"), bid=q[0], ask=q[1], K=K, S=S, s=s))
rows.sort(key=lambda r: r["close"])
days = sorted({day(r["close"]) for r in rows}); cut = days[int(len(days) * 2 / 3)]
train = [r for r in rows if day(r["close"]) < cut]; test = [r for r in rows if day(r["close"]) >= cut]
print(f"{len(rows)} samples, {len({r['tick'] for r in rows})} windows, {days[0]}..{days[-1]}, train < {cut}")

def pvol(r, vm=1.0, dist="t4"):
    sd = max(r["s"], 0.0002) * vm * math.sqrt(max(r["off"] - 2 / 3, 0.05))
    z = math.log(r["K"] / r["S"]) / sd
    return 1 - (t4(z) if dist == "t4" else ncdf(z))

def brier(rs, f): return np.mean([(f(r) - r["y"]) ** 2 for r in rs])
print("\nPrediction error on TEST (Brier, lower better)")
print(f"  market mid {brier(test, lambda r: (r['bid']+r['ask'])/2):.4f}   vol model {brier(test, pvol):.4f}")

def sim(rs, f, th, lo=2, hi=14):
    by = collections.defaultdict(list)
    for r in rs:
        if lo <= r["off"] <= hi: by[r["tick"]].append(r)
    out = []
    for xs in by.values():
        for r in sorted(xs, key=lambda r: -r["off"]):
            p = f(r); ey = p - r["ask"] - FEE(r["ask"]); npx = 1 - r["bid"]; en = (1 - p) - npx - FEE(npx)
            side, px, e = ("yes", r["ask"], ey) if ey >= en else ("no", npx, en)
            if e >= th and 0.03 <= px <= 0.97:
                win = (r["y"] == 1) == (side == "yes"); out.append(((1 - px) if win else -px) - FEE(px)); break
    return out
def summ(tr):
    if len(tr) < 5: return f"n={len(tr)}"
    a = np.array(tr); return f"n={len(a):4d} pnl {100*a.mean():+5.2f}c/contract win {np.mean(a>0):4.0%} t={a.mean()/(a.std(ddof=1)/math.sqrt(len(a))):+.2f}"

print("\nA) Vol model: choose settings on TRAIN, report TEST")
best = None
for dist in ("normal", "t4"):
    for vm in (0.8, 1.0, 1.2):
        for th in (0.03, 0.05, 0.08):
            for lo, hi in ((2, 6), (6, 14), (2, 14)):
                tr = sim(train, lambda r: pvol(r, vm, dist), th, lo, hi)
                if len(tr) >= 30:
                    sc = np.mean(tr) * math.sqrt(len(tr))
                    if not best or sc > best[0]: best = (sc, dist, vm, th, lo, hi)
_, dist, vm, th, lo, hi = best
print(f"  best on train: {dist} vol x{vm} edge>={th} window {lo}-{hi}m")
print("  TRAIN", summ(sim(train, lambda r: pvol(r, vm, dist), th, lo, hi)))
print("  TEST ", summ(sim(test, lambda r: pvol(r, vm, dist), th, lo, hi)))

print("\nB) Market-anchored blend (logistic on logit vol + logit mid)")
def fit(rs):
    X = np.array([[1, logit(pvol(r)), logit((r["bid"] + r["ask"]) / 2)] for r in rs]); y = np.array([r["y"] for r in rs]); w = np.zeros(3)
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w)); H = X.T @ (X * (p * (1 - p))[:, None]) + np.eye(3); w -= np.linalg.solve(H, X.T @ (p - y) + w)
    return w
w = fit(train); print("  weights [bias, vol, market]:", np.round(w, 3))
pb = lambda r: 1 / (1 + math.exp(-(w[0] + w[1] * logit(pvol(r)) + w[2] * logit((r["bid"] + r["ask"]) / 2))))
for th in (0.02, 0.04):
    print(f"  edge>={th}: TRAIN", summ(sim(train, pb, th)), "| TEST", summ(sim(test, pb, th)))

print("\nC) 'Buy the leading side' (whichever side is >= 50c) at a fixed time")
for off in (10, 5, 3, 2):
    for part, rs in (("train", train), ("test", test)):
        out = []
        for r in rs:
            if r["off"] != off: continue
            mid = (r["bid"] + r["ask"]) / 2
            if mid >= 0.5: px, win = r["ask"], r["y"] == 1
            else: px, win = 1 - r["bid"], r["y"] == 0
            if px < 0.97: out.append(((1 - px) if win else -px) - FEE(px))
        print(f"  {off:2d}m before close {part:5s}: {summ(out)}")

print("\nD) Price buckets on TEST (buy at ask, both sides, >=2m out): is anything mispriced?")
b = collections.defaultdict(list)
for r in test:
    for px, win in ((r["ask"], r["y"] == 1), (1 - r["bid"], r["y"] == 0)):
        b[min(int(px * 10), 9)].append(((1 - px) if win else -px) - FEE(px))
for k in sorted(b):
    a = np.array(b[k]); print(f"  {k*10:2d}-{k*10+9}c n={len(a):5d} win {np.mean(a > 0):5.1%} pnl {100*a.mean():+5.2f}c")
