# State (update at the end of any session that changes something)

Last updated: 2026-09-14, afternoon PT.

- Start bankroll $179.43 (Sep 13). Cash $340 on Sep 14 morning after the Sep 13 lows settled. Small Sep 14 day-ahead positions open (Miami 92-93 NO x7, LAX >82, Philly 74-75, Atlanta >92, all tiny).
- Latest deployed commit: "park rainbow black" plus Railway env `MODEL_CACHE_S=3600`, `CYCLE_SECONDS_FAST=300`.
- Pending push on Walker's Mac: Open-Meteo quota fix (1h cache, 2-day ensemble, 429 backoff, stale-serve, fast cycles pause while rate-limited). Command: `./deploy.sh "open-meteo quota fix"`.
- Bot has been blind since ~08:30 PT Sep 14 (429). Quota resets 17:00 PT. First thing to check next session: `get-logs` filter `cycle done` shows markets > 0.
- Open questions: is day-ahead trading earning its keep (watch the 3-7c tail buys); do floor trades ever fill at <= 97c; tonight's evening-low sizing with the 0.8F uncertainty floor.
- Housekeeping Walker still owes: add a card to Railway before the trial ends; delete the GitHub token he pasted in chat on Sep 13.
