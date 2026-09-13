"""Tiny status page + JSON, served on $PORT. No dependencies."""
import html
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import config

PAGE = """<!doctype html><meta charset=utf-8><title>Kalshi Weather Agent</title>
<style>body{font:14px system-ui;margin:24px;max-width:1100px}table{border-collapse:collapse;width:100%%;margin-bottom:24px}
td,th{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;font-size:13px}h2{margin:18px 0 6px}
.k{display:inline-block;margin-right:24px}.k b{font-size:20px;display:block}</style>
<h1>Kalshi Weather Agent</h1>
<div><span class=k><b>$%(balance).2f</b>balance</span><span class=k><b>%(pnl).2f</b>settled P&amp;L</span>
<span class=k><b>%(threshold).3f</b>edge threshold</span><span class=k><b>%(mode)s</b>mode</span>
<span class=k><b>%(age)s</b>since last cycle</span></div>
<h2>Latest forecasts</h2>%(forecasts)s
<h2>Recent orders</h2>%(orders)s
<h2>Settlements</h2>%(settlements)s
<h2>Last cycle decisions (top edges)</h2>%(decisions)s
"""


def table(rows, cols):
    if not rows:
        return "<p>none yet</p>"
    h = "<table><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
    for r in rows:
        h += "<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>"
    return h + "</table>"


def fmt_ts(rows):
    for r in rows:
        if "ts" in r:
            r["ts"] = time.strftime("%m-%d %H:%M", time.gmtime(r["ts"])) + "Z"
    return rows


def render(agent):
    db = agent.db
    st = agent.last_status or {}
    pnl = db.rows("SELECT COALESCE(SUM(pnl),0) p FROM settlements")[0]["p"]
    age = f"{int(time.time() - st['ts'])}s" if st.get("ts") else "n/a"
    fc = fmt_ts(db.rows("SELECT ts,series,target_date,kind,median,spread,observed,locked,notes FROM forecasts "
                        "WHERE id IN (SELECT MAX(id) FROM forecasts GROUP BY series,target_date) ORDER BY series,target_date"))
    orders = fmt_ts(db.rows("SELECT ts,ticker,outcome,count,yes_price,p_model,edge,fill_count,avg_fill,order_id FROM orders ORDER BY id DESC LIMIT 30"))
    sett = fmt_ts(db.rows("SELECT ts,ticker,outcome,result,count,avg_fill,p_model,edge,pnl FROM settlements ORDER BY ts DESC LIMIT 30"))
    dec = fmt_ts(db.rows("SELECT ts,ticker,outcome,p_model,price,edge,threshold,saturated,action,count,reason FROM decisions "
                         "WHERE cycle_id=(SELECT MAX(id) FROM cycles) ORDER BY edge DESC LIMIT 40"))
    return PAGE % dict(balance=st.get("balance", 0.0), pnl=pnl, threshold=agent.threshold,
                       mode="DRY RUN" if config.DRY_RUN else ("HALTED" if agent.halted else "LIVE"), age=age,
                       forecasts=table(fc, ["ts", "series", "target_date", "kind", "median", "spread", "observed", "locked", "notes"]),
                       orders=table(orders, ["ts", "ticker", "outcome", "count", "yes_price", "p_model", "edge", "fill_count", "avg_fill", "order_id"]),
                       settlements=table(sett, ["ts", "ticker", "outcome", "result", "count", "avg_fill", "p_model", "edge", "pnl"]),
                       decisions=table(dec, ["ts", "ticker", "outcome", "p_model", "price", "edge", "threshold", "saturated", "action", "count", "reason"]))


def serve(agent):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/status.json"):
                body = json.dumps(dict(agent.last_status, threshold=agent.threshold, halted=agent.halted)).encode()
                ct = "application/json"
            else:
                body = render(agent).encode()
                ct = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    HTTPServer(("0.0.0.0", config.PORT), H).serve_forever()
