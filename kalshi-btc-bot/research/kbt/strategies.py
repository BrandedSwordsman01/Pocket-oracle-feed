"""Strategies A-E for KXBTC15M. Each strategy decides on one feature row at a time (causal).

Interface:
  fit(train_rows)          optional; learns parameters from training data only
  entry(r) -> 'yes'|'no'|None
  exit(pos, r) -> bool     early exit at the bid (otherwise held to settlement)
  prob(r) -> float|None    model P(YES), for Brier/calibration (None if the strategy has no probability)
"""
import math
import numpy as np
from . import models as M


def tradeable(r, p):
    return r["fresh"] and r["spread"] <= p.get("max_spread", 0.05)


def side_price(r, side):
    return r["ask"] if side == "yes" else 1 - r["bid"]


def in_price_band(r, side, p):
    px = side_price(r, side)
    return p.get("min_price", 0.05) <= px <= p.get("max_price", 0.95)


class Strategy:
    name = "base"
    defaults = {}

    def __init__(self, **params):
        self.p = dict(self.defaults); self.p.update(params)

    def fit(self, rows):
        return self

    def entry(self, r):
        return None

    def exit(self, pos, r):
        return False

    def prob(self, r):
        return None

    def label(self):
        keys = sorted(k for k in self.p if self.p[k] != self.defaults.get(k))
        return self.name + ("(" + ",".join(f"{k}={self.p[k]}" for k in keys) + ")" if keys else "")


def sigma_choice(r, src):
    rv = max(r["sigma"], 0.0002)
    if src == "realized" or not (r.get("dvol") == r.get("dvol")):  # NaN check
        return rv
    iv = M.dvol_to_sigma_min(r["dvol"])
    return iv if src == "implied" else 0.5 * (rv + iv)


def spot_benchmark(r, adjust):
    """Coinbase spot, optionally shifted by the Coinbase->BRTI basis measured at window open."""
    if adjust and r["basis"] == r["basis"]:
        return r["S"] * (1 + r["basis"])
    return r["S"]


# ---------------------------------------------------------------- A: RSI momentum
class RSIMomentum(Strategy):
    """Reported approach: RSI(14) 1m > hi -> YES, < lo -> NO, 5m RSI confirms, 3-10 min left; exit on momentum reversal."""
    name = "A_rsi"
    defaults = dict(hi=60, lo=40, confirm5=True, min_left=3, max_left=10, min_price=0.05, max_price=0.95,
                    max_spread=0.05, exit_rsi=50, use_exit=True)

    def entry(self, r):
        p = self.p
        if not (p["min_left"] <= r["mins"] <= p["max_left"]) or not tradeable(r, p) or r["rsi1"] != r["rsi1"]:
            return None
        side = "yes" if r["rsi1"] > p["hi"] else "no" if r["rsi1"] < p["lo"] else None
        if side and p["confirm5"]:
            if r["rsi5"] != r["rsi5"] or (side == "yes" and r["rsi5"] <= 50) or (side == "no" and r["rsi5"] >= 50):
                side = None
        return side if side and in_price_band(r, side, p) else None

    def exit(self, pos, r):
        if not self.p["use_exit"] or r["rsi1"] != r["rsi1"]:
            return False
        return r["rsi1"] < self.p["exit_rsi"] if pos["side"] == "yes" else r["rsi1"] > 100 - self.p["exit_rsi"]


