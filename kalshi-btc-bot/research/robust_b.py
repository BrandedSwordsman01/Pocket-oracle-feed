"""Robustness of Strategy B (momentum/lag): every grid config on all splits, plus latency. Usage: robust_b.py DATA_DIR"""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(__file__))
from kbt import data as D, strategies as S, backtest as B
ds = D.load(sys.argv[1]); tr, va, te = B.split_days(ds.days)
W = lambda days: {k: w for k, w in ds.windows.items() if D.utc_day(w[0]["close"]) in days}
Wtr, Wva, Wte, Wall = W(tr), W(va), W(te), ds.windows
print("| z | lag_frac | bench adj | exit | train ¢ (n) | val ¢ (n) | test ¢ (n) | all 60d ¢ (n, t) | all, 1-min latency ¢ (n, t) |")
print("|---|---|---|---|---|---|---|---|---|")
cell = lambda m: f"{m['per_contract_c']:+.2f} ({m['n']})" if m.get("n") else "– (0)"
pos_all = tot = 0
for z, lf, ba, ex in itertools.product([1.0, 1.5, 2.0, 2.5], [0.3, 0.5, 0.8], [True, False], [True, False]):
    s = S.MomentumLag(z=z, lag_frac=lf, benchmark_adjust=ba, use_exit=ex)
    ms = [B.metrics(B.run(w, s)) for w in (Wtr, Wva, Wte, Wall)]
    lat = B.metrics(B.run(Wall, s, B.Costs(delay_rows=1)))
    if ms[3].get("n", 0) >= 30:
        tot += 1; pos_all += ms[3]["per_contract_c"] > 0
    print(f"| {z} | {lf} | {ba} | {ex} | {cell(ms[0])} | {cell(ms[1])} | {cell(ms[2])} | "
          f"{cell(ms[3])[:-1]}, t={ms[3].get('t', float('nan')):+.2f}) | {cell(lat)[:-1]}, t={lat.get('t', float('nan')):+.2f}) |")
print(f"\nConfigs with >=30 trades over 60 days: {tot}; positive: {pos_all}")
