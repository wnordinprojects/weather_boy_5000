"""Main loop: discover weather events, forecast, trade, reconcile, adapt."""
import json
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
from .strategy import plan_orders, available_at, fee
from .weather import build_forecast, fetch_observations, observed_extreme
from .history import calibrate_all, station_calibration
from .discovery import discover

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
        self.model_weight = self.db.get_state("model_weight", config.MODEL_WEIGHT)
        self.active_series = list(config.SERIES)
        self._dropped = set()
        self.last_status = {}

    def sync_orders(self):
        """Pull fill status for our recent non-final orders (resting makers fill after we placed them)."""
        for o in self.db.open_orders():
            try:
                r = self.k.order(o["order_id"])
            except KalshiError as e:
                log.warning("order %s: %s", o["order_id"], e)
                continue
            if not r:
                continue
            fill = float(r.get("fill_count_fp") or r.get("fill_count") or 0)
            status = r.get("status") or "resting"
            self.db.update_order(o["order_id"], fill, None, status)
            if fill != (o["fill_count"] or 0):
                log.info("order %s %s: filled %.0f/%s (%s)", o["outcome"], o["ticker"], fill, o["count"], status)

    def pending(self):
        """Resting orders as signed contract counts and dollars, so sizing treats them as held."""
        pend, spent = {}, {}
        try:
            rows = self.k.resting()
        except KalshiError as e:
            log.warning("resting orders: %s", e)
            return pend, spent
        for r in rows:
            n = float(r.get("remaining_count_fp") or r.get("remaining_count") or 0)
            if n <= 0:
                continue
            t = r.get("ticker")
            outcome = r.get("outcome_side") or ("yes" if r.get("book_side") == "bid" else "no")
            px = float(r.get("yes_price_dollars") or 0)
            cost = px if outcome == "yes" else 1 - px
            pend[t] = pend.get(t, 0) + (n if outcome == "yes" else -n)
            ev = t.rsplit("-", 1)[0]
            spent[ev] = spent.get(ev, 0.0) + n * cost
        return pend, spent

    # ---- one cycle --------------------------------------------------------
    def cycle(self):
        t0 = time.time()
        st = self.k.exchange_status()
        if not st.get("trading_active", True):
            log.info("exchange not trading; skipping")
            return
        self.sync_orders()
        bankroll = self.k.balance()
        pos_rows = self.k.positions()
        positions = {}
        event_spent = {}
        costs = {}
        for r in pos_rows:
            n = float(r.get("position_fp") or r.get("position") or 0)
            if n == 0:
                continue
            positions[r["ticker"]] = n
            hist = self.db.order_history(r["ticker"])
            filled = sum(h["fill_count"] for h in hist)
            if filled:
                costs[r["ticker"]] = sum(((h["avg_fill"] if h["avg_fill"] is not None else h["yes_price"])
                                          if h["outcome"] == "yes" else
                                          1 - (h["avg_fill"] if h["avg_fill"] is not None else h["yes_price"]))
                                         * h["fill_count"] for h in hist) / filled
            ev = r["ticker"].rsplit("-", 1)[0]
            event_spent[ev] = event_spent.get(ev, 0.0) + abs(float(r.get("market_exposure_dollars") or 0))

        pend, pend_spent = self.pending()
        for t, n in pend.items():
            positions[t] = positions.get(t, 0) + n
        for ev, d in pend_spent.items():
            event_spent[ev] = event_spent.get(ev, 0.0) + d
        n_markets = n_orders = 0
        cycle_id = self.db.cycle(balance=bankroll, n_markets=0, n_orders=0,
                                 edge_threshold=self.threshold, notes="")
        fc_cache = {}
        self.active_series = [t for t in config.SERIES if t not in getattr(self, "_dropped", set())]
        for series in list(self.active_series):
            if self.db.benched(series):
                continue
            try:
                events = self.k.events(series, status="open", nested=True)
            except KalshiError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    log.info("series %s not found; dropping", series)
                    self.active_series.remove(series)
                    self._dropped.add(series)
                    continue
                raise
            for ev in events:
                tgt = event_date(ev)
                if not tgt:
                    continue
                kind = series_kind(series)
                meta = config.SERIES[series]
                today = datetime.now(ZoneInfo(meta["tz"])).date()
                if tgt < today or tgt > today + timedelta(days=config.DAYS_AHEAD):
                    continue
                key = (series, tgt)
                if key not in fc_cache:
                    try:
                        fc_cache[key] = build_forecast(series, tgt, kind, self.db.bias(series), self.http,
                                                       cal=station_calibration(self.db, meta["station"]))
                        fc = fc_cache[key]
                        pct = np.percentile(fc.samples, [5, 25, 50, 75, 95]).round(1).tolist()
                        self.db.forecast(series=series, target_date=tgt.isoformat(), kind=kind,
                                         median=fc.median, spread=fc.spread, observed=fc.observed_extreme,
                                         locked=int(fc.locked), notes="; ".join(fc.notes),
                                         fan_json=json.dumps(fc.hourly_fan), obs_json=json.dumps(fc.obs_trace),
                                         pct_json=json.dumps(pct))
                        log.info("%s %s %s: median %.1f spread %.1f %s", series, tgt, kind,
                                 fc.median, fc.spread, fc.notes)
                    except Exception as e:
                        log.warning("forecast failed %s %s: %s", series, tgt, e)
                        continue
                fc = fc_cache[key]
                if kind == "low" and datetime.now(ZoneInfo(meta["tz"])).hour < config.LOW_TRADE_AFTER_HOUR:
                    # Still evaluate (dashboard + exits), but block new buys until the evening is visible.
                    lows_blocked = True
                else:
                    lows_blocked = False
                markets = [m for m in (ev.get("markets") or []) if m.get("status", "open") in ("open", "active")]
                if not markets:
                    markets = self.k.markets(event_ticker=ev["event_ticker"])
                n_markets += len(markets)
                rules = (markets[0].get("rules_primary") or "") if markets else ""
                cli = re.search(r"\(CLI([A-Z0-9]{3,4})\)", rules)
                if cli and "K" + cli.group(1) != meta["station"]:
                    # Wrong thermometer = guaranteed losses. Refuse to trade this event.
                    log.warning("station mismatch for %s: rules say CLI%s, config has %s; skipping",
                                ev["event_ticker"], cli.group(1), meta["station"])
                    self.db.skip(ticker=ev["event_ticker"], outcome="", reason=f"station mismatch CLI{cli.group(1)} vs {meta['station']}")
                    continue
                orders, decisions = plan_orders(fc, markets, bankroll, positions, self.threshold,
                                                event_spent.get(ev["event_ticker"], 0.0), costs, self.model_weight)
                if lows_blocked:
                    for d in decisions:
                        if d["action"] == "buy":
                            d["action"], d["count"], d["reason"] = "hold", 0, f"lows open after {config.LOW_TRADE_AFTER_HOUR}:00 local"
                    orders = [o for o in orders if o["action"] != "buy"]
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
                                threshold=self.threshold, model_weight=self.model_weight, halted=self.halted)
        log.info("cycle done: balance %.2f markets %d orders %d thr %.3f", bankroll, n_markets, n_orders, self.threshold)

    def execute(self, o, series):
        """Take the ask when the spread is tight; otherwise rest at mid and let the market come to us."""
        try:
            ob = self.k.orderbook(o["ticker"], depth=10)
            avail = available_at(ob, o["outcome"], o["yes_price"])
            yb, ya = o.get("yes_bid", 0.0), o.get("yes_ask", 1.0)
        except KalshiError as e:
            log.warning("orderbook %s: %s", o["ticker"], e)
            avail, yb, ya = 0, o.get("yes_bid", 0.0), o.get("yes_ask", 1.0)
        spread = ya - yb
        maker = spread > config.MAKER_SPREAD and o["action"] == "buy"
        if maker:
            # Rest at mid on the YES scale (mid is the same point for either side).
            mid = round((yb + ya) / 2, 2)
            o["yes_price"] = mid
            tif, expire = "good_till_canceled", config.MAKER_TTL
            log.info("rest %s %s x%d @ yes %.2f (spread %.2f)", o["outcome"], o["ticker"], o["count"], mid, spread)
        else:
            tif, expire = "immediate_or_cancel", None
            if avail <= 0:
                log.info("skip %s %s: no liquidity at %.2f", o["outcome"], o["ticker"], o["yes_price"])
                self.db.skip(ticker=o["ticker"], outcome=o["outcome"], reason=f"no liquidity at {o['yes_price']:.2f}")
                return 0
            if avail < o["count"]:
                log.info("cap %s %s: %d -> %d (book depth)", o["outcome"], o["ticker"], o["count"], avail)
                o["count"] = avail
        try:
            r = self.k.place(o["ticker"], o["outcome"], o["count"], o["yes_price"], tif=tif, expire_s=expire)
        except KalshiError as e:
            log.error("order failed %s: %s", o["ticker"], e)
            self.db.order(series=series, ticker=o["ticker"], event_ticker=o["event_ticker"],
                          outcome=o["outcome"], count=o["count"], yes_price=o["yes_price"],
                          p_model=o["p_model"], edge=o["edge"], order_id="ERR", fill_count=0,
                          avg_fill=None, raw=str(e)[:500])
            msg = str(e).lower()
            if "balance" in msg or "insufficient" in msg or "funds" in msg:
                self.db.skip(ticker=o["ticker"], outcome=o["outcome"], reason="insufficient balance")
                return 0                      # out of cash is not a bug; keep going
            raise
        fill = float(r.get("fill_count") or r.get("fill_count_fp") or 0)
        avg = r.get("average_fill_price")
        avg = float(avg) if avg not in (None, "") else None
        status = "executed" if fill >= o["count"] else ("resting" if maker else "canceled")
        self.db.order(series=series, ticker=o["ticker"], event_ticker=o["event_ticker"],
                      outcome=o["outcome"], count=o["count"], yes_price=o["yes_price"],
                      p_model=o["p_model"], edge=o["edge"], order_id=r.get("order_id"),
                      fill_count=fill, avg_fill=avg, raw=str(r)[:500], status=status)
        log.info("%s %s %s x%d @ yes %.2f (p=%.2f edge=%.3f) filled %.0f", o["action"], o["outcome"],
                 o["ticker"], o["count"], o["yes_price"], o["p_model"], o["edge"], fill)
        return 1 if fill > 0 or maker or r.get("dry_run") else 0

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
                pnl += n * (win - c - fee(c))
                count += n
                cost += n * c
            self.db.settlement(ticker=ticker, event_ticker=hist[0]["event_ticker"], series=series,
                               result=result, outcome=hist[-1]["outcome"], count=count,
                               avg_fill=cost / count if count else None, p_model=hist[-1]["p_model"],
                               edge=hist[-1]["edge"], pnl=round(pnl, 2))
            log.info("settled %s -> %s pnl %.2f", ticker, result, pnl)
        n_settled = self.db.rows("SELECT COUNT(*) n FROM settlements")[0]["n"]
        if n_settled != self.db.get_state("adapt_seen", 0):
            self.adapt()                       # only when there is new evidence
            self.db.set_state("adapt_seen", n_settled)
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
        # Model weight: does the model's stated probability match realized hit rate?
        # Calibration error = mean |p_model - hit| over recent settlements. Good (<0.15) earns trust.
        if len(recent) >= 10:
            err = float(np.mean([abs(r["p_model"] - (1.0 if r["outcome"] == r["result"] else 0.0)) for r in recent]))
            w = self.model_weight + (0.05 if err < 0.15 else -0.05 if err > 0.3 else 0.0)
            self.model_weight = float(min(config.MODEL_WEIGHT_MAX, max(config.MODEL_WEIGHT_MIN, w)))
            self.db.set_state("model_weight", self.model_weight)
            log.info("calibration error %.2f -> model weight %.2f", err, self.model_weight)
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

    def discovery(self):
        """Find new temperature cities once a day; register known ones at startup."""
        if not config.DISCOVER_SERIES:
            return
        if time.time() - self.db.get_state("discover_last", 0) < 86400 and self.db.get_state("series_map"):
            for t, meta in self.db.get_state("series_map", {}).items():
                config.register_series(t, meta)
            self.active_series = list(config.SERIES)
            return
        try:
            n = discover(self.k, self.db, self.http)
            self.active_series = list(config.SERIES)
            if n:
                log.info("discovery added %d series; now tracking %d", n, len(config.SERIES))
                self.db.set_state("hist_last", 0)      # calibrate the new stations right away
        except Exception as e:
            log.warning("discovery error: %s", e)

    def history_calibration(self):
        """Refit station biases from the last 45 days, once a day, in the background."""
        last = self.db.get_state("hist_last", 0)
        uncal = [m["station"] for m in config.SERIES.values() if not self.db.get_state(f"hist:{m['station']}")]
        if time.time() - last < 86400 and not uncal:
            return
        self.db.set_state("hist_last", time.time())
        only = None if time.time() - last >= 86400 else set(uncal)
        def run():
            try:
                res = calibrate_all(self.db, days=45, session=requests.Session(), only=only)
                log.info("history calibration done for %d stations", len(res))
            except Exception as e:
                log.warning("history calibration failed: %s", e)
        threading.Thread(target=run, daemon=True).start()

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
                self.discovery()
                self.history_calibration()
                self.reconcile()
                self.cycle()
                self.errors = 0
            except Exception as e:
                self.errors += 1
                log.exception("cycle error %d: %s", self.errors, e)
                if self.errors >= config.MAX_CONSECUTIVE_ERRORS:
                    self.halted = True
            time.sleep(config.CYCLE_SECONDS)


class RingLog(logging.Handler):
    """Keep the last N log lines in memory for the /logs endpoint."""
    def __init__(self, n=400):
        super().__init__()
        from collections import deque
        self.lines = deque(maxlen=n)

    def emit(self, record):
        self.lines.append(self.format(record))


RING = RingLog()


def main():
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    RING.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(RING)
    agent = Agent()
    from .dashboard import serve
    threading.Thread(target=serve, args=(agent,), daemon=True).start()
    agent.run_forever()


if __name__ == "__main__":
    main()
