"""JSON endpoints behind the dashboard. Live marks come from Kalshi with a short cache."""
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config
from .kalshi import KalshiError
from .strategy import prices, strike

_lock = threading.Lock()
_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def _thin(obs, max_points=300):
    """Keep the chart payload small: at most ~300 points, always including the last one."""
    if len(obs) <= max_points:
        return obs
    step = -(-len(obs) // max_points)
    out = obs[::step]
    if out[-1] != obs[-1]:
        out.append(obs[-1])
    return out


def _fill_price(h):
    """Cost per contract on the outcome's own scale."""
    px = h["avg_fill"] if h["avg_fill"] is not None else h["yes_price"]
    return px if h["outcome"] == "yes" else 1 - px


class Api:
    def __init__(self, agent):
        self.a = agent
        self.db = agent.db
        self.k = agent.k

    # ---- helpers ---------------------------------------------------------
    def _markets_for(self, tickers):
        if not tickers:
            return {}
        def fetch():
            out = {}
            ts = list(tickers)
            for i in range(0, len(ts), 50):
                try:
                    data = self.k._req("GET", "/markets", params=dict(tickers=",".join(ts[i:i + 50]), limit=100))
                    for m in data.get("markets", []):
                        out[m["ticker"]] = m
                except KalshiError:
                    pass
            return out
        return cached("mk:" + ",".join(sorted(tickers)), 4, fetch)

    def _event_markets(self, event_ticker):
        return cached("ev:" + event_ticker, 15,
                      lambda: self.k.markets(event_ticker=event_ticker, status="open"))

    def _positions(self):
        return cached("pos", 4, lambda: self.k.positions())

    def _last_decisions(self):
        return {r["ticker"]: r for r in self.db.rows(
            "SELECT * FROM decisions WHERE cycle_id=(SELECT MAX(id) FROM cycles)")}

    # ---- endpoints -------------------------------------------------------
    def summary(self):
        st = self.a.last_status or {}
        pnl = self.db.rows("SELECT COALESCE(SUM(pnl),0) p, COUNT(*) n, SUM(pnl>0) w FROM settlements")[0]
        pos = self.positions()["positions"]
        cash = st.get("balance") or 0.0
        exposure = sum(p["cost"] for p in pos)
        v_bid = sum(p["value"] for p in pos)
        v_mid = sum(p["value_mid"] for p in pos)
        v_model = sum(p["value_model"] if p["value_model"] is not None else p["value_mid"] for p in pos)
        start = self.db.get_state("start_equity")
        if start is None:
            first = self.db.rows("SELECT balance FROM cycles WHERE balance IS NOT NULL ORDER BY id LIMIT 1")
            start = first[0]["balance"] if first else cash
            self.db.set_state("start_equity", start)
        fees = self.db.rows("SELECT COALESCE(SUM(fill_count),0) c FROM orders WHERE fill_count>0")[0]["c"]
        today = self.db.rows("SELECT COALESCE(SUM(pnl),0) p FROM settlements WHERE ts > ?", (time.time() - 86400,))[0]["p"]
        return dict(
            mode="DRY RUN" if config.DRY_RUN else ("HALTED" if self.a.halted else "LIVE"),
            threshold=self.a.threshold, model_weight=getattr(self.a, "model_weight", config.MODEL_WEIGHT),
            last_cycle_ts=st.get("ts"), cycle_seconds=config.CYCLE_SECONDS,
            days_ahead=config.DAYS_AHEAD, now=time.time(), display_mult=config.DISPLAY_MULT,
            # money
            start_equity=round(start, 2), balance=round(cash, 2), exposure=round(exposure, 2),
            n_positions=len(pos), n_contracts=int(sum(p["count"] for p in pos)),
            value_bid=round(v_bid, 2), value_mid=round(v_mid, 2), value_model=round(v_model, 2),
            unrealized=round(v_bid - exposure, 2), unrealized_mid=round(v_mid - exposure, 2),
            unrealized_model=round(v_model - exposure, 2),
            equity=round(cash + v_bid, 2), equity_mid=round(cash + v_mid, 2), equity_model=round(cash + v_model, 2),
            best_case=round(cash + sum(p["count"] for p in pos), 2), worst_case=round(cash, 2),
            pnl_since_start_bid=round(cash + v_bid - start, 2), pnl_since_start_mid=round(cash + v_mid - start, 2),
            settled_pnl=round(pnl["p"] or 0, 2), settled_n=pnl["n"], settled_wins=pnl["w"] or 0,
            settled_24h=round(today or 0, 2), contracts_traded=int(fees),
            series={k: dict(city=v["city"], station=v["station"], kind="low" if k.startswith("KXLOWT") else "high")
                    for k, v in config.SERIES.items()})

    def positions(self):
        rows = self._positions()
        held = {}
        for r in rows:
            n = float(r.get("position_fp") or r.get("position") or 0)
            if n:
                held[r["ticker"]] = n
        mk = self._markets_for(list(held))
        dec = self._last_decisions()
        out = []
        for t, n in held.items():
            outcome = "yes" if n > 0 else "no"
            hist = self.db.order_history(t)
            cost_each = (sum(_fill_price(h) * h["fill_count"] for h in hist) / sum(h["fill_count"] for h in hist)) if hist else None
            m = mk.get(t, {})
            yb, ya, nb, na, vol, spread = prices(m) if m else (0, 1, 0, 1, 0, 1)
            bid, ask = (yb, ya) if outcome == "yes" else (nb, na)
            mid = (bid + ask) / 2
            cnt = abs(n)
            ce = cost_each if cost_each is not None else mid
            cost = ce * cnt
            d = dec.get(t, {})
            p = d.get("p_model") if d.get("outcome") == outcome else (1 - d["p_model"] if d else None)
            out.append(dict(ticker=t, event=t.rsplit("-", 1)[0], series=t.split("-")[0], outcome=outcome, count=cnt,
                            cost_each=round(ce, 3), cost=round(cost, 2),
                            mark=round(bid, 2), mid=round(mid, 3), p_model=p,
                            value=round(bid * cnt, 2), value_mid=round(mid * cnt, 2),
                            value_model=round(p * cnt, 2) if p is not None else None,
                            unrealized=round((bid - ce) * cnt, 2), unrealized_mid=round((mid - ce) * cnt, 2),
                            unrealized_model=round((p - ce) * cnt, 2) if p is not None else None,
                            win=round((1 - ce) * cnt, 2), lose=round(-cost, 2),
                            title=m.get("title") or m.get("yes_sub_title") or t, status=m.get("status"),
                            close_time=m.get("close_time")))
        out.sort(key=lambda p: (p["event"], p["ticker"]))
        return dict(positions=out, ts=time.time())

    def cities(self):
        """Latest forecast per series/date with fan, obs trace, strikes, prices and positions."""
        fcs = self.db.rows("SELECT * FROM forecasts WHERE id IN (SELECT MAX(id) FROM forecasts GROUP BY series,target_date) "
                           "ORDER BY series, target_date")
        pos = {p["ticker"]: p for p in self.positions()["positions"]}
        dec = self._last_decisions()
        out = []
        for f in fcs:
            series, tgt = f["series"], f["target_date"]
            meta = config.SERIES.get(series, {})
            z = ZoneInfo(meta.get("tz", "UTC"))
            today = datetime.now(z).date().isoformat()
            if tgt < today:
                continue
            ev = f"{series}-{datetime.fromisoformat(tgt).strftime('%y%b%d').upper()}"
            try:
                markets = self._event_markets(ev)
            except Exception:
                markets = []
            strikes = []
            for m in markets:
                try:
                    kind, lo, hi = strike(m)
                except Exception:
                    continue
                yb, ya, nb, na, vol, spread = prices(m)
                d = dec.get(m["ticker"], {})
                strikes.append(dict(ticker=m["ticker"], kind=kind, lo=lo, hi=hi, yes_bid=yb, yes_ask=ya,
                                    mid=round((yb + ya) / 2, 3), vol=vol,
                                    p_yes=(d["p_model"] if d.get("outcome") == "yes" else (1 - d["p_model"]) if d else None),
                                    position=pos.get(m["ticker"])))
            strikes.sort(key=lambda s: (s["lo"] if s["lo"] is not None else s["hi"] or 0))
            out.append(dict(series=series, city=meta.get("city", series), station=meta.get("station"), tz=meta.get("tz"),
                            kind=f["kind"], target_date=tgt, ts=f["ts"], median=f["median"], spread=f["spread"],
                            observed=f["observed"], locked=f["locked"], notes=f["notes"],
                            fan=json.loads(f["fan_json"] or "[]"), obs=_thin(json.loads(f["obs_json"] or "[]")),
                            pct=json.loads(f["pct_json"] or "[]"), strikes=strikes,
                            local_now=datetime.now(z).isoformat()))
        return dict(cities=out, ts=time.time())

    def scorecard(self):
        by_series = self.db.rows("SELECT series, COUNT(*) n, SUM(pnl) pnl, SUM(pnl>0) wins, SUM(count) contracts, "
                                 "AVG(edge) avg_edge FROM settlements GROUP BY series ORDER BY pnl DESC")
        by_kind = self.db.rows("SELECT CASE WHEN series LIKE 'KXLOWT%' THEN 'low' ELSE 'high' END kind, COUNT(*) n, "
                               "SUM(pnl) pnl, SUM(pnl>0) wins FROM settlements GROUP BY kind")
        # Calibration: bucket by model probability of the outcome we bought.
        cal = self.db.rows("SELECT ROUND(p_model*10)/10.0 bucket, COUNT(*) n, AVG(outcome=result) hit "
                           "FROM settlements GROUP BY bucket ORDER BY bucket")
        equity = self.db.rows("SELECT ts, balance FROM cycles WHERE balance IS NOT NULL ORDER BY ts")
        daily = self.db.rows("SELECT date(ts,'unixepoch') d, SUM(pnl) pnl, COUNT(*) n FROM settlements GROUP BY d ORDER BY d")
        bias = self.db.rows("SELECT series, bias_f, updated FROM calibration")
        bench = self.db.rows("SELECT series, until, reason FROM bench WHERE until > ?", (time.time(),))
        return dict(by_series=by_series, by_kind=by_kind, calibration=cal, equity=equity, daily=daily,
                    bias=bias, benched=bench, threshold=self.a.threshold)

    def activity(self):
        orders = self.db.rows("SELECT ts,ticker,outcome,count,yes_price,p_model,edge,fill_count,avg_fill,order_id,status "
                              "FROM orders ORDER BY id DESC LIMIT 50")
        skips = self.db.rows("SELECT ts,ticker,outcome,reason FROM skips ORDER BY id DESC LIMIT 50")
        passes = self.db.rows("SELECT ts,ticker,outcome,p_model,price,edge,threshold,reason FROM decisions "
                              "WHERE cycle_id=(SELECT MAX(id) FROM cycles) AND action='hold' ORDER BY edge DESC LIMIT 40")
        settled = self.db.rows("SELECT ts,ticker,outcome,result,count,avg_fill,p_model,edge,pnl FROM settlements "
                               "ORDER BY ts DESC LIMIT 50")
        return dict(orders=orders, skips=skips, passes=passes, settlements=settled)
