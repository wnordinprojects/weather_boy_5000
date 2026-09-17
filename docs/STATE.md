# State (update at the end of any session that changes something)

Last updated: 2026-09-16, ~8pm PT.

- Sep 16 evening: cash $92.87, Kalshi total ~$285 (cash + open bets at bid). Peak was ~$344 on Sep 14; flat to slightly down since. Sep 15's bets roughly broke even at payout (total $299 -> $294). "Balance" in logs is cash only and always lands near $90-100 because the bot reinvests each morning's payout.
- BUG FOUND Sep 16: reconcile() only accepted market status "settled", but Kalshi reports "finalized". Zero results were ever booked, so the scorecard, daily PnL and adapt() (threshold, model weight, benching) were all blind. Fix (pending deploy): accept settled/finalized/determined, and stamp each result with Kalshi's settlement_ts. Test: test_finalized_markets_are_booked_on_kalshis_payout_day. Tests 27/27 pass.
- After that deploy: the first cycle backfills ~3 days of results and adapt() runs for the first time. Expect one threshold step (+0.01 if losing), one model-weight step (+/-0.05), and possibly benched series (20+ results with negative PnL). Check logs for "settled", "calibration error", "benched"; check /api/scorecard by_series and daily fill in.
- "Money over time" chart (change 1) deployed Sep 16 02:09 UTC. cycles `value` (Kalshi portfolio_value) is filling in.
- Systems panel (change 2) is still undeployed and ships in the same push: `agent/health.py` counts outbound calls per service (Kalshi, Open-Meteo, NWS, IEM) incl. 429s and latency, captures WARNING+ lines; `/api/health` gives ok/degraded/down; dashboard gets a "Systems" section and header status pill. After deploy, check /api/health reads "ok".
- Logs noise, harmless: BrokenPipeError when a browser drops a dashboard request; IEM 429s during history calibration; occasional Open-Meteo 503 served from stale cache.
- Dashboard /api is reachable from Chrome only (cloud and Mac shells get a proxy 403). Dashboard URL: https://weatherfrog.up.railway.app (OPERATIONS.md still lists the old weatherboy5000 URL).
- Settlements pay out ~4-10am PT the next morning; the bot books each on its next cycle.
- Gotcha: device_commit_files with a stagedPath committed earlier can write the OLD content. Stage each commit under a new filename and verify with md5sum.
- Open questions: is the bot net profitable at all after Sep 14 (answer once the scorecard fills in); is day-ahead trading earning its keep; do floor trades ever fill at <= 97c.
- Housekeeping Walker still owes: add a card to Railway before the trial ends; delete the GitHub token he pasted in chat on Sep 13.
