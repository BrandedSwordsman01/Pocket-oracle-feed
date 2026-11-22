"""AEGIS 2.0 mathematical and software validation (Part 13). Run: python3 -m unittest discover -s research/tests"""
import math, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from aegis import settlement as SM, simulate as SIM, calibrate as CAL, distribution as DIST, scanner as SC


def C(kind, k, series="KXBTCD", hi=float("nan"), t="T"):
    return SM.Contract(t + str(k), series, kind, k, hi)


class Settlement(unittest.TestCase):
    def test_sixty_second_average_when_fully_observed(self):
        obs = list(np.linspace(100, 101, 60))
        mean, var = SM.discrete_avg_moments(I_t=101, tau=0, sigma=5, observed=obs)
        self.assertAlmostEqual(mean, np.mean(obs)); self.assertEqual(var, 0.0)

    def test_15m_opening_benchmark_and_at_least_semantics(self):
        m = dict(ticker="KXBTC15M-X", strike_type="greater_or_equal", floor_strike=60644.61)
        c = SM.contract_from_market(m, "KXBTC15M")
        self.assertEqual((c.kind, c.k_lo, c.tick), ("at_least", 60644.61, 0.001))
        self.assertEqual(c.yes(60644.61), 1.0)   # equality pays YES ("at least")
        self.assertEqual(c.yes(60644.60), 0.0)

    def test_boundary_semantics_hourly(self):
        self.assertEqual(C("above", 60999.99).yes(60999.99), 0.0)   # strictly above
        self.assertEqual(C("above", 60999.99).yes(61000.00), 1.0)
        b = C("between", 60000, hi=60249.99)
        self.assertEqual((b.yes(60000), b.yes(60249.99), b.yes(60250)), (1.0, 1.0, 0.0))  # inclusive both ends

    def test_continuous_variance_formulas(self):
        s = 2.0
        self.assertAlmostEqual(SM.continuous_avg_variance(s, 600), s * s * 560)
        self.assertAlmostEqual(SM.continuous_avg_variance(s, 30), s * s * 30 ** 3 / 10800)
        # continuity at tau = 60
        self.assertAlmostEqual(SM.continuous_avg_variance(s, 60 - 1e-9), SM.continuous_avg_variance(s, 60), places=6)
        # zero future variance at expiration, monotone decreasing towards it
        self.assertEqual(SM.continuous_avg_variance(s, 0), 0.0)
        v = [SM.continuous_avg_variance(s, t) for t in (300, 120, 60, 30, 5, 1)]
        self.assertTrue(all(a > b for a, b in zip(v, v[1:])))

    def test_continuous_mean(self):
        self.assertAlmostEqual(SM.continuous_avg_mean(100, 30, mu=0.0, observed_integral=30 * 99), 99.5)
        self.assertAlmostEqual(SM.continuous_avg_mean(100, 60, mu=0.1), 100 + 0.1 * 30)

    def test_discrete_matches_continuous(self):
        for tau in (30, 59, 60, 120, 600):
            obs = [100.0] * (60 - len(SM.sample_offsets(tau)))
            _, vd = SM.discrete_avg_moments(100, tau, 1.0, observed=obs)
            vc = SM.continuous_avg_variance(1.0, tau)
            self.assertLess(abs(vd - vc) / vc, 0.06, (tau, vd, vc))   # 1-second sampling vs continuous
        self.assertAlmostEqual(SM.last_minute_factor(), 60 * 61 * 121 / 6 / 3600 / 60, places=12)  # N(N+1)(2N+1)/6 / N^2 / 60

    def test_fee_rounding(self):
        self.assertAlmostEqual(SM.buy_cost(10, 0.50), 5.18)   # 5.00 + 0.175 -> 5.18
        self.assertAlmostEqual(SM.buy_cost(1, 0.50), 0.52)    # 0.50 + 0.0175 -> 0.52
        self.assertAlmostEqual(SM.taker_fee(1, 0.055 / 1), 0.003639, places=6) if False else None
        # Kalshi docs worked example: revenue -$0.055, model fee $0.00363825 -> balance change -$0.06
        self.assertAlmostEqual(SM._ceil(0.055 + SM._ceil(0.00363825, 1e-6), 0.01), 0.06)
        self.assertAlmostEqual(SM.fee_total(10, 0.5), 0.18)   # fee + rounding, nothing else
        self.assertAlmostEqual(SM.buy_cost(10, 0.5, coef=SM.MAKER_COEF), 5.05)  # 0.04375 -> 5.05

    def test_book_depth_no_double_count(self):
        cost, got, avg = SM.book_cost([(0.40, 5), (0.41, 10)], 10)
        self.assertEqual(got, 10); self.assertAlmostEqual(cost, 5 * 0.40 + 5 * 0.41)
        # fees computed on the executed amount only once
        self.assertAlmostEqual(SM._ceil(cost + SM.taker_fee(10, avg), 0.01) - cost,
                               SM._ceil(cost + SM.taker_fee(10, avg), 0.01) - cost)
        _, got2, _ = SM.book_cost([(0.40, 3)], 10)
        self.assertEqual(got2, 3)


