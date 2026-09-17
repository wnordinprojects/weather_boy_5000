"""System health: every outbound HTTP call and every warning is recorded here for /api/health.

Why: an Open-Meteo 429 blinded the bot for 90 minutes on Sep 14 and nobody saw it until the logs
were read by hand. This makes that kind of failure visible on the dashboard within one cycle."""
import logging
import threading
import time
from collections import deque
from urllib.parse import urlparse

import requests

SERVICES = {
    "external-api.kalshi.com": ("kalshi", "Kalshi", "trading, prices, balance"),
    "api.open-meteo.com": ("open-meteo", "Open-Meteo", "forecast models"),
    "ensemble-api.open-meteo.com": ("open-meteo", "Open-Meteo", "forecast models"),
    "previous-runs-api.open-meteo.com": ("open-meteo", "Open-Meteo", "forecast models"),
    "api.weather.gov": ("nws", "NWS", "live observations"),
    "mesonet.agron.iastate.edu": ("iem", "IEM ASOS", "history for calibration"),
}
WINDOW_S = 3600

_lock = threading.Lock()
_calls = {}      # key -> deque[(ts, status_code or 0, ms)]
_last_ok = {}    # key -> ts
_last_err = {}   # key -> (ts, text)
_issues = deque(maxlen=60)   # (ts, level, text) from WARNING+ log records


def service_key(url):
    host = urlparse(url).hostname or ""
    return SERVICES.get(host, (host, host, ""))[0]


def record(url, status, ms, err=None):
    key = service_key(url)
    now = time.time()
    with _lock:
        _calls.setdefault(key, deque(maxlen=2000)).append((now, status, ms))
        if err is None and status and status < 400:
            _last_ok[key] = now
        else:
            path = urlparse(url).path
            _last_err[key] = (now, f"{status or 'no response'} on {path}" + (f": {err}" if err else ""))


_orig_send = requests.adapters.HTTPAdapter.send


def _send(self, request, **kw):
    t0 = time.time()
    try:
        r = _orig_send(self, request, **kw)
    except Exception as e:
        record(request.url, 0, (time.time() - t0) * 1000, type(e).__name__)
        raise
    record(request.url, r.status_code, (time.time() - t0) * 1000)
    return r


def install():
    """Patch requests once so every library call (module-level get or Session) is counted."""
    if requests.adapters.HTTPAdapter.send is not _send:
        requests.adapters.HTTPAdapter.send = _send
    root = logging.getLogger()
    if not any(isinstance(h, _IssueLog) for h in root.handlers):
        root.addHandler(_IssueLog())


class _IssueLog(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, rec):
        text = rec.getMessage()
        if rec.exc_info:
            text += f" ({rec.exc_info[0].__name__})"
        _issues.append((rec.created, rec.levelname, text[:240]))


