"""Kalshi Engine entry point: python -m kbengine  (Windows: run_engine.bat)

Runs: exchange websockets, Kalshi pollers, a 2-second prediction loop, nightly training, and the HTTP API the phone reads.
No orders are ever placed from the PC; the phone app decides and trades.
"""
import argparse, asyncio, logging, os, socket, time, threading
from .db import DB
from . import feeds, features, model as M, forecast as FC

log = logging.getLogger("engine")


class Engine:
    def __init__(self, data_dir, use_gpu=False):
        self.db = DB(os.path.join(data_dir, "engine.db"))
        self.state = feeds.State()
        self.model = M.Model(os.path.join(data_dir, "model"))
        self.use_gpu = use_gpu
        self.latest = {}          # ticker -> prediction dict (served to the phone)
        self.started = time.time()
        self.last_train = 0
        self.training = False
        self.fc = {}              # latest multi-hour forecast + ladder scan
        self.seed_minutes = []    # (minute_ts, close) seeded from Coinbase REST until live history is long enough

    def predict_all(self, record=False):
        out = {}
        contracts = ([self.state.m15] if self.state.m15 else []) + list(self.state.hourly)
        for k in contracts:
            if not k:
                continue
            open_ts = k["close_ts"] - 900 if k["series"] == "KXBTC15M" else None
            anchor = self.db.anchor(open_ts) if open_ts else self._latest_anchor()
            b = features.build(self.state, k, anchor)
            if not b:
                continue
            p, learned = self.model.predict(b["feats"], b["mid"], b["p_base"])
            out[k["ticker"]] = dict(ticker=k["ticker"], series=k["series"], strike=k["strike"], kind=k["kind"], close_ts=k["close_ts"],
                                    bid=k["bid"], ask=k["ask"], p_market=b["mid"], p_base=b["p_base"], p_engine=p, learned=learned,
                                    centre=b["centre"], secs_left=b["secs_left"], ts=time.time())
            if record:
                self.db.add_row(dict(ts=time.time(), ticker=k["ticker"], series=k["series"], close_ts=k["close_ts"], strike=k["strike"],
                                     kind=k["kind"], bid=k["bid"], ask=k["ask"], feats=b["feats"], p_base=b["p_base"], p_model=p))
        self.latest = out

    def _latest_anchor(self):
        open_ts = (int(time.time()) // 900) * 900
        return self.db.anchor(open_ts)

    async def predict_loop(self):
        n = 0
        while True:
            self.state.tick_second()
            try:
                self.predict_all(record=(n % 5 == 0))   # predictions every 2 s, training rows every 10 s
            except Exception as e:
                self.state.errors.append(f"predict: {e}")
            n += 1
            await asyncio.sleep(2)

    async def second_loop(self):
        while True:
            self.state.tick_second()
            await asyncio.sleep(1)

    def train_now(self):
        if self.training:
            return {"status": "already training"}
        self.training = True
        try:
            m = self.model.train(self.db.labeled(), use_gpu=self.use_gpu)
            self.db.save_model(time.strftime("%Y%m%d-%H%M"), self.model.active, m)
            self.last_train = time.time()
            log.info("training: %s", m.get("status"))
            return m
        finally:
            self.training = False

    async def train_loop(self):
        while True:
            await asyncio.sleep(600)
            if time.time() - self.last_train > 6 * 3600:       # retrain every 6 hours
                await asyncio.get_running_loop().run_in_executor(None, self.train_now)

    def minute_series(self):
        mins = {}
        for s, p in self.state.comp_hist:
            mins[s // 60 * 60] = p
        live = sorted(mins.items())
        seed = [x for x in self.seed_minutes if not live or x[0] < live[0][0]]
        both = seed + live
        return [t for t, _ in both], [p for _, p in both]

    async def forecast_loop(self):
        loop = asyncio.get_running_loop()
        try:
            c = await loop.run_in_executor(None, feeds.http_json, "https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 60})
            self.seed_minutes = sorted((int(x[0]), float(x[4])) for x in c)
        except Exception as e:
            self.state.errors.append(f"seed candles: {e}")
        last_realized = 0
        while True:
            try:
                spot = self.state.composite()
                if spot:
                    now = time.time()
                    hours = [(int(now) // 3600 + k) * 3600 for k in range(1, 13)]
                    ts, ps = self.minute_series()
                    res = await loop.run_in_executor(None, FC.forecast, spot, ps, ts, hours)
                    nowS = int(now)
                    # every open hourly/daily ladder closing in the next 26 hours (paged)
                    allm, cursor = [], ""
                    for _ in range(4):
                        lad = await loop.run_in_executor(None, feeds.http_json, feeds.KALSHI + "/markets",
                                                         {"series_ticker": "KXBTCD", "status": "open", "limit": 1000, "cursor": cursor,
                                                          "min_close_ts": nowS + 60, "max_close_ts": nowS + 26 * 3600})
                        allm += lad.get("markets", []); cursor = lad.get("cursor") or ""
                        if not cursor:
                            break
                    closes = sorted({feeds.iso_ts(m["close_time"]) for m in allm if m.get("close_time")})
                    hours = sorted(set(hours) | set(closes))
                    res = await loop.run_in_executor(None, FC.forecast, spot, ps, ts, hours)
                    mkts = []
                    for m in allm:
                        try:
                            if m.get("strike_type") != "greater":
                                continue
                            mkts.append(dict(ticker=m["ticker"], close_ts=feeds.iso_ts(m["close_time"]), strike=float(m["floor_strike"]),
                                             bid=float(m.get("yes_bid_dollars") or 0), ask=float(m.get("yes_ask_dollars") or 0)))
                        except (TypeError, ValueError, KeyError):
                            continue
                    scan = FC.ladder_scan(res["samples"], mkts, M.fee)
                    self.fc = dict(spot=spot, generated=res.get("generated"), gpu=res.get("gpu"), vol_ann=res.get("vol_ann"),
                                   horizons=res["horizons"], ladder_top=scan[:12], ladder_count=len(scan))
                    self.db.add_forecasts(spot, res["horizons"])
                    # fill realised prices for past forecast horizons (our composite at that hour close)
                    if now - last_realized > 300:
                        last_realized = now
                        for s_, p_ in list(self.state.comp_hist):
                            if s_ % 3600 == 0:
                                self.db.fill_realized(s_, p_)
            except Exception as e:
                self.state.errors.append(f"forecast: {e}")
                if "429" in str(e):
                    await asyncio.sleep(60)
            await asyncio.sleep(120 if self.fc else 15)   # retry quickly until the first forecast exists

    def status(self):
        c = self.db.counts()
        return dict(
            version="0.1.0", uptime_s=int(time.time() - self.started),
            feeds={n: dict(connected=b.connected, mid=b.mid(), age_s=round(time.time() - b.updated, 1) if b.updated else None)
                   for n, b in self.state.books.items()},
            composite=self.state.composite(), kalshi_15m=self.state.m15["ticker"] if self.state.m15 else None,
            hourly_strikes=len(self.state.hourly), perp=self.state.perp,
            data=c, days_collected=round((time.time() - c["first_ts"]) / 86400, 2) if c["first_ts"] else 0,
            model=dict(active=self.model.active, metrics=self.model.metrics, need_rows=M.MIN_ROWS),
            forecast=dict(gpu=FC.GPU, accuracy=self.db.forecast_accuracy(), generated=self.fc.get("generated")),
            mode="learned model" if self.model.active else "learning (market-anchored fallback; phone bot will not bet on it)",
            recent_errors=list(self.state.errors)[-5:])


def make_app(engine):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    app = FastAPI(title="Kalshi Engine")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.get("/api/predictions")
    def predictions():
        return dict(ts=time.time(), active=engine.model.active, predictions=list(engine.latest.values()))

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.get("/api/forecast")
    def forecast():
        return dict(engine.fc, note="Forecasts how far Bitcoin may move, not which way. Kalshi's hourly ladder priced movement about right in our 30-day test, so ladder gaps are for reference.")

    @app.post("/api/train")
    def train():
        return engine.train_now()

    @app.get("/")
    def home():
        from fastapi.responses import HTMLResponse
        s = engine.status()
        rows = "".join(f"<tr><td>{p['ticker']}</td><td>{p['p_market']:.2f}</td><td>{p['p_engine']:.2f}</td><td>{'learned' if p['learned'] else 'fallback'}</td></tr>"
                       for p in engine.latest.values())
        return HTMLResponse(f"""<html><head><meta name=viewport content='width=device-width'><meta http-equiv=refresh content=5>
        <style>body{{font-family:system-ui;background:#131722;color:#d1d4dc;padding:16px}}td,th{{padding:4px 10px;text-align:left}}</style></head>
        <body><h2>Kalshi Engine</h2><p>{s['mode']}</p><p>Data: {s['data']['rows']} rows, {s['data']['labeled']} settled, {s['days_collected']} days</p>
        <p>Feeds: {', '.join(f"{n} {'✓' if v['connected'] else '✗'}" for n, v in s['feeds'].items())} · 15-min: {s['kalshi_15m']}</p>
        <table><tr><th>Market</th><th>Kalshi</th><th>Engine</th><th>Source</th></tr>{rows}</table>
        <p>Model: {s['model']['metrics'].get('status', 'not trained yet')}</p></body></html>""")
    return app


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.expanduser("~"), "KalshiEngine"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gpu", action="store_true", help="train LightGBM on the GPU (optional)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    eng = Engine(a.data, a.gpu)

    async def run():
        tasks = feeds.exchange_tasks(eng.state) + [feeds.kalshi_loop(eng.state, eng.db), feeds.perp_loop(eng.state),
                                                    feeds.settle_loop(eng.state, eng.db), eng.predict_loop(), eng.second_loop(), eng.train_loop(),
                                                    eng.forecast_loop()]
        await asyncio.gather(*tasks)

    threading.Thread(target=lambda: asyncio.run(run()), daemon=True).start()
    import uvicorn
    print(f"\n  Kalshi Engine running.  On your phone (same Wi-Fi) enter:  http://{lan_ip()}:{a.port}\n  Data folder: {a.data}\n")
    uvicorn.run(make_app(eng), host="0.0.0.0", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
