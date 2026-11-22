"""Unit tests for the kbt research framework. Run: python3 -m unittest discover -s research/tests"""
import math, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from kbt import models as M, strategies as S, backtest as B, data as D


def row(**kw):
    r = dict(ticker="T", close=1_800_000_000, t=1_800_000_000 - 300, mins=5, y=1, K=60000.0, bid=0.40, ask=0.42,
             mid=0.41, spread=0.02, quote_age=30, fresh=True, S=60000.0, S_bs=float("nan"), basis=0.0,
             sigma=0.0007, rsi1=50.0, rsi5=50.0, mom5=0.0, mom15=0.0, dvol=float("nan"), hour=12, dow=2, dmid5=0.0)
    r.update(kw)
    return r


class Models(unittest.TestCase):
    def test_fee_matches_kalshi_schedule(self):
        self.assertEqual(M.fee_cents(1, 0.50), 2)      # $0.0175 -> 2c
        self.assertEqual(M.fee_cents(100, 0.50), 175)
        self.assertEqual(M.fee_cents(10, 0.10), 7)
        self.assertEqual(M.fee_cents(0, 0.5), 0)

    def test_digital_symmetry_and_limits(self):
        p = M.digital_yes(60000, 60000, 0.0007, 7)
        self.assertAlmostEqual(p, 0.5, delta=0.01)
        self.assertGreater(M.digital_yes(60300, 60000, 0.0007, 7), 0.8)
        self.assertLess(M.digital_yes(59700, 60000, 0.0007, 7), 0.2)
        # less time -> more certain
        self.assertGreater(M.digital_yes(60100, 60000, 0.0007, 2), M.digital_yes(60100, 60000, 0.0007, 12))

    def test_touch_at_least_flip(self):
        for S_ in (59900, 60050, 60200):
            for mins in (1, 5, 12):
                self.assertGreaterEqual(M.touch_prob(S_, 60000, 0.0007, mins) + 1e-12,
                                        M.flip_prob(S_, 60000, 0.0007, mins) * 0.99)

    def test_dvol_conversion(self):
        self.assertAlmostEqual(M.dvol_to_sigma_min(50), 0.5 / math.sqrt(525600), places=12)


class Indicators(unittest.TestCase):
    def test_rsi_matches_known_values(self):
        up = np.arange(50, dtype=float) + 100
        self.assertEqual(D.wilder_rsi(up, 14)[-1], 100.0)
        flat_then_down = np.concatenate([np.full(20, 100.0), 100 - np.arange(1, 21, dtype=float)])
        self.assertLess(D.wilder_rsi(flat_then_down, 14)[-1], 5)

    def test_ewma_sigma_is_causal(self):
        x = 60000 * np.exp(np.cumsum(np.r_[0, np.random.RandomState(1).normal(0, 0.001, 300)]))
        a = D.ewma_sigma(x, 60)
        y = x.copy(); y[200:] *= 1.05  # change the future
        b = D.ewma_sigma(y, 60)
        np.testing.assert_allclose(a[:200], b[:200])


class Strategies(unittest.TestCase):
    def test_rsi_entry_and_exit(self):
        s = S.RSIMomentum()
        self.assertEqual(s.entry(row(rsi1=65, rsi5=55)), "yes")
        self.assertEqual(s.entry(row(rsi1=35, rsi5=45)), "no")
        self.assertIsNone(s.entry(row(rsi1=65, rsi5=45)))          # 5m disagrees
        self.assertIsNone(s.entry(row(rsi1=65, rsi5=55, mins=12)))  # outside 3-10 min window
        self.assertIsNone(s.entry(row(rsi1=65, rsi5=55, spread=0.08)))
        self.assertTrue(s.exit({"side": "yes"}, row(rsi1=45)))

    def test_fair_value_edge_after_fees(self):
        s = S.FairValue(edge=0.04)
        # spot well above strike with 3 min left -> fair YES ~ 0.99; ask 0.60 -> buy YES
        self.assertEqual(s.entry(row(S=60300, mins=3, bid=0.58, ask=0.60, mid=0.59)), "yes")
        # fairly priced -> nothing
        self.assertIsNone(s.entry(row(S=60000, mins=7, bid=0.49, ask=0.51, mid=0.50)))

    def test_lag_requires_market_not_caught_up(self):
        s = S.MomentumLag(z=1.5)
        r = row(S=60200, mins=5, mom5=math.log(60200 / 60000), dmid5=0.0, bid=0.50, ask=0.52, mid=0.51)
        self.assertEqual(s.entry(r), "yes")
        r2 = dict(r, dmid5=0.40)  # market already moved
        self.assertIsNone(s.entry(r2))


