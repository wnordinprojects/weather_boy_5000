"""SQLite log. Every forecast, decision, order and settlement lands here."""
import json
import os
import sqlite3
import threading
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY, ts REAL, balance REAL, n_markets INT, n_orders INT,
  edge_threshold REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, ts REAL, series TEXT, target_date TEXT, kind TEXT,
  median REAL, spread REAL, observed REAL, locked INT, notes TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, cycle_id INT, ticker TEXT, event_ticker TEXT, series TEXT,
  outcome TEXT, p_model REAL, price REAL, fee REAL, edge REAL, threshold REAL,
  saturated INT, action TEXT, count INT, reason TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, ts REAL, ticker TEXT, event_ticker TEXT, series TEXT, outcome TEXT,
  count INT, yes_price REAL, p_model REAL, edge REAL, order_id TEXT, fill_count REAL,
  avg_fill REAL, raw TEXT);
CREATE TABLE IF NOT EXISTS settlements (
  ticker TEXT PRIMARY KEY, ts REAL, event_ticker TEXT, series TEXT, result TEXT,
  outcome TEXT, count INT, avg_fill REAL, p_model REAL, edge REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS calibration (
  series TEXT PRIMARY KEY, bias_f REAL, n INT, updated REAL);
CREATE TABLE IF NOT EXISTS bench (series TEXT PRIMARY KEY, until REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS skips (id INTEGER PRIMARY KEY, ts REAL, ticker TEXT, outcome TEXT, reason TEXT);
"""
MIGRATIONS = [
    "ALTER TABLE forecasts ADD COLUMN fan_json TEXT",
    "ALTER TABLE forecasts ADD COLUMN obs_json TEXT",
    "ALTER TABLE forecasts ADD COLUMN pct_json TEXT",
]


class DB:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                self.c.execute(m)
            except sqlite3.OperationalError:
                pass  # column exists
        self.c.commit()

    def _ins(self, table, **kw):
        cols = ",".join(kw)
        q = ",".join("?" * len(kw))
        with self.lock:
            cur = self.c.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({q})", list(kw.values()))
            self.c.commit()
            return cur.lastrowid

    # state -----------------------------------------------------------------
    def get_state(self, key, default=None):
        with self.lock:
            r = self.c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_state(self, key, value):
        self._ins("state", key=key, value=json.dumps(value))

    # writes ----------------------------------------------------------------
    def cycle(self, **kw):       return self._ins("cycles", ts=time.time(), **kw)
    def forecast(self, **kw):    return self._ins("forecasts", ts=time.time(), **kw)
    def decision(self, **kw):    return self._ins("decisions", ts=time.time(), **kw)
    def order(self, **kw):       return self._ins("orders", ts=time.time(), **kw)
    def settlement(self, **kw):  return self._ins("settlements", ts=time.time(), **kw)
    def skip(self, **kw):        return self._ins("skips", ts=time.time(), **kw)

    def bias(self, series):
        r = self.c.execute("SELECT bias_f FROM calibration WHERE series=?", (series,)).fetchone()
        return float(r["bias_f"]) if r else config.DEFAULT_MAX_BIAS_F

    def set_bias(self, series, bias_f, n):
        self._ins("calibration", series=series, bias_f=bias_f, n=n, updated=time.time())

    def benched(self, series):
        r = self.c.execute("SELECT until FROM bench WHERE series=?", (series,)).fetchone()
        return bool(r and r["until"] > time.time())

    def bench_series(self, series, days, reason):
        self._ins("bench", series=series, until=time.time() + days * 86400, reason=reason)

    # reads -----------------------------------------------------------------
    def open_order_tickers(self):
        return {r["ticker"] for r in self.c.execute("SELECT DISTINCT ticker FROM orders WHERE fill_count > 0")}

    def order_history(self, ticker):
        return [dict(r) for r in self.c.execute(
            "SELECT * FROM orders WHERE ticker=? AND fill_count > 0 ORDER BY ts", (ticker,))]

    def unsettled_tickers(self):
        return [r["ticker"] for r in self.c.execute(
            "SELECT DISTINCT o.ticker FROM orders o LEFT JOIN settlements s ON s.ticker=o.ticker "
            "WHERE o.fill_count > 0 AND s.ticker IS NULL")]

    def recent_settlements(self, series=None, n=50):
        q = "SELECT * FROM settlements" + (" WHERE series=?" if series else "") + " ORDER BY ts DESC LIMIT ?"
        args = (series, n) if series else (n,)
        return [dict(r) for r in self.c.execute(q, args)]

    def rows(self, q, args=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(q, args)]
