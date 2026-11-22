"""Diagnostics: market calibration by price bucket, and model vol/tail tuning (train only)."""
import sys, math, collections, runpy
sys.argv = [sys.argv[0]] + sys.argv[1:]
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(__file__.replace("analyze.py", "backtest.py"), run_name="bt")
train, test, FEE, t4cdf = g["train"], g["test"], g["FEE"], g["t4cdf"]

def first_per_market(rows, min_off=10):
    by = {}
    for r in sorted(rows, key=lambda r: -r["off"]):
        if r["off"] >= min_off and r["tick"] not in by: by[r["tick"]] = r
    return list(by.values())

print("Market calibration: buying at the ask, by price paid (all samples >=10m, both sides)")
for name, rows in (("TRAIN", train), ("TEST", test)):
    b = collections.defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        if r["off"] < 10: continue
        for px, win in ((r["ask"], r["y"] == 1), (1 - r["bid"], r["y"] == 0)):
            k = min(int(px * 10), 9); b[k][0] += 1; b[k][1] += win; b[k][2] += (1 - px if win else -px) - FEE(px)
    print(f"  {name}: price bucket -> n, win rate, avg pnl/contract after fees")
    for k in sorted(b):
        n, w, p = b[k]; print(f"    {k*10:2d}-{k*10+9:2d}c  n={n:6d}  win {w/n:5.1%}  pnl {100*p/n:+6.2f}c")

import numpy as np
def ncdf(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
def pm(r, vm, dist, floor=0.0003):
    sd = max(r["sigma"], floor) * vm * math.sqrt(r["off"])
    z = math.log(r["K"] / r["S"]) / sd
    pa = 1 - (t4cdf(z) if dist == "t4" else ncdf(z))
    return pa if r["above"] else 1 - pa
def brier(rows, f): return sum((f(r) - r["y"]) ** 2 for r in rows) / len(rows)
print("\nModel tuning on TRAIN (brier, lower better); market mid brier = %.4f" % brier(train, lambda r: r["mid"]))
best = None
for dist in ("normal", "t4"):
    line = []
    for vm in (0.7, 0.8, 0.9, 1.0, 1.1, 1.15, 1.3):
        b = brier(train, lambda r: pm(r, vm, dist)); line.append(f"{vm}:{b:.4f}")
        if not best or b < best[0]: best = (b, vm, dist)
    print(f"  {dist:6s} " + "  ".join(line))
print("  best:", best, " -> on TEST: %.4f (market %.4f)" % (brier(test, lambda r: pm(r, best[1], best[2])), brier(test, lambda r: r["mid"])))
