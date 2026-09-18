"""Turn forecasts + market prices into sized orders."""
import logging
import math
import re
from dataclasses import dataclass

from . import config
from .weather import Forecast

log = logging.getLogger("strategy")


# --- market parsing --------------------------------------------------------
def _f(m, dollars_key, cents_key):
    if m.get(dollars_key) not in (None, ""):
        return float(m[dollars_key])
    if m.get(cents_key) is not None:
        return float(m[cents_key]) / 100.0
    return None


def prices(m):
    """(yes_bid, yes_ask, no_bid, no_ask, volume_24h, spread) on the 0..1 scale."""
    yb = _f(m, "yes_bid_dollars", "yes_bid") or 0.0
    ya = _f(m, "yes_ask_dollars", "yes_ask") or 1.0
    nb = _f(m, "no_bid_dollars", "no_bid") or max(0.0, 1 - ya)
    na = _f(m, "no_ask_dollars", "no_ask") or max(0.0, 1 - yb)
    vol = float(m.get("volume_24h_fp") or m.get("volume_24h") or m.get("volume_fp") or m.get("volume") or 0)
    return yb, ya, nb, na, vol, ya - yb


def strike(m):
    """Return (kind, lo, hi) describing when YES pays.
    kind: 'gt' (value > lo), 'ge', 'lt' (value < hi), 'le', 'between' (lo <= value <= hi, integers).
    """
    st = (m.get("strike_type") or "").lower()
    fl, cp = m.get("floor_strike"), m.get("cap_strike")
    if st in ("greater", "greater_than") and fl is not None:
        return "gt", float(fl), None
    if st in ("greater_or_equal",) and fl is not None:
        return "ge", float(fl), None
    if st in ("less", "less_than") and cp is not None:
        return "lt", None, float(cp)
    if st in ("less_or_equal",) and cp is not None:
        return "le", None, float(cp)
    if st == "between" and fl is not None and cp is not None:
        return "between", float(fl), float(cp)
    # Fallback: ticker suffix. T79 = "greater than 79" (80 or above). B80.5 = 80..81.
    suf = m["ticker"].rsplit("-", 1)[-1]
    mt = re.match(r"^T(-?\d+(?:\.\d+)?)$", suf)
    if mt:
        return "gt", float(mt.group(1)), None
    mb = re.match(r"^B(-?\d+(?:\.\d+)?)$", suf)
    if mb:
        c = float(mb.group(1))
        return "between", math.floor(c), math.ceil(c)
    raise ValueError(f"cannot parse strike for {m['ticker']}")


def p_yes(fc: Forecast, m) -> float:
    kind, lo, hi = strike(m)
    if kind == "gt":       return fc.prob_gt(lo)
    if kind == "ge":       return fc.prob_ge(lo)
    if kind == "lt":       return fc.prob_lt(hi)
    if kind == "le":       return fc.prob_le(hi)
    # 'between' strikes come as x.5 bounds (79.5..81.5 means 80..81) or integers (inclusive).
    lo_i = math.ceil(lo) if lo != int(lo) else int(lo)
    hi_i = math.floor(hi) if hi != int(hi) else int(hi)
    return fc.prob_between(lo_i, hi_i)


# --- economics -------------------------------------------------------------
def fee(price):
    """Kalshi taker fee per contract, ~7% of p(1-p), rounded up to the cent."""
    return math.ceil(config.FEE_RATE * price * (1 - price) * 100) / 100


def kelly_contracts(p, price, bankroll, fraction):
    """Contracts to buy at `price` (cost per contract) when true prob is `p`."""
    if price <= 0 or price >= 1:
        return 0
    b = (1 - price) / price                 # net odds
    f = (p * b - (1 - p)) / b               # Kelly fraction of bankroll
    if f <= 0:
        return 0
    dollars = fraction * f * bankroll
    return int(dollars / price + 1e-9)


def available_at(orderbook, outcome, yes_price):
    """Contracts we can take right now for `outcome` at YES-scale price `yes_price` or better.

    The book lists bids only. A YES buyer fills against NO bids at 1 - yes_price;
    a NO buyer fills against YES bids at yes_price.
    """
    def levels(key):
        out = []
        for lvl in orderbook.get(key) or []:
            try:
                p, q = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            if p > 1.5:      # legacy cents
                p /= 100.0
            out.append((p, q))
        return out
    if outcome == "yes":
        return int(sum(q for p, q in levels("no_dollars") + levels("no") if 1 - p <= yes_price + 1e-9))
    return int(sum(q for p, q in levels("yes_dollars") + levels("yes") if p >= yes_price - 1e-9))


def is_saturated(spread, vol_24h):
    return spread <= 0.02 and vol_24h >= 2000


@dataclass
class Candidate:
    m: dict
    outcome: str        # 'yes' | 'no'
    p: float            # model prob that this outcome wins
    price: float        # cost per contract for this outcome
    yes_price: float    # price to send on the YES scale
    fee: float
    edge: float
    saturated: bool
    threshold: float
    floor: bool = False # thermometer already decided this market
    direction: str | None = None   # 'warm' | 'cold' | None (pays when temp is higher / lower)
    longshot: bool = False         # model gave this side < LONGSHOT_P_MAX; its p was shrunk


