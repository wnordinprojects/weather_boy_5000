"""Forecast distribution for a station's daily high (or low) on a given local date.

Sources (both free, no keys):
  * Open-Meteo ensemble API: 30-100 members across GFS/ECMWF/ICON -> raw distribution.
  * NWS observations for the station: the running max/min so far today. This is
    the intraday edge: once the observed max passes a strike, that market is decided.

Output: a sample array of plausible final daily values (deg F), already bias-corrected
and spread-inflated, plus the observed running extreme. Probabilities for any strike
come from that sample.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests

from . import config

log = logging.getLogger("weather")
UA = {"User-Agent": "kalshi-weather-agent (contact: walker.nordin@ontailwind.com)"}
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NWS_OBS_URL = "https://api.weather.gov/stations/{station}/observations"


@dataclass
class Forecast:
    series: str
    target_date: date
    kind: str                      # "high" | "low"
    samples: np.ndarray            # final-value samples, deg F
    observed_extreme: float | None # running max (high) / min (low) so far today, deg F
    n_obs: int
    locked: bool                   # day's extreme is effectively in the books
    local_now: datetime
    notes: list = field(default_factory=list)
    hourly_fan: list = field(default_factory=list)   # [[p10,p50,p90] x 24] deg F, ensemble
    obs_trace: list = field(default_factory=list)    # [[iso_ts, temp_f], ...] today's observations

    @property
    def median(self):
        return float(np.median(self.samples))

    @property
    def spread(self):
        return float(np.std(self.samples))

    def prob_gt(self, x):   return float(np.mean(self.samples > x))
    def prob_ge(self, x):   return float(np.mean(self.samples >= x))
    def prob_lt(self, x):   return float(np.mean(self.samples < x))
    def prob_le(self, x):   return float(np.mean(self.samples <= x))
    def prob_between(self, lo, hi):  # inclusive of integer endpoints
        return float(np.mean((self.samples >= lo) & (self.samples <= hi)))


# --------------------------------------------------------------------------
import threading as _threading
import time as _time
_model_cache, _model_lock = {}, _threading.Lock()


_backoff_until = 0.0


def _cached(key, fn, ttl=None):
    """Model runs change hourly at most; fast afternoon cycles must not re-download them.

    Open-Meteo's free tier is a daily quota. On a 429 we stop asking for MODEL_BACKOFF_S and keep
    serving the last good payload (up to MODEL_STALE_S old) rather than going blind."""
    global _backoff_until
    ttl = config.MODEL_CACHE_S if ttl is None else ttl
    now = _time.time()
    with _model_lock:
        hit = _model_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    if now < _backoff_until:
        if hit and now - hit[0] < config.MODEL_STALE_S:
            return hit[1]
        raise RuntimeError("open-meteo rate limited; no cached run")
    try:
        val = fn()
    except Exception as e:
        if "429" in str(e):
            _backoff_until = now + config.MODEL_BACKOFF_S
            log.warning("open-meteo 429: backing off %ds", config.MODEL_BACKOFF_S)
        if hit and now - hit[0] < config.MODEL_STALE_S:
            log.info("serving stale model run for %s (%s)", key, e)
            return hit[1]
        raise
    with _model_lock:
        _model_cache[key] = (now, val)
    return val


def rate_limited():
    return _time.time() < _backoff_until


def fetch_ensemble(lat, lon, tz, days=None, session=None):
    days = config.ENSEMBLE_DAYS if days is None else days
    s = session or requests
    def go():
        r = s.get(ENSEMBLE_URL, params=dict(
            latitude=lat, longitude=lon, hourly="temperature_2m",
            models=config.ENSEMBLE_MODELS, temperature_unit="fahrenheit",
            timezone=tz, forecast_days=days), timeout=30, headers=UA)
        r.raise_for_status()
        return r.json()
    return _cached(("ens", lat, lon, days), go)


def fetch_hrrr(lat, lon, tz, target: date, session=None):
    """HRRR hourly temps (deg F) for `target` local date, or None if unavailable.
    3 km grid, refreshed hourly: the best same-day guidance for US stations."""
    s = session or requests
    try:
        def go():
            r = s.get(FORECAST_URL, params=dict(latitude=lat, longitude=lon, hourly="temperature_2m",
                                                models="gfs_hrrr", temperature_unit="fahrenheit", timezone=tz,
                                                forecast_days=2), timeout=30, headers=UA)
            r.raise_for_status()
            return r.json()
        h = _cached(("hrrr", lat, lon), go, config.HRRR_CACHE_S)["hourly"]
        vals = [v for t, v in zip(h["time"], h["temperature_2m"]) if t.startswith(target.isoformat())]
        if len(vals) != 24 or any(v is None for v in vals):
            return None
        return np.array(vals, dtype=float)
    except Exception as e:
        log.info("hrrr unavailable: %s", e)
        return None


def ensemble_hourly(payload, target: date) -> np.ndarray:
    """(members x 24) hourly temps for `target` local date. NaN-only members dropped."""
    hourly = payload["hourly"]
    times = hourly["time"]
    idx = [i for i, t in enumerate(times) if t.startswith(target.isoformat())]
    if not idx:
        return np.empty((0, 0))
    rows = []
    for k, arr in hourly.items():
        if not k.startswith("temperature_2m"):
            continue
        col = np.array([arr[i] for i in idx], dtype=float)
        if np.isnan(col).all():
            continue
        rows.append(col)
    return np.array(rows, dtype=float)


def ensemble_daily_extremes(payload, target: date, kind: str) -> np.ndarray:
    """Per-member daily max/min for `target` local date from hourly ensemble output."""
    m = ensemble_hourly(payload, target)
    if m.size == 0:
        return np.array([])
    return np.nanmax(m, axis=1) if kind == "high" else np.nanmin(m, axis=1)


def nowcast(members_hourly: np.ndarray, obs, target: date, tz: str, kind: str, local_now: datetime,
            recent_hours: int = 3):
    """Bias-correct each member by its error over the most RECENT observed hours, then take
    the member's extreme over the remaining hours of the day.

    Using the recent error (not the error at the day's extreme) matters: a model that ran
    4F warm at dawn says nothing about how it will do at 11pm. The caller adds station noise
    and then applies the observed floor/ceiling.
    Returns (corrected future extreme per member, hours_left).
    """
    z = ZoneInfo(tz)
    hour_now = local_now.hour + local_now.minute / 60
    todays = [(ts.astimezone(z), v) for ts, v in obs if ts.astimezone(z).date() == target]
    passed = max(1, int(hour_now))                     # hours 0..passed-1 are behind us
    future = members_hourly[:, passed:]
    if future.shape[1] == 0:
        vals = np.array([v for _, v in todays])
        ext = float(vals.max() if kind == "high" else vals.min())
        return np.full(members_hourly.shape[0], ext), 0
    # Hourly mean of observations for the last `recent_hours` completed hours.
    errs = []
    for h in range(max(0, passed - recent_hours), passed):
        vals = [v for t, v in todays if t.hour == h]
        if vals:
            errs.append(np.mean(vals) - members_hourly[:, h])
    err = np.mean(errs, axis=0) if errs else np.zeros(members_hourly.shape[0])
    corrected = future + err[:, None]
    fut_ext = np.nanmax(corrected, axis=1) if kind == "high" else np.nanmin(corrected, axis=1)
    return fut_ext, future.shape[1]


def fetch_observations(station, start_utc: datetime, session=None, max_pages=6, raw_out=None):
    """All observations since start_utc. Busy ASOS stations report every minute, so one
    page (max 500) covers only a few hours; follow pagination until the window is exhausted.
    If `raw_out` is a list, (ts, rawMessage) pairs are appended to it for METAR remark parsing."""
    s = session or requests
    url = NWS_OBS_URL.format(station=station)
    params = dict(start=start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=500)
    out = []
    for _ in range(max_pages):
        r = s.get(url, params=params, timeout=30, headers=UA)
        r.raise_for_status()
        j = r.json()
        feats = j.get("features", [])
        for f in feats:
            p = f.get("properties", {})
            t = p.get("temperature", {}) or {}
            v = t.get("value")
            if v is None:
                continue
            ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
            f_val = v * 9 / 5 + 32 if (t.get("unitCode", "").endswith("degC")) else v
            out.append((ts, float(f_val)))
            if raw_out is not None and p.get("rawMessage"):
                raw_out.append((ts, p["rawMessage"]))
        nxt = (j.get("pagination") or {}).get("next")
        if not nxt or len(feats) < 500:
            break
        url, params = nxt, None
    return sorted(set(out))


import re as _re
_METAR_MAX = _re.compile(r"(?:^|\s)1([01])(\d{3})(?=\s|$)")
_METAR_MIN = _re.compile(r"(?:^|\s)2([01])(\d{3})(?=\s|$)")


def _group_f(sign, ttt):
    c = int(ttt) / 10.0 * (-1 if sign == "1" else 1)
    return c * 9 / 5 + 32


def metar_extreme(raws, target: date, tz, kind):
    """Exact 6-hour max/min from METAR remarks (1sTTT / 2sTTT groups, reported at 00/06/12/18Z).

    Hourly and even 1-minute obs can miss the true peak between reports; the 6-hour group is the
    sensor's own running extreme and is what the climate report (settlement) is built from.
    A group covers the 6 hours ending at the report time; only windows lying wholly inside the
    target local day count, so a window that straddles midnight is ignored.
    Returns deg F or None."""
    z = ZoneInfo(tz)
    pat = _METAR_MAX if kind == "high" else _METAR_MIN
    vals = []
    for ts, raw in raws:
        if "RMK" not in raw:
            continue
        m = pat.search(raw.split("RMK", 1)[1])
        if not m:
            continue
        end = ts.astimezone(z)
        start = end - timedelta(hours=6)
        if end.date() != target or start.date() != target:
            continue
        vals.append(_group_f(m.group(1), m.group(2)))
    if not vals:
        return None
    return max(vals) if kind == "high" else min(vals)


def observed_extreme(obs, target: date, tz, kind):
    z = ZoneInfo(tz)
    vals = [v for ts, v in obs if ts.astimezone(z).date() == target]
    if not vals:
        return None, 0
    return (max(vals) if kind == "high" else min(vals)), len(vals)


# --------------------------------------------------------------------------
def build_forecast(series, target: date, kind: str, bias_f: float, session=None,
                   now=None, cal=None) -> Forecast:
    """`cal` is the station's history calibration (history.station_calibration) or None."""
    meta = config.SERIES[series]
    z = ZoneInfo(meta["tz"])
    local_now = (now or datetime.now(z)).astimezone(z)
    notes = []

    payload = fetch_ensemble(meta["lat"], meta["lon"], meta["tz"], session=session)
    hourly = ensemble_hourly(payload, target)
    if hourly.size == 0:
        raise RuntimeError(f"no ensemble data for {series} {target}")
    hourly = hourly.copy()
    if config.HRRR_WEIGHT > 0 and target <= local_now.date() + timedelta(days=1):
        hrrr = fetch_hrrr(meta["lat"], meta["lon"], meta["tz"], target, session)
        if hrrr is not None:
            # Pull every member toward the high-resolution trace; the ensemble keeps its spread.
            hourly = (1 - config.HRRR_WEIGHT) * hourly + config.HRRR_WEIGHT * hrrr[None, :]
            notes.append("hrrr")
    if cal and cal.get("evening_bias") is not None:
        # Learned nighttime error at this station (grid cell vs sensor, urban heat, etc.).
        hourly[:, 17:] += cal["evening_bias"]
        notes.append(f"evening bias {cal['evening_bias']:+.1f}F")
    members = np.nanmax(hourly, axis=1) if kind == "high" else np.nanmin(hourly, axis=1)
    rng = np.random.default_rng(int(target.strftime("%Y%m%d")))
    reps = max(1, 2000 // members.size)
    sign = 1 if kind == "high" else -1
    station_sd = config.STATION_ERROR_F
    if cal:
        sd_key = "high_sd" if kind == "high" else "low_sd"
        station_sd = float(min(3.0, max(0.8, cal.get(sd_key, station_sd))))
        bias_f = cal.get("high_bias" if kind == "high" else "low_bias", bias_f) * sign  # oriented below

    fan = np.nanpercentile(hourly, [10, 50, 90], axis=0).T.round(1).tolist() if hourly.shape[1] else []
    obs_trace = []
    obs_ext, n_obs = None, 0
    locked = False
    if target <= local_now.date():
        start_utc = datetime.combine(target, datetime.min.time(), z).astimezone(ZoneInfo("UTC"))
        try:
            raws = []
            obs = fetch_observations(meta["station"], start_utc - timedelta(hours=1), session=session, raw_out=raws)
            obs_ext, n_obs = observed_extreme(obs, target, meta["tz"], kind)
            mx = metar_extreme(raws, target, meta["tz"], kind)
            if mx is not None and obs_ext is not None and ((kind == "high" and mx > obs_ext) or (kind == "low" and mx < obs_ext)):
                notes.append(f"metar 6h {kind} {mx:.1f}F beats obs {obs_ext:.1f}F")
                obs_ext = mx
            obs_trace = [[ts.astimezone(z).isoformat(), round(v, 1)] for ts, v in obs
                         if ts.astimezone(z).date() == target]
        except Exception as e:  # observations are an enhancement, never a blocker
            notes.append(f"obs unavailable: {e}")
        if obs_ext is not None:
            # Only a finished day is locked. A high can still print at 11pm under a warm
            # front and a calendar-day low often lands just before midnight.
            locked = target < local_now.date()
            if locked:
                notes.append(f"locked on observed {kind} {obs_ext:.1f}F")
                members = np.full(members.size, obs_ext)
                station_sd = 0.3      # only rounding risk left
            else:
                members, hours_left = nowcast(hourly, obs, target, meta["tz"], kind, local_now)
                # Uncertainty shrinks with the hours left in the day.
                # Never below ~0.8F while hours remain: a 1-2F evening drift is routine, and the
                # day-one low losses came from the model calling the last 3 hours near-certain.
                station_sd = max(config.MIN_INTRADAY_SD_F, config.STATION_ERROR_F * min(1.0, hours_left / 12))
                notes.append(f"observed so far {obs_ext:.1f}F over {n_obs} obs, {hours_left}h left")

    if obs_ext is None:
        # Pure forecast: hourly sampling misses peaks; grid vs sensor. Bias is learned per station.
        members = members + sign * bias_f
    center = np.median(members)
    dev = (members - center) * (1.0 if locked else config.SPREAD_INFLATION)
    samples = np.tile(center + dev, reps) + rng.normal(0, station_sd, members.size * reps)
    if obs_ext is not None and not locked:
        samples = np.maximum(samples, obs_ext) if kind == "high" else np.minimum(samples, obs_ext)

    # Settlement is a whole-degree value; round samples so strike math is exact.
    samples = np.round(samples)
    return Forecast(series, target, kind, samples, obs_ext, n_obs, locked, local_now, notes, fan, obs_trace)