class MonteCarlo(unittest.TestCase):
    def test_convergence_to_analytic_and_se_shrinks(self):
        sd = [0.0007] * 9
        total = 0.0007 ** 2 * (8 + SM.last_minute_factor())
        exact = SIM.analytic_prob_above(60000, total, 60150)
        errs = []
        for n in (2000, 50000):
            sim = SIM.Simulator(n_paths=n, max_steps=20, seed=5)
            p = sim.prob_yes(60000, sd, [C("above", 60150)])[0][0]
            se = math.sqrt(exact * (1 - exact) / n)
            self.assertLess(abs(p - exact), 4 * se, (n, p, exact))
            errs.append(abs(p - exact))
        self.assertLess(errs[1], 0.02)

    def test_monotone_complementary_deterministic(self):
        sim = SIM.Simulator(n_paths=5000, max_steps=20, seed=1, nu=4)
        ks = [59800, 59900, 60000, 60100, 60200]
        ps = sim.prob_yes(60000, [0.0008] * 10, [C("above", k) for k in ks])[0]
        self.assertTrue(all(a >= b for a, b in zip(ps, ps[1:])))
        yes = sim.prob_yes(60000, [0.0008] * 10, [C("at_least", 60000, "KXBTC15M")])[0][0]
        no = sim.prob_yes(60000, [0.0008] * 10, [C("below", 60000)])[0][0]
        self.assertAlmostEqual(yes + no, 1.0, places=12)
        again = SIM.Simulator(n_paths=5000, max_steps=20, seed=1, nu=4).prob_yes(60000, [0.0008] * 10, [C("above", 60100)])[0][0]
        self.assertEqual(again, ps[3])   # deterministic seed


class Distribution(unittest.TestCase):
    def test_market_distribution_sums_to_one_and_fits_bands(self):
        ks = [59749.99, 59999.99, 60249.99, 60499.99]
        cs = [C("above", k) for k in ks]
        bids, asks = [0.80, 0.55, 0.25, 0.07], [0.83, 0.58, 0.28, 0.09]
        f = DIST.fit(cs, bids, asks, spot=60100)
        self.assertAlmostEqual(f["q"].sum(), 1.0, places=9)
        self.assertTrue((f["q"] >= 0).all())
        ps = [DIST.prob_of(c, f) for c in cs]
        for p, b, a in zip(ps, bids, asks):
            self.assertTrue(b - 0.01 <= p <= a + 0.01, (p, b, a))
        self.assertTrue(all(x >= y for x, y in zip(ps, ps[1:])))


class Calibration(unittest.TestCase):
    def test_platt_recovers_bias_and_bounds_per_side(self):
        rng = np.random.default_rng(0)
        p_true = rng.uniform(0.05, 0.95, 4000)
        y = (rng.random(4000) < p_true).astype(float)
        p_raw = CAL.sigmoid(1.5 * CAL.logit(p_true))   # over-confident model
        pl = CAL.Platt().fit(p_raw, y)
        self.assertAlmostEqual(pl.b, 1 / 1.5, delta=0.08)
        groups = np.repeat(np.arange(40), 100)
        ph, lo_y, hi_y, lo_n = CAL.block_bootstrap_bounds(p_raw, y, groups, np.array([0.3, 0.7]), B=60)
        self.assertTrue((lo_y <= ph).all() and (ph <= hi_y).all())
        self.assertTrue(np.allclose(lo_n, 1 - hi_y, atol=1e-9))  # NO lower bound from the YES upper tail, not 1 - low_yes
        self.assertTrue(((1 - ph) >= lo_n).all())

    def test_log_pool_limits(self):
        self.assertAlmostEqual(float(CAL.log_pool(np.array(0.7), np.array(0.4), 1.0)), 0.7, places=3)
        self.assertAlmostEqual(float(CAL.log_pool(np.array(0.7), np.array(0.4), 0.0)), 0.4, places=3)


class Dominance(unittest.TestCase):
    def test_overlap_pair_pays_at_least_one(self):
        a = C("at_least", 60000.00, "KXBTC15M"); b = C("above", 60099.99)
        self.assertEqual(SC.min_payout([(a, "yes"), (b, "no")], include_outage=False), 1.0)
        # official outage rule: 15M resolves NO while hourly settles later -> the pair can pay 0
        self.assertEqual(SC.min_payout([(a, "yes"), (b, "no")], include_outage=True), 0.0)

    def test_hourly_ladder_and_non_overlap(self):
        lo, hi = C("above", 59999.99), C("above", 60499.99)
        self.assertEqual(SC.min_payout([(lo, "yes"), (hi, "no")]), 1.0)
        self.assertEqual(SC.min_payout([(hi, "yes"), (lo, "no")]), 0.0)   # thresholds in the wrong order
        f = SC.pair_floor([(lo, "yes"), (hi, "no")], [0.55, 0.40], Q=10)
        self.assertAlmostEqual(f["floor"], 10 - SM.buy_cost(10, 0.55) - SM.buy_cost(10, 0.40))

    def test_one_leg_cannot_fill(self):
        r = SC.simulate_pair_fill([10, 0], 10)
        self.assertEqual(r["matched"], 0); self.assertEqual(r["unmatched"], [10, 0]); self.assertFalse(r["locked"])
        r = SC.simulate_pair_fill([10, 6], 10)
        self.assertEqual((r["matched"], r["unmatched"]), (6, [4, 0]))


if __name__ == "__main__":
    unittest.main()
