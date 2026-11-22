"""Market-implied settlement distribution from a ladder of contracts sharing one settlement value.

Unknown: bucket masses q_b >= 0, sum q = 1 over a grid of price intervals. Each quoted contract i defines a linear
functional P_i(q) = sum_b q_b * g_i(bucket b). The fit minimises the squared distance of P_i(q) to its executable quote
band [bid_i, ask_i] (zero inside the band), weighted by quote quality, plus a smoothness penalty on q:

    min_q  sum_i w_i * dist(P_i(q), [bid_i, ask_i])^2 + lam * ||D2 q||^2   s.t. q >= 0, 1'q = 1

Quotes are treated as noisy bounds, not exact probabilities; one-sided (zero-bid) quotes give a one-sided constraint.
Bucket edges are placed exactly at every strike so threshold contracts have no bucket straddling their boundary.
Solved with SLSQP (scipy). Survival probabilities derived from q are monotone by construction.
"""
import numpy as np
from scipy import optimize


def bucket_edges(strikes, spot, width_pct=0.025, n_fill=16):
    lo, hi = spot * (1 - width_pct), spot * (1 + width_pct)
    edges = set(np.round(np.linspace(lo, hi, n_fill), 2)) | {round(k, 2) for k in strikes if lo < k < hi}
    return np.array(sorted(edges))


def contract_matrix(contracts, edges):
    """G[i, b] = payout of contract i if settlement lies in bucket b (buckets: (-inf,e0], (e0,e1], ..., (e_last, inf)).
    For 'above K' with K on an edge, bucket (K, next] pays; for 'at_least K' we treat the edge as K - 0.005 (cent grid)."""
    mids = np.r_[edges[0] - 1, (edges[:-1] + edges[1:]) / 2, edges[-1] + 1]
    G = np.zeros((len(contracts), len(mids)))
    for i, c in enumerate(contracts):
        for b, x in enumerate(mids):
            G[i, b] = c.yes(x)
    return G


def fit(contracts, bids, asks, spot, weights=None, lam=1e-3, edges=None):
    edges = edges if edges is not None else bucket_edges([c.k_lo for c in contracts], spot)
    G = contract_matrix(contracts, edges)
    nb = G.shape[1]
    bids = np.asarray(bids, float); asks = np.asarray(asks, float)
    w = np.ones(len(contracts)) if weights is None else np.asarray(weights, float)
    D2 = np.diff(np.eye(nb), 2, axis=0)

    def obj(q):
        p = G @ q
        under = np.clip(bids - p, 0, None); over = np.clip(p - asks, 0, None)
        return float(np.sum(w * (under ** 2 + over ** 2)) + lam * np.sum((D2 @ q) ** 2))

    DtD = D2.T @ D2

    def jac(q):
        p = G @ q
        under = np.clip(bids - p, 0, None); over = np.clip(p - asks, 0, None)
        return G.T @ (2 * w * (over - under)) + 2 * lam * DtD @ q

    q0 = np.full(nb, 1 / nb)
    res = optimize.minimize(obj, q0, jac=jac, method="SLSQP", bounds=[(0, 1)] * nb,
                            constraints=[dict(type="eq", fun=lambda q: q.sum() - 1)], options=dict(maxiter=300, ftol=1e-12))
    q = np.clip(res.x, 0, None); q /= q.sum()
    return dict(q=q, edges=edges, G=G, fit_error=float(obj(q)), ok=bool(res.success))


def prob_of(contract, fitres):
    g = contract_matrix([contract], fitres["edges"])[0]
    return float(g @ fitres["q"])
