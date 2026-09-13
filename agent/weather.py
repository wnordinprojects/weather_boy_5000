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


def ensemble_daily_extremes(payload, target: date, kind: str) -> np.ndarray:
    """Per-member daily max/min for `target` local date from hourly ensemble output."""
    hourly = payload["hourly"]
    times = hourly["time"]
    idx = [i for i, t in enumerate(times) if t.startswith(target.isoformat())]
    if not idx:
        return np.array([])
    vals = []
    for k, arr in hourly.items():
        if not k.startswith("temperature_2m"):
            continue
        col = np.array([arr[i] for i in idx], dtype=float)
        if np.isnan(col).all():
            continue
        vals.append(np.nanmax(col) if kind == "high" else np.nanmin(col))
    return np.array(vals, dtype=float)


def fetch_observations(station, start_utc: datetime, session=None):
    s = session or requests
    r = s.get(NWS_OBS_URL.format(station=station),
              params=dict(start=start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=200),
              timeout=30, headers=UA)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        t = p.get("temperature", {}) or {}
        v = t.get("value")
        if v is None:
            continue
        ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
        f_val = v * 9 / 5 + 32 if (t.get("unitCode", "").endswith("degC")) else v
        out.append((ts, float(f_val)))
    return sorted(out)


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
    members = ensemble_daily_extremes(payload, target, kind)
    if members.size == 0:
        raise RuntimeError(f"no ensemble data for {series} {target}")

    # Bias (hourly sampling misses peaks; grid vs sensor) then inflate spread.
    center = np.median(members) + (bias_f if kind == "high" else -bias_f)
    dev = (members - np.median(members)) * config.SPREAD_INFLATION
    rng = np.random.default_rng(int(target.strftime("%Y%m%d")))
    reps = max(1, 2000 // members.size)
    samples = np.tile(center + dev, reps) + rng.normal(0, config.STATION_ERROR_F, members.size * reps)

    obs_ext, n_obs = None, 0
    locked = False
    if target <= local_now.date():
        start_utc = datetime.combine(target, datetime.min.time(), z).astimezone(ZoneInfo("UTC"))
        try:
            obs = fetch_observations(meta["station"], start_utc - timedelta(hours=1), session=session)
            obs_ext, n_obs = observed_extreme(obs, target, meta["tz"], kind)
        except Exception as e:  # observations are an enhancement, never a blocker
            notes.append(f"obs unavailable: {e}")
        if obs_ext is not None:
            hours_left = 24 - local_now.hour if target == local_now.date() else 0
            lock_hour = config.HIGH_LOCKED_HOUR if kind == "high" else config.LOW_LOCKED_HOUR
            locked = target < local_now.date() or local_now.hour >= lock_hour
            if locked:
                # Rounding: climate reports use whole degrees; obs are sub-degree.
                samples = obs_ext + rng.normal(0, 0.4, samples.size)
                notes.append(f"locked on observed {kind} {obs_ext:.1f}F")
            else:
                # Final extreme can't be below (above) what's already observed.
                samples = np.maximum(samples, obs_ext) if kind == "high" else np.minimum(samples, obs_ext)
                # As the day progresses, shrink toward the observed value.
                w = min(1.0, max(0.0, 1 - hours_left / 14))
                samples = (1 - w) * samples + w * np.maximum(samples, obs_ext) if kind == "high" else samples
                notes.append(f"observed so far {obs_ext:.1f}F over {n_obs} obs")

    # Settlement is a whole-degree value; round samples so strike math is exact.
    samples = np.round(samples)
    return Forecast(series, target, kind, samples, obs_ext, n_obs, locked, local_now, notes)
