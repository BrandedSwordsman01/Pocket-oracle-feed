"""Load KXBTC15M history and build one causal feature row per (window, minute-before-close).

Inputs (from research/fetch_data.py and research/fetch_aux.py):
  KXBTC15M_markets.json   settled markets: floor_strike (= BRTI 60s avg at window open), result, expiration_value (BRTI at close)
  KXBTC15M_candles.json   per-minute Kalshi YES bid/ask closes, {event: {ticker: [[end_ts, bid, ask], ...]}}
  coinbase_1m.json        Coinbase BTC-USD 1m candles [time, low, high, open, close, volume]
  bitstamp_1m.json        optional, [[time, close], ...]
  deribit_dvol_1m.json    optional, [[time, dvol_pct], ...]

Every feature at decision time t uses only data timestamped <= t (Coinbase candle starting t-60 closes at t).
"""
import json, math, os, datetime
from dataclasses import dataclass, field
import numpy as np

MINUTES = list(range(14, 0, -1))  # decision points: 14 .. 1 minutes before close


def iso_ts(s):
    return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def utc_day(t):
    return datetime.datetime.fromtimestamp(t, datetime.UTC).date()


class MinuteSeries:
    """Forward-filled 1-minute close series on a regular grid (index by candle start time)."""

    def __init__(self, pairs, max_gap_min=5):
        pairs = sorted((int(t), float(v)) for t, v in pairs)
        self.t0 = pairs[0][0] // 60 * 60
        n = (pairs[-1][0] - self.t0) // 60 + 1
        vals = np.full(n, np.nan)
        for t, v in pairs:
            vals[(t - self.t0) // 60] = v
        self.raw = vals.copy()
        # forward fill, but leave NaN after gaps longer than max_gap_min (data outage)
        last, gap = np.nan, 0
        for i in range(n):
            if np.isnan(vals[i]):
                gap += 1
                vals[i] = last if gap <= max_gap_min else np.nan
            else:
                last, gap = vals[i], 0
        self.v = vals

    def idx(self, t):
        return (int(t) - self.t0) // 60

    def at_close(self, t):
        """Close of the last candle that has fully closed by time t (candle starting t-60)."""
        i = self.idx(t) - 1
        return self.v[i] if 0 <= i < len(self.v) else np.nan


def wilder_rsi(closes, n=14):
    out = np.full(len(closes), np.nan)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if np.isnan(d):
            continue
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            gain += g / n; loss += l / n
            if i < n:
                continue
        else:
            gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def ewma_sigma(closes, hl=60):
    """Per-minute EWMA volatility of log returns; value at i uses returns up to i."""
    lam = 0.5 ** (1 / hl)
    out = np.full(len(closes), np.nan)
    var = None
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if np.isnan(a) or np.isnan(b):
            out[i] = math.sqrt(var) if var else np.nan
            continue
        r = math.log(b / a)
        var = r * r if var is None else lam * var + (1 - lam) * r * r
        out[i] = math.sqrt(var)
    return out


@dataclass
class Dataset:
    rows: list                      # list of dict feature rows
    windows: dict                   # ticker -> list of rows (14 -> 1 min)
    days: list
    meta: dict = field(default_factory=dict)


def load(data_dir, series="KXBTC15M", max_quote_age=180):
    markets = json.load(open(os.path.join(data_dir, f"{series}_markets.json")))
    candles = json.load(open(os.path.join(data_dir, f"{series}_candles.json")))
    cb_raw = json.load(open(os.path.join(data_dir, "coinbase_1m.json")))
    cb = MinuteSeries([(c[0], c[4]) for c in cb_raw])
    bs = None
    p = os.path.join(data_dir, "bitstamp_1m.json")
    if os.path.exists(p):
        bs = MinuteSeries(json.load(open(p)))
    dv = None
    p = os.path.join(data_dir, "deribit_dvol_1m.json")
    if os.path.exists(p):
        dv = MinuteSeries(json.load(open(p)), max_gap_min=60)

    # indicators on the full Coinbase grid (index i = candle starting t0 + 60*i, closing one minute later)
    rsi1 = wilder_rsi(cb.v, 14)
    sig = ewma_sigma(cb.v, 60)
    # 5-minute bars: close at each 5-min boundary; RSI over those bars, mapped back to minute index
    five_idx = np.array([i for i in range(len(cb.v)) if ((cb.t0 + 60 * (i + 1)) % 300) == 0])
    rsi5_bars = wilder_rsi(cb.v[five_idx], 14)
    rsi5 = np.full(len(cb.v), np.nan)
    j = -1
    for i in range(len(cb.v)):
        while j + 1 < len(five_idx) and five_idx[j + 1] <= i:
            j += 1
        if j >= 0:
            rsi5[i] = rsi5_bars[j]

    mk = {m["ticker"]: m for m in markets}
    rows, windows = [], {}
    for ev, by in candles.items():
        for tick, cs in by.items():
            m = mk.get(tick)
            if not m or m.get("result") not in ("yes", "no") or m.get("floor_strike") is None:
                continue
            close = iso_ts(m["close_time"])
            open_t = close - 900
            K = float(m["floor_strike"])
            y = 1 if m["result"] == "yes" else 0
            cs = sorted(cs)
            # Coinbase-vs-BRTI basis at window open: BRTI 60s avg (= strike) vs Coinbase close of the minute before open
            cb_open = cb.at_close(open_t)
            basis = (K / cb_open - 1) if cb_open and not np.isnan(cb_open) else np.nan
            wrows = []
            for mins in MINUTES:
                t = close - 60 * mins
                q, qend = None, None
                for end, bid, ask in cs:
                    if end <= t:
                        q, qend = (bid, ask), end
                    else:
                        break
                if q is None or q[0] is None or q[1] is None:
                    continue
                bid, ask = float(q[0]), float(q[1])
                i = cb.idx(t) - 1
                if i < 20 or i >= len(cb.v):
                    continue
                S = cb.v[i]
                if np.isnan(S) or np.isnan(sig[i]):
                    continue
                S5, S15 = cb.v[i - 5], cb.v[i - 15]
                r = dict(
                    ticker=tick, close=close, t=t, mins=mins, y=y, K=K,
                    bid=bid, ask=ask, mid=(bid + ask) / 2, spread=ask - bid, quote_age=t - qend,
                    fresh=(t - qend) <= max_quote_age and 0 < bid < ask < 1,
                    S=S, S_bs=(bs.at_close(t) if bs else np.nan), basis=basis,
                    sigma=float(sig[i]), rsi1=float(rsi1[i]), rsi5=float(rsi5[i]),
                    mom5=math.log(S / S5) if S5 and not np.isnan(S5) else np.nan,
                    mom15=math.log(S / S15) if S15 and not np.isnan(S15) else np.nan,
                    dvol=(dv.at_close(t) if dv else np.nan),
                    hour=datetime.datetime.fromtimestamp(t, datetime.UTC).hour,
                    dow=datetime.datetime.fromtimestamp(t, datetime.UTC).weekday(),
                )
                wrows.append(r)
            # mid change over the previous 5 decision minutes (market reaction, for lag detection)
            for k, r in enumerate(wrows):
                prev = [x for x in wrows[:k] if x["mins"] >= r["mins"] + 5]
                r["dmid5"] = r["mid"] - prev[-1]["mid"] if prev else np.nan
            if wrows:
                windows[tick] = wrows
                rows.extend(wrows)
    rows.sort(key=lambda r: (r["close"], -r["mins"]))
    days = sorted({utc_day(r["close"]) for r in rows})
    return Dataset(rows=rows, windows=windows, days=days,
                   meta=dict(series=series, n_markets=len(windows), has_bitstamp=bs is not None, has_dvol=dv is not None))
