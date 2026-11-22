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
            "dev_cb", "dev_kr", "dev_bs", "book_imb", "perp_prem", "funding", "hour_sin", "hour_cos",
            # longer history + Kalshi's own price momentum
            "r900", "r3600", "vol3600", "kmid_chg30", "kmid_chg120", "kmid_chg600", "dow_sin", "dow_cos", "min_of_hour",
            # futures / derivatives (missing = NaN when a venue is blocked)
            "fut_basis_bn", "fut_basis_by", "fut_basis_ok", "fut_basis_db", "fut_basis_hl", "fut_basis_kf", "fut_basis_avg", "fut_lead30", "fut_lead120", "fofi30", "fofi120", "fofi300",
            "liq_net300", "liq_tot300", "liq_net900", "fund_bn", "fund_by", "fund_ok", "fund_db", "fund_hl", "fund_kf", "oi_chg300", "oi_chg1800", "dvol", "dvol_chg",
            # local AI (Chronos) forecast
            "ai_logit", "ai_edge", "ai_med", "ai_width"]

NAN = float("nan")


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
    f.update(extra(state, k, comp, mid, pb, centre, secs_left, now))
    return dict(feats=f, p_base=pb, mid=mid, centre=centre, secs_left=secs_left)


def _minute_ret(mins, comp, secs):
    if not mins:
        return NAN
    target = mins[-1][0] - secs
    for t, p in reversed(mins):
        if t <= target:
            return math.log(comp / p) * 1e4
    return NAN


def _slog(x):
    return math.copysign(math.log1p(abs(x)), x)


def extra(state, k, comp, mid, pb, centre, secs_left, now):
    g = time.gmtime(now)
    mins = getattr(state, "minutes", None) or []
    out = dict(r900=_minute_ret(mins, comp, 900), r3600=_minute_ret(mins, comp, 3600), vol3600=NAN,
               dow_sin=math.sin(2 * math.pi * g.tm_wday / 7), dow_cos=math.cos(2 * math.pi * g.tm_wday / 7), min_of_hour=g.tm_min)
    last = [p for t, p in mins[-61:]]
    if len(last) > 20:
        r = [math.log(last[i] / last[i - 1]) for i in range(1, len(last))]
        out["vol3600"] = math.sqrt(sum(x * x for x in r) / len(r)) * 1e4
    hist = getattr(state, "kmid_hist", {}).get(k.get("ticker"))
    for w in (30, 120, 600):
        v = NAN
        if hist:
            for t, m in reversed(hist):
                if now - t >= w:
                    v = (mid - m) * 100; break
        out[f"kmid_chg{w}"] = v
    ext = getattr(state, "ext", None)
    d = ext.d if ext else {}
    def basis(n):
        m = d[n].mid() if n in d else None
        return (m / comp - 1) * 1e4 if m else NAN
    out.update(fut_basis_bn=basis("binance"), fut_basis_by=basis("bybit"), fut_basis_ok=basis("okx"),
               fut_basis_db=basis("deribit"), fut_basis_hl=basis("hyperliquid"), fut_basis_kf=basis("krakenf"))
    bs = [v for v in (basis(n) for n in d) if v == v]
    out["fut_basis_avg"] = sum(bs) / len(bs) if bs else NAN
    from .features import returns as _r
    for w in (30, 120):
        rs = [x for x in (d[n].ret(w) for n in d) if x is not None]
        out[f"fut_lead{w}"] = (sum(rs) / len(rs) - _r(state, w) * 1e4) if rs else NAN
    for w in (30, 120, 300):
        fl = [d[n].flow(w) for n in d if d[n].connected]
        b = sum(x[0] for x in fl); s_ = sum(x[1] for x in fl)
        out[f"fofi{w}"] = (b - s_) / (b + s_) if b + s_ > 0 else NAN
    for w, net_k, tot_k in ((300, "liq_net300", "liq_tot300"), (900, "liq_net900", None)):
        lq = [d[n].liq(w) for n in d if d[n].connected]
        if lq:
            up = sum(x[0] for x in lq); dn = sum(x[1] for x in lq)
            out[net_k] = _slog(up - dn)
            if tot_k: out[tot_k] = math.log1p(up + dn)
        else:
            out[net_k] = NAN
            if tot_k: out[tot_k] = NAN
    for n, key in (("binance", "fund_bn"), ("bybit", "fund_by"), ("okx", "fund_ok"), ("deribit", "fund_db"),
                   ("hyperliquid", "fund_hl"), ("krakenf", "fund_kf")):
        fv = d[n].funding if n in d else None
        out[key] = fv * 1e4 if fv is not None else NAN
    for w in (300, 1800):
        ch = [x for x in (d[n].oi_change(w) for n in d) if x is not None]
        out[f"oi_chg{w}"] = sum(ch) / len(ch) if ch else NAN
    out["dvol"] = ext.dvol if ext and ext.dvol else NAN
    out["dvol_chg"] = NAN
    if ext and len(ext.dvol_hist) > 1:
        t1, v1 = ext.dvol_hist[-1]
        for t, v in reversed(ext.dvol_hist):
            if t1 - t >= 1800:
                out["dvol_chg"] = v1 - v; break
    from . import ai as AI
    a = AI.prob_above(getattr(state, "ai_res", None), k["strike"] if k["kind"] == "above" else k["strike"] - 0.01, secs_left, now)
    if a:
        out.update(ai_logit=_logit(a["p"]), ai_edge=a["p"] - mid, ai_med=a["med_bps"], ai_width=a["width_bps"])
    else:
        out.update(ai_logit=NAN, ai_edge=NAN, ai_med=NAN, ai_width=NAN)
    return out
