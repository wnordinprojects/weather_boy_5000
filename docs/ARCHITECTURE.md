# Architecture

Python 3.12, no framework. One process on Railway runs the trading loop and a small HTTP dashboard. SQLite at `/data/agent.db` (Railway volume) is the only state.

Repo: github.com/wnordinprojects/weather_boy_5000. Railway project "meticulous-embrace", service id 0b3a1f3d-7699-4a68-9048-8dcc80672507, project id 5e29a86d-3510-42da-adf4-8055d6632531. Auto-deploys on push to main.

## Files (agent/)

- `config.py` Every tunable, env-overridable. Read this first when a question is "what is X set to".
- `agent.py` Main loop. `run_forever` -> `discovery`, `history_calibration`, `reconcile`, `cycle`. `cycle` fetches balance/positions/resting orders from Kalshi, builds a forecast per city-day, calls `plan_orders`, executes. `execute` rests a limit at mid when spread > 2c, otherwise takes the ask IOC capped to book depth. `reconcile` books settlements and calls `adapt` (threshold, model weight, benching). `RingLog` feeds /logs.
- `strategy.py` Pure decision logic. `strike(m)` parses a market into gt/lt/between. `evaluate` blends model and market probability, detects floor trades, computes edge net of fee. `plan_orders` sizes with half Kelly, applies event budget, 2 strikes per event, no averaging down, exits on reversed edge, correlation shrink, global exposure cap. `complement_norm` rescales market probs when an event's legs don't sum to 1.
- `weather.py` Forecast distribution. Open-Meteo ensemble (GFS/ECMWF/ICON) blended 50/50 with HRRR for today/tomorrow, station history calibration applied, nowcast corrects members by the last 3 hours of NWS observations, station noise added, then observed running max/min applied as floor/ceiling. METAR 6-hour max/min groups override hourly obs. Model downloads are cached (1h ensemble, 30m HRRR) with 429 backoff and stale-serve.
- `history.py` 45-day calibration per station: Open-Meteo previous-runs forecast vs IEM ASOS actuals -> high/low bias, residual sd, evening bias. Stored in db state `hist:<station>`.
- `discovery.py` Finds every Kalshi temperature series (`GET /series?category=Climate and Weather`), reads the station from the rules text `(CLIxxx)`, resolves lat/lon/tz from NWS. ~25 cities as of Sep 14.
- `kalshi.py` Signed REST client (RSA-PSS). `place` sends V2 orders; buy NO = ask at yes price.
- `db.py` Schema and helpers. Tables: cycles, forecasts, decisions, orders, settlements, calibration, bench, state, skips.
- `api.py` JSON for the dashboard, short Kalshi cache. `summary`, `positions`, `cities`, `scorecard`, `activity`.
- `dashboard.py` ThreadingHTTPServer routes. `dashboard.html` single-file UI (polls 5s/20s/60s).

## Data flow per cycle

Kalshi events -> for each city-day: forecast samples -> per strike p_yes -> blend with market mid -> edge -> Kelly size within budgets -> order -> db. Settlements are picked up the next cycle after a market settles and feed threshold/weight/benching.

## External services and limits

- Kalshi API v2 `https://external-api.kalshi.com/trade-api/v2`. Settlement source is The Weather Company; rules name the station as CLIxxx.
- Open-Meteo (free, no key). Daily quota; ensemble calls are weighted heavily. This blew up once (see STATE.md). Keep MODEL_CACHE_S >= 3600.
- NWS `api.weather.gov` observations (500/page, paginated). IEM ASOS archive for history.

## Tests

`tests/test_agent.py`, 24 tests, all pure/mocked, run in under a second. Add one per behavior change.
