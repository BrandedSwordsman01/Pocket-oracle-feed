# BTC Kalshi Bot (Android)

An Android app that trades Kalshi's Bitcoin price markets (series `KXBTCD` by default)
using a simple, transparent pricing model. It runs paper trading by default.

> **Read this first.** The model is the textbook approach, and professional market makers on Kalshi
> run the same math with faster data. Expect it to roughly break even before fees
> and lose a little after them, unless the calibration panel shows otherwise. Never trade money you
> can't lose.

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

For a contract "BTC above $K at time T":

- Volatility comes from an EWMA (60-minute half-life) of Coinbase 1-minute log returns, inflated 15% for humility.
- Price moves until expiry are modelled as zero drift with fat tails (Student-t, 4 degrees of freedom, scaled to the same variance).
- Fair value: `P(YES) = 1 − F(ln(K/S) / (σ·√minutes))`. Range ("between") and "below" markets are handled too.
- It buys YES or NO only when `fair − ask − Kalshi taker fee ≥ 4¢`. Edges over 25¢ are treated as bad data
  and skipped. Contract prices outside 8–92¢ are skipped.
- Sizing is 15% fractional Kelly, capped by the per-trade and total-exposure limits. Positions are held to settlement.
- It only trades between 10 and 90 minutes before close. Kalshi settles on a 60-second average of the
  CF Benchmarks index, and Coinbase spot is only a proxy for that index.

Where an edge could come from: the market misjudging short-term volatility or reacting slowly to fast BTC moves.
Where losses come from: fees, the spread, better-informed traders, and the gap between Coinbase spot and the settlement index.

## Layout

```
web/            UI + all trading logic (plain JS, bundled into the APK as assets)
  js/model.js   pricing, fees, sizing
  js/bot.js     trading loop, risk limits, settlement, calibration
  js/api.js     Kalshi + Coinbase clients (requests go through the Android bridge)
android/        minimal Java shell: WebView, request signing, Keystore-encrypted key storage, foreground service
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
