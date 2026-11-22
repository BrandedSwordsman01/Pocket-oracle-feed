"""SQLite storage (one file: data/engine.db). Local ingestion time `ts` (seconds) on every row."""
import json, os, sqlite3, threading, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (           -- one training row per open contract every ~10 s
  ts REAL, ticker TEXT, series TEXT, close_ts INTEGER, strike REAL, kind TEXT,
  bid REAL, ask REAL, feats TEXT, p_base REAL, p_model REAL, y INTEGER DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS rows_ticker ON rows(ticker);
CREATE INDEX IF NOT EXISTS rows_ts ON rows(ts);
CREATE TABLE IF NOT EXISTS outcomes (ticker TEXT PRIMARY KEY, result TEXT, expiration_value REAL, ts REAL);
CREATE TABLE IF NOT EXISTS models (ts REAL, version TEXT, active INTEGER, metrics TEXT);
CREATE TABLE IF NOT EXISTS forecasts (ts REAL, close_ts INTEGER, spot REAL, q05 REAL, q25 REAL, q50 REAL, q75 REAL, q95 REAL, realized REAL DEFAULT NULL);
CREATE TABLE IF NOT EXISTS anchors (open_ts INTEGER PRIMARY KEY, strike REAL, comp_at_open REAL);
"""


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript(SCHEMA)
        self.lock = threading.Lock()

    def add_row(self, r):
        with self.lock:
            self.c.execute("INSERT INTO rows(ts,ticker,series,close_ts,strike,kind,bid,ask,feats,p_base,p_model) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (r["ts"], r["ticker"], r["series"], r["close_ts"], r["strike"], r["kind"], r["bid"], r["ask"],
                            json.dumps(r["feats"]), r["p_base"], r.get("p_model")))
            self.c.commit()

    def set_outcome(self, ticker, result, ev):
        y = 1 if result == "yes" else 0
        with self.lock:
            self.c.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?)", (ticker, result, ev, time.time()))
            self.c.execute("UPDATE rows SET y=? WHERE ticker=? AND y IS NULL", (y, ticker))
            self.c.commit()

    def pending_tickers(self, before_ts):
        with self.lock:
            q = self.c.execute("SELECT DISTINCT ticker FROM rows WHERE y IS NULL AND close_ts < ?", (before_ts,))
            return [r[0] for r in q.fetchall()]

    def labeled(self, since_ts=0):
        with self.lock:
            q = self.c.execute("SELECT ts,ticker,series,close_ts,strike,kind,bid,ask,feats,p_base,y FROM rows WHERE y IS NOT NULL AND ts>=? ORDER BY ts", (since_ts,))
            return q.fetchall()

    def counts(self):
        with self.lock:
            a = self.c.execute("SELECT COUNT(*), SUM(y IS NOT NULL), COUNT(DISTINCT ticker) FROM rows").fetchone()
            first = self.c.execute("SELECT MIN(ts) FROM rows").fetchone()[0]
        return dict(rows=a[0] or 0, labeled=a[1] or 0, contracts=a[2] or 0, first_ts=first)

    def save_model(self, version, active, metrics):
        with self.lock:
            self.c.execute("INSERT INTO models VALUES (?,?,?,?)", (time.time(), version, int(active), json.dumps(metrics)))
            self.c.commit()

    def last_model(self):
        with self.lock:
            r = self.c.execute("SELECT ts,version,active,metrics FROM models ORDER BY ts DESC LIMIT 1").fetchone()
        return None if not r else dict(ts=r[0], version=r[1], active=bool(r[2]), metrics=json.loads(r[3]))

    def add_forecasts(self, spot, horizons):
        with self.lock:
            for h in horizons:
                self.c.execute("INSERT INTO forecasts(ts,close_ts,spot,q05,q25,q50,q75,q95) VALUES (?,?,?,?,?,?,?,?)",
                               (time.time(), h["close_ts"], spot, h["q05"], h["q25"], h["q50"], h["q75"], h["q95"]))
            self.c.commit()

    def fill_realized(self, close_ts, price):
        with self.lock:
            self.c.execute("UPDATE forecasts SET realized=? WHERE close_ts=? AND realized IS NULL", (price, close_ts))
            self.c.commit()

    def forecast_accuracy(self):
        """Share of realised prices inside the 50% and 90% ranges (ideal 0.50 / 0.90)."""
        with self.lock:
            r = self.c.execute("SELECT COUNT(*), AVG(realized BETWEEN q25 AND q75), AVG(realized BETWEEN q05 AND q95) FROM forecasts WHERE realized IS NOT NULL").fetchone()
        return dict(n=r[0] or 0, inside50=r[1], inside90=r[2])

    def set_anchor(self, open_ts, strike, comp):
        with self.lock:
            self.c.execute("INSERT OR IGNORE INTO anchors VALUES (?,?,?)", (open_ts, strike, comp))
            self.c.commit()

    def anchor(self, open_ts):
        with self.lock:
            r = self.c.execute("SELECT strike, comp_at_open FROM anchors WHERE open_ts=?", (open_ts,)).fetchone()
        return r
