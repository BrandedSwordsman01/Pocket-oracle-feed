"""Local AI forecaster: Amazon Chronos-Bolt (time-series foundation model) on your graphics card.

Every 5 s on a graphics card (30 s on CPU) it reads the last ~8 hours of 1-minute composite prices and forecasts price quantiles for each of the
next 60 minutes. The engine turns that into, per Kalshi contract, the AI's probability that price ends above the
strike, its median move and its spread. These become extra inputs to the learned model, which only goes live if
the whole package beats Kalshi's prices on unseen data. Optional: install with setup_ai.bat.
"""
import math, time, logging

log = logging.getLogger("ai")
QL = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
H = 60
MODEL_ID = "amazon/chronos-bolt-small"


class AI:
    def __init__(self):
        self.pipe = None
        self.status = "not installed (run setup_ai.bat)"
        self.device = None
        self.result = None      # dict(ts, spot, q=[[H values] per quantile level])
        self.tried = False

    def load(self):
        self.tried = True
        try:
            import torch
            from chronos import BaseChronosPipeline
        except Exception:
            self.status = "not installed (run setup_ai.bat)"
            return False
        try:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            kw = dict(device_map=self.device)
            if self.device == "cuda":
                kw["torch_dtype"] = torch.bfloat16
            self.pipe = BaseChronosPipeline.from_pretrained(MODEL_ID, **kw)
            self.status = f"running on {'graphics card' if self.device == 'cuda' else 'CPU'}"
            log.info("Chronos loaded on %s", self.device)
            return True
        except Exception as e:
            self.status = f"failed to load: {e}"[:200]
            return False

    def run(self, closes):
        """closes: 1-minute closes, oldest first."""
        if self.pipe is None or len(closes) < 60:
            return None
        import torch
        ctx = torch.tensor(closes[-512:], dtype=torch.float32)
        q, _ = self.pipe.predict_quantiles(ctx, prediction_length=H, quantile_levels=QL)
        q = q[0].float().cpu().numpy()          # [H, Q]
        self.result = dict(ts=time.time(), spot=float(closes[-1]), q=[[float(q[h, j]) for h in range(H)] for j in range(len(QL))])
        return self.result


def _ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def prob_above(res, strike, secs_left, now=None):
    """P(price at close > strike) from the AI quantiles; None if no fresh forecast covers this horizon."""
    if not res:
        return None
    now = now or time.time()
    if now - res["ts"] > 120:
        return None
    step = (secs_left + (now - res["ts"])) / 60.0
    if step > H + 0.5:
        return None
    h = min(max(int(round(step)) - 1, 0), H - 1)
    qs = [res["q"][j][h] for j in range(len(QL))]
    for j in range(1, len(qs)):                 # enforce monotone quantiles
        qs[j] = max(qs[j], qs[j - 1] + 1e-6)
    # piecewise-linear CDF inside q10..q90, normal tails fitted to the q10/q90 spread outside
    sd = max((qs[-1] - qs[0]) / 2.563, 1e-6)
    if strike <= qs[0]:
        cdf = 0.1 * _ncdf((strike - qs[0]) / sd) / 0.5
    elif strike >= qs[-1]:
        cdf = 1 - 0.1 * (1 - _ncdf((strike - qs[-1]) / sd)) / 0.5
    else:
        cdf = 0.5
        for j in range(1, len(qs)):
            if strike <= qs[j]:
                cdf = QL[j - 1] + (QL[j] - QL[j - 1]) * (strike - qs[j - 1]) / (qs[j] - qs[j - 1])
                break
    return dict(p=min(max(1 - cdf, 1e-3), 1 - 1e-3), med_bps=(qs[4] / res["spot"] - 1) * 1e4,
                width_bps=(qs[-1] - qs[0]) / res["spot"] * 1e4)