def floor_margin(fc: Forecast, m):
    """Degrees F by which today's observed extreme has already decided this market, or None.

    High markets are decided by the running max M: '>lo' is YES once M passes lo; '<hi' and
    'between' are NO once M passes hi. Low markets mirror with the running min.
    """
    x = fc.observed_extreme
    if x is None or fc.locked:
        return None
    kind, lo, hi = strike(m)
    if fc.kind == "high":
        return x - lo if kind in ("gt", "ge") else x - hi
    return lo - x if kind in ("gt", "ge", "between") else hi - x


def direction(m, outcome):
    """Which way the temperature has to go for this bet to pay."""
    kind, _, _ = strike(m)
    if kind in ("gt", "ge"):
        return "warm" if outcome == "yes" else "cold"
    if kind in ("lt", "le"):
        return "cold" if outcome == "yes" else "warm"
    return None


def complement_norm(markets):
    """Sum of the market mids over an event whose strikes partition the outcome space.

    Returns 1.0 when the strikes are not a partition or the sum is already close to 1;
    otherwise the sum, which callers divide market-implied probabilities by.
    """
    kinds, total = [], 0.0
    for m in markets:
        try:
            k, _, _ = strike(m)
        except Exception:
            return 1.0
        kinds.append(k)
        yb, ya, *_ = prices(m)
        if yb == 0 and ya == 1:
            return 1.0                       # an unpriced leg makes the sum meaningless
        total += (yb + ya) / 2
    has_lo = any(k in ("lt", "le") for k in kinds)
    has_hi = any(k in ("gt", "ge") for k in kinds)
    if not (has_lo and has_hi and len(markets) >= 3):
        return 1.0
    return total if abs(total - 1) > config.COMPLEMENT_TOL else 1.0


def longshot_p(p_side, mkt_side, floor):
    """(probability to trade on, is_longshot) for one side of a market.

    Settled calibration says the model's low-probability claims are inflated: p buckets 0.1-0.5
    hit 2 of 56 when ~14 were expected, while p >= 0.7 was honest. Below LONGSHOT_P_MAX we keep
    only LONGSHOT_SHRINK of the model's disagreement with the market. Floors are exempt: the
    thermometer, not the model, decides those.
    """
    if floor or p_side >= config.LONGSHOT_P_MAX:
        return p_side, False
    return mkt_side + (p_side - mkt_side) * config.LONGSHOT_SHRINK, True


def evaluate(fc: Forecast, m, threshold, model_weight=None, mkt_norm=1.0) -> Candidate | None:
    """Best side to buy on this market, or None.

    The probability used for edge is a blend of the model and the market's own mid: the market
    encodes information the model lacks (station quirks, settlement source), so the model only
    earns full weight once calibration shows its odds are honest. `mkt_norm` rescales the
    market's mid when the event's legs don't sum to 1 (see complement_norm).
    """
    yb, ya, nb, na, vol, spread = prices(m)
    w = config.MODEL_WEIGHT if model_weight is None else model_weight
    p_model = p_yes(fc, m)
    p_mkt = (yb + ya) / 2 / mkt_norm if (yb > 0 or ya < 1) else p_model
    p_mkt = min(1.0, max(0.0, p_mkt))
    fm = floor_margin(fc, m)
    floor = fm is not None and fm >= config.FLOOR_MARGIN_F and (p_model >= 0.99 or p_model <= 0.01)
    if floor:
        # The thermometer has spoken. The market's mid is now mostly settlement-source risk
        # and stale quotes; trust the observation.
        w = max(w, config.FLOOR_MODEL_WEIGHT)
    p = w * p_model + (1 - w) * p_mkt
    p = min(config.MODEL_P_CAP, max(1 - config.MODEL_P_CAP, p))
    sat = is_saturated(spread, vol)
    thr = (config.FLOOR_EDGE if floor else threshold) + (config.SATURATION_PENALTY if sat else 0.0)
    # Extreme prices are where the market has settlement information we lack (and fee
    # drag is worst). Only open inside the tradeable band.
    lo, hi = config.MIN_OPEN_PRICE, config.MAX_OPEN_PRICE
    cands = []
    if lo <= ya <= hi:
        ps, ls = longshot_p(p, p_mkt, floor)
        e = ps - ya - fee(ya)
        cands.append(Candidate(m, "yes", ps, ya, ya, fee(ya), e, sat,
                               thr * (config.LONGSHOT_EDGE_MULT if ls else 1.0), floor, direction(m, "yes"), ls))
    if lo <= na <= hi:
        ps, ls = longshot_p(1 - p, 1 - p_mkt, floor)
        e = ps - na - fee(na)
        cands.append(Candidate(m, "no", ps, na, 1 - na, fee(na), e, sat,
                               thr * (config.LONGSHOT_EDGE_MULT if ls else 1.0), floor, direction(m, "no"), ls))
    if not cands:
        return None
    best = max(cands, key=lambda c: c.edge)
    return best


