"""Calibrate each station against history instead of waiting for settlements.

For the last N days, compare the forecast issued the day before (Open-Meteo previous-runs API)
with what the station actually recorded (IEM ASOS archive). From that we learn, per station:

  * day_bias[high|low]   : actual daily extreme - forecast daily extreme (mean), deg F
  * evening_bias         : actual - forecast over local hours 17-23 (mean), deg F
                           (this is the nighttime-cooling error that hurt the low markets)
  * resid_sd[high|low]   : std of the daily-extreme residual -> honest station noise

Everything is best-effort: any failure leaves the previous calibration untouched.
"""
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import requests

from . import config

log = logging.getLogger("history")
PREV_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
UA = {"User-Agent": "kalshi-weather-agent (contact: walker.nordin@ontailwind.com)"}


def fetch_prev_forecasts(lat, lon, tz, days, session=None):
    """hourly temps predicted 24h before valid time, for the last `days` days. -> {date: [24 temps]}"""
    s = session or requests
    r = s.get(PREV_URL, params=dict(latitude=lat, longitude=lon, hourly="temperature_2m_previous_day1",
                                    past_days=days, forecast_days=1, temperature_unit="fahrenheit",
                                    timezone=tz, models="best_match"), timeout=40, headers=UA)
    r.raise_for_status()
    h = r.json()["hourly"]
    out = defaultdict(lambda: [None] * 24)
    for t, v in zip(h["time"], h["temperature_2m_previous_day1"]):
        d, hh = t[:10], int(t[11:13])
        out[d][hh] = v
    return dict(out)


def fetch_asos(station, start: date, end: date, tz, session=None):
    """Station observations from the IEM archive. -> [(local_datetime, temp_f)]"""
    s = session or requests
    sid = station[1:] if len(station) == 4 and station.startswith("K") else station
    r = s.get(ASOS_URL, params=dict(station=sid, data="tmpf", year1=start.year, month1=start.month, day1=start.day,
                                    year2=end.year, month2=end.month, day2=end.day, tz=tz, format="onlycomma",
                                    latlon="no", direct="no", missing="null", trace="null"), timeout=60, headers=UA)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        parts = line.split(",")
        if len(parts) < 3 or parts[0] == "station":
            continue
        try:
            ts = datetime.strptime(parts[1], "%Y-%m-%d %H:%M")
            v = float(parts[2])
        except ValueError:
            continue
        out.append((ts, v))
    return out


def fit(prev, obs):
    """Compare forecast vs observed by local day. Returns dict of biases and sds, or None if thin."""
    by_day = defaultdict(list)
    for ts, v in obs:
        by_day[ts.date().isoformat()].append((ts.hour, v))
    hi_res, lo_res, eve_res = [], [], []
    for d, fc in prev.items():
        if d not in by_day or any(x is None for x in fc):
            continue
        vals = by_day[d]
        if len(vals) < 18:               # need most of the day
            continue
        a = np.array([v for _, v in vals])
        f = np.array(fc, dtype=float)
        hi_res.append(a.max() - f.max())
        lo_res.append(a.min() - f.min())
        hourly_obs = defaultdict(list)
        for hh, v in vals:
            hourly_obs[hh].append(v)
        for hh in range(17, 24):
            if hourly_obs[hh]:
                eve_res.append(np.mean(hourly_obs[hh]) - f[hh])
    if len(hi_res) < 10:
        return None
    return dict(n=len(hi_res),
                high_bias=float(np.mean(hi_res)), high_sd=float(np.std(hi_res)),
                low_bias=float(np.mean(lo_res)), low_sd=float(np.std(lo_res)),
                evening_bias=float(np.mean(eve_res)) if eve_res else 0.0)


def calibrate_all(db, days=45, session=None):
    """Run for every station (highs and lows share one). Stores results in db state 'hist:<station>'."""
    done = {}
    seen = set()
    for series, meta in config.SERIES.items():
        st = meta["station"]
        if st in seen:
            continue
        seen.add(st)
        try:
            end = datetime.now().date()
            start = end - timedelta(days=days)
            prev = fetch_prev_forecasts(meta["lat"], meta["lon"], meta["tz"], days, session)
            obs = fetch_asos(st, start, end, meta["tz"], session)
            res = fit(prev, obs)
            if not res:
                log.warning("history %s: not enough overlapping days", st)
                continue
            res["updated"] = time.time()
            db.set_state(f"hist:{st}", res)
            done[st] = res
            log.info("history %s: n=%d high %+.2f±%.2f low %+.2f±%.2f evening %+.2f", st, res["n"],
                     res["high_bias"], res["high_sd"], res["low_bias"], res["low_sd"], res["evening_bias"])
        except Exception as e:
            log.warning("history %s failed: %s", st, e)
    return done


def station_calibration(db, station):
    """{'high_bias','low_bias','evening_bias','high_sd','low_sd'} or None."""
    return db.get_state(f"hist:{station}")
