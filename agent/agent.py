"""Main loop: discover weather events, forecast, trade, reconcile, adapt."""
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests

from . import config
from .db import DB
from .kalshi import Kalshi, KalshiError
from .strategy import plan_orders, available_at
from .weather import build_forecast, fetch_observations, observed_extreme

log = logging.getLogger("agent")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(ev) -> date | None:
    sd = ev.get("strike_date")
    tick = ev.get("event_ticker", "")
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", tick)
    if m:
        return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))
    if sd:
        return datetime.fromisoformat(sd.replace("Z", "+00:00")).date()
    return None


def series_kind(series):
    return "low" if series.startswith("KXLOWT") else "high"


class Agent:
    def __init__(self):
        self.k = Kalshi()
        self.db = DB()
        self.http = requests.Session()
        self.errors = 0
        self.halted = False
        self.threshold = self.db.get_state("edge_threshold", config.EDGE_THRESHOLD)
        self.active_series = list(config.SERIES)
        self.last_status = {}

    # ---- one cycle --------------------------------------------------------
    def cycle(self):
        t0 = time.time()
        st = self.k.exchange_status()
        if not st.get("trading_active", True):
            log.info("exchange not trading; skipping")
            return
        bankroll = self.k.balance()
        pos_rows = self.k.positions()
        positions = {}
        event_spent = {}
        for r in pos_rows:
            n = float(r.get("position_fp") or r.get("position") or 0)
            if n == 0:
                continue
            positions[r["ticker"]] = n
            ev = r["ticker"].rsplit("-", 1)[0]
            event_spent[ev] = event_spent.get(ev, 0.0) + abs(float(r.get("market_exposure_dollars") or 0))

        n_markets = n_orders = 0
        cycle_id = self.db.cycle(balance=bankroll, n_markets=0, n_orders=0,
                                 edge_threshold=self.threshold, notes="")
        fc_cache = {}
        for series in list(self.active_series):
            if self.db.benched(series):
                continue
            try:
                events = self.k.events(series, status="open", nested=True)
            except KalshiError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    log.info("series %s not found; dropping", series)
                    self.active_series.remove(series)
                    continue
                raise
            for ev in events:
                tgt = event_date(ev)
                if not tgt:
                    continue
                kind = series_kind(series)
                meta = config.SERIES[series]
                today = datetime.now(ZoneInfo(meta["tz"])).date()
                if tgt < today or tgt > today + timedelta(days=2):
                    continue  # ensembles beyond 2-3 days add noise, not edge
                key = (series, tgt)
                if key not in fc_cache:
                    try:
                        fc_cache[key] = build_forecast(series, tgt, kind, self.db.bias(series), self.http)
                        fc = fc_cache[key]
                        self.db.forecast(series=series, target_date=tgt.isoformat(), kind=kind,
                                         median=fc.median, spread=fc.spread, observed=fc.observed_extreme,
                                         locked=int(fc.locked), notes="; ".join(fc.notes))
                        log.info("%s %s %s: median %.1f spread %.1f %s", series, tgt, kind,
                                 fc.median, fc.spread, fc.notes)
                    except Exception as e:
                        log.warning("forecast failed %s %s: %s", series, tgt, e)
                        continue
                fc = fc_cache[key]
                markets = [m for m in (ev.get("markets") or []) if m.get("status", "open") in ("open", "active")]
                if not markets:
                    markets = self.k.markets(event_ticker=ev["event_ticker"])
                n_markets += len(markets)
                rules = (markets[0].get("rules_primary") or "") if markets else ""
                if rules and meta["station"] not in rules and meta["city"].split()[0] not in rules:
                    log.warning("rules for %s do not mention %s: %s", ev["event_ticker"], meta["station"], rules[:200])
                orders, decisions = plan_orders(fc, markets, bankroll, positions, self.threshold,
                                                event_spent.get(ev["event_ticker"], 0.0))
                for d in decisions:
                    self.db.decision(cycle_id=cycle_id, series=series, **d)
                for o in orders:
                    filled = self.execute(o, series)
                    n_orders += filled
                    if filled and o["action"] == "buy" and not config.DRY_RUN:
                        bankroll -= o["count"] * (o["yes_price"] if o["outcome"] == "yes" else 1 - o["yes_price"])
        self.db.c.execute("UPDATE cycles SET n_markets=?, n_orders=?, notes=? WHERE id=?",
                          (n_markets, n_orders, f"{time.time()-t0:.1f}s", cycle_id))
        self.db.c.commit()
        self.last_status = dict(ts=time.time(), balance=bankroll, markets=n_markets, orders=n_orders,
                                threshold=self.threshold, halted=self.halted)
        log.info("cycle done: balance %.2f markets %d orders %d thr %.3f", bankroll, n_markets, n_orders, self.threshold)

    def execute(self, o, series):
        # Top-of-book fields can be stale or empty. Size against the live book.
        try:
            ob = self.k.orderbook(o["ticker"], depth=10)
            avail = available_at(ob, o["outcome"], o["yes_price"])
        except KalshiError as e:
            log.warning("orderbook %s: %s", o["ticker"], e)
            avail = 0
        if avail <= 0:
            log.info("skip %s %s: no liquidity at %.2f", o["outcome"], o["ticker"], o["yes_price"])
            return 0
        if avail < o["count"]:
            log.info("cap %s %s: %d -> %d (book depth)", o["outcome"], o["ticker"], o["count"], avail)
            o["count"] = avail
        try:
            r = self.k.place(o["ticker"], o["outcome"], o["count"], o["yes_price"])
        except KalshiError as e:
            log.error("order failed %s: %s", o["ticker"], e)
            self.db.order(series=series, ticker=o["ticker"], event_ticker=o["event_ticker"],
                          outcome=o["outcome"], count=o["count"], yes_price=o["yes_price"],
                          p_model=o["p_model"], edge=o["edge"], order_id="ERR", fill_count=0,
                          avg_fill=None, raw=str(e)[:500])
            raise
        fill = float(r.get("fill_count") or 0)
        avg = r.get("average_fill_price")
        avg = float(avg) if avg not in (None, "") else None
        self.db.order(series=series, ticker=o["ticker"], event_ticker=o["event_ticker"],
                      outcome=o["outcome"], count=o["count"], yes_price=o["yes_price"],
                      p_model=o["p_model"], edge=o["edge"], order_id=r.get("order_id"),
                      fill_count=fill, avg_fill=avg, raw=str(r)[:500])
        log.info("%s %s %s x%d @ yes %.2f (p=%.2f edge=%.3f) filled %.0f", o["action"], o["outcome"],
                 o["ticker"], o["count"], o["yes_price"], o["p_model"], o["edge"], fill)
        return 1 if fill > 0 or r.get("dry_run") else 0

    # ---- reconcile settled markets, learn --------------------------------
    def reconcile(self):
        for ticker in self.db.unsettled_tickers():
            try:
                m = self.k.market(ticker)
            except KalshiError as e:
                log.warning("market %s: %s", ticker, e)
                continue
            if m.get("status") != "settled" or not m.get("result"):
                continue
            hist = self.db.order_history(ticker)
            if not hist:
                continue
            series = hist[0]["series"]
            result = m["result"]  # 'yes' | 'no'
            pnl = 0.0
            count = 0
            cost = 0.0
            for h in hist:
                n = h["fill_count"]
                px = h["avg_fill"] if h["avg_fill"] is not None else (
                    h["yes_price"] if h["outcome"] == "yes" else 1 - h["yes_price"])
                # avg_fill is on the YES scale; cost of a NO contract is 1 - price.
                c = px if h["outcome"] == "yes" else 1 - px
                win = 1.0 if h["outcome"] == result else 0.0
                pnl += n * (win - c)
                count += n
                cost += n * c
            self.db.settlement(ticker=ticker, event_ticker=hist[0]["event_ticker"], series=series,
                               result=result, outcome=hist[-1]["outcome"], count=count,
                               avg_fill=cost / count if count else None, p_model=hist[-1]["p_model"],
                               edge=hist[-1]["edge"], pnl=round(pnl, 2))
            log.info("settled %s -> %s pnl %.2f", ticker, result, pnl)
        self.adapt()
        self.calibrate()

    def adapt(self):
        """Move the edge threshold with realized results; bench series that lose."""
        recent = self.db.recent_settlements(n=30)
        if len(recent) >= 10:
            realized = sum(r["pnl"] for r in recent) / max(1, sum(r["count"] for r in recent))
            predicted = float(np.mean([r["edge"] for r in recent]))
            thr = self.threshold
            if realized < 0:
                thr += 0.01
            elif realized > 0.5 * predicted:
                thr -= 0.005
            self.threshold = float(min(config.EDGE_MAX, max(config.EDGE_MIN, thr)))
            self.db.set_state("edge_threshold", self.threshold)
        for series in list(config.SERIES):
            rs = self.db.recent_settlements(series, config.ROTATION_WINDOW)
            if len(rs) >= config.ROTATION_WINDOW and sum(r["pnl"] for r in rs) < 0 and not self.db.benched(series):
                self.db.bench_series(series, config.ROTATION_BENCH_DAYS, f"negative pnl over last {len(rs)}")
                log.info("benched %s for %d days", series, config.ROTATION_BENCH_DAYS)

    def calibrate(self):
        """Learn per-station bias from yesterday's observed extreme vs our pre-day forecast."""
        for series, meta in config.SERIES.items():
            z = ZoneInfo(meta["tz"])
            yday = datetime.now(z).date() - timedelta(days=1)
            done = self.db.get_state(f"cal:{series}", "")
            if done == yday.isoformat():
                continue
            rows = self.db.rows("SELECT median FROM forecasts WHERE series=? AND target_date=? AND observed IS NULL "
                                "ORDER BY ts DESC LIMIT 1", (series, yday.isoformat()))
            if not rows:
                continue
            try:
                start = datetime.combine(yday, datetime.min.time(), z).astimezone(ZoneInfo("UTC"))
                obs = fetch_observations(meta["station"], start - timedelta(hours=1), self.http)
                truth, n = observed_extreme(obs, yday, meta["tz"], series_kind(series))
            except Exception as e:
                log.warning("calibration obs %s: %s", series, e)
                continue
            if truth is None or n < 12:
                continue
            old = self.db.bias(series)
            raw_median = rows[0]["median"] - (old if series_kind(series) == "high" else -old)
            err = truth - raw_median if series_kind(series) == "high" else raw_median - truth
            new = 0.8 * old + 0.2 * err          # EMA; sign already oriented per kind
            self.db.set_bias(series, float(new), 0)
            self.db.set_state(f"cal:{series}", yday.isoformat())
            log.info("calibrated %s: truth %.1f model %.1f bias %.2f -> %.2f", series, truth, raw_median, old, new)

    # ---- loop -------------------------------------------------------------
    def run_forever(self):
        log.info("starting. base=%s dry_run=%s cycle=%ss", config.KALSHI_BASE, config.DRY_RUN, config.CYCLE_SECONDS)
        while True:
            if self.halted:
                # Bug guard: back off for an hour rather than hammer a broken API, then retry.
                log.error("HALTED after %d consecutive errors; retrying in 1h", self.errors)
                time.sleep(3600)
                self.halted, self.errors = False, 0
            try:
                self.reconcile()
                self.cycle()
                self.errors = 0
            except Exception as e:
                self.errors += 1
                log.exception("cycle error %d: %s", self.errors, e)
                if self.errors >= config.MAX_CONSECUTIVE_ERRORS:
                    self.halted = True
            time.sleep(config.CYCLE_SECONDS)


def main():
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    agent = Agent()
    from .dashboard import serve
    threading.Thread(target=serve, args=(agent,), daemon=True).start()
    agent.run_forever()


if __name__ == "__main__":
    main()