def plan_orders(fc: Forecast, markets, bankroll, positions, threshold, event_spent, costs=None, model_weight=None,
                corr=None, global_budget=None, event_fraction=None, longshot_budget=None):
    """Decide orders for one event. Returns (orders, decisions).

    positions: {ticker: signed contracts (+yes, -no)}
    event_spent: dollars already committed to this event
    costs: {ticker: average cost per contract on the held outcome's scale}
    corr: {'warm': n, 'cold': n} other open events already leaning that way (correlation shrink)
    global_budget: dollars of new exposure still allowed across all events this cycle (None = no cap)
    event_fraction: override for MAX_EVENT_FRACTION (day-ahead events get a smaller one)
    longshot_budget: dollars still allowed on sub-LONGSHOT_P_MAX bets in the rolling 24h (None = no cap)
    """
    costs = costs or {}
    corr = corr or {}
    orders, decisions = [], []
    ef = config.MAX_EVENT_FRACTION if event_fraction is None else event_fraction
    budget = ef * bankroll - event_spent
    floor_budget = config.FLOOR_EVENT_FRACTION * bankroll - event_spent
    norm = complement_norm(markets)
    cands = []
    for m in markets:
        try:
            c = evaluate(fc, m, threshold, model_weight, norm)
        except Exception as e:
            log.warning("skip %s: %s", m.get("ticker"), e)
            continue
        if c is None:
            continue
        pos = positions.get(m["ticker"], 0)
        held = "yes" if pos > 0 else "no" if pos < 0 else None
        action, count, reason = "hold", 0, ""
        if held and held != c.outcome and c.edge >= config.EXIT_EDGE:
            # Edge flipped hard against an open position: reverse it (sell = buy the other side).
            action, count, reason = "exit", abs(pos), "edge reversed"
        elif held == c.outcome and costs.get(m["ticker"]) and c.price < config.ADD_DRAWDOWN * costs[m["ticker"]]:
            # The market has moved hard against us since entry. Do not average down.
            reason = "market moved against position; not adding"
        elif c.edge >= c.threshold and (held is None or held == c.outcome):
            target = kelly_contracts(c.p, c.price, bankroll, config.KELLY_FRACTION)
            if not c.floor and c.direction and corr.get(c.direction, 0) > 0:
                # Other cities already lean this way today; one synoptic pattern moves them all.
                target = int(target / math.sqrt(1 + corr[c.direction]))
            count = max(0, target - abs(pos))
            if count > 0:
                action, reason = "buy", ("floor" if c.floor else "edge>threshold")
            else:
                reason = "at target size"
        else:
            reason = "edge below threshold" if c.edge < c.threshold else "holding other side"
        if norm != 1.0:
            reason += f" (legs sum {norm:.2f})"
        decisions.append(dict(ticker=m["ticker"], event_ticker=m.get("event_ticker"),
                              outcome=c.outcome, p_model=round(c.p, 4), price=c.price, fee=c.fee,
                              edge=round(c.edge, 4), threshold=c.threshold, saturated=int(c.saturated),
                              action=action, count=count, reason=reason))
        if action in ("buy", "exit") and count > 0:
            cands.append((c, count, action))

    # Highest edge first, spend the event budget in order. Exits always go through;
    # new buys are limited to the best few strikes since they express the same view.
    # Floors draw on their own (larger) budget: they are a different kind of risk.
    cands.sort(key=lambda t: (not t[0].floor, -t[0].edge))
    buys = 0
    for c, count, action in cands:
        if action == "buy":
            already = positions.get(c.m["ticker"], 0) != 0
            if not already and buys >= config.MAX_MARKETS_PER_EVENT and not c.floor:
                continue
            pool = floor_budget if c.floor else budget
            if global_budget is not None:
                pool = min(pool, global_budget)
            if c.longshot and longshot_budget is not None:
                # The whole longshot class shares one rolling-24h wallet, so a bad day costs
                # LONGSHOT_DAILY_FRACTION of equity and no more.
                pool = min(pool, longshot_budget)
            affordable = int(pool // c.price) if c.price > 0 else 0
            count = min(count, affordable)
            if count <= 0:
                continue
            spent = count * c.price
            if c.floor:
                floor_budget -= spent
            else:
                budget -= spent
            if global_budget is not None:
                global_budget -= spent
            if c.longshot and longshot_budget is not None:
                longshot_budget -= spent
            if not already and not c.floor:
                buys += 1
        yb, ya, *_ = prices(c.m)
        orders.append(dict(ticker=c.m["ticker"], event_ticker=c.m.get("event_ticker"),
                           outcome=c.outcome, count=count, yes_price=c.yes_price,
                           p_model=c.p, edge=c.edge, action=action, yes_bid=yb, yes_ask=ya,
                           floor=c.floor, direction=c.direction, longshot=c.longshot,
                           spent=count * c.price))
    return orders, decisions
