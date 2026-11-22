# BTC Kalshi Bot (Android)

An Android app that trades Kalshi's Bitcoin price markets (series `KXBTCD` by default)
using a simple, transparent pricing model. It runs paper trading by default.

> **Read this first.** The backtest found Kalshi's BTC prices to be efficient. The bot trades rarely and its edge is unproven.
> Never trade money you can't lose.

## Install on your phone

1. Open this repo's **Releases** page on your phone and find the newest *Kalshi BTC Bot build*.
2. Download `kalshi-btc-bot.apk` and open it. Allow "Install unknown apps" for your browser when asked.
3. Set battery usage to **Unrestricted** (Settings → Apps → BTC Kalshi Bot → Battery). Otherwise
   Samsung's battery saver freezes the bot.

## How to use it (in this order)

1. **Paper** mode needs no keys. It uses real Kalshi and Coinbase prices with fake fills and a $100 virtual bankroll.
   Run it for days. Watch **"Is the model any good?"**: it compares the model's predictions against
   Kalshi's own prices on every settled market (Brier score; lower wins).
2. **Demo** mode places real orders on Kalshi's demo exchange with fake money. Create demo API keys at
   demo.kalshi.co, then paste them under *API keys → Demo*. This checks the order plumbing end to end.
3. **Live** mode trades real money. Only use it if the calibration panel has scored a few hundred markets
   and the model beats the market. You must type `LIVE` to start.
   The default limits are $5 per trade, $20 at risk at once, and a stop after a $10 loss in a day.

## The model

**Backtest summary (30 days of real Kalshi data, see [research/RESULTS.md](research/RESULTS.md)).** A pure volatility model
loses money against Kalshi's hourly BTC prices. The app therefore uses a *market-anchored blend*:

- A volatility model: EWMA vol from Coinbase 1-minute returns, fat-tailed Student-t, zero drift. It gives `p_vol = P(BTC above strike at close)`.
- A blend fitted on 20 days and tested on the next 10: `logit(p) = -0.068 + 0.108·logit(p_vol) + 0.986·logit(market_mid)`.
- It buys YES or NO only when `p − ask − taker fee ≥ 2¢`, between 10 and 90 minutes before close, at prices from 8¢ to 92¢.
- Sizing is 15% fractional Kelly, capped by the per-trade and total-exposure limits. Positions are held to settlement.

On the unseen test days the blend made 67 trades, +5.3¢ per contract, and was profitable on 8 of 10 days. That's **not statistically significant**,
so paper-trade it and grow the sample before going live.

## 15-minute strategy research (v0.6)

A full study of RSI momentum, momentum/lag, binary fair value (realized and Deribit implied vol), flip-risk filters, an ensemble and baselines
over 60 days and 5,686 `KXBTC15M` windows, with executable prices, fees, slippage, latency and train/validation/test plus walk-forward:

- **No strategy has a validated edge.** Results: [docs/backtest_results_15m.md](docs/backtest_results_15m.md).
- Research review with sources and evidence labels: [docs/kalshi_btc_research_review.md](docs/kalshi_btc_research_review.md).
- Project audit: [docs/project_audit.md](docs/project_audit.md). Next experiments and data gaps: [docs/roadmap.md](docs/roadmap.md).
- Framework: `research/kbt/` (data, models, strategies A–E, backtester) and `research/run_15m_study.py`.
- **Paper trader:** `python3 research/paper_trader.py --journal paper_journal --fit-data DATA_DIR`, then
  `python3 research/paper_report.py paper_journal`. It journals every signal and simulates depth-checked fills. It has no order code.

## Chart setups and perps (v0.3)

- **Chart:** EMA 9/21, VWAP, Bollinger Bands, auto support/resistance, RSI or MACD pane, Kalshi strike lines, and setup arrows.
- **Signals:** every setup is labelled with its 6-month backtest result ([research/SETUPS.md](research/SETUPS.md)).
  Trend setups (EMA cross, breakouts, squeezes) showed **no edge**. Mean reversion (VWAP stretch, RSI extremes) showed a weak,
  unconfirmed edge, smaller than typical perps fees for RSI.
- **Perps tab:** Kalshi BTC perpetual (`KXBTCPERP`, 0.0001 BTC per contract). It shows live price, funding, max leverage and the order book,
  and has a long/short ticket with leverage, take-profit and stop-loss. Paper mode simulates fills, fees (0.12%) and liquidation.
  Demo and live modes use Kalshi's Perps API (`/trade-api/v2/margin`, same API key) and confirm every order. Perps trading is
  **manual only**: no setup proved good enough to automate leveraged trades.

## Layout

```
web/            UI + all trading logic (plain JS, bundled into the APK as assets)
  js/model.js   pricing, fees, sizing
  js/bot.js     trading loop, risk limits, settlement, calibration
  js/api.js     Kalshi, Kalshi Perps and Coinbase clients (requests go through the Android bridge)
  js/indicators.js  indicators + setup detectors (also used by the backtest)
  js/perps.js   paper perps engine; js/perps_ui.js the Perps tab
android/        minimal Java shell: WebView, request signing, Keystore-encrypted key storage, foreground service
research/       data fetcher, walk-forward backtest, results
test/           node tests for model/bot + a cross-check of the Java RSA-PSS signer
```

The private key is only handled in Java (`KeyVault`, `KalshiSigner`). It is encrypted with an Android Keystore AES key,
and the native bridge only talks to Kalshi and Coinbase hosts.

## Develop

```
node --test test/*.test.js          # model + bot tests
open web/index.html                  # UI in a desktop browser (paper data only)
gradle -p android assembleDebug      # APK (needs the Android SDK; CI does this for you)
```

The GitHub Action `.github/workflows/kalshi-btc-bot-apk.yml` runs the tests, builds the APK, and publishes it as a pre-release.

## Known limits

- The bot only runs while the app process is alive. The foreground service and screen-on flag help, but Android can still kill it.
  For true 24/7 trading, run the same logic on a small server.
- Field names in Kalshi's API change occasionally. Demo mode is there to catch that before real money is involved.
