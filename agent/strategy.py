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


def evaluate(fc: Forecast, m, threshold) -> Candidate | None:
    """Best side to buy on this market, or None."""
    yb, ya, nb, na, vol, spread = prices(m)
    p = p_yes(fc, m)
    sat = is_saturated(spread, vol)
    thr = threshold + (config.SATURATION_PENALTY if sat else 0.0)
    # Extreme prices are where the market has settlement information we lack (and fee
    # drag is worst). Only open inside the tradeable band.
    lo, hi = config.MIN_OPEN_PRICE, config.MAX_OPEN_PRICE
    cands = []
    if lo <= ya <= hi:
        e = p - ya - fee(ya)
        cands.append(Candidate(m, "yes", p, ya, ya, fee(ya), e, sat, thr))
    if lo <= na <= hi:
        e = (1 - p) - na - fee(na)
        cands.append(Candidate(m, "no", 1 - p, na, 1 - na, fee(na), e, sat, thr))
    if not cands:
        return None
    best = max(cands, key=lambda c: c.edge)
    return best


def plan_orders(fc: Forecast, markets, bankroll, positions, threshold, event_spent, costs=None):
    """Decide orders for one event. Returns (orders, decisions).

    positions: {ticker: signed contracts (+yes, -no)}
    event_spent: dollars already committed to this event
    costs: {ticker: average cost per contract on the held outcome's scale}
    """
    costs = costs or {}
    orders, decisions = [], []
    budget = config.MAX_EVENT_FRACTION * bankroll - event_spent
    cands = []
    for m in markets:
        try:
            c = evaluate(fc, m, threshold)
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
            count = max(0, target - abs(pos))
            if count > 0:
                action, reason = "buy", "edge>threshold"
            else:
                reason = "at target size"
        else:
            reason = "edge below threshold" if c.edge < c.threshold else "holding other side"
        decisions.append(dict(ticker=m["ticker"], event_ticker=m.get("event_ticker"),
                              outcome=c.outcome, p_model=round(c.p, 4), price=c.price, fee=c.fee,
                              edge=round(c.edge, 4), threshold=c.threshold, saturated=int(c.saturated),
                              action=action, count=count, reason=reason))
        if action in ("buy", "exit") and count > 0:
            cands.append((c, count, action))

    # Highest edge first, spend the event budget in order.
    cands.sort(key=lambda t: -t[0].edge)
    for c, count, action in cands:
        if action == "buy":
            affordable = int(budget // c.price) if c.price > 0 else 0
            count = min(count, affordable)
            if count <= 0:
                continue
            budget -= count * c.price
        orders.append(dict(ticker=c.m["ticker"], event_ticker=c.m.get("event_ticker"),
                           outcome=c.outcome, count=count, yes_price=c.yes_price,
                           p_model=c.p, edge=c.edge, action=action))
    return orders, decisions
