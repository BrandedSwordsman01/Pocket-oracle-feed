"""Endgame strategy: trade KXBTCD in the last 1-10 minutes before close.

Settlement = average of BRTI over the final 60 seconds. With m minutes left, the remaining
uncertainty is roughly sigma^2 * (m - 1 + 1/3) minutes (the averaging minute counts a third).
Usage: python3 endgame.py DATA_DIR
"""
import io, sys, math, json, contextlib, runpy, collections, datetime
import numpy as np

sys.argv = ["backtest.py", sys.argv[1]]
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(__file__.replace("endgame.py", "backtest.py"), run_name="bt")
markets, candles, ts, quote_at, spot_at, ewma_sigma, t4cdf, FEE = (
    g["markets"], g["candles"], g["ts"], g["quote_at"], g["spot_at"], g["ewma_sigma"], g["t4cdf"], g["FEE"])
mk = {m["ticker"]: m for m in markets}

def ncdf(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))

OFFS = [10, 8, 6, 5, 4, 3, 2]
rows = []
for ev, by in candles.items():
    for tick, rs in by.items():
        m = mk.get(tick)
        if not m or m["result"] not in ("yes", "no") or m["strike_type"] not in ("greater", "less"): continue
        close = ts(m["close_time"]); y = 1 if m["result"] == "yes" else 0
        K = float(m["floor_strike"] if m["strike_type"] == "greater" else m["cap_strike"])
        rs = sorted(rs)
        for off in OFFS:
            t = close - off * 60
            q = quote_at(rs, t); S = spot_at(t); s = ewma_sigma(t)
            if not q or not S or not s: continue
            rows.append(dict(tick=tick, close=close, off=off, y=y, bid=q[0], ask=q[1], K=K, S=S, s=s,
                             above=m["strike_type"] == "greater"))
rows.sort(key=lambda r: r["close"])
days = sorted({datetime.datetime.fromtimestamp(r["close"], datetime.UTC).date() for r in rows})
cut = days[int(len(days) * 2 / 3)]
isTrain = lambda r: datetime.datetime.fromtimestamp(r["close"], datetime.UTC).date() < cut
print(f"{len(rows)} endgame samples, {len({r['tick'] for r in rows})} markets; train < {cut}")

def p_yes(r, vm, dist, basis=0.0):
    eff = max(r["off"] - 1 + 1 / 3, 0.05)
    sd = max(r["s"], 0.0002) * vm * math.sqrt(eff)
    z = (math.log(r["K"] / (r["S"] * (1 + basis)))) / sd
    pa = 1 - (t4cdf(z) if dist == "t4" else ncdf(z))
    return pa if r["above"] else 1 - pa

def sim(rs, vm, dist, th, max_off, min_off=2):
    by = collections.defaultdict(list)
    for r in rs:
        if min_off <= r["off"] <= max_off: by[r["tick"]].append(r)
    trades = []
    for tick, xs in by.items():
        for r in sorted(xs, key=lambda r: -r["off"]):
            p = p_yes(r, vm, dist)
            ey = p - r["ask"] - FEE(r["ask"]); npx = 1 - r["bid"]; en = (1 - p) - npx - FEE(npx)
            side, px, e = ("yes", r["ask"], ey) if ey >= en else ("no", npx, en)
            if e >= th and 0.03 <= px <= 0.97:
                win = (r["y"] == 1) == (side == "yes")
                trades.append(((1 - px) if win else -px) - FEE(px)); break
    return trades

def summarize(tr):
    if len(tr) < 5: return f"n={len(tr)}"
    a = np.array(tr); t = a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))
    return f"n={len(a):4d} pnl/contract {100*a.mean():+5.2f}c  win {np.mean(a>0):4.0%}  t={t:+.2f}"

train = [r for r in rows if isTrain(r)]; test = [r for r in rows if not isTrain(r)]
print("\nGrid on TRAIN (pick best), then report TEST once")
best = None
for dist in ("normal", "t4"):
    for vm in (0.8, 1.0, 1.2, 1.5):
        for th in (0.03, 0.05, 0.08):
            for mo in (5, 10):
                tr = sim(train, vm, dist, th, mo)
                if len(tr) < 30: continue
                score = np.mean(tr) * math.sqrt(len(tr))
                if not best or score > best[0]: best = (score, dist, vm, th, mo)
                print(f"  {dist:6s} vol x{vm:<4} edge>={th:.2f} window<= {mo:2d}m  {summarize(tr)}")
_, dist, vm, th, mo = best
print(f"\nBEST on train: {dist}, vol x{vm}, edge >= {th}, trade 2-{mo} min before close")
print("  TRAIN:", summarize(sim(train, vm, dist, th, mo)))
print("  TEST :", summarize(sim(test, vm, dist, th, mo)))
daily = collections.defaultdict(float)
by = collections.defaultdict(list)
for r in test:
    if 2 <= r["off"] <= mo: by[r["tick"]].append(r)
json.dump(dict(dist=dist, vm=vm, th=th, max_off=mo), open("/dev/stdout", "w")) if False else None