def _service_status(key, now):
    name, what = next(((n, w) for k, n, w in SERVICES.values() if k == key), (key, ""))
    calls = [c for c in _calls.get(key, ()) if now - c[0] < WINDOW_S]
    n = len(calls)
    n429 = sum(1 for c in calls if c[1] == 429)
    nerr = sum(1 for c in calls if c[1] == 0 or c[1] >= 400)
    recent429 = any(c[1] == 429 and now - c[0] < 1800 for c in calls)
    ok_ts, err = _last_ok.get(key), _last_err.get(key)
    last = calls[-1] if calls else None
    if not calls:
        state, note = "idle", "no calls in the last hour"
    elif (last[1] == 0 or last[1] >= 400) and (not ok_ts or now - ok_ts > 900):
        state, note = "down", "failing, no success in 15 min"
    elif recent429:
        state, note = "degraded", f"rate limited ({n429} × 429 this hour)"
    elif nerr / n > 0.2:
        state, note = "degraded", f"{nerr} of {n} calls failed this hour"
    else:
        state, note = "ok", f"{n} calls this hour"
    return dict(key=key, name=name, what=what, state=state, note=note, calls_1h=n, errors_1h=nerr,
                rate_limited_1h=n429, p50_ms=round(sorted(c[2] for c in calls)[n // 2]) if n else None,
                last_ok=ok_ts, last_error=dict(ts=err[0], text=err[1]) if err else None)


def snapshot(agent, config, db=None, weather=None):
    """Everything the Systems panel needs. `agent` supplies loop state; the rest is module state."""
    now = time.time()
    with _lock:
        keys = list(dict.fromkeys([v[0] for v in SERVICES.values()] + list(_calls)))
        services = [_service_status(k, now) for k in keys]
        issues = [dict(ts=t, level=l, text=x) for t, l, x in _issues if now - t < 6 * 3600][::-1]
    checks = []

    def check(name, state, note):
        checks.append(dict(name=name, state=state, note=note))

    st = agent.last_status or {}
    expect = config.CYCLE_SECONDS_FAST if st.get("fast") else config.CYCLE_SECONDS
    age = now - st["ts"] if st.get("ts") else None
    if st.get("trading_active") is False:
        check("Trading loop", "degraded", "waiting: Kalshi exchange is paused")
    elif age is None:
        check("Trading loop", "degraded", "no cycle finished yet since restart")
    elif age > expect * 4:
        check("Trading loop", "down", f"last cycle {age / 60:.0f} min ago (expected every {expect // 60} min)")
    elif age > expect * 2.2:
        check("Trading loop", "degraded", f"last cycle {age / 60:.0f} min ago (expected every {expect // 60} min)")
    else:
        check("Trading loop", "ok", f"last cycle {age / 60:.0f} min ago")

    if agent.halted:
        check("Error guard", "down", f"halted after {agent.errors} errors in a row; retries within the hour")
    elif agent.errors:
        check("Error guard", "degraded", f"{agent.errors} cycle error(s) in a row")
    else:
        check("Error guard", "ok", "no cycle errors")

    if config.DRY_RUN:
        check("Mode", "degraded", "DRY_RUN is on: no real orders")
    else:
        check("Mode", "ok", "live trading")

    m = st.get("markets")
    if m is None:
        pass
    elif m == 0:
        check("Forecasts", "down", "0 markets priced last cycle; forecasts are failing")
    elif weather is not None and weather.rate_limited():
        check("Forecasts", "degraded", f"{m} markets priced, Open-Meteo backoff on (serving cached runs)")
    else:
        check("Forecasts", "ok", f"{m} markets priced last cycle")


    tb = sum(1 for i in issues if i["level"] in ("ERROR", "CRITICAL") and now - i["ts"] < WINDOW_S)
    if tb:
        check("Errors logged", "degraded", f"{tb} error(s) in the last hour")
    else:
        check("Errors logged", "ok", "none in the last hour")

    if db is not None:
        stale = late_settlements(db, now)
        if stale:
            check("Settlements", "degraded", f"{len(stale)} market(s) past payout time not booked yet: "
                  + ", ".join(stale[:3]) + ("…" if len(stale) > 3 else ""))
        else:
            check("Settlements", "ok", "nothing overdue")

    rank = dict(ok=0, idle=0, degraded=1, down=2)
    worst = max([rank[c["state"]] for c in checks] +
                [rank[s["state"]] for s in services if s["key"] in ("kalshi", "open-meteo", "nws")] + [0])
    overall = ["ok", "degraded", "down"][worst]
    return dict(overall=overall, checks=checks, services=services, issues=issues[:25], now=now)


def late_settlements(db, now):
    """Held markets more than ~6h past their usual payout (next day ~13:00 UTC) with no settlement."""
    from .api import _settle_est
    out = []
    rows = db.rows("SELECT DISTINCT o.ticker FROM orders o LEFT JOIN settlements s ON s.ticker=o.ticker "
                   "WHERE o.fill_count > 0 AND s.ticker IS NULL")
    for t in (r["ticker"] for r in rows):
        est = _settle_est(t)
        if est and now > est + 6 * 3600:
            out.append(t)
    return out
