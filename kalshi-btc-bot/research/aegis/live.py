"""Live AEGIS signal for the paper trader and the app: frozen parameters from research/aegis_results.json.

Produces, for each contract sharing a settlement: raw P(YES) (model E), calibrated, pooled with the market, conservative
per-side bounds, and an action (BUY_YES / BUY_NO / PASS / INSUFFICIENT_DATA) from EDGE_LOW = Q*p_low - cost - fees - L.
No order placement anywhere in this module.
"""
import json, math, os
import numpy as np
from . import settlement as SM, simulate as SIM, calibrate as CAL, volmodels as V

PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "aegis_results.json")


class LiveAegis:
    def __init__(self, params_path=PARAMS_PATH, n_paths=4000, theta=0.02, latency=0.01, Q=10):
        P = json.load(open(params_path))
        g = P["vol_models"]["garch"]
        self.garch = V.GarchT(); self.garch.w, self.garch.a, self.garch.b, self.garch.nu = g["w"], g["a"], g["b"], g["nu"]
        self.garch.uncond = g["w"] / (1 - g["a"] - g["b"])
        self.diur = np.array(P["vol_models"]["diurnal"])
        self.platt = CAL.Platt(); self.platt.a, self.platt.b = P["platt"]["E"]["a"], P["platt"]["E"]["b"]
        self.draws = P.get("platt_E_draws", [])
        self.omega = P["omega"]
        self.sim = SIM.Simulator(n_paths, 120, seed=24)  # jumps omitted live unless sizes are stored
        self.theta, self.lat, self.Q = theta, latency, Q

    def sd_forecast(self, times, closes, t_decision, minutes):
        r = np.r_[0.0, np.diff(np.log(np.asarray(closes, float)))]
        hrs = (np.asarray(times) // 3600) % 24
        h = self.garch.filter(r / np.sqrt(self.diur[hrs]))
        fut_h = ((t_decision + 60 * np.arange(minutes)) // 3600) % 24
        return np.sqrt(self.garch.forecast(h[-1], minutes) * self.diur[fut_h])

    def evaluate(self, centre, sd, contracts, quotes, fresh=True):
        """quotes: {ticker: (bid, ask, ask_size_yes, ask_size_no)}. Returns one decision dict per contract."""
        raw, A = self.sim.prob_yes(centre, sd, contracts)
        out = []
        for c, p in zip(contracts, raw):
            q = quotes.get(c.ticker)
            rec = dict(ticker=c.ticker, strike=c.k_lo, kind=c.kind, p_raw=p)
            if not q or not fresh or not (0 < q[0] < q[1] < 1):
                rec["action"] = "INSUFFICIENT_DATA"; out.append(rec); continue
            mid = (q[0] + q[1]) / 2
            pc = float(self.platt(np.array(p)))
            pf = float(CAL.log_pool(np.array(pc), np.array(mid), self.omega))
            if self.draws:
                ly, ln = CAL.bounds_from_params(self.draws, p)
                ly = float(CAL.log_pool(np.array(ly), np.array(mid), self.omega))
                ln = float(1 - CAL.log_pool(np.array(1 - ln), np.array(mid), self.omega))
            else:
                ly, ln = pf, 1 - pf
            ey = ly - SM.buy_cost(self.Q, q[1]) / self.Q - self.lat
            en = ln - SM.buy_cost(self.Q, 1 - q[0]) / self.Q - self.lat
            side, e = ("yes", ey) if ey >= en else ("no", en)
            size = q[2] if side == "yes" else q[3]
            action = "PASS"
            if e > self.theta and (size is None or size >= self.Q):
                action = "BUY_YES" if side == "yes" else "BUY_NO"
            rec.update(bid=q[0], ask=q[1], mid=mid, p_cal=pc, p_final=pf, p_low_yes=ly, p_low_no=ln,
                       edge_low_c=e * 100, side=side, action=action)
            out.append(rec)
        qs = np.quantile(A, [0.05, 0.25, 0.5, 0.75, 0.95])
        return out, qs
