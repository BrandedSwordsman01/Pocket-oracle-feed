"""Calibration, scoring, conservative bounds and probability pooling.

Bounds: day-blocked bootstrap of the calibration map (resampling whole settlement days keeps the dependence between
the many decision rows of one window/day together). For each row:
  p_low_yes = q_alpha of bootstrap calibrated P(YES);  p_low_no = q_alpha of bootstrap calibrated P(NO) = 1 - q_(1-alpha) P(YES)
computed per side from the bootstrap draws, NOT as 1 - p_low_yes.
"""
import math
import numpy as np
from scipy import optimize

EPS = 1e-4


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def logloss(p, y):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS); y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def ece(p, y, bins=10):
    p = np.asarray(p); y = np.asarray(y); e = 0.0
    for b in range(bins):
        sel = (p >= b / bins) & (p < (b + 1) / bins) if b < bins - 1 else (p >= b / bins)
        if sel.any():
            e += sel.mean() * abs(p[sel].mean() - y[sel].mean())
    return float(e)


def reliability(p, y, bins=10):
    p = np.asarray(p); y = np.asarray(y); out = []
    for b in range(bins):
        sel = (p >= b / bins) & (p < (b + 1) / bins) if b < bins - 1 else (p >= b / bins)
        if sel.any():
            out.append((b / bins, (b + 1) / bins, int(sel.sum()), float(p[sel].mean()), float(y[sel].mean())))
    return out


def pit_stats(pit):
    """PIT values of realised settlement under the predictive CDF; uniform if the distribution is calibrated."""
    pit = np.sort(np.asarray(pit))
    n = len(pit)
    ks = float(np.max(np.abs(np.arange(1, n + 1) / n - pit)))
    hist = np.histogram(pit, bins=10, range=(0, 1))[0] / n
    return dict(n=n, ks=ks, ks_crit_95=1.36 / math.sqrt(n), hist=hist.round(3).tolist(),
                tails=float(np.mean((pit < 0.05) | (pit > 0.95))))


class Platt:
    """p_cal = sigmoid(a + b * logit(p_raw)) fitted by maximum likelihood (L2 on (a, b-1))."""

    def fit(self, p, y, l2=1e-3):
        x = logit(p); y = np.asarray(y, float)

        def nll(th):
            q = np.clip(sigmoid(th[0] + th[1] * x), EPS, 1 - EPS)
            return -np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)) + l2 * (th[0] ** 2 + (th[1] - 1) ** 2)
        self.a, self.b = optimize.minimize(nll, [0.0, 1.0], method="BFGS").x
        return self

    def __call__(self, p):
        return sigmoid(self.a + self.b * logit(p))


def block_bootstrap_bounds(p_fit, y_fit, groups, p_apply, alpha=0.10, B=200, seed=3):
    """Refit Platt on day-block resamples; return (p_hat, low_yes, high_yes, low_no) for p_apply."""
    rng = np.random.default_rng(seed)
    groups = np.asarray(groups); ug = np.unique(groups)
    idx_by = {g: np.where(groups == g)[0] for g in ug}
    draws = []
    for _ in range(B):
        pick = rng.choice(ug, size=len(ug), replace=True)
        ii = np.concatenate([idx_by[g] for g in pick])
        draws.append(Platt().fit(p_fit[ii], y_fit[ii])(p_apply))
    D = np.array(draws)
    p_hat = Platt().fit(p_fit, y_fit)(p_apply)
    low_yes = np.quantile(D, alpha, axis=0)
    high_yes = np.quantile(D, 1 - alpha, axis=0)
    low_no = np.quantile(1 - D, alpha, axis=0)
    return p_hat, low_yes, high_yes, low_no


def log_pool(p_model, p_market, omega, eps=1e-4):
    """Two-bucket logarithmic pool: normalised (pm+eps)^w (pk+eps)^(1-w) over {YES, NO}."""
    a = (p_model + eps) ** omega * (p_market + eps) ** (1 - omega)
    b = (1 - p_model + eps) ** omega * (1 - p_market + eps) ** (1 - omega)
    return a / (a + b)


def fit_omega(p_model, p_market, y):
    grid = np.linspace(0, 1, 41)
    scores = [logloss(log_pool(p_model, p_market, w), y) for w in grid]
    return float(grid[int(np.argmin(scores))]), dict(zip(grid.round(3).tolist(), scores))


def bootstrap_platt_params(p_fit, y_fit, groups, B=200, seed=3):
    """Day-block bootstrap draws of Platt (a, b); used live to compute per-side conservative bounds."""
    rng = np.random.default_rng(seed)
    groups = np.asarray(groups); ug = np.unique(groups)
    idx_by = {g: np.where(groups == g)[0] for g in ug}
    out = []
    for _ in range(B):
        ii = np.concatenate([idx_by[g] for g in rng.choice(ug, size=len(ug), replace=True)])
        pl = Platt().fit(p_fit[ii], y_fit[ii]); out.append((float(pl.a), float(pl.b)))
    return out


def bounds_from_params(params, p_raw, alpha=0.10):
    D = np.array([sigmoid(a + b * logit(p_raw)) for a, b in params])
    return float(np.quantile(D, alpha)), float(np.quantile(1 - D, alpha))  # (low_yes, low_no)
