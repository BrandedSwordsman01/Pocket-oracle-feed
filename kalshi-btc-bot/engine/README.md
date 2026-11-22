# Kalshi Engine (runs on your Windows PC)

Collects live Bitcoin market data 24/7 (Coinbase, Kraken and Bitstamp order books and trades, Kalshi's 15-minute and
hourly order books, and the Kalshi BTC perp), learns from it, and serves a fresh prediction for every open bet every
2 seconds to the phone app.

**It does not place bets.** The phone app decides and trades. Your Kalshi key stays on the phone.

## Install (about 5 minutes)

1. Install **Python 3.12** from https://www.python.org/downloads/. On the first installer screen, tick **"Add python.exe to PATH"**.
2. Download this repository (GitHub → Code → Download ZIP) and unzip it anywhere, e.g. `Documents\kalshi`.
3. Open `kalshi-btc-bot\engine` and double-click **`setup.bat`**. Wait for "Setup complete".
4. Double-click **`run_engine.bat`**. It prints an address like `http://192.168.1.23:8765`. Leave the window open (minimise it).
   If Windows Firewall asks, click **Allow** (Private networks).
5. Optional: double-click **`start_with_windows.bat`** so the engine starts automatically when you log in.

## Connect the phone

The phone must be on the **same Wi-Fi** as the PC. In the app go to Settings → **PC engine**, type the address from step 4 and tap **Test**.
You can also open that address in the phone's browser to see the engine's status page.

## What to expect

| Time running | What happens |
|---|---|
| First minutes | Feeds connect; predictions start (market-anchored fallback, marked "learning") |
| ~3–7 days | Enough settled 15-minute bets to train the first model |
| Every 6 hours | Retrains on all data. The learned model goes live only if it beats Kalshi's prices on recent unseen data **and** shows a profit after fees there |

While it says **learning**, the phone bot will not bet on its numbers. Data is stored in `%USERPROFILE%\KalshiEngine\engine.db`.

GPU: optional. Start with `run_engine.bat --gpu` to train on your graphics card (LightGBM GPU build needed). The CPU is fast enough.
