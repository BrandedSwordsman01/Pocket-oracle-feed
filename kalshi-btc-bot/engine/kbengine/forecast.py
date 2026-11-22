"""Multi-hour Bitcoin price-range forecasts (Monte Carlo) and a scan of Kalshi's hourly ladders against them.

Volatility: GARCH(1,1) x time-of-day profile (parameters fit offline, vol_params.json), state updated from live
1-minute closes. Paths: Student-t innovations (fat tails), zero drift (no direction is predictable at these
horizons). The GPU is used automatically if CuPy is installed (pip install cupy-cuda12x); NumPy otherwise.

Honest scope: this forecasts HOW FAR Bitcoin may move, not which way. Our 30-day test found Kalshi's hourly
ladder prices movement about right (implied/forecast vol ratio 0.92), so the ladder scan is informational.
"""
import json, math, os, time
import numpy as np

try:
    import cupy as xp  # optional GPU
    GPU = True
except Exception:
    xp = np
    GPU = False

P = json.load(open(os.path.join(os.path.dirname(__file__), "vol_params.json")))
G = P["garch"]; DIUR = np.array(P["diurnal"])
UNC = G["w"] / (1 - G["a"] - G["b"])


def garch_state(minute_closes, minute_times):
    """One-step-ahead variance (de-seasonalised) after filtering recent 1-minute returns."""
    h = UNC
    for i in range(1, len(minute_closes)):
        r = math.log(minute_closes[i] / minute_closes[i - 1]) / math.sqrt(DIUR[(int(minute_times[i]) // 3600) % 24])
        if math.isfinite(r):
            h = G["w"] + G["a"] * r * r + G["b"] * h
    return h


def per_minute_sd(h_next, t0, minutes):
    k = np.arange(minutes)
    hk = UNC + (G["a"] + G["b"]) ** k * (h_next - UNC)
    hours = ((t0 + 60 * k) // 3600) % 24
    return np.sqrt(hk * DIUR[hours])


def simulate(spot, sd, n_paths=20000, seed=7):
    """Cumulative log-return paths (n_paths x minutes) with unit-variance t innovations."""
    rng = xp.random.default_rng(seed) if GPU else np.random.default_rng(seed)
    nu = max(G.get("nu", 6.0), 2.5)
    m = len(sd)
    z = rng.standard_normal((n_paths, m), dtype=xp.float32 if GPU else np.float64)
    chi = rng.chisquare(nu, size=(n_paths, m))
    e = z / xp.sqrt(chi / nu) * math.sqrt((nu - 2) / nu)
    sdx = xp.asarray(sd)
    steps = e * sdx - 0.5 * sdx ** 2
    return spot * xp.exp(xp.cumsum(steps, axis=1))


def forecast(spot, minute_closes, minute_times, hour_closes, n_paths=20000):
    """Quantiles of price at each requested close time (unix seconds) and the simulated values for ladder pricing."""
    now = time.time()
    t0 = int(now // 60 * 60)
    horizons = sorted(c for c in hour_closes if c > now + 60)
    if not horizons:
        return dict(horizons=[], samples={})
    mins = int(math.ceil((horizons[-1] - t0) / 60))
    h_next = garch_state(minute_closes[-300:], minute_times[-300:]) if len(minute_closes) > 30 else UNC
    sd = per_minute_sd(h_next, t0, mins)
    paths = simulate(spot, sd, n_paths)
    out, samples = [], {}
    for c in horizons:
        idx = max(0, min(mins - 1, int(round((c - t0) / 60)) - 1))
        col = paths[:, idx]
        if GPU:
            col = xp.asnumpy(col)
        q = np.quantile(col, [0.05, 0.25, 0.5, 0.75, 0.95])
        out.append(dict(close_ts=int(c), q05=float(q[0]), q25=float(q[1]), q50=float(q[2]), q75=float(q[3]), q95=float(q[4]),
                        mins=int((c - now) // 60)))
        samples[int(c)] = col
    return dict(horizons=out, samples=samples, gpu=GPU, vol_ann=float(np.sqrt(np.mean(sd ** 2) * 525600)), generated=now)


def ladder_scan(samples, markets, fee_fn):
    """Compare every hourly strike with the forecast. markets: dicts with ticker, close_ts, strike, kind, bid, ask."""
    rows = []
    for m in markets:
        col = samples.get(int(m["close_ts"]))
        if col is None or not (0 < m["bid"] < m["ask"] < 1):
            continue
        p = float(np.mean(col > m["strike"]))
        ey = p - m["ask"] - fee_fn(m["ask"]); en = (1 - p) - (1 - m["bid"]) - fee_fn(1 - m["bid"])
        rows.append(dict(ticker=m["ticker"], close_ts=int(m["close_ts"]), strike=m["strike"], p_forecast=p,
                         bid=m["bid"], ask=m["ask"], mid=(m["bid"] + m["ask"]) / 2,
                         best_side="yes" if ey >= en else "no", edge=max(ey, en)))
    rows.sort(key=lambda r: -r["edge"])
    return rows
