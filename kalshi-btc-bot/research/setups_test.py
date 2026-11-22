"""Do the chart setups predict anything?

Part A (perps-relevant): forward BTC return after each setup fires, 180 days of 1-min data.
Part B (Kalshi-relevant): does a recent setup improve on the market-anchored blend for
settled KXBTCD markets?

Events come from the app's own detector: node research/detect_events.js BTC.json EVENTS.json
Usage: python3 setups_test.py DATA_DIR BTC_180D.json EVENTS_180D.json
"""
import io, json, math, sys, contextlib, runpy, collections, datetime
import numpy as np

data_dir, btc_path, ev_path = sys.argv[1], sys.argv[2], sys.argv[3]
btc = {int(c[0]): float(c[4]) for c in json.load(open(btc_path))}
events = json.load(open(ev_path))
times = sorted(btc)
t0, t1 = times[0], times[-1]
split = t0 + (t1 - t0) * 2 / 3
day = lambda t: datetime.datetime.fromtimestamp(t, datetime.UTC).date()
print(f"BTC 1-min candles: {len(btc)} ({day(t0)} .. {day(t1)}), train < {day(split)}")

def fwd(t, h):
    a, b = btc.get(t), btc.get(t + 60 * h)
    return math.log(b / a) if a and b else None

def stats(xs):
    n = len(xs)
    if n < 5: return n, float('nan'), float('nan'), float('nan')
    a = np.array(xs); m = a.mean(); se = a.std(ddof=1) / math.sqrt(n)
    return n, m * 1e4, (a > 0).mean(), m / se if se > 0 else 0.0

# Unconditional drift baseline: a random long has this mean per horizon.
print("\nPART A: signed forward return after each setup (dir * log return), in basis points")
print("A setup is useful for perps only if TEST mean beats round-trip costs (~4-15 bps) with |t| > 2.")
by = collections.defaultdict(list)
for e in events: by[e["setup"]].append(e)
rows_out = []
for setup in sorted(by):
    for h in (5, 15, 60):
        res = {}
        for part in ("train", "test"):
            xs, last = [], -1e18
            for e in by[setup]:
                t = e["time"]
                if (t < split) != (part == "train"): continue
                if t - last < 60 * h: continue  # non-overlapping windows
                r = fwd(t, h)
                if r is None: continue
                xs.append(e["dir"] * r); last = t
            res[part] = stats(xs)
        tr, te = res["train"], res["test"]
        rows_out.append((setup, h, tr, te))
        print(f"  {setup:15s} {h:3d}m  train n={tr[0]:5d} mean {tr[1]:+6.2f}bp hit {tr[2]:5.1%} t={tr[3]:+5.2f} | "
              f"test n={te[0]:5d} mean {te[1]:+6.2f}bp hit {te[2]:5.1%} t={te[3]:+5.2f}")

# ---------------- Part B
print("\nPART B: does a setup in the last 30 min improve the Kalshi blend? (KXBTCD, 30 days)")
sys.argv = ["backtest.py", data_dir]
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(__file__.replace("setups_test.py", "backtest.py"), run_name="bt")
train, test = g["train"], g["test"]
logit = g["logit"]
ev_by_setup = {s: sorted((e["time"], e["dir"]) for e in by[s]) for s in by}
import bisect
def recent_dir(setup, t, window=1800):
    arr = ev_by_setup[setup]; i = bisect.bisect_right(arr, (t, 9)) - 1
    if i >= 0 and t - arr[i][0] <= window: return arr[i][1]
    return 0

def fit(X, y, l2=1.0):
    w = np.zeros(X.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w)); g_ = X.T @ (p - y) + l2 * w
        H = X.T @ (X * (p * (1 - p))[:, None]) + l2 * np.eye(len(w)); w -= np.linalg.solve(H, g_)
    cov = np.linalg.inv(H)
    return w, np.sqrt(np.diag(cov))
def logloss(X, y, w):
    p = np.clip(1 / (1 + np.exp(-X @ w)), 1e-4, 1 - 1e-4)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())

def design(rows, setup):
    X = []
    for r in rows:
        f = [1.0, logit(r["p_model"]), logit(r["mid"])]
        if setup:
            d = recent_dir(setup, r["close"] - r["off"] * 60)
            f.append(d if r["above"] else -d)  # bullish helps YES on "above" markets
        X.append(f)
    return np.array(X), np.array([r["y"] for r in rows], dtype=float)

Xb, yb = design(train, None); wb, _ = fit(Xb, yb)
Xt, yt = design(test, None); base = logloss(Xt, yt, wb)
print(f"  baseline blend test logloss {base:.5f}")
for setup in sorted(by):
    X, y = design(train, setup); w, se = fit(X, y)
    Xt, yt = design(test, setup)
    active = int((Xt[:, 3] != 0).sum())
    ll = logloss(Xt, yt, w)
    print(f"  + {setup:15s} coef {w[3]:+.3f} (t={w[3]/se[3]:+.2f})  test logloss {ll:.5f} ({ll-base:+.5f})  active in {active} test samples")
