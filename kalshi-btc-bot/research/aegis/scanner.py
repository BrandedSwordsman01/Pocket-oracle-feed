"""Cross-market dominance scanner (separate from the probability model).

For a set of legs (contract, side) that share EXACTLY the same settlement value, enumerate settlement states and compute
the minimum combined payout. States: every distinct region between the legs' thresholds, evaluated at points just
below/at/above each threshold (so strict vs inclusive boundaries are handled exactly), plus the official
"no data" state where KXBTC15M resolves NO while KXBTCD/KXBTC move to a later minute (we model that later value as unknown,
i.e. any state), which is why 15M+hourly pairs are NOT guaranteed even when thresholds overlap.

PAIR_FLOOR(Q) = Q * min_payout - sum(buy_cost of each leg) - reserve.  A signal only; the floor is locked only after
every leg is filled.
"""
import itertools
from . import settlement as SM


def leg_payout(contract, side, A):
    y = contract.yes(A)
    return y if side == "yes" else 1.0 - y


def states(legs, tick=0.001):
    pts = set()
    for c, _ in legs:
        for k in (c.k_lo, c.k_hi):
            if k == k:  # not NaN
                pts |= {k - tick, k, k + tick}
    lo, hi = min(pts) - 1000, max(pts) + 1000
    return sorted(pts | {lo, hi})


def min_payout(legs, include_outage=True):
    """Minimum total payout (per 1 contract of each leg) over all settlement states, incl. the data-outage state."""
    S = states(legs)
    normal = min(sum(leg_payout(c, s, A) for c, s in legs) for A in S)
    if not include_outage or len({c.series for c, _ in legs}) == 1:
        return normal
    # outage: KXBTC15M legs pay as if YES fails (resolve NO); other series settle at some unknown later value
    worst = float("inf")
    for A in S:
        tot = 0.0
        for c, s in legs:
            if c.series == "KXBTC15M":
                tot += 1.0 if s == "no" else 0.0
            else:
                tot += leg_payout(c, s, A)
        worst = min(worst, tot)
    return min(normal, worst)


def pair_floor(legs, prices, Q, reserve=0.0, include_outage=True, multipliers=None):
    """prices: ask price paid for each leg (dollars). Returns dict with floor, cost, min payout."""
    mp = min_payout(legs, include_outage)
    cost = sum(SM.buy_cost(Q, p, (multipliers or {}).get(c.series, 1.0)) for (c, _), p in zip(legs, prices))
    return dict(min_payout=mp, cost=cost, floor=Q * mp - cost - reserve, min_payout_no_outage=min_payout(legs, False))


def scan_shared(contracts, quotes, Q=10, reserve=0.0):
    """All 2-leg combinations among contracts sharing a settlement. quotes: {ticker: (yes_bid, yes_ask)}.
    Returns opportunities with floor > 0 (ignoring outage) and their floor including the outage state."""
    out = []
    for a, b in itertools.combinations(contracts, 2):
        for sa, sb in itertools.product(("yes", "no"), repeat=2):
            qa, qb = quotes.get(a.ticker), quotes.get(b.ticker)
            if not qa or not qb:
                continue
            pa = qa[1] if sa == "yes" else 1 - qa[0]
            pb = qb[1] if sb == "yes" else 1 - qb[0]
            # two legs on distinct thresholds can never both pay in every state, so min payout <= 1:
            # floor > 0 requires pa + pb < 1 (fees only add cost)
            if not (0 < pa < 1 and 0 < pb < 1) or pa + pb >= 1:
                continue
            legs = [(a, sa), (b, sb)]
            if min_payout(legs, include_outage=False) < 1:
                continue
            r = pair_floor(legs, [pa, pb], Q, reserve, include_outage=False)
            if r["floor"] > 0:
                r_out = pair_floor(legs, [pa, pb], Q, reserve, include_outage=True)
                out.append(dict(legs=[(a.ticker, sa, pa), (b.ticker, sb, pb)], floor=r["floor"], cost=r["cost"],
                                floor_with_outage=r_out["floor"], gross_edge=1 - pa - pb))
    return out


def simulate_pair_fill(available, Q):
    """available: contracts fillable per leg at the quoted prices (e.g. from depth or a fill model).
    Returns matched pairs (floor locked) and the unmatched, directional quantity per leg (NOT protected)."""
    matched = min(min(available), Q)
    unmatched = [min(a, Q) - matched for a in available]
    return dict(matched=matched, unmatched=unmatched, locked=matched > 0 and all(u == 0 for u in unmatched))
