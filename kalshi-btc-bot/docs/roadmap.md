# Roadmap: what to try next (prioritised)

None of the minute-resolution strategies built on public data survive costs (see `backtest_results_15m.md`). Further work should go
where the information or cost advantage could plausibly be, and where we can't test today because data is missing.

| # | Experiment | Why it could work | Missing data / work | Effort |
|---|---|---|---|---|
| 1 | **Forward paper test** of B (lag, benchmark-adjusted) and the blend for 4+ weeks with `research/paper_trader.py` | Only B was positive in all three periods; forward data is uncontaminated by selection | Keep the paper trader running (needs an always-on machine); the journal also records live depth | Low |
| 2 | **Second-resolution BRTI replica** (Coinbase, Kraken, Bitstamp, Gemini, LMAX, ... via websockets) | Settlement is a 60s BRTI average; a better real-time estimate in the final 1–3 minutes is the only structural information edge | Live collectors (no free historical tick data); measure replica error against `expiration_value` | Medium |
| 3 | **Maker strategy** (rest limit orders at fair ± k, cancel on spot moves) | Every taker strategy loses about 1–3¢ to fees and spread; resting orders can earn the spread | Needs order-book and queue data: the paper trader now journals top-5 depth; a fill model must come from live demo-exchange tests | High |
| 4 | **Second-level order-book history** | Order flow and book imbalance are the best-established short-horizon predictors in market microstructure | Kalshi websocket `orderbook_delta` recorder; weeks of recording before any test | Medium |
| 5 | **Intraday options surface** instead of DVOL | Research 3's premise; DVOL (30-day) is too coarse for 15 minutes | Deribit option-chain snapshots every minute (live collector; history is paid) | Medium |
| 6 | **Event/regime filters** (CPI/FOMC minutes, US open, weekends) | Volatility and mispricing may cluster around scheduled events | Economic calendar feed; needs months of data per regime | Low |

## Data limitations (documented, not invented)

- No historical order-book depth or queue position: fills are modelled at the top-of-book close with fixed 10-lot size, optional slippage, a fill-probability haircut and 1-minute latency.
  Live books show thousands of contracts at the touch, so size is unlikely to bind for small accounts. **Speed and queue position are untested.**
- Kalshi minute candles only print on change, so quotes are forward-filled and rows older than 180 seconds are excluded.
- BRTI itself isn't available; it's observed only at window boundaries (strike and settlement values). Coinbase and Bitstamp are proxies (basis std ≈ 2.9 bp).
- Deribit DVOL is a 30-day index, not a 15-minute implied vol.
- 60 days and 5,686 windows. One regime (BTC around $60k); results may not transfer.

## Live trading policy

Real-money order placement remains **disabled in all research code**. In the Android app it requires the user's own key, Live mode, and typing `LIVE`.
No strategy in this repo currently meets the bar for live deployment: positive after costs on validation and test, t > 2, positive walk-forward.
