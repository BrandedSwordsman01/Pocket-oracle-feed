"""Volatility models on 1-minute log returns: EWMA, GARCH(1,1) with Student-t innovations, diurnal seasonality, jumps.

All parameters are estimated on a training slice only and then frozen.
"""
import math
import numpy as np
from scipy import optimize, special, signal

MIN_PER_DAY = 1440


def log_returns(closes):
    c = np.asarray(closes, float)
    r = np.diff(np.log(c))
    return np.where(np.isfinite(r), r, 0.0)


def diurnal_factors(times, r):
    """Mean squared return by UTC hour, normalised to mean 1 (multiplicative variance seasonality)."""
    hours = (np.asarray(times) // 3600) % 24
    f = np.array([np.mean(r[hours == h] ** 2) if np.any(hours == h) else np.nan for h in range(24)])
    f = np.where(np.isfinite(f) & (f > 0), f, np.nanmean(f))
    return f / f.mean()


def t_std_logpdf(z, nu):
    """Log density of a unit-variance Student-t."""
    s = math.sqrt((nu - 2) / nu)
    return (special.gammaln((nu + 1) / 2) - special.gammaln(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
            - (nu + 1) / 2 * np.log1p(z * z / (nu - 2)))


def garch_filter(r, w, a, b, h0):
    """h[0] = h0; h[i+1] = w + a r[i]^2 + b h[i]  (vectorised with a linear filter). Returns len(r)+1 values."""
    x = w + a * np.r_[0.0, r] ** 2
    x[0] = h0
    return signal.lfilter([1.0], [1.0, -b], x)


class GarchT:
    """r_t = sqrt(h_t) z_t, h_t = w + a r_{t-1}^2 + b h_{t-1}, z ~ unit-variance t(nu). Fit on de-seasonalised returns."""

    def fit(self, r):
        r = np.asarray(r, float)
        var0 = r.var()

        def nll(theta):
            w, a, b, nu = theta
            if w <= 0 or a < 0 or b < 0 or a + b >= 0.9999 or nu <= 2.05:
                return 1e12
            h = garch_filter(r, w, a, b, var0)[:-1]
            return -np.sum(t_std_logpdf(r / np.sqrt(h), nu) - 0.5 * np.log(h))

        # coarse grid start, then Nelder-Mead (robust, no gradients needed)
        x0 = [var0 * 0.05, 0.08, 0.90, 5.0]
        res = optimize.minimize(nll, x0, method="Nelder-Mead", options=dict(maxiter=600, xatol=1e-10, fatol=1e-3))
        self.w, self.a, self.b, self.nu = res.x
        self.uncond = self.w / (1 - self.a - self.b)
        self.nll = res.fun
        return self

    def filter(self, r):
        return garch_filter(np.asarray(r, float), self.w, self.a, self.b, self.uncond)  # h[i] = variance forecast for return i (made with info up to i-1); h[-1] = next-step forecast

    def forecast(self, h_next, steps):
        """Multi-step variance forecasts h_{t+1..t+steps} given the one-step forecast."""
        p = self.a + self.b
        k = np.arange(steps)
        return self.uncond + (p ** k) * (h_next - self.uncond)


def jump_params(z, thresh=4.5):
    """Jumps = standardised residuals beyond `thresh`. Returns per-minute intensity and the empirical jump sizes (in z units)."""
    z = np.asarray(z)
    j = z[np.abs(z) > thresh]
    return dict(lam=len(j) / len(z), sizes=j, thresh=thresh)


def fit_t_nu(z):
    """MLE of unit-variance t degrees of freedom for standardised residuals."""
    z = np.asarray(z)
    res = optimize.minimize_scalar(lambda nu: -np.sum(t_std_logpdf(z, nu)), bounds=(2.1, 60), method="bounded")
    return res.x
