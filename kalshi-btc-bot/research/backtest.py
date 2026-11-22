"""Walk-forward backtest of BTC predictors on settled Kalshi markets.

Train on the first 2/3 of days, report on the last 1/3 (never seen in fitting).
Usage: python3 backtest.py DATA_DIR [series]
"""
import json, math, sys, datetime, collections
import numpy as np

data = sys.argv[1]; series = sys.argv[2] if len(sys.argv) > 2 else "KXBTCD"
markets = json.load(open(f"{data}/{series}_markets.json"))
candles = json.load(open(f"{data}/{series}_candles.json"))
btc = {int(c[0]): float(c[4]) for c in json.load(open(f"{data}/coinbase_1m.json"))}

OFFSETS = [90, 75, 60, 45, 30, 20, 15, 10, 5]   # minutes before close
FEE = lambda p: 0.07 * p * (1 - p) + 0.001        # per contract, + rounding slack

def ts(iso): return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
def t4cdf(z):
    t = z * math.sqrt(2); s = t * t
    return 0.5 + 0.75 * (t / math.sqrt(4 + s)) * (1 - s / (3 * (4 + s)))
def logit(p): p = min(max(p, 0.01), 0.99); return math.log(p / (1 - p))
def sig(x): return 1 / (1 + math.exp(-x))

def spot_at(t):  # close of the last full minute before t
    m = (t // 60) * 60 - 60
    for k in range(4):
        if m - 60 * k in btc: return btc[m - 60 * k]
    return None

def ewma_sigma(t, n=240, hl=60):
    lam = 0.5 ** (1 / hl); var = None; prev = None
    for i in range(n, 0, -1):
        p = btc.get((t // 60) * 60 - 60 * i)
        if p and prev:
            r = math.log(p / prev); var = r * r if var is None else lam * var + (1 - lam) * r * r
        if p: prev = p
    return math.sqrt(var) if var else None

def quote_at(rows, t):
    best = None
    for end, bid, ask in rows:
        if end <= t: best = (bid, ask)
        else: break
    if not best or best[0] is None or best[1] is None: return None
    bid, ask = float(best[0]), float(best[1])
    if not (0 < bid < ask < 1): return None
    return bid, ask

# ---- build samples
samples = []
mk = {m["ticker"]: m for m in markets}
for ev, rows_by in candles.items():
    for tick, rows in rows_by.items():
        m = mk.get(tick)
        if not m or m["result"] not in ("yes", "no") or m["strike_type"] not in ("greater", "less"): continue
        close = ts(m["close_time"]); y = 1 if m["result"] == "yes" else 0
        K = float(m["floor_strike"] if m["strike_type"] == "greater" else m["cap_strike"])
        rows = sorted(rows)
        for off in OFFSETS:
            t = close - off * 60
            q = quote_at(rows, t); S = spot_at(t); s = ewma_sigma(t)
            if not q or not S or not s: continue
            S15 = spot_at(t - 15 * 60)
            sd = max(s, 0.0003) * 1.15 * math.sqrt(off)
            p_above = 1 - t4cdf(math.log(K / S) / sd)
            # express everything as P(YES)
            p_model = p_above if m["strike_type"] == "greater" else 1 - p_above
            mom = (math.log(S / S15) / (max(s, 0.0003) * math.sqrt(15))) if S15 else 0.0
            if m["strike_type"] == "less": mom = -mom
            samples.append(dict(ev=ev, tick=tick, close=close, off=off, y=y, bid=q[0], ask=q[1],
                                mid=(q[0] + q[1]) / 2, p_model=p_model, mom=mom, sigma=s, K=K, S=S,
                                above=m["strike_type"] == "greater"))

samples.sort(key=lambda r: r["close"])
days = sorted({datetime.datetime.utcfromtimestamp(r["close"]).date() for r in samples})
cut = days[int(len(days) * 2 / 3)]
train = [r for r in samples if datetime.datetime.utcfromtimestamp(r["close"]).date() < cut]
test = [r for r in samples if datetime.datetime.utcfromtimestamp(r["close"]).date() >= cut]
print(f"{len(samples)} samples, {len({r['tick'] for r in samples})} markets, {len(days)} days "
      f"({days[0]} .. {days[-1]}); train < {cut} ({len(train)}), test ({len(test)})")

# ---- fitted model: logistic regression on [logit p_model, logit p_mkt, momentum]
def feats(r): return [1.0, logit(r["p_model"]), logit(r["mid"]), r["mom"]]
def fit(rows, l2=1.0):
    X = np.array([feats(r) for r in rows]); y = np.array([r["y"] for r in rows]); w = np.zeros(X.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w)); g = X.T @ (p - y) + l2 * w
        H = X.T @ (X * (p * (1 - p))[:, None]) + l2 * np.eye(len(w)); w -= np.linalg.solve(H, g)
    return w
w = fit(train)
print("fitted weights [bias, model, market, momentum]:", np.round(w, 3))
def p_fit(r): return sig(float(np.dot(w, feats(r))))

def brier(rows, f): return sum((f(r) - r["y"]) ** 2 for r in rows) / len(rows)
def logloss(rows, f): return -sum(math.log(min(max(f(r) if r["y"] else 1 - f(r), 1e-4), 1)) for r in rows) / len(rows)

print("\nPrediction quality on TEST (lower is better)")
for name, f in [("market mid", lambda r: r["mid"]), ("vol model (app v0.1)", lambda r: r["p_model"]), ("fitted blend", p_fit)]:
    print(f"  {name:22s} brier {brier(test, f):.4f}   logloss {logloss(test, f):.4f}")

def simulate(rows, f, thresh, label):
    by_m = collections.defaultdict(list)
    for r in rows: by_m[r["tick"]].append(r)
    trades = []
    for tick, rs in by_m.items():
        for r in sorted(rs, key=lambda r: -r["off"]):
            if r["off"] < 10: continue
            p = f(r)
            e_yes = p - r["ask"] - FEE(r["ask"]); no_px = 1 - r["bid"]; e_no = (1 - p) - no_px - FEE(no_px)
            side, px, e = ("yes", r["ask"], e_yes) if e_yes >= e_no else ("no", no_px, e_no)
            if e >= thresh and 0.05 <= px <= 0.95:
                win = (r["y"] == 1) == (side == "yes")
                trades.append(dict(day=datetime.datetime.utcfromtimestamp(r["close"]).date(),
                                   pnl=(1 - px if win else -px) - FEE(px), edge=e, px=px, side=side, win=win))
                break
    if not trades: print(f"  {label:34s} no trades"); return
    daily = collections.defaultdict(float)
    for t in trades: daily[t["day"]] += t["pnl"]
    pos = sum(1 for v in daily.values() if v > 0)
    tot = sum(t["pnl"] for t in trades)
    print(f"  {label:34s} trades {len(trades):5d}  win {np.mean([t['win'] for t in trades]):.0%}  "
          f"pnl/contract {100*tot/len(trades):+.2f}c  total {tot:+.2f}$/contract-unit  "
          f"green days {pos}/{len(daily)}")

print("\nTrading simulation on TEST (1 contract per trade, fees included, first signal per market >=10m out)")
for th in (0.02, 0.04, 0.06):
    simulate(test, lambda r: r["p_model"], th, f"vol model, edge>={th:.2f}")
    simulate(test, p_fit, th, f"fitted blend, edge>={th:.2f}")
print("\nSame on TRAIN (in-sample, for overfitting check)")
simulate(train, p_fit, 0.04, "fitted blend, edge>=0.04")
