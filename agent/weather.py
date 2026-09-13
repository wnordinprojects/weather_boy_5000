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
def fetch_ensemble(lat, lon, tz, days=4, session=None):
    s = session or requests
    r = s.get(ENSEMBLE_URL, params=dict(
        latitude=lat, longitude=lon, hourly="temperature_2m",
        models=config.ENSEMBLE_MODELS, temperature_unit="fahrenheit",
        timezone=tz, forecast_days=days), timeout=30, headers=UA)
    r.raise_for_status()
    return r.json()


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


def nowcast(members_hourly: np.ndarray, obs, target: date, tz: str, kind: str, local_now: datetime):
    """Blend today's observations into each member's remaining-hours forecast.

    For each member: error = observed extreme so far - member's extreme over hours already passed.
    Shift the member's remaining hours by that error, then final = combine(observed, corrected remaining).
    Late in the day this collapses onto the observation, which is what the market does too.
    Returns (samples per member, hours_left).
    """
    z = ZoneInfo(tz)
    hour_now = local_now.hour + local_now.minute / 60
    todays = [(ts.astimezone(z), v) for ts, v in obs if ts.astimezone(z).date() == target]
    vals = np.array([v for _, v in todays])
    obs_ext = float(vals.max() if kind == "high" else vals.min())
    passed = max(1, int(hour_now))                     # hours 0..passed-1 are behind us
    past = members_hourly[:, :passed]
    future = members_hourly[:, passed:]
    if kind == "high":
        past_ext = np.nanmax(past, axis=1)
        err = obs_ext - past_ext
        if future.shape[1] == 0:
            return np.full(members_hourly.shape[0], obs_ext), 0
        fut_ext = np.nanmax(future + err[:, None], axis=1)
        return np.maximum(obs_ext, fut_ext), future.shape[1]
    past_ext = np.nanmin(past, axis=1)
    err = obs_ext - past_ext
    if future.shape[1] == 0:
        return np.full(members_hourly.shape[0], obs_ext), 0
    fut_ext = np.nanmin(future + err[:, None], axis=1)
    return np.minimum(obs_ext, fut_ext), future.shape[1]


def fetch_observations(station, start_utc: datetime, session=None, max_pages=6):
    """All observations since start_utc. Busy ASOS stations report every minute, so one
    page (max 500) covers only a few hours; follow pagination until the window is exhausted."""
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
        nxt = (j.get("pagination") or {}).get("next")
        if not nxt or len(feats) < 500:
            break
        url, params = nxt, None
    return sorted(set(out))


def observed_extreme(obs, target: date, tz, kind):
    z = ZoneInfo(tz)
    vals = [v for ts, v in obs if ts.astimezone(z).date() == target]
    if not vals:
        return None, 0
    return (max(vals) if kind == "high" else min(vals)), len(vals)


# --------------------------------------------------------------------------
def build_forecast(series, target: date, kind: str, bias_f: float, session=None,
                   now=None) -> Forecast:
    meta = config.SERIES[series]
    z = ZoneInfo(meta["tz"])
    local_now = (now or datetime.now(z)).astimezone(z)
    notes = []

    payload = fetch_ensemble(meta["lat"], meta["lon"], meta["tz"], session=session)
    hourly = ensemble_hourly(payload, target)
    if hourly.size == 0:
        raise RuntimeError(f"no ensemble data for {series} {target}")
    members = np.nanmax(hourly, axis=1) if kind == "high" else np.nanmin(hourly, axis=1)
    rng = np.random.default_rng(int(target.strftime("%Y%m%d")))
    reps = max(1, 2000 // members.size)
    sign = 1 if kind == "high" else -1
    station_sd = config.STATION_ERROR_F

    fan = np.nanpercentile(hourly, [10, 50, 90], axis=0).T.round(1).tolist() if hourly.shape[1] else []
    obs_trace = []
    obs_ext, n_obs = None, 0
    locked = False
    if target <= local_now.date():
        start_utc = datetime.combine(target, datetime.min.time(), z).astimezone(ZoneInfo("UTC"))
        try:
            obs = fetch_observations(meta["station"], start_utc - timedelta(hours=1), session=session)
            obs_ext, n_obs = observed_extreme(obs, target, meta["tz"], kind)
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
                station_sd = config.STATION_ERROR_F * min(1.0, hours_left / 12)
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
