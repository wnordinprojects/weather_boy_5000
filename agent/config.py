"""All tunables in one place. Env vars override defaults.

Every number here is a starting point. The agent adjusts EDGE_THRESHOLD and
per-station bias on its own from settled results (see calibrate.py).
"""
import os


def _env(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "on")
    return cast(v)


# ---- Kalshi ---------------------------------------------------------------
KALSHI_BASE = _env("KALSHI_BASE", "https://external-api.kalshi.com/trade-api/v2")
KALSHI_KEY_ID = _env("KALSHI_KEY_ID", "")
# Private key: either the PEM text itself (Railway secret) or a file path.
KALSHI_PRIVATE_KEY = _env("KALSHI_PRIVATE_KEY", "")
KALSHI_PRIVATE_KEY_PATH = _env("KALSHI_PRIVATE_KEY_PATH", "")

# ---- Run mode -------------------------------------------------------------
DRY_RUN = _env("DRY_RUN", False, bool)          # log decisions, place no orders
CYCLE_SECONDS = _env("CYCLE_SECONDS", 600, int)  # 10 min. Weather moves intraday.
DB_PATH = _env("DB_PATH", "/data/agent.db")
PORT = _env("PORT", 8080, int)
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
# Dashboard only: multiply displayed dollar amounts (contract prices stay real). Trading math ignores it.
DISPLAY_MULT = _env("DISPLAY_MULT", 10.0, float)

# ---- Strategy -------------------------------------------------------------
# Net edge (model prob - price - est. fee) required to open. Adaptive; this is the seed.
EDGE_THRESHOLD = _env("EDGE_THRESHOLD", 0.06, float)
EDGE_MIN, EDGE_MAX = 0.04, 0.16
# Extra edge demanded on markets that look bot-saturated (tight spread + heavy volume).
SATURATION_PENALTY = _env("SATURATION_PENALTY", 0.03, float)
# Kelly fraction. 0.5 = half Kelly. Full Kelly on correlated weather bets blows up.
KELLY_FRACTION = _env("KELLY_FRACTION", 0.5, float)
# Cap per event (all strikes on one city-day are the same bet). Fraction of bankroll.
MAX_EVENT_FRACTION = _env("MAX_EVENT_FRACTION", 0.25, float)
# Strikes on one city-day are near-perfectly correlated for a nowcast: take at most this many.
MAX_MARKETS_PER_EVENT = _env("MAX_MARKETS_PER_EVENT", 2, int)
# Weight on the model's probability vs the market's implied probability when computing edge.
# 0.5 = trust them equally. Rises automatically as calibration proves the model (see adapt()).
MODEL_WEIGHT = _env("MODEL_WEIGHT", 0.5, float)
MODEL_WEIGHT_MIN, MODEL_WEIGHT_MAX = 0.3, 0.9
# Never let the model claim certainty. Settlement source (TWC) can differ from NWS obs by a degree.
MODEL_P_CAP = _env("MODEL_P_CAP", 0.97, float)
# How many days ahead to trade. 0 = same-day only (live observations are the edge).
# Raise to 1-2 once per-station calibration has a week of data.
DAYS_AHEAD = _env("DAYS_AHEAD", 0, int)
# Low-temperature markets: only open new positions after this local hour. A calendar-day low is
# decided by the evening cooling curve, which is only visible late; afternoon bets on it lost all day one.
LOW_TRADE_AFTER_HOUR = _env("LOW_TRADE_AFTER_HOUR", 20, int)
# Only open positions priced inside this band. A liquid 1c or 99c market that disagrees
# with the model usually knows something about settlement that the model does not.
MIN_OPEN_PRICE = _env("MIN_OPEN_PRICE", 0.03, float)
MAX_OPEN_PRICE = _env("MAX_OPEN_PRICE", 0.97, float)
# Don't add to a position once its price has fallen below this fraction of our average cost.
ADD_DRAWDOWN = _env("ADD_DRAWDOWN", 0.6, float)
# Reverse an open position when model edge flips against it by this much.
EXIT_EDGE = _env("EXIT_EDGE", 0.15, float)
# Execution: when the spread is wider than this, rest a limit order at mid instead of taking the ask.
MAKER_SPREAD = _env("MAKER_SPREAD", 0.02, float)
# How long a resting order lives before the exchange cancels it (seconds). Keep under 2 cycles.
MAKER_TTL = _env("MAKER_TTL", 900, int)
# Taker fee estimate: Kalshi charges ~0.07 * p * (1-p) per contract on most series.
FEE_RATE = _env("FEE_RATE", 0.07, float)
# Bench a series for this many days if its realized edge over the last N trades is negative.
ROTATION_WINDOW = _env("ROTATION_WINDOW", 20, int)
ROTATION_BENCH_DAYS = _env("ROTATION_BENCH_DAYS", 7, int)
# Halt trading after this many consecutive API errors (bug guard, not a risk limit).
MAX_CONSECUTIVE_ERRORS = 3

# ---- Forecast model -------------------------------------------------------
# Ensemble spreads are underdispersed and grid cells are not the ASOS sensor.
# Inflate spread and add a floor of station error (deg F).
SPREAD_INFLATION = _env("SPREAD_INFLATION", 1.3, float)
STATION_ERROR_F = _env("STATION_ERROR_F", 1.6, float)
# Hourly sampling misses the true daily max by a bit. Learned per station over time.
DEFAULT_MAX_BIAS_F = _env("DEFAULT_MAX_BIAS_F", 0.8, float)
ENSEMBLE_MODELS = _env("ENSEMBLE_MODELS", "gfs_seamless,ecmwf_ifs025,icon_seamless")

# ---- Markets --------------------------------------------------------------
# Candidate series. Discovery drops any with no open events. Add new cities here.
# station: NWS/METAR ID used for intraday observations. lat/lon are the station.
STATIONS = {
    "KXHIGHNY":   dict(city="New York",     station="KNYC", lat=40.779, lon=-73.969, tz="America/New_York"),
    "KXHIGHCHI":  dict(city="Chicago",      station="KMDW", lat=41.786, lon=-87.752, tz="America/Chicago"),
    "KXHIGHMIA":  dict(city="Miami",        station="KMIA", lat=25.795, lon=-80.290, tz="America/New_York"),
    "KXHIGHAUS":  dict(city="Austin",       station="KAUS", lat=30.194, lon=-97.670, tz="America/Chicago"),  # CLIAUS = Bergstrom, not Camp Mabry
    "KXHIGHLAX":  dict(city="Los Angeles",  station="KLAX", lat=33.938, lon=-118.389, tz="America/Los_Angeles"),
    "KXHIGHDEN":  dict(city="Denver",       station="KDEN", lat=39.847, lon=-104.656, tz="America/Denver"),
    "KXHIGHPHIL": dict(city="Philadelphia", station="KPHL", lat=39.873, lon=-75.241, tz="America/New_York"),
}
# Low-temp series share stations. Ticker family was renamed to KXLOWT* in Aug 2026.
LOW_SERIES = {f"KXLOWT{k[6:]}": v for k, v in STATIONS.items()}
SERIES = {**STATIONS, **LOW_SERIES}

