"""Find every Kalshi daily-temperature series and map it to a weather station.

Kalshi's rules name the settlement station as a climate-report code, e.g. "Phoenix (CLIPHX)".
The ASOS station is "K" + that code for every US city we have seen. NWS's station endpoint
gives coordinates and time zone, which is all the forecast model needs. Anything that fails a
lookup is skipped with a warning, never guessed.
"""
import logging
import re
import time

import requests

from . import config
from .kalshi import KalshiError

log = logging.getLogger("discovery")
UA = {"User-Agent": "kalshi-weather-agent (contact: walker.nordin@ontailwind.com)"}
RULE_RE = re.compile(r"at ([A-Z][A-Za-z .'-]+?) \(CLI([A-Z0-9]{3,4})\)")


def kalshi_temperature_series(k):
    """All open series tickers that look like daily temperature markets."""
    found = set()
    try:  # newer API: list series by category
        data = k._req("GET", "/series", params={"category": "Climate and Weather", "limit": 500})
        for srs in data.get("series", []) or []:
            t = srs.get("ticker", "")
            if t.startswith(("KXHIGH", "KXLOWT")):
                found.add(t)
    except KalshiError as e:
        log.info("series list unavailable (%s); paging events instead", e)
    if not found:
        params = {"status": "open", "limit": 200}
        for _ in range(60):
            data = k._req("GET", "/events", params=params)
            for ev in data.get("events", []) or []:
                t = ev.get("series_ticker") or ev.get("event_ticker", "").split("-")[0]
                if t.startswith(("KXHIGH", "KXLOWT")):
                    found.add(t)
            cur = data.get("cursor")
            if not cur:
                break
            params["cursor"] = cur
    return sorted(found)


def station_meta(station, session=None):
    s = session or requests
    r = s.get(f"https://api.weather.gov/stations/{station}", timeout=20, headers=UA)
    if r.status_code != 200:
        return None
    j = r.json()
    lon, lat = j["geometry"]["coordinates"][:2]
    tz = j["properties"].get("timeZone")
    if not tz:
        return None
    return dict(lat=round(lat, 3), lon=round(lon, 3), tz=tz)


def resolve(k, series, session=None):
    """series ticker -> meta dict or None."""
    ms = k.markets(series_ticker=series, status="open")
    if not ms:
        return None
    m = RULE_RE.search(ms[0].get("rules_primary") or "")
    if not m:
        log.warning("%s: rules have no CLI code: %s", series, (ms[0].get("rules_primary") or "")[:120])
        return None
    city, code = m.group(1).strip(), m.group(2)
    station = "K" + code
    meta = station_meta(station, session)
    if not meta:
        log.warning("%s: NWS has no station %s", series, station)
        return None
    return dict(city=city, station=station, **meta)


def discover(k, db, session=None):
    """Register any temperature series we do not trade yet. Persists the map so restarts are free."""
    known = db.get_state("series_map", {})
    for t, meta in known.items():
        config.register_series(t, meta)
    added = 0
    try:
        tickers = kalshi_temperature_series(k)
    except KalshiError as e:
        log.warning("discovery failed: %s", e)
        return 0
    for t in tickers:
        if t in config.SERIES:
            continue
        try:
            meta = resolve(k, t, session)
        except Exception as e:
            log.warning("%s: resolve failed: %s", t, e)
            continue
        if not meta:
            continue
        config.register_series(t, meta)
        known[t] = meta
        added += 1
        log.info("discovered %s -> %s %s (%s)", t, meta["city"], meta["station"], meta["tz"])
    db.set_state("series_map", known)
    db.set_state("discover_last", time.time())
    return added