class Backtester(unittest.TestCase):
    def window(self, y, path):
        return [row(mins=m, y=y, **kw) for m, kw in path]

    def test_settlement_and_fees(self):
        w = {"T": self.window(1, [(5, dict(rsi1=65, rsi5=55, bid=0.58, ask=0.60, mid=0.59))])}
        tr = B.run(w, S.RSIMomentum(use_exit=False), B.Costs(contracts=10))
        self.assertEqual(len(tr), 1)
        t = tr[0]
        self.assertEqual(t["how"], "settle")
        self.assertEqual(t["fee_in"], M.fee_cents(10, 0.60))
        self.assertAlmostEqual(t["pnl_c"], (1 - 0.60) * 1000 - t["fee_in"])

    def test_early_exit_at_bid_with_slippage(self):
        w = {"T": self.window(0, [(8, dict(rsi1=65, rsi5=55, bid=0.50, ask=0.52)), (6, dict(rsi1=40, bid=0.45, ask=0.47))])}
        tr = B.run(w, S.RSIMomentum(), B.Costs(contracts=10, slippage=0.01))
        t = tr[0]
        self.assertEqual(t["how"], "exit")
        self.assertAlmostEqual(t["entry_px"], 0.53)
        self.assertAlmostEqual(t["exit_px"], 0.44)
        self.assertAlmostEqual(t["pnl_c"], (0.44 - 0.53) * 1000 - M.fee_cents(10, 0.53) - M.fee_cents(10, 0.44))

    def test_stale_quotes_not_traded(self):
        w = {"T": self.window(1, [(5, dict(rsi1=65, rsi5=55, fresh=False))])}
        self.assertEqual(B.run(w, S.RSIMomentum(), B.Costs()), [])

    def test_metrics(self):
        trades = [dict(pnl_c=100, capital_c=500, close=0, entry_min=5, exit_min=0, how="settle"),
                  dict(pnl_c=-50, capital_c=500, close=86400, entry_min=5, exit_min=0, how="settle")]
        m = B.metrics(trades)
        self.assertEqual(m["n"], 2)
        self.assertAlmostEqual(m["net"], 0.5)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertAlmostEqual(m["max_dd"], 0.5)

    def test_split_is_chronological(self):
        days = list(range(20))
        tr, va, te = B.split_days(days)
        self.assertLess(max(tr), min(va)); self.assertLess(max(va), min(te))


if __name__ == "__main__":
    unittest.main()


class PaperTraderTests(unittest.TestCase):
    def test_parse_book_derives_yes_ask_from_no_bids(self):
        import paper_trader as PT
        b = PT.parse_book({"orderbook_fp": {"yes_dollars": [["0.60", "100"], ["0.61", "50"]], "no_dollars": [["0.37", "80"], ["0.38", "20"]]}})
        self.assertEqual((b["bid"], b["bid_size"]), (0.61, 50.0))
        self.assertAlmostEqual(b["ask"], 0.62); self.assertEqual(b["ask_size"], 20.0)

    def test_depth_check_and_settlement(self):
        import tempfile, paper_trader as PT
        with tempfile.TemporaryDirectory() as d:
            class Always(S.Strategy):
                name = "always"
                def entry(self, r): return "yes"
            pt = PT.PaperTrader(PT.Journal(d), [Always()], contracts=10, slippage=0.01)
            close = int(time.time()) - 120
            book_ok = dict(bid=0.50, bid_size=100, ask=0.52, ask_size=100, levels={})
            book_thin = dict(book_ok, ask_size=5)
            r = row(ticker="X", close=close, mins=5)
            pt.build_row = lambda t, m, book: (dict(r, bid=book["bid"], ask=book["ask"]), None)
            mkt = {"markets": [{"ticker": "X", "floor_strike": 60000, "close_time": datetime_iso(close + 600)}]}
            calls = {"book": book_thin}
            PT.try_get = lambda url, params=None: (mkt if url.endswith("/markets") else
                {"orderbook_fp": {"yes_dollars": [[str(calls["book"]["bid"]), str(calls["book"]["bid_size"])]],
                                  "no_dollars": [[str(round(1 - calls["book"]["ask"], 2)), str(calls["book"]["ask_size"])]]}}
                if "orderbook" in url else {"market": {"result": "yes"}})
            pt.step(close - 300)
            self.assertEqual(pt.pos, {})  # 5 contracts at the ask < 10 wanted: rejected
            calls["book"] = book_ok
            pt.step(close - 240)
            pos = list(pt.pos.values())[0]
            self.assertAlmostEqual(pos["entry_px"], 0.53)
            for p in pt.pos.values():
                p["close"] = close - 600  # force settlement window
            pt.settle()
            lines = [json.loads(l) for l in open(os.path.join(d, "trades.jsonl"))]
            closed = [l for l in lines if l["event"] == "close"][0]
            self.assertEqual(closed["how"], "settle")
            self.assertAlmostEqual(closed["pnl_c"], (1 - 0.53) * 1000 - M.fee_cents(10, 0.53))


import json, time, datetime
def datetime_iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
