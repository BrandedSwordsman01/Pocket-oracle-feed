"""Monte Carlo settlement simulator with common random numbers (CRN).

log(A_T / C_t) = sum_{k=1}^{m-1} sd_k * e_k + sd_m * sqrt(f_last) * e_m + jumps
  - C_t: benchmark-adjusted centre (Coinbase spot * (1 + basis measured at the last BRTI anchor))
  - sd_k: per-minute volatility forecasts for the remaining minutes (EWMA, GARCH, diurnal...)
  - f_last = settlement.last_minute_factor() (~0.336): variance share of the final 60-sample average
  - e_k: standardised innovations (normal or unit-variance t(nu)); jumps: compound Poisson in the m-1 full minutes
    and f_last of the final minute, sizes drawn from the empirical jump distribution (z units * sd_k)
Zero drift (the -0.5 v convexity term is applied so E[A/C] = 1 under the chosen definition; negligible at these horizons).
The same paths are used for every strike that shares the settlement, so probabilities are monotone and coherent.
Per-second paths are replaced by minute steps + the exact discrete last-minute factor; see docs/aegis2_mathematics.md.
"""
import numpy as np
from . import settlement as SM


class Simulator:
    def __init__(self, n_paths=4000, max_steps=120, seed=11, nu=None, jumps=None):
        rng = np.random.default_rng(seed)
        self.N, self.max_steps = n_paths, max_steps
        if nu:
            g = rng.chisquare(nu, size=(n_paths, max_steps)) / nu
            self.E = rng.standard_normal((n_paths, max_steps)) / np.sqrt(g) * np.sqrt((nu - 2) / nu)
        else:
            self.E = rng.standard_normal((n_paths, max_steps))
        self.U = rng.random((n_paths, max_steps))          # for jump arrivals
        self.J = rng.integers(0, 1 << 30, size=(n_paths, max_steps))  # for jump-size draws
        self.jumps = jumps
        self.f_last = SM.last_minute_factor()

    def log_returns(self, sd):
        """sd: per-minute vol forecasts for the m remaining minutes (last element = final minute). Returns N samples."""
        m = len(sd)
        w = np.array(sd, float).copy()
        w[-1] *= np.sqrt(self.f_last)
        x = self.E[:, :m] @ w
        if self.jumps and self.jumps["lam"] > 0 and len(self.jumps["sizes"]):
            lam = self.jumps["lam"] * np.r_[np.ones(m - 1), self.f_last]
            hit = self.U[:, :m] < lam                      # at most one jump per minute (lam << 1)
            sizes = self.jumps["sizes"][self.J[:, :m] % len(self.jumps["sizes"])]
            x = x + (hit * sizes * np.array(sd)).sum(axis=1)
        var = float(np.sum(w ** 2))
        return x - 0.5 * var

    def prob_yes(self, centre, sd, contracts):
        """P(YES) for each contract sharing this settlement, using the same simulated paths."""
        A = centre * np.exp(self.log_returns(sd))
        out = []
        for c in contracts:
            if c.kind == "at_least":
                out.append(float(np.mean(A >= c.k_lo)))
            elif c.kind == "above":
                out.append(float(np.mean(A > c.k_lo)))
            elif c.kind == "below":
                out.append(float(np.mean(A < c.k_lo)))
            else:
                out.append(float(np.mean((A >= c.k_lo) & (A <= c.k_hi))))
        return out, A

    def quantiles(self, centre, sd, qs=(0.05, 0.25, 0.5, 0.75, 0.95)):
        A = centre * np.exp(self.log_returns(sd))
        return np.quantile(A, qs)


def analytic_prob_above(centre, total_var, K, strict=True):
    """Gaussian benchmark: P(A > K) with log(A/C) ~ N(-v/2, v)."""
    from math import erf, log, sqrt
    sd = sqrt(max(total_var, 1e-18))
    z = (log(K / centre) + 0.5 * total_var) / sd
    return 0.5 * (1 - erf(z / sqrt(2)))
