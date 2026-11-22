"""Per-contract feature vector, computed from live state only (no future data).

Groups:
  price/vol   : composite returns over 5/30/60/300 s, realised per-second vol (60/300 s)
  order flow  : taker buy-vs-sell imbalance across exchanges over 30/120/300 s, traded volume rate
  cross-exch  : each exchange's deviation from the composite (who is leading), top-of-book size imbalance
  perp        : Kalshi BTC perp premium vs spot, funding estimate
  contract    : Kalshi mid/spread/book imbalance, time left, benchmark-adjusted distance to strike, baseline probability
"""
import math, time

FEATURES = ["logit_mid", "logit_base", "z", "spread", "kbook_imb", "mins_left",
            "r5", "r30", "r60", "r300", "vol60", "vol300", "ofi30", "ofi120", "ofi300", "vrate60",
            "dev_cb", "dev_kr", "dev_bs", "book_imb", "perp_prem", "funding", "hour_sin", "hour_cos"]


def _logit(p):
    p = min(max(p, 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


def _ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def returns(state, secs):
    h = state.comp_hist
    if len(h) < 2:
        return 0.0
    now_s, now_p = h[-1]
    for s, p in reversed(h):
        if now_s - s >= secs:
            return math.log(now_p / p)
    return 0.0


def realised_vol(state, secs):
    h = [p for s, p in state.comp_hist if s >= state.comp_hist[-1][0] - secs] if state.comp_hist else []
    if len(h) < 20:
        return None
    r = [math.log(h[i] / h[i - 1]) for i in range(1, len(h))]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / len(r))


def base_prob(centre, strike, secs_left, sigma_s, kind):
    """P(settlement average >= / > strike): zero drift, exact 60-sample averaging variance, plus proxy-error floor."""
    if secs_left <= 0:
        return (1.0, 6.0) if centre >= strike else (0.0, -6.0)
    full = max(secs_left - 60, 0)
    nf = min(secs_left, 60)
    var_avg = (nf * (nf + 1) * (2 * nf + 1) / 6) / 3600.0      # last-minute averaging (1 s samples)
    var = sigma_s ** 2 * (full + var_avg) + (2.0e-4) ** 2       # + ~2 bp composite-vs-BRTI error
    z = math.log(strike / centre) / math.sqrt(var)
    return 1 - _ncdf(z), -z


def build(state, k, anchor=None, now=None):
    """Feature dict for one Kalshi contract k (from state.kalshi). Returns None if data is missing or stale."""
    now = now or time.time()
    comp = state.composite()
    if not comp or not k or now - k.get("updated", 0) > 20:
        return None
    vol300 = realised_vol(state, 300) or 1.2e-4
    vol60 = realised_vol(state, 60) or vol300
    sigma = max(vol300, 3e-5)
    basis = 0.0
    if anchor and anchor[1]:
        basis = anchor[0] / anchor[1] - 1          # BRTI strike at open vs our composite at open
        if abs(basis) > 0.002:
            basis = 0.0
    centre = comp * (1 + basis)
    secs_left = k["close_ts"] - now
    pb, z = base_prob(centre, k["strike"], secs_left, sigma, k["kind"])
    mid = (k["bid"] + k["ask"]) / 2
    flows = {w: [b.flow(w) for b in state.books.values()] for w in (30, 120, 300)}
    def ofi(w):
        buy = sum(f[0] for f in flows[w]); sell = sum(f[1] for f in flows[w])
        return (buy - sell) / (buy + sell) if buy + sell > 0 else 0.0
    dev = {n: ((b.mid() / comp - 1) * 1e4 if b.mid() else 0.0) for n, b in state.books.items()}
    imbs = [(b.bid_sz - b.ask_sz) / (b.bid_sz + b.ask_sz) for b in state.books.values() if b.bid_sz and b.ask_sz]
    perp = state.perp if now - state.perp.get("updated", 0) < 60 else {}
    hour = time.gmtime(now).tm_hour
    f = dict(
        logit_mid=_logit(mid), logit_base=_logit(pb), z=max(-6, min(6, z)), spread=k["ask"] - k["bid"],
        kbook_imb=(k["bid_sz"] - k["ask_sz"]) / (k["bid_sz"] + k["ask_sz"]) if k.get("bid_sz") and k.get("ask_sz") else 0.0,
        mins_left=secs_left / 60, r5=returns(state, 5) * 1e4, r30=returns(state, 30) * 1e4, r60=returns(state, 60) * 1e4,
        r300=returns(state, 300) * 1e4, vol60=vol60 * 1e4, vol300=vol300 * 1e4,
        ofi30=ofi(30), ofi120=ofi(120), ofi300=ofi(300),
        vrate60=math.log1p(sum(f[0] + f[1] for f in [b.flow(60) for b in state.books.values()])),
        dev_cb=dev["coinbase"], dev_kr=dev["kraken"], dev_bs=dev["bitstamp"],
        book_imb=sum(imbs) / len(imbs) if imbs else 0.0,
        perp_prem=((perp["mid"] / comp - 1) * 1e4) if perp.get("mid") else 0.0, funding=perp.get("funding", 0.0) * 1e4,
        hour_sin=math.sin(2 * math.pi * hour / 24), hour_cos=math.cos(2 * math.pi * hour / 24),
    )
    return dict(feats=f, p_base=pb, mid=mid, centre=centre, secs_left=secs_left)
