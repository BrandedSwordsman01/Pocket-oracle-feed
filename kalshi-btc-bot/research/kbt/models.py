"""Probability models and costs for 15-minute BTC binaries.

Contract: YES pays $1 if BRTI_avg(last 60s before close) >= K, where K = BRTI_avg(60s before open).
"""
import math

MIN_PER_YEAR = 525600.0


def ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def t4cdf(z):
    """Student-t(4) CDF rescaled to unit variance."""
    t = z * math.sqrt(2); s = t * t
    return 0.5 + 0.75 * (t / math.sqrt(4 + s)) * (1 - s / (3 * (4 + s)))


def effective_minutes(mins):
    """Remaining variance of a 60s-average settlement: full minutes until the averaging window, plus 1/3 of it."""
    return max(mins - 1 + 1 / 3, 0.05)


def dvol_to_sigma_min(dvol_pct):
    """Deribit DVOL (annualised %, 30-day) -> per-minute volatility."""
    return dvol_pct / 100.0 / math.sqrt(MIN_PER_YEAR)


def digital_yes(S, K, sigma_min, mins, dist="normal", drift_min=0.0):
    """Black-76 style digital: P(S_T >= K) with zero (or given) drift over the effective horizon."""
    tau = effective_minutes(mins)
    sd = max(sigma_min, 1e-6) * math.sqrt(tau)
    z = (math.log(K / S) - drift_min * tau + 0.5 * sd * sd) / sd
    return 1 - (t4cdf(z) if dist == "t4" else ncdf(z))


def touch_prob(S, K, sigma_min, mins):
    """Reflection principle: P(price touches K before expiry), driftless log-price."""
    tau = max(mins, 0.05)
    sd = max(sigma_min, 1e-6) * math.sqrt(tau)
    return min(1.0, 2 * (1 - ncdf(abs(math.log(S / K)) / sd)))


def flip_prob(S, K, sigma_min, mins, dist="normal"):
    """P(finishing on the other side of K from where price is now)."""
    p_yes = digital_yes(S, K, sigma_min, mins, dist)
    return p_yes if S < K else 1 - p_yes


def adverse_move_prob(sigma_min, mins, move):
    """P(|log return| over the remaining time exceeds `move`)."""
    sd = max(sigma_min, 1e-6) * math.sqrt(max(mins, 0.05))
    return 2 * (1 - ncdf(abs(move) / sd))


def fee_cents(count, price):
    """Kalshi taker fee for one order: ceil(0.07 * C * P * (1 - P)) dollars, in cents. price in [0, 1]."""
    if count <= 0:
        return 0
    cents = round(price * 100)
    n = 7 * count * cents * (100 - cents)
    return (n + 9999) // 10000


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))