# ---------------------------------------------------------------- B: momentum / market lag
class MomentumLag(Strategy):
    """Spot moved hard (|z| over 5 min) but the Kalshi mid has not moved as much as fair value implies -> trade the lag."""
    name = "B_lag"
    defaults = dict(z=1.5, lag_frac=0.5, min_left=2, max_left=12, min_price=0.05, max_price=0.95, max_spread=0.05,
                    benchmark_adjust=True, use_exit=True)

    def entry(self, r):
        p = self.p
        if not (p["min_left"] <= r["mins"] <= p["max_left"]) or not tradeable(r, p):
            return None
        if r["mom5"] != r["mom5"] or r["dmid5"] != r["dmid5"]:
            return None
        sig = max(r["sigma"], 0.0002)
        z = r["mom5"] / (sig * math.sqrt(5))
        if abs(z) < p["z"]:
            return None
        S = spot_benchmark(r, p["benchmark_adjust"])
        fair_now = M.digital_yes(S, r["K"], sig, r["mins"])
        S_then = S / math.exp(r["mom5"])
        fair_then = M.digital_yes(S_then, r["K"], sig, r["mins"] + 5)
        dfair = fair_now - fair_then
        if abs(dfair) < 0.03:
            return None
        caught_up = r["dmid5"] / dfair  # fraction of the fair-value move the market already priced
        if caught_up > p["lag_frac"]:
            return None
        side = "yes" if z > 0 else "no"
        return side if in_price_band(r, side, p) else None

    def exit(self, pos, r):
        if not self.p["use_exit"] or r["mom5"] != r["mom5"]:
            return False
        return (r["mom5"] < 0) if pos["side"] == "yes" else (r["mom5"] > 0)


# ---------------------------------------------------------------- C: binary-option fair value
class FairValue(Strategy):
    """Digital-option fair value vs executable price; trade when edge after fees exceeds a threshold."""
    name = "C_fair"
    defaults = dict(edge=0.04, vol="realized", vol_mult=1.0, dist="normal", benchmark_adjust=True,
                    min_left=2, max_left=14, min_price=0.05, max_price=0.95, max_spread=0.05, use_exit=False,
                    flip_max=None)

    def prob(self, r):
        sig = sigma_choice(r, self.p["vol"]) * self.p["vol_mult"]
        return M.digital_yes(spot_benchmark(r, self.p["benchmark_adjust"]), r["K"], sig, r["mins"], self.p["dist"])

    def edges(self, r):
        p = self.prob(r)
        ya, na = r["ask"], 1 - r["bid"]
        return p, p - ya - M.fee_cents(10, ya) / 1000, (1 - p) - na - M.fee_cents(10, na) / 1000

    def entry(self, r):
        p = self.p
        if not (p["min_left"] <= r["mins"] <= p["max_left"]) or not tradeable(r, p):
            return None
        prob, ey, en = self.edges(r)
        side, e = ("yes", ey) if ey >= en else ("no", en)
        if e < p["edge"] or not in_price_band(r, side, p):
            return None
        if p["flip_max"] is not None:  # D as a risk filter
            sig = sigma_choice(r, p["vol"]) * p["vol_mult"]
            S = spot_benchmark(r, p["benchmark_adjust"])
            favoured = (side == "yes") == (S >= r["K"])
            if favoured and M.flip_prob(S, r["K"], sig, r["mins"]) > p["flip_max"]:
                return None
        return side

    def exit(self, pos, r):
        if not self.p["use_exit"]:
            return False
        prob, ey, en = self.edges(r)
        fair = prob if pos["side"] == "yes" else 1 - prob
        bid = r["bid"] if pos["side"] == "yes" else 1 - r["ask"]
        return bid > fair  # market now pays more than fair value: take it


# ---------------------------------------------------------------- D: flip / barrier risk (exit manager on top of a base)
class FlipGuard(Strategy):
    """Wraps a base strategy; exits when the probability of the position flipping against us exceeds a limit."""
    name = "D_flip"
    defaults = dict(flip_exit=0.5)

    def __init__(self, base, **params):
        super().__init__(**params); self.base = base
        self.name = "D_flip+" + base.name

    def fit(self, rows):
        self.base.fit(rows); return self

    def entry(self, r):
        return self.base.entry(r)

    def prob(self, r):
        return self.base.prob(r)

    def exit(self, pos, r):
        if self.base.exit(pos, r):
            return True
        sig = max(r["sigma"], 0.0002)
        S = spot_benchmark(r, True)
        p_yes = M.digital_yes(S, r["K"], sig, r["mins"])
        p_win = p_yes if pos["side"] == "yes" else 1 - p_yes
        return (1 - p_win) > self.p["flip_exit"]


