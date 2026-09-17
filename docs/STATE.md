# State (update at the end of any session that changes something)

Last updated: 2026-09-16, ~8pm PT.

- Sep 16 evening: cash $92.87, Kalshi total ~$285 (cash + open bets at bid). Peak was ~$344 on Sep 14; flat to slightly down since. Sep 15's bets roughly broke even at payout (total $299 -> $294). "Balance" in logs is cash only and always lands near $90-100 because the bot reinvests each morning's payout.
- Sep 16 ~8pm PT: deployed d4b5ee3 "book finalized settlements + systems panel". Systems pill reads ok. Backfill booked 60 results but showed settled -$531.57 (18/60 won), which is impossible: bad day-one (Sep 13) order rows produced -$336 (BOS low B62.5), -$233 (NOLA low), +$163 (CHI low) on single markets. Sep 13 was really a winning day (cash $179 -> $340). adapt() ran once on that data: model weight 0.50 -> 0.45, threshold 0.06 -> 0.07. Both are cautious moves and were left as-is.
- Fix 2 (pending deploy): reconcile() now books PnL from Kalshi's own /portfolio/settlements records (revenue - yes/no cost - fees), falling back to our fills only when Kalshi has no record (position sold out before settlement). A one-time rebook wipes and rebuilds the settlements table and does NOT run adapt() again. The first run logs "kalshi settlement fields: [...]"; check that revenue/cost fields parsed (money fields may be *_dollars or cents). Known limit: profit from positions sold before settlement isn't counted. Tests 28/28.
- The /api/positions 500 on the dashboard was a one-off during the restart; the endpoint works.
- "Money over time" chart (change 1) deployed Sep 16 02:09 UTC. cycles `value` (Kalshi portfolio_value) is filling in.
- Systems panel (change 2) deployed Sep 16 ~8pm PT (d4b5ee3): `agent/health.py` counts outbound calls per service (Kalshi, Open-Meteo, NWS, IEM) incl. 429s and latency, captures WARNING+ lines; `/api/health` gives ok/degraded/down; dashboard gets a "Systems" section and header status pill. /api/health reads "ok".
- Logs noise, harmless: BrokenPipeError when a browser drops a dashboard request; IEM 429s during history calibration; occasional Open-Meteo 503 served from stale cache.
- Dashboard /api is reachable from Chrome only (cloud and Mac shells get a proxy 403). Dashboard URL: https://weatherfrog.up.railway.app (OPERATIONS.md still lists the old weatherboy5000 URL).
- Settlements pay out ~4-10am PT the next morning; the bot books each on its next cycle.
- Gotcha: device_commit_files with a stagedPath committed earlier can write the OLD content. Stage each commit under a new filename and verify with md5sum.
- Open questions: is the bot net profitable at all after Sep 14 (answer once the scorecard fills in); is day-ahead trading earning its keep; do floor trades ever fill at <= 97c.
- Housekeeping Walker still owes: add a card to Railway before the trial ends; delete the GitHub token he pasted in chat on Sep 13.
