# AEGIS 2.0: mathematics, validation and corrections

Module: `research/aegis/` (settlement, volmodels, simulate, calibrate, distribution, scanner, live). App port: `web/js/aegis.js`.
Tests: `research/tests/test_aegis.py` (Python) and `test/aegis.test.js` (app).

## 1. Official mechanics (verified Nov 22, 2026)

Sources: Kalshi contract terms [CRYPTO.pdf](https://assets.kalshi.com/contract_terms/CRYPTO.pdf) (KXBTC15M) and
[BTC.pdf](https://assets.kalshi.com/contract_terms/BTC.pdf) (KXBTCD, KXBTC), the [help article](https://help.kalshi.com/en/articles/13823838-crypto-markets),
the series API (`/series/{ticker}`), each market's `rules_primary`, and [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding.md).

| | KXBTC15M (15-minute) | KXBTCD (hourly above/below) | KXBTC (hourly range) |
|---|---|---|---|
| Underlying | simple average of CF BRTI over the 60 s before T | same (60 s before T) | same |
| Strike | K = 60-s BRTI average before the window **opened** (`floor_strike`) | listed strike, N,NNN.99 | [K_lo, K_hi] |
| Payout | YES iff A_T **≥** K ("at least") | YES iff A_T **>** K ("above", strict) | YES iff K_lo ≤ A_T ≤ K_hi (inclusive) |
| Tick | **$0.001** | $0.01 | $0.01 |
| Missing data | affected strikes **resolve NO** | expiration moves to the **first later minute with data** | same as KXBTCD |
| Fees (API) | quadratic, multiplier 1 | quadratic, multiplier 1 | quadratic, multiplier 1 |
| Revisions | after expiration ignored | ignored | ignored |

**Consequences**
1. A 15-minute window closing on the hour and the hourly ladder for that hour **share A_T in normal operation but not in a data outage**.
   Any 15M + hourly pair is therefore not riskless, even when thresholds overlap; the scanner enumerates that state explicitly.
2. Fee: per [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding.md), trade fee = ceil to $0.000001 of M·c·Q·P·(1−P)
   (c = 0.07 taker, 0.0175 maker, **from the brief**: kalshi.com's fee page sits behind a bot checkpoint and could not be read here).
   A rounding fee then floors the balance change to $0.01 (non-direct members), so cash cost = ceil_cent(Q·P + fee). The accumulator
   rebates over-rounding across fills of one order. We charge per order, which is conservative.
3. **BRTI access:** historical index values are available only through Kalshi's authenticated CF Benchmarks passthrough with an entitlement.
   We had neither, so **all index values in the backtest are a proxy**: Coinbase 1-minute closes, re-centred on the BRTI anchors Kalshi
   publishes (each 15-minute strike is an exact 60-s BRTI average). Proxy error: basis std 2.9 bp at anchors (`docs/backtest_results_15m.md` §1).
4. Sample selection: BRTI is published once per second. We model the 60 samples as T−59 … T. The exact CF selection rule wasn't
   available, and the difference from T−60 … T−1 is one sample (≈1/60 of the window).

## 2. Settlement-averaging variance (Part 4): validated

Arithmetic BM dI = μ dt + σ dW (σ in $/√s), A_T = (1/60)∫_{T−60}^{T} I_s ds, τ = T − t.

- **τ ≥ 60:** A_T − I_t = μ(τ−30) + σ[(W_{T−60} − W_t) + (1/60)∫(W_s − W_{T−60})ds]. The two pieces are independent with variances (τ−60) and 60/3,
  so **Var = σ²(τ − 40)**. ✔ The brief's formula is correct.
- **τ < 60:** the observed part is fixed; the future part (1/60)∫_t^T (W_s − W_t)ds has variance τ³/(3·3600), so **Var = σ²τ³/10800**. ✔
- Continuity at τ = 60: 60³/10800 = 20 = 60 − 40. ✔ Zero at τ = 0. ✔
- Mean for τ < 60: (∫observed + τI_t + ½μτ²)/60. ✔ For τ ≥ 60 it's I_t + μ(τ−30) (the brief only gave τ < 60).
- **Discrete production formula** (`discrete_avg_moments`): with future sample offsets d_k, Var = σ²/3600 · Σ_{j,k} min(d_j, d_k).
  For a full future window the final-minute factor is Σ_{j,k=1..60} min(j,k)/60³ = **0.34171** (73,810/216,000), vs 1/3 continuous.
  Discrete and continuous agree within 6% for τ ∈ {30, 59, 60, 120, 600} (test).

**Correction (practicality):** per-second Monte Carlo over 80k historical decision rows is not tractable in Python. Paths use **1-minute steps**
for the full minutes plus the exact discrete factor for the averaging minute. All decisions in the backtest are made at τ ≥ 60 s, so no
already-printed window samples exist; the observed-sample branch is implemented and tested for live use.

## 3. Price process (Part 3)

- **3A diffusion:** X = log I, X_{t+1} = X_t + (μ − ½v)Δ + √(vΔ)ε. μ = 0: prior research (`research/SETUPS.md`, the 60-day study) found no
  out-of-sample directional predictability, so the regularised drift estimate is exactly zero. Gaussian and unit-variance Student-t ε are implemented.
- **3B volatility:** instead of a hand-specified 3-state latent model we use **GARCH(1,1)-t on de-seasonalised 1-minute returns × a 24-hour
  diurnal variance profile**, estimated by MLE on data before the train cutoff. That is the brief's allowed "GARCH if easier to estimate reliably"
  route. A latent-regime model was not fitted: GARCH persistence (α+β ≈ 0.999) already captures regime-like clustering, and a 3-state HMM
  on 1-minute data adds parameters without an out-of-sample test we could pass with 60 days of contracts. Regimes are used for **reporting**
  (vol terciles).
- **3C jumps:** compound Poisson with intensity λ and the empirical distribution of standardised residuals beyond 4.5σ.
  **Correction:** jumps + Student-t innovations double-count tail mass, since t already fattens tails. Model D uses t without jumps;
  model E uses Gaussian diffusion + empirical jumps. The −½v convexity term is applied to the total diffusion variance. Jump compensation
  is omitted because the empirical jump sizes are mean ≈ 0.
- **Centre:** C_t = Coinbase_t · (1 + basis), where basis = K_15M / Coinbase(window open) − 1 is measured at the latest BRTI anchor.

## 4. Monte Carlo (CRN)

`Simulator` pre-draws one innovation matrix (N × 120) per model and reuses it across all strikes of a settlement and across rows.
That gives common random numbers: probabilities are coherent and monotone in strike, YES/NO complementary to machine precision,
and deterministic for a seed. Convergence was tested against the Gaussian closed form at N = 2,000 and 50,000 (|error| < 4 SE).

## 5. Market-implied distribution (Part 5)

`distribution.fit`: bucket masses q ≥ 0, Σq = 1, with bucket edges at every strike, so no bucket straddles a threshold.
The objective is squared distance of each contract's implied probability **to its bid–ask band** (zero inside), plus λ‖D²q‖² smoothness.
Solved with SLSQP and an analytic gradient. Strike survival probabilities from q are monotone by construction. Quotes are bounds, not probabilities.

## 6. Pooling (Part 6)

For one binary contract, the brief's logarithmic pool over the two buckets {YES, NO} reduces to logit p = ω·logit p_model + (1−ω)·logit p_market
(plus ε terms). ω is fitted on **validation** log loss after the model is Platt-calibrated on **train**. Double counting is avoided because the
simulator never sees market prices; the market-implied distribution is built from quotes only.

## 7. Calibration and bounds (Part 7)

- Platt scaling (logit-linear) fitted on train, and reported per horizon, volatility regime and price range.
  PIT of the realised settlement average under each model's predictive distribution (KS against uniform).
- **Bounds:** 150–200 day-block bootstrap draws of the calibration map (whole days resampled, so the dependence across a window's 14 rows
  stays together). p_low,YES = q10 of calibrated P(YES); p_low,NO = q10 of calibrated P(NO) = 1 − q90 of P(YES), so it is **not** 1 − p_low,YES (tested).
  Coverage was checked on test, by bin and per side.
- **Limitation:** the bootstrap captures calibration-map uncertainty, not volatility-model parameter uncertainty or regime shifts.

## 8. Expectancy (Part 8)

EDGE_LOW(Q) = Q·p_low − C_book(Q) − F(Q) − L(Q), held to settlement. With no historical depth, C_book(Q) = Q × top-of-book ask (paper
trading records live depth for future use). F is the official fee with rounding, charged once on the executed amount; L is a latency penalty
tested at 0, 1 and 2¢, plus a one-minute-delayed fill. **Maker orders are not backtested**: there's no queue or fill history (§ execution risks).

## 9. Dominance scanner (Part 9)

The minimum combined payout is computed by **enumerating settlement states** at each threshold ± one tick (strict vs inclusive boundaries
handled exactly), plus the 15M "no data → NO" state. PAIR_FLOOR = Q·min_payout − Σ buy_cost(leg) − reserve. A two-leg signal is valid
only if both legs fill (`simulate_pair_fill` models partial and unmatched legs).

## 10. Alternatives tested (beyond the brief)

- **M: market recalibration.** Platt on the market mid itself, to check whether Kalshi prices are biased (favourite–longshot) in a way a
  simple map can exploit.
- **X: cross-market consensus.** Fair value of the 15-minute contract taken from the hourly ladder settling at the same instant
  (model-free, via §5). It tests whether two markets on the same random variable disagree.
- **Trading uses conservative bounds by default (G).** Point-estimate versions are reported alongside to show how much the bound costs.