# ---------------------------------------------------------------- E: ensemble (interpretable logistic model)
FEATURES = ["logit_mid", "logit_fair", "rsi_c", "z5", "iv_rv", "flip", "spread", "mins_c", "hour_sin", "hour_cos"]


def feature_vector(r, groups=None):
    sig = max(r["sigma"], 0.0002)
    S = spot_benchmark(r, True)
    fair = M.digital_yes(S, r["K"], sig, r["mins"])
    z5 = r["mom5"] / (sig * math.sqrt(5)) if r["mom5"] == r["mom5"] else 0.0
    iv = M.dvol_to_sigma_min(r["dvol"]) if r["dvol"] == r["dvol"] else sig
    f = dict(
        logit_mid=M.logit(r["mid"]), logit_fair=M.logit(fair),
        rsi_c=((r["rsi1"] - 50) / 50) if r["rsi1"] == r["rsi1"] else 0.0,
        z5=max(-4, min(4, z5)), iv_rv=math.log(iv / sig), flip=M.flip_prob(S, r["K"], sig, r["mins"]),
        spread=r["spread"], mins_c=(r["mins"] - 7) / 7,
        hour_sin=math.sin(2 * math.pi * r["hour"] / 24), hour_cos=math.cos(2 * math.pi * r["hour"] / 24),
    )
    names = [k for k in FEATURES if groups is None or k in groups]
    return [1.0] + [f[k] for k in names]


class Ensemble(Strategy):
    """Logistic regression on interpretable features (always includes the market mid as anchor), L2-regularised."""
    name = "E_ensemble"
    defaults = dict(edge=0.03, l2=5.0, min_left=2, max_left=14, min_price=0.05, max_price=0.95, max_spread=0.05,
                    features=None)

    def fit(self, rows):
        groups = self.p["features"]
        rows = [r for r in rows if r["fresh"] and 0.03 <= r["mid"] <= 0.97]
        X = np.array([feature_vector(r, groups) for r in rows]); y = np.array([r["y"] for r in rows], float)
        w = np.zeros(X.shape[1]); I = np.eye(len(w)); I[0, 0] = 0
        for _ in range(60):
            pr = 1 / (1 + np.exp(-X @ w))
            H = X.T @ (X * (pr * (1 - pr))[:, None]) + self.p["l2"] * I
            w -= np.linalg.solve(H, X.T @ (pr - y) + self.p["l2"] * I @ w)
        self.w = w
        self.names = ["bias"] + [k for k in FEATURES if groups is None or k in groups]
        return self

    def prob(self, r):
        return 1 / (1 + math.exp(-float(np.dot(self.w, feature_vector(r, self.p["features"])))))

    def entry(self, r):
        p = self.p
        if not (p["min_left"] <= r["mins"] <= p["max_left"]) or not tradeable(r, p):
            return None
        q = self.prob(r)
        ya, na = r["ask"], 1 - r["bid"]
        ey = q - ya - M.fee_cents(10, ya) / 1000
        en = (1 - q) - na - M.fee_cents(10, na) / 1000
        side, e = ("yes", ey) if ey >= en else ("no", en)
        return side if e >= p["edge"] and in_price_band(r, side, p) else None


class Blend(Ensemble):
    """The app's existing market-anchored blend (logit mid + logit vol fair value), refit on each training fold."""
    name = "base_blend"
    defaults = dict(Ensemble.defaults, features=["logit_mid", "logit_fair"], edge=0.02, l2=1.0)


# ---------------------------------------------------------------- baselines
class MarketOnly(Strategy):
    """Probability = market mid. Never trades (benchmark for Brier)."""
    name = "base_market"

    def prob(self, r):
        return r["mid"]


class LeadingSide(Strategy):
    """Popular heuristic: buy whichever side is >= 50c at a fixed minute."""
    name = "base_leading"
    defaults = dict(at_min=5, max_spread=0.05, min_price=0.05, max_price=0.95)

    def entry(self, r):
        if r["mins"] != self.p["at_min"] or not tradeable(r, self.p):
            return None
        side = "yes" if r["mid"] >= 0.5 else "no"
        return side if in_price_band(r, side, self.p) else None
