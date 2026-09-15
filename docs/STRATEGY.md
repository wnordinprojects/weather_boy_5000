# Strategy, and why each rule exists

Goal: make as much money as possible from a ~$180 bankroll, fully autonomous, no manual limits. Every rule below came from a loss or a near miss.

## Core edge

Same-day markets. The thermometer decides these and prices lag it. The model is a distribution of the final daily value: ensemble members corrected by the last 3 hours of observed error, station noise, then clamped by the observed running max/min. Once the observed value is 2F past a strike the market is a "floor" trade: near-certain, priced in the 90s, sized from its own budget.

## Probability used for edge

p = w * p_model + (1 - w) * p_market, w = MODEL_WEIGHT (0.5, adapts 0.3..0.9 from calibration error on settled trades). Capped at 0.97 because the settlement source can disagree with NWS obs by a degree. Floors use w >= 0.85.

Edge = p - price - fee, fee = 7% * p * (1 - p). Must clear EDGE_THRESHOLD (0.06, adapts 0.04..0.16 only when new settlements arrive). Saturated books (spread <= 2c, volume >= 2000) need 3 extra points. Floors need 2 points.

## Sizing and limits

- Half Kelly. 25% of bankroll per event, 35% for floors, 10% for day-ahead events.
- Max 2 new strikes per event (they express one view).
- Never add to a position after its price has fallen below 60% of our cost.
- Reverse a position when edge flips against it by 15 points.
- Same-direction bets across cities (warm/cold) shrink by 1/sqrt(1 + N other events leaning that way). Total exposure capped at 70% of equity.
- Price band 3c..97c. Extreme prices know something about settlement we don't.

## Timing

- Low markets: no new speculative buys before 20:00 local. Day one lost ~$70 buying afternoon lows; the evening cooling curve decides them and isn't visible until late. Floors are exempt.
- Intraday uncertainty never below 0.8F while hours remain (the model once called the last 3 hours "certain").
- 10-minute cycles, 5-minute (env) in the noon..17:00 local window.
- Day-ahead trading turns on per station once history calibration has >= 20 days and residual sd <= 2.5F. It switched on Sep 13 evening. Verdict pending.

## Execution

Spread <= 2c: take the ask, IOC, capped to book depth. Wider: rest at mid for 15 minutes. Resting orders count as held for sizing.

## Learning

- History calibration (45 days) per station: bias, sd, evening bias. Found lows run 2-5F warmer than raw forecast at KMDW/KLAX/KPHL/KMIA.
- Nightly EMA bias from our own forecasts vs observed.
- Series with negative P&L over its last 20 settled trades is benched 7 days.

## Track record

- Sep 13 (day one): -$45..70 at mark, all from afternoon low bets and one large LA high bet. Fixed with the timing and sizing rules above.
- Sep 13 night: evening lows (Boston, New Orleans, Chicago, taken after 20:00) paid. Cash $52 -> $340 by Sep 14 morning.
- Sep 14: blind from ~08:30 PT to 17:00 PT (Open-Meteo quota). No trades.

## Ideas not built

- Fallback forecast source (NWS gridpoint hourly) when Open-Meteo is rate-limited.
- Persist the model cache across restarts.
- Track TWC vs NWS disagreement per station to tune FLOOR_MARGIN_F.
- Sports and mentions markets (dashboard tabs are placeholders).
