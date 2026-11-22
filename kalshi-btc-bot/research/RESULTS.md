# Backtest results: Kalshi KXBTCD (hourly BTC above/below), Oct 23 – Nov 22, 2026

Data: 129,044 settled markets. The backtest scored 10,264 markets that were within 2% of the settlement price,
at 9 decision times each (5–90 min before close), for 37,003 samples. Kalshi's 1-minute bid/ask
history came from `/markets/candlesticks`, and BTC prices from Coinbase 1-minute candles. Models were fitted on
Oct 23 – Nov 11 and scored on Nov 12 – Nov 22, which they never saw.

Reproduce:
```
python3 research/fetch_data.py DATA_DIR 30
python3 research/backtest.py DATA_DIR
python3 research/analyze.py DATA_DIR
```

## Prediction quality (test period, lower is better)

| Predictor | Brier | Log loss |
|---|---|---|
| Kalshi mid price | 0.0990 | 0.3228 |
| Vol model (app v0.1) | 0.1011 | 0.3312 |
| Vol model, best tuning (t4, vol × 0.9) | 0.1004 | n/a |
| Fitted blend (market + model) | 0.0989 | 0.3230 |

The blend's fitted weights are 0.986 on the market price and 0.108 on the vol model, so the market price does almost all the work.

## Trading simulation (test period, 1 contract per trade, taker fees included)

| Strategy | Trades | Win rate | P&L per contract | Green days |
|---|---|---|---|---|
| Vol model, edge ≥ 2¢ | 1,940 | 27% | −2.23¢ | 2/11 |
| Vol model, edge ≥ 4¢ | 1,247 | 26% | −1.32¢ | 4/11 |
| Blend, edge ≥ 2¢ | 67 | 87% | +5.28¢ | 8/10 |

## Market calibration

When you buy at the ask, the win rate tracks the price paid closely in every 10¢ bucket.
After fees, every bucket lost money in the test period (−0.5¢ to −4.3¢ per contract).
There's a slight long-shot bias (cheap contracts win a bit less often than their price implies),
but fees cancel it out.

## Conclusions

1. **Kalshi's hourly BTC prices are efficient.** A volatility model built on public Coinbase data does not beat them,
   and trading on it loses money after fees.
2. **The blend is the only thing that came out positive, but it isn't proven.** 67 trades at +5.28¢ is roughly
   1.2 standard errors from zero, which could be luck. The app uses the blend, so the bot trades rarely and only on small
   disagreements. Paper-trade it to build a larger sample before risking money.
3. **Where a real edge would more likely come from:**
   - Better data than the crowd: a real-time copy of the CF Benchmarks BRTI index built from its constituent exchanges.
   - Speed in the last minutes before close.
   - Market making, i.e. resting limit orders and earning the spread. This can't be tested with minute candles because it needs fill modelling.

   TradingView does not provide any of these.

## Endgame test (last 2–10 minutes before close)

`research/endgame.py`: the vol model uses Coinbase spot and an effective horizon that accounts for the 60-second settlement average.
The best training-period configuration (t4, vol × 0.8, edge ≥ 8¢, 2–10 min) made **+0.57¢ per contract, t = 0.22** on the test days (256 trades).
That's no edge. A single-exchange spot price isn't better information than what Kalshi's market makers already use.

## 15-minute series (KXBTC15M), Nov 8 – 22, 2026

`research/test15.py`: 1,326 windows and 11,760 samples (2–14 min before close). Train is Nov 8–17, test is Nov 18–22.

| Strategy | Train | Test |
|---|---|---|
| "Buy the leading side" at 10/5/3/2 min | −1.4 to −2.8¢, 72–79% win | **−2.0 to −5.4¢**, 68–80% win |
| Vol model (best grid on train) | +3.9¢, n=242, t=1.7 | +0.8¢, n=98, t=0.2 |
| **Blend (vol 0.38 + market 0.63), edge ≥ 2¢** | **+3.8¢, n=205, 66% win, t=1.5** | **+5.3¢, n=108, 72% win, t=1.4** |
| Any price bucket | n/a | all negative (−0.4 to −3.4¢) |

Public claims checked against this data: the popular "buy the favourite late" idea loses after fees, as a vendor's own study of
6,298 windows also found (+0.67¢ edge vs. a 1.55¢ average fee). The market-anchored blend is positive in both periods on
both series. Pooled 15-min train+test gives t ≈ 2, which is the strongest evidence so far but still modest. The app ships the blend for both series.

## UPDATE: 60-day KXBTC15M study (supersedes the 14-day result above)

`research/run_15m_study.py` on 5,686 windows (Sep 23 – Nov 22): the blend's walk-forward result is **+1.15¢/contract, n=370, t=0.51**,
so the 14-day +5.3¢ was within noise. No strategy family (RSI, momentum/lag, fair value, flip filter, ensemble) passed validation and test.
See `docs/backtest_results_15m.md`.
