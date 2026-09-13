# Kalshi Weather Agent

Autonomous trader for Kalshi daily high/low temperature markets. Runs 24/7 on Railway.

## How it makes money

1. Every 10 minutes it pulls every open temperature event for the next 3 days across 7 cities (high and low series).
2. For each city-day it builds a probability distribution of the final settled value from ~90 ensemble
   weather model members (GFS, ECMWF, ICON via Open-Meteo), bias-corrected per station and spread-inflated.
3. Intraday it pulls the station's NWS observations. The running max is a hard floor on the day's high,
   and after 7pm local the high is locked. This is the main edge: prices often lag what the thermometer
   has already done.
4. For every strike it computes model probability minus price minus fee. If that edge clears the
   threshold, it sizes with half Kelly and sends an immediate-or-cancel order at the ask.
5. It learns: threshold moves with realized P&L, station bias updates nightly from observed vs forecast,
   and a series that loses over its last 20 settled trades is benched for a week (rotation).

Markets that look bot-saturated (spread <= 2c and heavy volume) need 3 extra points of edge.

## Deploy to Railway (about 5 minutes)

1. Push this folder to a GitHub repo (private). Do not commit the .pem.
2. Railway -> New Project -> Deploy from GitHub repo -> pick it. It detects the Dockerfile.
3. Service -> Variables -> Raw Editor, paste:
   ```
   KALSHI_KEY_ID=<from key_id.txt>
   KALSHI_PRIVATE_KEY=<entire contents of kalshi.pem, BEGIN to END lines>
   DRY_RUN=true
   ```
   Multi-line values work in the raw editor. If Railway flattens it, replace newlines with `\n`.
4. Service -> Settings -> Volumes -> add a volume mounted at `/data` (keeps the SQLite log across deploys).
5. Service -> Settings -> Networking -> Generate Domain. That URL is the dashboard.
6. Deploy. Watch logs for `cycle done`. Check the dashboard: forecasts and decisions should populate.
7. Flip `DRY_RUN` to `false`. Railway redeploys. It's live.

Alternative without GitHub: `npm i -g @railway/cli && railway login && railway init && railway up` from this folder.

## Files

- `agent/config.py` every tunable. Cities, thresholds, Kelly fraction, cycle time.
- `agent/kalshi.py` signed API client (V2 single-book orders, legacy fallback).
- `agent/weather.py` ensemble + observations -> distribution.
- `agent/strategy.py` strike math, edge, sizing, order planning.
- `agent/agent.py` loop, settlement reconciliation, adaptive threshold, calibration, rotation.
- `agent/dashboard.py` status page on $PORT, JSON at /status.json.
- `tests/` offline tests. `python -m pytest tests`.

## Guards (bugs, not risk appetite)

- 3 consecutive API errors -> pause 1 hour, then resume.
- Never adds beyond Kelly target on a ticker; max 50% of bankroll per city-day event.
- Immediate-or-cancel orders only: nothing rests on the book.

## Things to watch in week one

- `rules for ... do not mention` warnings in logs mean a station mapping in config.py is wrong.
- If the V2 order path 404s, the client falls back to the legacy order shape automatically. Check the
  `raw` column in the orders table for what actually happened.
- Fees: FEE_RATE assumes 7% of p(1-p). If weather series have maker fees or a different multiplier, adjust.
