"""AEGIS 2.0 study. Writes docs/aegis2_backtest_results.md and research/aegis_results.json.

Usage: python3 run_aegis.py DATA15_DIR HOURLY_DIR BTC_180D_JSON
  DATA15_DIR : KXBTC15M_markets/candles + coinbase_1m (+ bitstamp, dvol)    (research/fetch_data.py, fetch_aux.py)
  HOURLY_DIR : KXBTCD_markets/candles (research/fetch_data.py ... KXBTCD)
  BTC_180D   : Coinbase 1m history used ONLY before the train cutoff for frozen vol-model parameters

Protocol: vol-model parameters fit on Coinbase data < 2026-10-23 and frozen. Calibration maps fit on TRAIN, thresholds and
pool weight on VALIDATION, TEST evaluated once. Bootstrap resamples whole days.
"""
import bisect, collections, datetime, json, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from kbt import data as D, backtest as KB
from aegis import settlement as SM, volmodels as V, simulate as SIM, calibrate as CAL, distribution as DIST, scanner as SC

T0 = time.time()
d15, dh, btc_path = sys.argv[1], sys.argv[2], sys.argv[3]
OUT_MD = os.path.join(os.path.dirname(__file__), "..", "docs", "aegis2_backtest_results.md")
CUTOFF = int(datetime.datetime(2026, 10, 23, tzinfo=datetime.UTC).timestamp())
L = []; P = L.append
R = collections.OrderedDict()
log = lambda *a: print(*a, file=sys.stderr)

