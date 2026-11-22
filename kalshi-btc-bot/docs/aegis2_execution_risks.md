# AEGIS 2.0: execution risks

| Risk | Evidence | Mitigation in code | Residual |
|---|---|---|---|
| **Proxy index, not BRTI** | Historical BRTI needs Kalshi's entitled CF passthrough. We use Coinbase re-centred on BRTI anchors (basis std 2.9 bp ≈ 0.7 min of volatility) | Basis adjustment at the latest anchor; proxy labelled everywhere | Model error inside the window is unmeasured until BRTI access exists |
| **Data-outage settlement mismatch** | Contract terms: 15M → NO if data missing; hourly → first later minute with data | Scanner enumerates the outage state; app flags "NOT risk-free" | A 15M + hourly "dominance" pair can lose both legs |
| **Coinbase candle latency** | Live check (Nov 22): the public 1-minute candle feed was **~5.5 minutes behind** | Paper trader falls back to the live ticker and journals `S_source` / `candle_lag_s`; app shows a delay warning and drops the basis | Volatility and RSI inputs are minutes stale when the feed lags |
| **Stale Kalshi quotes in history** | Minute candles print only on change; G's edge falls from +1.58¢ to +0.28¢ with a one-minute-delayed fill | Rows with quotes > 180 s old excluded; delayed-fill stress test | Part of the backtest edge is likely un-fillable |
| **No historical depth / queue** | Candles carry top-of-book only. Live books show thousands of contracts at the touch | 10-lot size; live depth journaled; paper fills require size ≥ Q | Maker strategies untestable; slippage for size unknown |
| **Asynchronous multi-leg fills** | Two separate books, sequential orders | `simulate_pair_fill`: a floor is locked only when every leg fills; unmatched legs reported as directional risk | Real latency between legs not measured |
| **Calibration drift** | Bootstrap lower bounds covered only 11/20 bins on test | Bounds reported with coverage; app defaults to PASS | Bounds understate uncertainty under regime change |
| **Fee schedule** | kalshi.com fee page not machine-readable here; coefficients from the brief; series API shows quadratic, multiplier 1 | Official rounding implemented and tested (incl. the docs' worked example) | Series fee changes (`/series/fee_changes`) not yet polled |
| **Multiple comparisons** | 30 strategy/threshold configurations on validation, plus earlier studies (~400 configs) | Thresholds chosen on validation only; test touched once; CIs reported | A ±2 t-stat "winner" is expected by chance |
| **Tick size** | 15M ticks at $0.001 while the app and backtests use cent quotes from candles | Costs rounded per the docs | Sub-cent queue priority ignored |
| **Live trading safety** | — | Research code contains no order placement; app live mode needs key + Live mode + typing LIVE; AEGIS actions are display-only | — |
