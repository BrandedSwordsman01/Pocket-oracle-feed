"""Official settlement mechanics, payoffs, fees and the settlement-averaging variance.

Verified (Nov 22, 2026) from Kalshi contract terms CRYPTO.pdf (KXBTC15M) and BTC.pdf (KXBTCD/KXBTC), market rules text,
the series API, and docs.kalshi.com/getting_started/fee_rounding:

  KXBTC15M  YES iff A_T >= A_open ("at least"); A = simple average of BRTI over the 60 seconds before the time.
            Strike K = A_open (API floor_strike). Minimum tick $0.001. No/incomplete data at expiry -> resolves NO.
  KXBTCD    YES iff A_T > K ("above", strictly). Strikes listed as N,NNN.99. Tick $0.01.
            No data -> expiration moves to the first later minute with data (NOT the same as 15M).
  KXBTC     YES iff K_lo <= A_T <= K_hi ("between", inclusive both ends).
  Fees      fee_type quadratic, fee_multiplier 1 for all three series (series API). Taker coefficient 0.07, maker 0.0175
            (from the brief; kalshi.com fee page was not machine-readable here). Trade fee is rounded up to $0.000001,
            then the balance change is floored to the member's precision ($0.01 for non-direct members) via a rounding fee.

BRTI sampling: BRTI is published once per second; the contract text says "simple average ... for the 60 seconds prior".
We model the window as the 60 one-second samples at T-59, ..., T (configurable); the exact CF sample selection is not public
in the documents we could access, so this is an explicit assumption (difference vs T-60..T-1 is one sample).
"""
import math
from dataclasses import dataclass

TAKER_COEF = 0.07
MAKER_COEF = 0.0175
WINDOW = 60  # one-second BRTI samples in the settlement average


# ------------------------------------------------------------------------------------------- payoffs
@dataclass(frozen=True)
class Contract:
    ticker: str
    series: str          # KXBTC15M | KXBTCD | KXBTC
    kind: str            # 'at_least' | 'above' | 'below' | 'between'
    k_lo: float
    k_hi: float = float("nan")
    close_ts: int = 0
    tick: float = 0.01
    fee_multiplier: float = 1.0

    def yes(self, A):
        """Exact YES payout (1 or 0) for settlement average A."""
        if self.kind == "at_least":
            return 1.0 if A >= self.k_lo else 0.0
        if self.kind == "above":
            return 1.0 if A > self.k_lo else 0.0
        if self.kind == "below":
            return 1.0 if A < self.k_lo else 0.0
        if self.kind == "between":
            return 1.0 if self.k_lo <= A <= self.k_hi else 0.0
        raise ValueError(self.kind)


def contract_from_market(m, series):
    st = (m.get("strike_type") or "").lower()
    lo = m.get("floor_strike"); hi = m.get("cap_strike")
    kind = {"greater_or_equal": "at_least", "greater": "above", "less": "below", "less_or_equal": "below",
            "between": "between"}.get(st, st)
    if kind == "below" and lo is None:
        lo = hi
    tick = 0.001 if series == "KXBTC15M" else 0.01
    return Contract(m["ticker"], series, kind, float(lo), float(hi) if (kind == "between" and hi is not None) else float("nan"),
                    tick=tick)


# ------------------------------------------------------------------------------------------- fees
def _ceil(x, q):
    return math.ceil(round(x / q, 9)) * q


def taker_fee(Q, P, multiplier=1.0, coef=TAKER_COEF):
    """Model fee in dollars, rounded up to $0.000001 (trade fee)."""
    return _ceil(multiplier * coef * Q * P * (1 - P), 1e-6)


def buy_cost(Q, P, multiplier=1.0, coef=TAKER_COEF, precision=0.01):
    """Total cash debited for buying Q contracts at price P (dollars), including the rounding fee.

    revenue = -Q*P; aligned change = floor_precision(revenue - fee) -> cost = ceil_precision(Q*P + fee)."""
    return _ceil(Q * P + taker_fee(Q, P, multiplier, coef), precision)


def fee_total(Q, P, multiplier=1.0, coef=TAKER_COEF, precision=0.01):
    """Trade fee + rounding fee, i.e. everything paid beyond Q*P."""
    return buy_cost(Q, P, multiplier, coef, precision) - Q * P


def book_cost(levels, Q):
    """Executable cost of buying Q from ask levels [(price, size), ...] best first. Returns (cost, filled, avg_price)."""
    cost, got = 0.0, 0.0
    for px, sz in levels:
        take = min(sz, Q - got)
        if take <= 0:
            break
        cost += take * px; got += take
    return cost, got, (cost / got if got else float("nan"))


# ------------------------------------------------------------------------------------------- averaging variance
def continuous_avg_variance(sigma, tau):
    """Var(A_T | F_t) for arithmetic BM dI = mu dt + sigma dW, continuous 60s averaging. sigma in $/sqrt(s), tau in s."""
    if tau <= 0:
        return 0.0
    if tau >= 60:
        return sigma ** 2 * (tau - 40.0)
    return sigma ** 2 * tau ** 3 / 10800.0


def continuous_avg_mean(I_t, tau, mu=0.0, observed_integral=0.0):
    """E[A_T | F_t] for tau < 60: (integral of observed values + tau*I_t + 0.5*mu*tau^2) / 60; for tau >= 60: I_t + mu*(tau-30)."""
    if tau >= 60:
        return I_t + mu * (tau - 30.0)
    return (observed_integral + tau * I_t + 0.5 * mu * tau ** 2) / 60.0


def sample_offsets(tau, window=WINDOW):
    """Future one-second sample offsets d_k = s_k - t (> 0) for samples s_k at T-window+1..T; tau = T - t seconds."""
    return [tau - (window - k) for k in range(1, window + 1) if tau - (window - k) > 0]


def discrete_avg_moments(I_t, tau, sigma, mu=0.0, observed=(), window=WINDOW):
    """Exact mean/variance of the discrete 60-sample average under arithmetic BM.

    observed: samples already printed in the window (fixed, no variance). Future sample X_k = I_t + mu d_k + sigma W(d_k),
    Cov(X_j, X_k) = sigma^2 min(d_j, d_k)."""
    d = sample_offsets(tau, window)
    n_obs = window - len(d)
    obs = list(observed)[:n_obs]
    if len(obs) != n_obs:
        raise ValueError(f"need {n_obs} observed samples, got {len(obs)}")
    mean = (sum(obs) + sum(I_t + mu * x for x in d)) / window
    s = 0.0
    for a in d:
        for b in d:
            s += min(a, b)
    return mean, sigma ** 2 * s / window ** 2


def last_minute_factor(window=WINDOW):
    """Variance of the final-window average relative to one full minute of variance, discrete 1s sampling starting at T-60.
    = sum_{j,k=1..60} min(j,k) / 60^2 / 60 ; continuous limit 1/3."""
    s = sum(min(j, k) for j in range(1, window + 1) for k in range(1, window + 1))
    return s / window ** 2 / 60.0
