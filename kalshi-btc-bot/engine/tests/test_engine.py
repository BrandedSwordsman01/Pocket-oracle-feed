"""Engine unit tests (no network). Run: python -m unittest discover -s engine/tests"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kbengine import feeds, features, model as M
from kbengine.db import DB


def fake_state(price=60000.0):
    st = feeds.State()
    now = time.time()
    for i, (n, b) in enumerate(st.books.items()):
        b.bid, b.ask, b.bid_sz, b.ask_sz, b.updated = price - 1 + i, price + 1 + i, 2.0, 1.0, now
        for k in range(49, -1, -1):          # chronological, like the live feeds
            b.trades.append((now - k, 0.1 if k % 3 else -0.05))
    s0 = int(now) - 400
    for k in range(400):
        st.comp_hist.append((s0 + k, price * (1 + 0.0001 * ((k % 7) - 3))))
    return st


class Features(unittest.TestCase):
    def test_composite_is_median_and_features_complete(self):
        st = fake_state()
        self.assertAlmostEqual(st.composite(), 60001.0)
        k = dict(ticker="T", series="KXBTC15M", close_ts=time.time() + 300, strike=60000.0, kind="at_least",
                 bid=0.55, ask=0.57, bid_sz=100, ask_sz=50, updated=time.time())
        b = features.build(st, k, anchor=(60000.0, 60001.0))
        self.assertEqual(set(b["feats"]), set(features.FEATURES))
        self.assertTrue(0 < b["p_base"] < 1)
        self.assertGreater(b["feats"]["ofi30"], 0)       # more taker buying than selling in the fake flow

    def test_base_prob_monotone_and_expiry(self):
        ps = [features.base_prob(60000, k, 300, 1e-4, "above")[0] for k in (59900, 60000, 60100)]
        self.assertTrue(ps[0] > ps[1] > ps[2])
        self.assertEqual(features.base_prob(60000, 59999, 0, 1e-4, "above")[0], 1.0)

    def test_stale_contract_rejected(self):
        st = fake_state()
        k = dict(ticker="T", series="KXBTC15M", close_ts=time.time() + 300, strike=60000.0, kind="at_least",
                 bid=0.5, ask=0.52, bid_sz=1, ask_sz=1, updated=time.time() - 60)
        self.assertIsNone(features.build(st, k))


class ModelGate(unittest.TestCase):
    def test_fallback_and_not_promoted_without_data(self):
        with tempfile.TemporaryDirectory() as d:
            m = M.Model(d)
            p, learned = m.predict({k: 0 for k in features.FEATURES}, 0.6, 0.7)
            self.assertFalse(learned); self.assertTrue(0.6 < p < 0.7)
            self.assertEqual(m.train([])["status"], "waiting")

    def test_gate_rejects_noise(self):
        try:
            import lightgbm  # noqa
        except ImportError:
            self.skipTest("lightgbm not installed")
        import random
        random.seed(0)
        rows = []
        for w in range(400):
            close = 1_800_000_000 + w * 900
            yv = random.random() < 0.5
            for k in range(20):
                f = {n: random.gauss(0, 1) for n in features.FEATURES}
                rows.append((close - 600 + k * 10, f"T{w}", "KXBTC15M", close, 60000, "at_least", 0.48, 0.52, json.dumps(f), 0.5, int(yv)))
        with tempfile.TemporaryDirectory() as d:
            m = M.Model(d).train(rows)
            self.assertEqual(m["status"], "not better than market")   # pure noise must never be promoted


class Storage(unittest.TestCase):
    def test_rows_labeled_on_settlement(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, "e.db"))
            db.add_row(dict(ts=1, ticker="T", series="KXBTC15M", close_ts=10, strike=1, kind="at_least", bid=.4, ask=.5,
                            feats={"a": 1}, p_base=.5))
            self.assertEqual(db.pending_tickers(20), ["T"])
            db.set_outcome("T", "yes", 1.0)
            self.assertEqual(db.labeled()[0][-1], 1)
            self.assertEqual(db.counts()["labeled"], 1)


if __name__ == "__main__":
    unittest.main()


class Forecast(unittest.TestCase):
    def test_quantiles_ordered_and_widen_with_time(self):
        from kbengine import forecast as FC
        now = time.time()
        hours = [(int(now) // 3600 + k) * 3600 for k in (1, 3, 6)]
        mt = [int(now) - 60 * (300 - i) for i in range(300)]
        mc = [60000 * (1 + 0.0003 * ((i % 5) - 2)) for i in range(300)]
        r = FC.forecast(60000.0, mc, mt, hours, n_paths=4000)
        hs = r["horizons"]
        self.assertEqual(len(hs), 3)
        for h in hs:
            self.assertTrue(h["q05"] < h["q25"] < h["q50"] < h["q75"] < h["q95"])
        widths = [h["q95"] - h["q05"] for h in hs]
        self.assertTrue(widths[0] < widths[1] < widths[2])
        lad = FC.ladder_scan(r["samples"], [dict(ticker="A", close_ts=hours[0], strike=60000, bid=0.48, ask=0.50),
                                            dict(ticker="B", close_ts=hours[0], strike=62000, bid=0.01, ask=0.02)], M.fee)
        pa = [x for x in lad if x["ticker"] == "A"][0]["p_forecast"]
        self.assertAlmostEqual(pa, 0.5, delta=0.05)
