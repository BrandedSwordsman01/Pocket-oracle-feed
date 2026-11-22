# Project audit: kalshi-btc-bot (Nov 22, 2026)

## 1. Architecture

```
kalshi-btc-bot/
  web/                    App UI and ALL live trading logic (plain JS, bundled into the APK)
    js/model.js           vol model (EWMA + Student-t4), per-series market-anchored blends, Kalshi fees, Kelly sizing, Brier
    js/bot.js             trading loop: scan, risk limits, paper/demo/live orders, settlement, calibration log
    js/api.js             Kalshi event-contract client (V2 orders), Kalshi Perps client, Coinbase prices
    js/indicators.js      EMA/RSI/MACD/BB/VWAP/levels + 7 setup detectors (shared with research)
    js/perps.js, perps_ui.js  paper perps engine + Perps tab
    js/app.js             UI: chart, ladder, signals, stats, settings, test-order buttons
  android/                Java shell: WebView, RSA-PSS signer, Keystore-encrypted key vault, foreground service
  research/               Python backtests + data fetchers (data lives outside the repo)
  test/                   node tests (model, bot, api, perps, indicators) + Java signer cross-check
.github/workflows/kalshi-btc-bot-apk.yml   tests -> APK -> pre-release
```

## 2. Existing strategies

| Strategy | Where | Status |
|---|---|---|
| Vol model (EWMA σ, t4 tails, zero drift) vs ask | `model.js`, `research/backtest.py` | **Loses** after fees on KXBTCD (−1.3 to −2.2¢/contract test) |
| Market-anchored blend (logistic on logit p_vol + logit mid) | `model.js` `SERIES` | Positive on both series' test periods (+5.3¢), not yet significant |
| Endgame (last 2–10 min, KXBTCD) | `research/endgame.py` | No edge (+0.57¢, t=0.2) |
| 7 chart setups (trend + mean reversion) | `indicators.js`, `research/setups_test.py` | No edge on BTC direction or on Kalshi |
| 15-min blend (KXBTC15M) | `research/test15.py` | +5.3¢ test / +3.8¢ train; leading-side heuristic loses |

## 3. Historical results and the Brier problem

- KXBTCD test Brier: market 0.0990, vol model 0.1011, blend 0.0989. KXBTC15M: market 0.1513, vol model 0.1514.
- **Diagnosis:** the vol model is not badly built. Kalshi prices already contain the same information and more,
  so a stand-alone model can't beat the market's Brier score. The useful question is whether the **residual** (model minus market)
  predicts outcomes after costs. That is what the blend measures.
- Brier is dominated by easy, near-certain contracts. Scores should be restricted to tradeable prices (5–95¢) and reported per time-to-expiry bucket.

## 4. Reusable components

- Kalshi minute candle fetcher (`fetch_data.py`), Coinbase 1-minute history (`fetch_btc_history.py`), setup detector runner.
- Vol model and t4 CDF, fee formula, logistic fitting, sample builder (`quote_at`, `spot_at`, `ewma_sigma`).
- App: signed Kalshi client (V2 orders verified against docs), Keystore key storage, paper engine, journaled log.

## 5. Weaknesses and unrealistic assumptions

| # | Issue | Impact | Fix in this round |
|---|---|---|---|
| 1 | Fills at the candle-close touch with unlimited size | Overstates fill quality | Executable bid/ask, size cap, slippage parameter, skip when quote stale |
| 2 | **No historical order-book depth**; candles give only top-of-book close | Can't model partial fills | Conservative size cap + slippage sensitivity; paper trader **records depth going forward** |
| 3 | Hold-to-settlement only; no early exits | Can't test momentum strategies that exit early | Backtester supports exits at the bid (with fees) |
| 4 | Single train/test split, grid search on train | Overfitting risk, one draw of the test set | Train / validation / untouched test + walk-forward |
| 5 | Coinbase spot used as the settlement index | Apparent "lag" may just be the Coinbase–BRTI basis | Measure basis from BRTI anchors (15-min strike/expiration values) and Bitstamp |
| 6 | Kalshi candles only print on change; we forward-fill | Stale quotes can look like mispricing | Staleness limit per sample |
| 7 | Small samples (67–108 test trades) | Wide confidence intervals | 60 days of KXBTC15M (~5,700 windows) |
| 8 | Calibration log in the app records one sample per market at first sight | Biased toward early, wide markets | Paper trader journals every signal at every scan |
| 9 | Live bot runs in a WebView; Android may kill it | Missed trades/settlements | Desktop/server paper trader in Python for research-grade logging |
| 10 | No implied volatility | Model ignores the options market | Deribit DVOL feature (30-day IV; a coarse proxy for 15-min vol) |

## 6. Kalshi integration status

- Event orders: V2 `/portfolio/events/orders` (legacy writes return 410). Request bodies are unit tested. The live round trip still needs
  the user's key (Settings → Test live order).
- Perps: `/trade-api/v2/margin/*`, manual only.
- **Live order placement is user-initiated in the app. Nothing in the research code places orders.**
