# Operations

## Deploy

1. Edit files in `~/Desktop/kalshi-weather-agent` (Walker's Mac, linked to the Cowork session). Write whole files; don't reconstruct from truncated tool output.
2. `python3 -m pytest -q tests` in the cloud copy or on the Mac if pytest exists there (it doesn't by default).
3. Tell Walker: `./deploy.sh "message"`. That's `git add -A && git commit && git push`. Railway builds the Dockerfile and swaps in the new deployment in ~40s. The SQLite volume persists.
4. Never run git on his Mac from the device shell.

## Verify

- Railway connector: `list-deployments` (commit message and status), `get-logs` with a filter. Useful filters: `cycle done`, `WARNING`, `buy`, `rest`, `settled`, `discovered`, `history`, `429`, `Traceback`. Log lines show as severity "error" because they go to stderr; that's cosmetic.
- Dashboard: https://weatherboy5000-production.up.railway.app. `/logs` is the last 400 lines. `/api/summary` has cash, equity at bid/mid/model, exposure. `/api/positions` has every open position with cost, mark, model EV.
- A healthy cycle line: `cycle done: balance X markets ~400 orders N thr 0.060`. `markets 0` means forecasts are failing (check for 429).

## Env vars (Railway service variables)

Set with the `set-variables` tool; it redeploys. Secrets: KALSHI_KEY_ID, KALSHI_PRIVATE_KEY (never touch). Operational: DRY_RUN, CYCLE_SECONDS, CYCLE_SECONDS_FAST, MODEL_CACHE_S, HRRR_CACHE_S, ENSEMBLE_DAYS, DAY_AHEAD_AUTO, DISCOVER_SERIES, DISPLAY_MULT, plus every strategy number in config.py.

Kill switch: set `DRY_RUN=true`. The bot keeps evaluating and logging but places no orders. Open positions stay open.

## Known failure modes

- Open-Meteo 429: bot goes blind until midnight UTC. Fixed with 1h cache, 2-day ensemble, backoff, stale-serve. If it recurs, raise MODEL_CACHE_S or drop cities.
- Kalshi private key flattened in Railway UI: `normalize_pem` rebuilds it; if signing fails, the variable needs BEGIN/END lines.
- Insufficient balance is logged as a skip, not an error.
- 3 consecutive cycle errors halts trading for 1h, then retries.

## Dashboard UI notes

Light theme, cream #f6f1e7, Newsreader / IBM Plex Sans / IBM Plex Mono. Sky panorama with weather effects. Big hypnotic frog header with cycling pupils, green happy lasers (manic grin) when equity rises, blue sad lasers (tears) when it falls. Small frog hops on a new fill; duck empty state. All dollars x10 with a label at the page bottom. Header reads "weatherfrog.edu" with a rainbow that shimmers 3 times then parks black. Cities grid scrolls in a box matched to the Activity log column height. Positions table is compact with a "more columns" toggle and a total row.
