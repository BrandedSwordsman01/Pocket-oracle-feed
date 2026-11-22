# Do chart setups predict BTC? (May 26 – Nov 22, 2026)

Data: 259,175 Coinbase BTC-USD 1-minute candles. Setups are detected by the app's own code
(`web/js/indicators.js`, run through `research/detect_events.js`), so the backtest tests exactly what the chart shows.
Training is May 26 – Sep 22 and test is Sep 23 – Nov 22. Forward windows don't overlap.

Reproduce:
```
python3 research/fetch_btc_history.py DATA/btc_180d.json 180
node research/detect_events.js DATA/btc_180d.json DATA/events_180d.json
python3 research/setups_test.py DATA DATA/btc_180d.json DATA/events_180d.json
```

## Part A: forward return after the setup fires, trading in the setup's direction

The bar for a perps trade is a mean above round-trip costs (about 4–15 bps, depending on fee tier and spread),
holding in **both** train and test, with |t| > 2.

| Setup | Horizon | Train mean / hit / t | Test mean / hit / t | Verdict |
|---|---|---|---|---|
| EMA 9/21 cross | 15m | +0.0bp / 49.5% / +0.1 | −0.3bp / 48.1% / −0.7 | no edge |
| 60-min breakout | 15m | +0.1bp / 46.2% / +0.1 | +0.2bp / 45.1% / +0.3 | no edge |
| Bollinger squeeze break | 60m | +0.4bp / 48.9% / +0.4 | −2.6bp / 47.3% / −1.7 | no edge |
| 1h momentum (2σ) | 60m | −3.6bp / 42.2% / −1.6 | +2.1bp / 46.7% / +0.6 | unstable, noise |
| Shock candle reversal | 60m | −5.2bp / 53.5% / −1.2 | −15.0bp / 53.0% / −1.7 | no edge |
| RSI(14) extreme → reversion | 15m | −0.2bp / 55.0% / −0.3 | +1.5bp / 58.1% / +1.4 | consistent hit rate, edge too small |
| VWAP stretch 2.5σ → reversion | 60m | **+8.7bp / 67.5% / +1.9** | **+8.0bp / 65.2% / +1.5** | promising, not significant (n=126/66) |

21 setup-and-horizon combinations were tested, so about one result with |t| > 2 is expected by chance alone.
The single t = 2.6 result (1h momentum, 5m horizon, test period) flips sign in training, so it is chance.

## Part B: adding a recent setup to the Kalshi KXBTCD model

No setup improved test log loss by more than 0.0003 (baseline 0.3230). 1h momentum and shock reversal
had strong training coefficients but made the test worse, which is classic overfitting. **Setups don't help on Kalshi binaries.**

## What this means for the app

- Trend setups (EMA cross, breakouts, squeezes) are drawn on the chart as **visual only**.
- Mean-reversion setups (VWAP stretch, RSI extreme) are flagged **"weak edge, unconfirmed"**. VWAP stretch
  is the only candidate for a perps signal, and it needs more data before it trades automatically.
- Nothing here justifies automated leveraged trading. Perps are manual only, with exit triggers.
