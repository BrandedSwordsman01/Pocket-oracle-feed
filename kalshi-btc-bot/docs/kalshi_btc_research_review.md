# Kalshi BTC 15-minute markets: research review

Reviewed Nov 22, 2026. **Access limitation:** this environment's web tools refuse reddit.com, so the three Reddit threads
**could not be read directly**. The claims below come from (a) the summaries in the task brief and (b) search-engine summaries
and related public work found by web search. Every Reddit claim is therefore labelled **author assertion (second-hand)**.
If the thread text is pasted in, this review can be updated with exact figures and comments.

Evidence labels: **Verified** = we reproduced it on our data · **Simulated** = the author's backtest · **Forward** = live or paper results
reported over time · **Assertion** = stated without data · **Hypothesis** = plausible, needs testing.

## Contract mechanics (verified from Kalshi market data)

`KXBTC15M` pays $1 if the **simple average of CF Benchmarks BRTI over the 60 seconds before close** is **≥** the 60-second
BRTI average before the window opened. The API's `floor_strike` is that opening average, and `expiration_value` is the closing
average (checked on settled markets: each window's strike equals the previous window's expiration value). Consequences:

- **Coinbase is not the benchmark.** BRTI aggregates several constituent exchanges. Any "Coinbase leads Kalshi" signal has to beat the
  Coinbase–BRTI basis before it can be called lag (measured in `docs/backtest_results_15m.md` §1).
- The 60-second average lowers late-window variance: with *m* minutes left, remaining variance ≈ σ²·(m − 1 + 1/3) minutes.
- Fees: taker fee ceil(0.07·C·P·(1−P)) dollars per order. That's about 1.75¢ per contract at 50¢, and it is charged again on early exits.

## Research 1: RSI momentum ("I tested 100 versions of a Kalshi 15-minute BTC strategy")

| Claim | Label |
|---|---|
| RSI(14) on 1-minute candles: YES above 60, NO below 40, 5-minute RSI confirmation, entries 3–10 min before close, price and spread limits, exit on momentum reversal | Assertion (second-hand) |
| 100 configurations tested, all profitable; best simulated profit $315.88 | **Simulated**; fills, fees and sample size unknown |

Concerns before testing: selecting the best of 100 variants inflates the reported result (multiple comparisons). "All 100 profitable" is a red flag
for a shared bias such as mid-price fills, ignored fees or lookahead, rather than for robustness. Our own 6-month test found RSI-extreme
signals predict BTC direction only marginally (58% hit rate, ~1.5 bp/15m; `research/SETUPS.md`).
**Status: tested as Strategy A.**

## Research 2: Coinbase momentum and Kalshi lag

| Claim | Label |
|---|---|
| Large 5- and 15-minute spot moves are priced into Kalshi with a delay | Hypothesis / assertion |
| Filters on price, liquidity, spread, volume and time to expiry; exit on reversal | Assertion |
| Best simulated profit $34.92 | **Simulated**; small |

Related public work: a vendor batch of 5,732 backtests reports 99 of 100 variants of a "15-minute spot momentum" family
"passing", and itself warns that this does not establish live performance ([TurbineFi](https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026), vendor, Simulated).
An open-source latency bot targets the same effect, without published results ([nxd914/chiron](https://github.com/nxd914/chiron), Hypothesis).
**Status: tested as Strategy B, with and without the BRTI basis correction.**

## Research 3: binary-option fair value (Black-76, Deribit implied vol)

| Claim | Label |
|---|---|
| 15-minute contracts can be priced as digital options using Deribit implied vol, a vol smile, realized vol and momentum | Hypothesis (sound in principle) |
| Model vs executable-price gaps identify trades; the author's later posts cover entry thresholds, stops, near-expiry gamma, time-of-day regimes, flip/barrier probabilities | Assertion (second-hand; follow-up posts not accessible) |

Related public work:
- An academic study finds Kalshi and Polymarket 15-minute BTC contracts price near-identically, with a roughly symmetric smile and a
  robust **variance risk premium** that is only weakly correlated with Deribit's volatility index
  ([International Review of Financial Analysis / ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1057521926003601), **peer-reviewed empirical**).
  Implication: Kalshi's implied vol tends to sit above realized vol, and Deribit DVOL (30-day) is a weak input for 15-minute pricing.
- A Black-76 digital priced off a Deribit surface every 15 minutes, folding the 60-second BRTI average into time to expiry
  ([trading-rsi PR #163](https://github.com/JihuiTanUchicago/trading-rsi/pull/163), Hypothesis; predictive test pending).
- On hourly contracts, a significant gap between Kalshi-implied and Deribit-implied vol (p ≈ 5.6×10⁻⁹, n = 162)
  ([milanroundraise/kalshi-mispricing](https://github.com/milanroundraise/kalshi-mispricing), Simulated/empirical, hourly only, small n).
- A vendor study of 6,298 15-minute windows: "buy the leading side" earns +0.67¢ before costs vs a 1.55¢ average fee, so it loses net
  ([botforkalshi](https://www.botforkalshi.com/blog/kalshi-15-minute-bitcoin-markets), vendor; **consistent with our own test**: −2 to −5¢/contract).

**Status: tested as Strategy C (realized, implied and mixed vol, normal and t4 tails), Strategy D (flip/touch probability as a filter and
exit), and Strategy E (ensemble).** Implied vol here means **Deribit DVOL only** (30-day index), because historical intraday option-chain
data for a full smile isn't freely available. That limitation is a gap, not a tested negative.

## Our prior evidence (verified, this repo)

| Finding | Source |
|---|---|
| Raw vol model loses on KXBTCD after fees (−1.3 to −2.2¢/contract test) | `research/RESULTS.md` |
| Market-anchored blend +5.3¢/contract on test for both KXBTCD (67 trades) and KXBTC15M (108 trades), t ≈ 1.4 | `research/RESULTS.md` |
| "Buy the leading side" on KXBTC15M: −2.0 to −5.4¢/contract test | `research/test15.py` |
| Chart setups have no directional edge on BTC (6 months) | `research/SETUPS.md` |

## What would count as evidence

A strategy is considered real only if it is positive after executable prices and fees on validation **and** on an untouched test period,
with t > 2, robust to +1¢ slippage, and positive across walk-forward folds. Results: `docs/backtest_results_15m.md`.
