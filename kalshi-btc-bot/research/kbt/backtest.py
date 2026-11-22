"""Execution-aware backtester for KXBTC15M strategies.

Execution model (conservative; no historical depth is available):
  - entries pay the ask (YES) or 1 - bid (NO), plus `slippage` per contract
  - early exits receive the bid (YES) or 1 - ask (NO), minus `slippage`
  - Kalshi taker fee on every order: ceil(0.07 * C * P * (1-P)) dollars
  - fixed size `contracts` per trade (assumed available at the touch; see docs for the depth caveat)
  - `fill_prob` < 1 randomly drops entries (deterministic seed) to model missed fills
  - stale quotes (older than the data loader's max age) are never traded
  - at most one position per window; held to settlement unless the strategy exits
"""
import math, random, collections
import numpy as np
from . import models as M


class Costs:
    def __init__(self, contracts=10, slippage=0.0, fill_prob=1.0, seed=7, delay_rows=0):
        self.contracts, self.slippage, self.fill_prob = contracts, slippage, fill_prob
        self.delay_rows = delay_rows  # latency: fill at the quote this many decision minutes after the signal
        self.rng = random.Random(seed)


def run(windows, strategy, costs=None, tickers=None):
    """windows: {ticker: rows (14 -> 1 min)}. Returns list of trade dicts (chronological)."""
    costs = costs or Costs()
    C = costs.contracts
    trades = []
    keys = sorted(tickers if tickers is not None else windows.keys(), key=lambda k: windows[k][0]["close"])
    for tick in keys:
        rows = windows[tick]
        pos = None
        for k, r in enumerate(rows):
            if pos is None:
                side = strategy.entry(r)
                if not side:
                    continue
                if costs.delay_rows:
                    if k + costs.delay_rows >= len(rows) or not rows[k + costs.delay_rows]["fresh"]:
                        continue
                    r = rows[k + costs.delay_rows]
                    pending_skip = costs.delay_rows
                else:
                    pending_skip = 0
                if costs.fill_prob < 1 and costs.rng.random() > costs.fill_prob:
                    continue
                px = (r["ask"] if side == "yes" else 1 - r["bid"]) + costs.slippage
                if px >= 1:
                    continue
                pos = dict(ticker=tick, side=side, entry_px=px, entry_min=r["mins"], entry_t=r["t"],
                           fee_in=M.fee_cents(C, px), close=r["close"], y=r["y"],
                           sigma=r["sigma"], hour=r["hour"], skip=pending_skip)
                continue
            if pos.get("skip"):
                pos["skip"] -= 1
                continue
            if strategy.exit(pos, r) and r["fresh"]:
                bid = (r["bid"] if pos["side"] == "yes" else 1 - r["ask"]) - costs.slippage
                if bid > 0:
                    pos.update(exit_px=bid, exit_min=r["mins"], fee_out=M.fee_cents(C, bid), how="exit")
                    break
        if pos is None:
            continue
        if "exit_px" not in pos:
            win = (pos["y"] == 1) == (pos["side"] == "yes")
            pos.update(exit_px=1.0 if win else 0.0, exit_min=0, fee_out=0, how="settle")
        gross = (pos["exit_px"] - pos["entry_px"]) * C * 100  # cents
        pos["pnl_c"] = gross - pos["fee_in"] - pos["fee_out"]
        pos["capital_c"] = pos["entry_px"] * C * 100 + pos["fee_in"]
        trades.append(pos)
    return trades


def metrics(trades, contracts=10):
    n = len(trades)
    if n == 0:
        return dict(n=0)
    pnl = np.array([t["pnl_c"] for t in trades]) / 100.0  # dollars
    per_c = pnl / contracts * 100  # cents per contract
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    cum = np.cumsum(pnl)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], cum])) - np.concatenate([[0], cum])))
    se = per_c.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
    cap = np.array([t["capital_c"] for t in trades]) / 100.0
    days = len({int(t["close"] // 86400) for t in trades})
    daily = collections.defaultdict(float)
    for t in trades:
        daily[int(t["close"] // 86400)] += t["pnl_c"] / 100
    return dict(
        n=n, net=float(pnl.sum()), per_contract_c=float(per_c.mean()), t=float(per_c.mean() / se) if se and se > 0 else float("nan"),
        win_rate=float((pnl > 0).mean()), avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        profit_factor=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf"),
        max_dd=dd, roc=float(pnl.sum() / cap.mean()) if cap.mean() > 0 else float("nan"),
        avg_hold_min=float(np.mean([t["entry_min"] - t["exit_min"] for t in trades])),
        early_exits=float(np.mean([t["how"] == "exit" for t in trades])),
        green_days=f"{sum(1 for v in daily.values() if v > 0)}/{len(daily)}", trade_days=days,
    )


def brier_report(rows, prob_fn, lo=0.05, hi=0.95):
    """Brier + log loss on tradeable rows (market mid within [lo, hi]) for a probability function vs the market mid."""
    xs = [r for r in rows if r["fresh"] and lo <= r["mid"] <= hi]
    if not xs:
        return {}
    y = np.array([r["y"] for r in xs], float)
    pm = np.array([prob_fn(r) for r in xs]); pk = np.array([r["mid"] for r in xs])
    clip = lambda p: np.clip(p, 1e-4, 1 - 1e-4)
    ll = lambda p: float(-(y * np.log(clip(p)) + (1 - y) * np.log(1 - clip(p))).mean())
    return dict(n=len(xs), brier_model=float(((pm - y) ** 2).mean()), brier_market=float(((pk - y) ** 2).mean()),
                ll_model=ll(pm), ll_market=ll(pk))


def calibration_table(rows, prob_fn, bins=10):
    xs = [r for r in rows if r["fresh"]]
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [r for r in xs if lo <= prob_fn(r) < hi or (b == bins - 1 and prob_fn(r) == 1)]
        if sel:
            out.append((lo, hi, len(sel), float(np.mean([prob_fn(r) for r in sel])), float(np.mean([r["y"] for r in sel]))))
    return out


def by_bucket(trades, key):
    groups = collections.defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {k: metrics(v) for k, v in sorted(groups.items())}


def split_days(days, train=0.5, val=0.25):
    n = len(days)
    a, b = int(n * train), int(n * (train + val))
    return set(days[:a]), set(days[a:b]), set(days[b:])
