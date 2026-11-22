"""Learning model with a promotion gate.

Until a trained model has beaten Kalshi's own prices on held-out data, predictions are the market-anchored
fallback (log-pool of Kalshi mid and the baseline settlement probability, weight 0.2) and the engine marks
itself "learning" so the phone bot does not bet on it.

Training (nightly + on demand): LightGBM binary classifier on the feature vector (which includes the Kalshi mid,
so the model learns *corrections* to the market). Validation = the most recent 25% of settled contracts by time,
grouped by contract so rows from one window never straddle train/validation. Promotion requires:
  - log loss better than the market mid by at least MIN_GAIN, and
  - positive simulated P&L after Kalshi fees on validation (taker, one trade per contract, edge > THETA).
"""
import json, math, os, time
import numpy as np
from .features import FEATURES

MIN_ROWS = 4000
MIN_GAIN = 0.002
THETA = 0.02


def _logit(p):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))


def fallback(mid, p_base, w=0.2):
    a = (p_base + 1e-4) ** w * (mid + 1e-4) ** (1 - w)
    b = (1 - p_base + 1e-4) ** w * (1 - mid + 1e-4) ** (1 - w)
    return a / (a + b)


def fee(P):
    return math.ceil(round(0.07 * 10 * P * (1 - P) * 100, 6)) / 1000  # per contract, 10-lot, cents rounding


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


class Model:
    def __init__(self, folder):
        self.folder = folder
        self.booster = None
        self.active = False
        self.metrics = {}
        os.makedirs(folder, exist_ok=True)
        self._load()

    def _load(self):
        p = os.path.join(self.folder, "model.txt"); m = os.path.join(self.folder, "model.json")
        if os.path.exists(p) and os.path.exists(m):
            try:
                import lightgbm as lgb
                self.booster = lgb.Booster(model_file=p)
                with open(m) as fh:
                    meta = json.load(fh)
                self.active, self.metrics = meta.get("active", False), meta.get("metrics", {})
            except Exception:
                self.booster = None

    def predict(self, feats, mid, p_base):
        fb = fallback(mid, p_base)
        if self.booster is None or not self.active:
            return fb, False
        x = np.array([[feats[k] for k in FEATURES]])
        return float(self.booster.predict(x)[0]), True

    def train(self, rows, use_gpu=False):
        """rows: (ts,ticker,series,close_ts,strike,kind,bid,ask,feats_json,p_base,y). Returns metrics dict."""
        import lightgbm as lgb
        if len(rows) < MIN_ROWS:
            return dict(status="waiting", rows=len(rows), need=MIN_ROWS)
        tick_close = {}
        for r in rows:
            tick_close[r[1]] = r[3]
        order = sorted(tick_close, key=tick_close.get)
        cut = order[int(len(order) * 0.75)]
        cut_ts = tick_close[cut]
        X = np.array([[json.loads(r[8])[k] for k in FEATURES] for r in rows])
        y = np.array([r[10] for r in rows], float)
        mid = np.array([(r[6] + r[7]) / 2 for r in rows])
        tr = np.array([r[3] < cut_ts for r in rows]); va = ~tr
        if va.sum() < 500 or tr.sum() < 2000:
            return dict(status="waiting", rows=len(rows), need="more settled contracts")
        params = dict(objective="binary", learning_rate=0.03, num_leaves=15, min_data_in_leaf=200, feature_fraction=0.8,
                      bagging_fraction=0.8, bagging_freq=1, lambda_l2=5.0, verbose=-1)
        if use_gpu:
            params["device_type"] = "gpu"
        ds = lgb.Dataset(X[tr], y[tr])
        dv = lgb.Dataset(X[va], y[va], reference=ds)
        try:
            bst = lgb.train(params, ds, num_boost_round=600, valid_sets=[dv], callbacks=[lgb.early_stopping(50, verbose=False)])
        except Exception:
            params.pop("device_type", None)
            bst = lgb.train(params, ds, num_boost_round=600, valid_sets=[dv], callbacks=[lgb.early_stopping(50, verbose=False)])
        pv = bst.predict(X[va])
        ll_model, ll_market = logloss(pv, y[va]), logloss(mid[va], y[va])
        # one-trade-per-contract taker simulation on validation
        pnl, n = 0.0, 0
        done = set()
        rv = [r for r, keep in zip(rows, va) if keep]
        for r, p in zip(rv, pv):
            if r[1] in done:
                continue
            ya, na = r[7], 1 - r[6]
            ey, en = p - ya - fee(ya), (1 - p) - na - fee(na)
            side, e, px = ("yes", ey, ya) if ey >= en else ("no", en, na)
            if e > THETA and 0.03 <= px <= 0.97:
                win = (r[10] == 1) == (side == "yes")
                pnl += (1 - px if win else -px) - fee(px); n += 1; done.add(r[1])
        gain = ll_market - ll_model
        active = gain >= MIN_GAIN and pnl > 0 and n >= 30
        imp = dict(zip(FEATURES, [int(v) for v in bst.feature_importance("gain")]))
        metrics = dict(status="promoted" if active else "not better than market", rows=len(rows), train_rows=int(tr.sum()),
                       val_rows=int(va.sum()), ll_model=ll_model, ll_market=ll_market, gain=gain, val_trades=n,
                       val_pnl_per_contract_c=(pnl / n * 100 if n else 0.0), top_features=sorted(imp, key=imp.get, reverse=True)[:8],
                       trained_at=time.time())
        bst.save_model(os.path.join(self.folder, "model.txt"))
        with open(os.path.join(self.folder, "model.json"), "w") as fh:
            json.dump(dict(active=active, metrics=metrics), fh)
        self.booster, self.active, self.metrics = bst, active, metrics
        return metrics