# ============================================================== 1. frozen volatility models
raw = sorted(json.load(open(btc_path)), key=lambda c: c[0])
cb_t = np.array([c[0] for c in raw]); cb_c = np.array([c[4] for c in raw], float)
# regular grid with forward fill
grid_t = np.arange(cb_t[0], cb_t[-1] + 60, 60)
pos = np.searchsorted(cb_t, grid_t, side="right") - 1
grid_c = cb_c[pos]
r = np.r_[0.0, np.diff(np.log(grid_c))]
hours = (grid_t // 3600) % 24
fit_mask = grid_t < CUTOFF
diur = V.diurnal_factors(grid_t[fit_mask], r[fit_mask])
r_ds = r / np.sqrt(diur[hours])
fit_idx = np.where(fit_mask)[0][-120000:]
garch = V.GarchT().fit(r_ds[fit_idx])
h_all = garch.filter(r_ds)             # h_all[i] = forecast for return i (info to i-1); h_all[i+1] uses return i
z_fit = r_ds[fit_idx] / np.sqrt(h_all[fit_idx])
jumps = V.jump_params(z_fit, 4.5)
# EWMA (hl 60) standardised residuals for the t-dof of model C
ew = D.ewma_sigma(grid_c, 60)
z_ew = r[1:][fit_mask[1:]] / np.where(ew[:-1][fit_mask[1:]] > 0, ew[:-1][fit_mask[1:]], np.nan)
nu_ew = V.fit_t_nu(z_ew[np.isfinite(z_ew)][-120000:])
grid_index = {int(t): i for i, t in enumerate(grid_t)}
log(f"garch a={garch.a:.3f} b={garch.b:.3f} nu={garch.nu:.2f}; nu_ewma={nu_ew:.2f}; jumps lam={jumps['lam']:.5f}/min")
R["vol_models"] = dict(garch=dict(w=garch.w, a=garch.a, b=garch.b, nu=garch.nu), nu_ewma=nu_ew, jump_lam=jumps["lam"],
                       n_jumps=int(len(jumps["sizes"])), diurnal=diur.round(3).tolist())

SIMS = dict(B=SIM.Simulator(3000, 120, seed=21), C=SIM.Simulator(3000, 120, seed=22, nu=nu_ew),
            D=SIM.Simulator(3000, 120, seed=23, nu=garch.nu), E=SIM.Simulator(3000, 120, seed=24, jumps=jumps))


def sd_paths(t, m, ewma_sigma):
    """Per-minute vol forecasts for the m minutes ending at settlement, decided at time t (last closed candle t-60)."""
    i = grid_index.get(int(t) - 60)
    if i is None:
        return None
    h_next = h_all[i + 1]
    fut_hours = ((t + 60 * np.arange(m)) // 3600) % 24
    g = np.sqrt(garch.forecast(h_next, m) * diur[fut_hours])
    const = np.full(m, max(ewma_sigma, 1e-5))
    return dict(B=const, C=const, D=g, E=g)


# ============================================================== 2. 15-minute dataset + model probabilities
log("loading 15M ...")
ds = D.load(d15)
tr_days, va_days, te_days = KB.split_days(ds.days)
split_of = lambda t: "train" if D.utc_day(t) in tr_days else ("val" if D.utc_day(t) in va_days else "test")
rows = [r_ for r_ in ds.rows if r_["fresh"] and r_["basis"] == r_["basis"]]
mk15 = {m["ticker"]: m for m in json.load(open(os.path.join(d15, "KXBTC15M_markets.json")))}
for r_ in rows:
    sp = sd_paths(r_["t"], r_["mins"], r_["sigma"])
    if sp is None:
        r_["skip"] = True; continue
    centre = r_["S"] * (1 + r_["basis"])
    c = SM.Contract(r_["ticker"], "KXBTC15M", "at_least", r_["K"])
    for k in ("B", "C", "D", "E"):
        p, A = SIMS[k].prob_yes(centre, sp[k], [c])
        r_["p_" + k] = p[0]
        if r_["mins"] in (2, 5, 10):  # PIT on a few fixed horizons per window
            ev = mk15.get(r_["ticker"], {}).get("expiration_value")
            if ev:
                r_.setdefault("pit", {})[k] = float(np.mean(A <= float(ev)))
    r_["split"] = split_of(r_["close"])
rows = [r_ for r_ in rows if not r_.get("skip")]
log(f"15M rows: {len(rows)}  ({time.time()-T0:.0f}s)")
y_of = lambda rs: np.array([x["y"] for x in rs], float)
by_split = {s: [x for x in rows if x["split"] == s] for s in ("train", "val", "test")}

# calibration maps fitted on TRAIN (tradeable rows)
tradeable = lambda x: 0.03 <= x["mid"] <= 0.97
cal = {}
for k in ("B", "C", "D", "E"):
    tr = [x for x in by_split["train"] if tradeable(x)]
    cal[k] = CAL.Platt().fit(np.array([x["p_" + k] for x in tr]), y_of(tr))
# market-only recalibration (alternative M1): is the market itself biased?
trm = [x for x in by_split["train"] if tradeable(x)]
cal["M"] = CAL.Platt().fit(np.array([x["mid"] for x in trm]), y_of(trm))
# pool weight for F fitted on VALIDATION (model E calibrated vs market mid)
va = [x for x in by_split["val"] if tradeable(x)]
omega, omega_curve = CAL.fit_omega(cal["E"](np.array([x["p_E"] for x in va])), np.array([x["mid"] for x in va]), y_of(va))
for x in rows:
    for k in ("B", "C", "D", "E"):
        x["c_" + k] = float(cal[k](np.array(x["p_" + k])))
    x["c_M"] = float(cal["M"](np.array(x["mid"])))
    x["p_F"] = float(CAL.log_pool(np.array(x["c_E"]), np.array(x["mid"]), omega))
R["omega"] = omega
R["platt"] = {k: dict(a=float(v.a), b=float(v.b)) for k, v in cal.items()}

# ============================================================== 3. probability quality
P("# AEGIS 2.0 backtest results\n")
P(f"Generated by `research/run_aegis.py` in {{ELAPSED}}s. All numbers are reproducible from the public data fetchers.\n")
P(f"**15-minute data:** {len(ds.windows)} KXBTC15M windows, {len(rows)} fresh decision rows (2–14 min before close), {ds.days[0]} .. {ds.days[-1]}. "
  f"Train {min(tr_days)}..{max(tr_days)} · validation {min(va_days)}..{max(va_days)} · **test {min(te_days)}..{max(te_days)} (touched once)**.\n")
P(f"**Frozen vol models (fit on Coinbase 1m before {datetime.date(2026,10,23)}):** GARCH(1,1)-t on de-seasonalised returns: "
  f"α={garch.a:.3f}, β={garch.b:.3f}, ν={garch.nu:.2f}; diurnal variance factor range {diur.min():.2f}–{diur.max():.2f}×; "
  f"EWMA-residual t ν={nu_ew:.2f}; jumps (|z|>4.5) λ={jumps['lam']*1440:.1f}/day.\n")
P("**Models:** A market mid · B constant-vol (EWMA) Gaussian · C EWMA Student-t · D GARCH-t × diurnal · E AEGIS: GARCH × diurnal + empirical jumps "
  "(Gaussian diffusion, to avoid double-counting tails with t) · F AEGIS ⊕ market log-pool (ω on validation) · M market re-calibrated (Platt on mid; tests market bias). "
  "All models use the benchmark-adjusted centre and the exact discrete 60-sample settlement averaging. Raw = uncalibrated; cal = Platt fit on train.\n")
P("## 1. Probability quality on TEST (rows with mid 3–97¢)\n")
P("| Model | Brier | Log loss | ECE | Brier (raw) |\n|---|---|---|---|---|")
te = [x for x in by_split["test"] if tradeable(x)]
yte = y_of(te)
qual = {}
for name, key, rawkey in [("A market mid", "mid", None), ("B const-vol normal", "c_B", "p_B"), ("C EWMA Student-t", "c_C", "p_C"),
                          ("D GARCH-t × diurnal", "c_D", "p_D"), ("E AEGIS (GARCH+jumps)", "c_E", "p_E"),
                          (f"F AEGIS ⊕ market (ω={omega:.2f})", "p_F", None), ("M market re-calibrated", "c_M", None)]:
    p = np.array([x[key] for x in te])
    qual[name] = dict(brier=CAL.brier(p, yte), ll=CAL.logloss(p, yte), ece=CAL.ece(p, yte))
    rb = f"{CAL.brier(np.array([x[rawkey] for x in te]), yte):.4f}" if rawkey else "–"
    P(f"| {name} | {qual[name]['brier']:.4f} | {qual[name]['ll']:.4f} | {qual[name]['ece']:.4f} | {rb} |")
R["quality_test"] = qual
P(f"\nPool weight ω on validation log loss (0 = market only, 1 = AEGIS only): best ω = **{omega:.2f}**. "
  + "Curve: " + ", ".join(f"{w}: {s:.4f}" for w, s in list(omega_curve.items())[::8]) + "\n")

# block-bootstrap difference in log loss F vs market (grouped by day)
def ll_vec(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4); return -(y * np.log(p) + (1 - y) * np.log(1 - p))
days_te = np.array([D.utc_day(x["close"]).toordinal() for x in te])
for key, nm in (("p_F", "F"), ("c_E", "E"), ("c_M", "M")):
    diff = ll_vec(np.array([x[key] for x in te]), yte) - ll_vec(np.array([x["mid"] for x in te]), yte)
    ud = np.unique(days_te); rng = np.random.default_rng(1); bs = []
    idx_by = {d: np.where(days_te == d)[0] for d in ud}
    for _ in range(1000):
        pick = rng.choice(ud, len(ud)); ii = np.concatenate([idx_by[d] for d in pick]); bs.append(diff[ii].mean())
    lo, hi = np.percentile(bs, [2.5, 97.5])
    P(f"- Log-loss difference {nm} − market on test: **{diff.mean():+.5f}** (day-block bootstrap 95% CI {lo:+.5f} .. {hi:+.5f}); negative = better than market.")
    R[f"ll_diff_{nm}"] = dict(mean=float(diff.mean()), lo=float(lo), hi=float(hi))
P("")

P("### Calibration by horizon / regime / price (TEST, model F vs market; Brier)\n")
P("| Slice | n | F | market |\n|---|---|---|---|")
sig_q = np.percentile([x["sigma"] for x in by_split["train"]], [33, 67])
slices = [("2–4 min", lambda x: x["mins"] <= 4), ("5–9 min", lambda x: 5 <= x["mins"] <= 9), ("10–14 min", lambda x: x["mins"] >= 10),
          ("low vol", lambda x: x["sigma"] < sig_q[0]), ("mid vol", lambda x: sig_q[0] <= x["sigma"] <= sig_q[1]), ("high vol", lambda x: x["sigma"] > sig_q[1]),
          ("price 3–20¢/80–97¢", lambda x: x["mid"] < 0.2 or x["mid"] > 0.8), ("price 20–80¢", lambda x: 0.2 <= x["mid"] <= 0.8)]
for nm, f in slices:
    s = [x for x in te if f(x)]
    if s:
        ys = y_of(s)
        P(f"| {nm} | {len(s)} | {CAL.brier(np.array([x['p_F'] for x in s]), ys):.4f} | {CAL.brier(np.array([x['mid'] for x in s]), ys):.4f} |")
P("\n### Reliability (TEST)\n")
P("| Bin | n | E cal predicted | realised | market predicted | realised |\n|---|---|---|---|---|---|")
re_e = {b[0]: b for b in CAL.reliability(np.array([x["c_E"] for x in te]), yte)}
re_m = {b[0]: b for b in CAL.reliability(np.array([x["mid"] for x in te]), yte)}
for b0 in sorted(set(re_e) | set(re_m)):
    e = re_e.get(b0); m = re_m.get(b0)
    P(f"| {b0:.1f} | {e[2] if e else 0} | {e[3]:.3f} | {e[4]:.3f} | " if e else f"| {b0:.1f} | 0 | – | – | " )
    L[-1] += (f"{m[3]:.3f} | {m[4]:.3f} |" if m else "– | – |")
P("\n### PIT of realised settlement average under each model (TEST; uniform = calibrated distribution)\n")
P("| Model | n | KS | KS 95% crit | share in 5% tails (ideal 0.10) | decile histogram |\n|---|---|---|---|---|---|")
for k in ("B", "C", "D", "E"):
    pits = [x["pit"][k] for x in by_split["test"] if "pit" in x and k in x["pit"]]
    if pits:
        st = CAL.pit_stats(pits)
        P(f"| {k} | {st['n']} | {st['ks']:.3f} | {st['ks_crit_95']:.3f} | {st['tails']:.3f} | {st['hist']} |")
        R.setdefault("pit", {})[k] = st
P("")

# ============================================================== 4. conservative bounds + trading (G)
log("bounds + trading ...")
trn = [x for x in by_split["train"] if tradeable(x)]
groups = np.array([D.utc_day(x["close"]).toordinal() for x in trn])
pE_tr = np.array([x["p_E"] for x in trn]); y_tr = y_of(trn)
for part in ("val", "test"):
    xs = by_split[part]
    pa = np.array([x["p_E"] for x in xs])
    ph, lo_y, hi_y, lo_n = CAL.block_bootstrap_bounds(pE_tr, y_tr, groups, pa, alpha=0.10, B=150)
    for x, a, b, c in zip(xs, lo_y, lo_n, ph):
        # conservative pooled probabilities per side (pool each bound with the market at the same ω)
        x["low_yes"] = float(CAL.log_pool(np.array(a), np.array(x["mid"]), omega))
        x["low_no"] = float(1 - CAL.log_pool(np.array(1 - b), np.array(x["mid"]), omega))

R["platt_E_draws"] = CAL.bootstrap_platt_params(pE_tr, y_tr, groups, B=200)

# coverage check of lower bounds on TEST: realised win-rate vs mean bound, by bin
def coverage(xs, side):
    key = "low_yes" if side == "yes" else "low_no"
    p = np.array([x[key] for x in xs]); y = y_of(xs) if side == "yes" else 1 - y_of(xs)
    out = []
    for b in range(10):
        sel = (p >= b / 10) & (p < (b + 1) / 10)
        if sel.sum() >= 50:
            out.append((b / 10, int(sel.sum()), float(p[sel].mean()), float(y[sel].mean())))
    return out
P("## 2. Conservative lower bounds: coverage on TEST\n")
P("Bounds = 10th percentile of a 150-draw day-block bootstrap of the calibration map, pooled with the market at ω. A bound is valid if the realised win rate is at least the bound.\n")
P("| Side | Bin | n | Mean p_low | Realised | Covered |\n|---|---|---|---|---|---|")
cov_ok = cov_n = 0
for side in ("yes", "no"):
    for b0, n, mp, yr in coverage([x for x in by_split["test"] if tradeable(x)], side):
        ok = yr >= mp; cov_ok += ok; cov_n += 1
        P(f"| {side} | {b0:.1f} | {n} | {mp:.3f} | {yr:.3f} | {'yes' if ok else '**no**'} |")
P(f"\nBins covered: {cov_ok}/{cov_n}.\n")
R["bound_coverage"] = dict(ok=cov_ok, n=cov_n)


def trade_sim(xs, theta, Q=10, lat=0.0, delay=False, use="bound", sized=False, model_key="p_F"):
    """One trade per window: first row (earliest) where conservative edge per contract > theta. Hold to settlement."""
    byw = collections.defaultdict(list)
    for x in xs:
        byw[x["ticker"]].append(x)
    trades = []
    daily_loss = collections.defaultdict(float)
    for tk in sorted(byw, key=lambda k: byw[k][0]["close"]):
        ws = sorted(byw[tk], key=lambda x: -x["mins"])
        for i, x in enumerate(ws):
            if not tradeable(x) or x["spread"] > 0.05:
                continue
            if use == "bound":
                py, pn = x["low_yes"], x["low_no"]
            else:
                py = x[model_key]; pn = 1 - py
            best = None
            for side, p, px in (("yes", py, x["ask"]), ("no", pn, 1 - x["bid"])):
                if not 0.03 <= px <= 0.97:
                    continue
                e = p - SM.buy_cost(Q, px) / Q - lat
                if best is None or e > best[1]:
                    best = (side, e, px, p)
            if not best or best[1] <= theta:
                continue
            fill = x
            if delay:
                if i + 1 >= len(ws) or not ws[i + 1]["fresh"]:
                    break
                fill = ws[i + 1]
            side = best[0]; px = fill["ask"] if side == "yes" else 1 - fill["bid"]
            q = Q
            if sized:  # conservative Kelly on the bound, capped: $5 max loss/trade, $25 daily loss stop
                p = best[3]; f = max(0.0, (p - px) / (1 - px)) * 0.25
                q = int(min(50, max(1, f * 500 / px), 5 / px))
                day = D.utc_day(x["close"])
                if daily_loss[day] <= -25:
                    break
            cost = SM.buy_cost(q, px)
            win = (x["y"] == 1) == (side == "yes")
            pnl = (q if win else 0) - cost
            if sized:
                daily_loss[D.utc_day(x["close"])] += min(pnl, 0)
            trades.append(dict(ticker=tk, close=x["close"], side=side, px=px, q=q, pnl=pnl, exp_edge=best[1], mins=fill["mins"],
                               fee=cost - q * px, sigma=x["sigma"]))
            break
    return trades


def tstats(trades):
    if not trades:
        return dict(n=0)
    pnl = np.array([t["pnl"] for t in trades]); q = np.array([t["q"] for t in trades])
    per_c = pnl / q * 100
    cum = np.cumsum(pnl); dd = float(np.max(np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]))
    days = np.array([D.utc_day(t["close"]).toordinal() for t in trades]); ud = np.unique(days)
    rng = np.random.default_rng(2); idx = {d: np.where(days == d)[0] for d in ud}
    bs = [pnl[np.concatenate([idx[d] for d in rng.choice(ud, len(ud))])].sum() for _ in range(1000)]
    cap = np.array([t["px"] * t["q"] for t in trades])
    return dict(n=len(trades), net=float(pnl.sum()), per_contract_c=float(per_c.mean()), win=float(np.mean(pnl > 0)),
                exp_edge_c=float(np.mean([t["exp_edge"] for t in trades]) * 100), fees=float(sum(t["fee"] for t in trades)),
                max_dd=dd, ci_lo=float(np.percentile(bs, 2.5)), ci_hi=float(np.percentile(bs, 97.5)),
                roc=float(pnl.sum() / max(cap.mean(), 1e-9)))


def row_t(name, s):
    if not s.get("n"):
        return f"| {name} | 0 | | | | | | | |"
    return (f"| {name} | {s['n']} | {s['exp_edge_c']:+.2f} | {s['per_contract_c']:+.2f} | {s['win']:.0%} | ${s['net']:+.2f} | "
            f"${s['ci_lo']:+.0f} .. ${s['ci_hi']:+.0f} | ${s['fees']:.2f} | ${s['max_dd']:.2f} |")


THETAS = [0.0, 0.01, 0.02, 0.03, 0.05]
P("## 3. Trading (15-minute): thresholds chosen on validation, TEST once\n")
P("One trade per window (first qualifying minute), 10 contracts, hold to settlement, executable ask, official fee + rounding. "
  "Edge columns are cents per contract; CI = day-block bootstrap 95% interval of test net P&L.\n")
P("| Strategy | Trades | Expected edge ¢ | Realised ¢/contract | Win | Net | 95% CI | Fees | Max DD |\n|---|---|---|---|---|---|---|---|---|")
strategies = collections.OrderedDict([
    ("G AEGIS ⊕ market, conservative bound", dict(use="bound")),
    ("F AEGIS ⊕ market, point estimate", dict(use="point", model_key="p_F")),
    ("E AEGIS calibrated (no market)", dict(use="point", model_key="c_E")),
    ("D GARCH-t calibrated", dict(use="point", model_key="c_D")),
    ("B const-vol calibrated", dict(use="point", model_key="c_B")),
    ("M market re-calibrated", dict(use="point", model_key="c_M")),
])
trade_results = {}
n_configs = 0
for nm, kw in strategies.items():
    vals = []
    for th in THETAS:
        n_configs += 1
        vals.append((th, tstats(trade_sim(by_split["val"], th, **kw))))
    elig = [v for v in vals if v[1].get("n", 0) >= 20]
    th_best = max(elig, key=lambda v: v[1]["net"])[0] if elig else THETAS[-1]
    s_val = dict(vals)[th_best]
    s_te = tstats(trade_sim(by_split["test"], th_best, **kw))
    trade_results[nm] = dict(theta=th_best, val=s_val, test=s_te, kw=kw)
    P(row_t(f"{nm} (θ={th_best:.2f}) · val", s_val)); P(row_t(f"**{nm} · TEST**", s_te))
R["trading_15m"] = {k: dict(theta=v["theta"], val=v["val"], test=v["test"]) for k, v in trade_results.items()}
P(f"\nConfigurations evaluated on validation: {n_configs} (6 strategies × {len(THETAS)} thresholds). With this many looks, a single "
  "validation winner at |t|≈2 is expected by chance; only the test column counts.\n")

g = trade_results["G AEGIS ⊕ market, conservative bound"]
P("### Execution stress on TEST (strategy G at its validation threshold)\n")
P("| Variant | Trades | Realised ¢/contract | Net |\n|---|---|---|---|")
for nm, kw in (("baseline", {}), ("+1¢ latency penalty", dict(lat=0.01)), ("+2¢ latency penalty", dict(lat=0.02)),
               ("fill one minute later", dict(delay=True)), ("conservative Kelly sizing + $25 daily stop", dict(sized=True))):
    s = tstats(trade_sim(by_split["test"], g["theta"], use="bound", **kw))
    P(f"| {nm} | {s.get('n', 0)} | {s.get('per_contract_c', float('nan')):+.2f} | ${s.get('net', 0):+.2f} |")
P("")
P("### By time to expiry (TEST, strategy F point estimate at θ=0, all qualifying minutes)\n")
P("| Entry minute | Trades | ¢/contract |\n|---|---|---|")
allf = trade_sim(by_split["test"], 0.0, use="point", model_key="p_F")
for nm, f in (("10–14", lambda t: t["mins"] >= 10), ("5–9", lambda t: 5 <= t["mins"] <= 9), ("2–4", lambda t: t["mins"] <= 4)):
    s = tstats([t for t in allf if f(t)])
    P(f"| {nm} | {s.get('n', 0)} | {s.get('per_contract_c', float('nan')):+.2f} |")
P("")

# ============================================================== 5. hourly market + cross-market
log(f"hourly ... ({time.time()-T0:.0f}s)")
mh = json.load(open(os.path.join(dh, "KXBTCD_markets.json")))
ch = json.load(open(os.path.join(dh, "KXBTCD_candles.json")))
anchors = sorted((D.iso_ts(m["close_time"]) - 900, float(m["floor_strike"])) for m in mk15.values() if m.get("floor_strike"))
anc_t = [a[0] for a in anchors]
cbs = D.MinuteSeries([(c[0], c[4]) for c in raw])


def basis_at(t):
    i = bisect.bisect_right(anc_t, t) - 1
    if i < 0:
        return float("nan")
    ta, K = anchors[i]
    c = cbs.at_close(ta)
    return K / c - 1 if c == c else float("nan")


def quote_at(cs, t, max_age=180):
    q = None
    for end, b, a in cs:
        if end <= t:
            q = (end, b, a)
        else:
            break
    if not q or q[1] is None or q[2] is None or t - q[0] > max_age:
        return None
    b, a = float(q[1]), float(q[2])
    return (b, a) if 0 < b < a < 1 else None


mhk = {m["ticker"]: m for m in mh}
H_OFFS = [60, 45, 30, 20, 15, 10, 5, 3, 2]
hrows = []
c15_by_close = collections.defaultdict(list)
for tk, m in mk15.items():
    c15_by_close[D.iso_ts(m["close_time"])].append(m)
cand15 = {}
for ev, by in json.load(open(os.path.join(d15, "KXBTC15M_candles.json"))).items():
    for tk, cs in by.items():
        cand15[tk] = sorted(cs)
xmkt = []          # cross-market consistency rows
scan = collections.Counter(); scan_examples = []
sims_h = SIMS["E"]
for ev, by in ch.items():
    ms = [mhk[t] for t in by if t in mhk and mhk[t].get("result") in ("yes", "no") and mhk[t].get("strike_type") == "greater"]
    if not ms:
        continue
    close = D.iso_ts(ms[0]["close_time"])
    cons = [SM.contract_from_market(m, "KXBTCD") for m in ms]
    for off in H_OFFS:
        t = close - 60 * off
        qs = {m["ticker"]: quote_at(sorted(by[m["ticker"]]), t) for m in ms}
        live = [(c, m, qs[m["ticker"]]) for c, m in zip(cons, ms) if qs[m["ticker"]]]
        if len(live) < 3:
            continue
        i = grid_index.get(int(t) - 60); bas = basis_at(t)
        if i is None or bas != bas:
            continue
        S = grid_c[i]; centre = S * (1 + bas)
        sd = sd_paths(t, off, ew[i] if ew[i] == ew[i] else 5e-4)
        ps, A = sims_h.prob_yes(centre, sd["E"], [c for c, _, _ in live])
        for (c, m, q), p in zip(live, ps):
            hrows.append(dict(ticker=m["ticker"], close=close, mins=off, y=1 if m["result"] == "yes" else 0, bid=q[0], ask=q[1],
                              mid=(q[0] + q[1]) / 2, p_E=p, split=("train" if close < CUTOFF + 14 * 86400 else ("val" if close < CUTOFF + 22 * 86400 else "test"))))
        # 15M window settling at the same instant (shares A_T except in a data outage)
        for m15 in c15_by_close.get(close, []):
            q15 = quote_at(cand15.get(m15["ticker"], []), t)
            if not q15 or off > 14:
                continue
            c15 = SM.contract_from_market(m15, "KXBTC15M")
            quotes = {c.ticker: q for c, _, q in live}; quotes[c15.ticker] = q15
            opps = SC.scan_shared([c15] + [c for c, _, _ in live], quotes, Q=10)
            scan["minutes_checked"] += 1
            if opps:
                scan["minutes_with_opp"] += 1; scan["opps"] += len(opps)
                scan["opps_survive_outage"] += sum(1 for o in opps if o["floor_with_outage"] > 0)
                if len(scan_examples) < 5:
                    best = max(opps, key=lambda o: o["floor"]); scan_examples.append((t, best))
            # cross-market consensus: hourly-implied P(A >= K15) vs 15M quote
            fitres = DIST.fit([c for c, _, _ in live], [q[0] for _, _, q in live], [q[1] for _, _, q in live], spot=centre)
            p_x = DIST.prob_of(c15, fitres)
            xmkt.append(dict(ticker=m15["ticker"], close=close, mins=off, y=1 if m15["result"] == "yes" else 0, bid=q15[0], ask=q15[1],
                             mid=(q15[0] + q15[1]) / 2, p_x=p_x, fit_err=fitres["fit_error"],
                             split=("train" if close < CUTOFF + 14 * 86400 else ("val" if close < CUTOFF + 22 * 86400 else "test"))))
        # within-ladder dominance (hourly strikes only)
        quotes = {c.ticker: q for c, _, q in live}
        lad = SC.scan_shared([c for c, _, _ in live], quotes, Q=10)
        scan["ladder_minutes"] += 1
        if lad:
            scan["ladder_minutes_with_opp"] += 1; scan["ladder_opps"] += len(lad)
log(f"hourly rows {len(hrows)}, xmkt {len(xmkt)} ({time.time()-T0:.0f}s)")

P("## 4. Hourly market (KXBTCD) vs 15-minute\n")
P(f"Hourly data: {len({r_['ticker'] for r_ in hrows})} near-the-money strikes (within 2% of settlement), {len(hrows)} decision rows at 2–60 min, "
  "Oct 23 – Nov 22. Splits: train first 14 days · validation next 8 · test rest. AEGIS (model E) probabilities share one path set per (event, minute).\n")
P("| Split | n | Brier AEGIS raw | Brier market | Log loss AEGIS | Log loss market |\n|---|---|---|---|---|---|")
for s in ("train", "val", "test"):
    xs = [x for x in hrows if x["split"] == s and 0.03 <= x["mid"] <= 0.97]
    if xs:
        yy = y_of(xs); pe = np.array([x["p_E"] for x in xs]); pm = np.array([x["mid"] for x in xs])
        P(f"| {s} | {len(xs)} | {CAL.brier(pe, yy):.4f} | {CAL.brier(pm, yy):.4f} | {CAL.logloss(pe, yy):.4f} | {CAL.logloss(pm, yy):.4f} |")
P("")

P("## 5. Cross-market dominance scanner (H)\n")
P(f"For every minute where a KXBTC15M window and a KXBTCD ladder settle at the same instant (minute-close quotes, 2–14 min out), all 2-leg combinations "
  "with full outcome coverage were checked after official fees for 10 contracts (minimum payout over enumerated settlement states).\n")
P(f"- Shared-settlement minutes checked: **{scan['minutes_checked']}**; minutes with ≥1 after-fee dominance signal: **{scan['minutes_with_opp']}** "
  f"({scan['opps']} pair signals). Signals that also survive the official data-outage rule (15M resolves NO, hourly settles later): **{scan['opps_survive_outage']}**.")
P(f"- Within-ladder (hourly strikes only) minutes checked: {scan['ladder_minutes']}; with a signal: **{scan['ladder_minutes_with_opp']}** ({scan['ladder_opps']} signals).")
P("- Executability is **not** established: candle closes are minute snapshots of separate books (not synchronous), there is no historical depth, and legs are taken sequentially.")
for t, o in scan_examples:
    P(f"  - {datetime.datetime.fromtimestamp(t, datetime.UTC):%Y-%m-%d %H:%M} UTC: {o['legs']} floor ${o['floor']:+.2f} (with outage ${o['floor_with_outage']:+.2f})")
R["scanner"] = dict(scan)
P("")

P("## 6. Cross-market consensus (X, original alternative)\n")
P("Fair value for the 15-minute contract = probability implied by the **hourly ladder that settles at the same instant**, extracted with the "
  "constrained simplex fit (`aegis/distribution.py`). This is model-free (no volatility model) and tests whether the two markets disagree enough to trade.\n")
if xmkt:
    P("| Split | n | Brier hourly-implied | Brier 15M mid | mean |p_x − mid| |\n|---|---|---|---|---|")
    for s in ("train", "val", "test"):
        xs = [x for x in xmkt if x["split"] == s and 0.03 <= x["mid"] <= 0.97]
        if xs:
            yy = y_of(xs)
            P(f"| {s} | {len(xs)} | {CAL.brier(np.array([x['p_x'] for x in xs]), yy):.4f} | {CAL.brier(np.array([x['mid'] for x in xs]), yy):.4f} | "
              f"{np.mean([abs(x['p_x'] - x['mid']) for x in xs]):.4f} |")
    P("\n| Threshold θ | Split | Trades | ¢/contract | Net |\n|---|---|---|---|---|")
    for th in (0.0, 0.02, 0.05):
        for s in ("val", "test"):
            xs = [dict(x, fresh=True, spread=x["ask"] - x["bid"], sigma=0.0, low_yes=x["p_x"], low_no=1 - x["p_x"])
                  for x in xmkt if x["split"] == s]
            st = tstats(trade_sim(xs, th, use="bound"))
            P(f"| {th:.2f} | {s} | {st.get('n', 0)} | {st.get('per_contract_c', float('nan')):+.2f} | ${st.get('net', 0):+.2f} |")
    R["xmkt_n"] = len(xmkt)
P("")

elapsed = round(time.time() - T0)
open(OUT_MD, "w").write("\n".join(L).replace("{ELAPSED}", str(elapsed)) + "\n")
json.dump(R, open(os.path.join(os.path.dirname(__file__), "aegis_results.json"), "w"), indent=1, default=float)
print("\n".join(L).replace("{ELAPSED}", str(elapsed)))
