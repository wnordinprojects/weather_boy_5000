"""Offline tests: signing, strike math, forecast distribution, sizing, full-cycle with mocked APIs."""
import base64
import os
import tempfile
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DRY_RUN"] = "false"

from agent import config, kalshi, strategy, weather  # noqa: E402
from agent.db import DB  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_verifies_and_ignores_query():
    sig = kalshi.sign(KEY, 1700000000000, "get", "/trade-api/v2/portfolio/orders")
    KEY.public_key().verify(
        base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    k = kalshi.Kalshi(key_id="kid", private_key=KEY)
    h = k._headers("GET", "/markets")
    assert set(h) >= {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
    assert k.prefix == "/trade-api/v2"


def _fc(samples, kind="high"):
    return weather.Forecast("KXHIGHNY", date(2026, 9, 13), kind, np.array(samples, float), None, 0, False,
                            datetime(2026, 9, 13, 12, tzinfo=ZoneInfo("America/New_York")))


def test_strike_parsing_and_probabilities():
    fc = _fc([78, 79, 80, 81, 82, 83, 84, 85, 86, 87])
    above = dict(ticker="KXHIGHNY-26SEP13-T84", strike_type="greater", floor_strike=83.5)
    between = dict(ticker="KXHIGHNY-26SEP13-B80.5", strike_type="between", floor_strike=79.5, cap_strike=81.5)
    below = dict(ticker="KXHIGHNY-26SEP13-T79", strike_type="less", cap_strike=79.5)
    assert strategy.p_yes(fc, above) == pytest.approx(0.4)      # 84..87
    assert strategy.p_yes(fc, between) == pytest.approx(0.2)    # 80, 81
    assert strategy.p_yes(fc, below) == pytest.approx(0.2)      # 78, 79
    # ticker-only fallback
    assert strategy.strike(dict(ticker="KXHIGHNY-26SEP13-T84")) == ("gt", 84.0, None)
    assert strategy.strike(dict(ticker="KXHIGHNY-26SEP13-B80.5")) == ("between", 80, 81)


def test_prices_prefer_dollar_fields_and_fall_back_to_cents():
    yb, ya, nb, na, vol, spread = strategy.prices(dict(yes_bid_dollars="0.40", yes_ask_dollars="0.45",
                                                       no_bid_dollars="0.55", no_ask_dollars="0.60", volume_24h_fp="10.00"))
    assert (yb, ya, nb, na, vol) == (0.40, 0.45, 0.55, 0.60, 10.0)
    yb, ya, *_ = strategy.prices(dict(yes_bid=40, yes_ask=45))
    assert (yb, ya) == (0.40, 0.45)


def test_kelly_and_fee():
    assert strategy.fee(0.5) == pytest.approx(0.02)     # ceil(0.07*0.25*100)/100
    assert strategy.kelly_contracts(0.7, 0.5, 100, 0.5) == 40   # f=0.4, half -> $20 -> 40 contracts
    assert strategy.kelly_contracts(0.4, 0.5, 100, 0.5) == 0


def test_plan_orders_buys_best_side_and_respects_event_budget():
    fc = _fc([96] * 2 + [90] * 5 + [80] * 3)  # P(>85)=0.7, P(>95)=0.2
    ms = [dict(ticker="E-T85", event_ticker="E", strike_type="greater", floor_strike=84.5,
               yes_bid_dollars="0.50", yes_ask_dollars="0.55", no_bid_dollars="0.45", no_ask_dollars="0.50", volume_24h_fp="10"),
          dict(ticker="E-T95", event_ticker="E", strike_type="greater", floor_strike=94.5,
               yes_bid_dollars="0.30", yes_ask_dollars="0.35", no_bid_dollars="0.65", no_ask_dollars="0.70", volume_24h_fp="10")]
    orders, decisions = strategy.plan_orders(fc, ms, bankroll=1000, positions={}, threshold=0.06, event_spent=0, model_weight=1.0)
    by = {o["ticker"]: o for o in orders}
    assert by["E-T85"]["outcome"] == "yes" and by["E-T85"]["yes_price"] == 0.55          # buy yes at ask
    assert by["E-T95"]["outcome"] == "no" and by["E-T95"]["yes_price"] == pytest.approx(0.30)  # buy no at 1 - no_ask
    spent = sum(o["count"] * (o["yes_price"] if o["outcome"] == "yes" else 1 - o["yes_price"]) for o in orders)
    assert spent <= config.MAX_EVENT_FRACTION * 1000 + 1e-9
    # small bankroll: the best strike eats the whole event budget, second gets nothing
    orders_small, _ = strategy.plan_orders(_fc([90] * 9 + [80]), ms, bankroll=100, positions={}, threshold=0.06, event_spent=0, model_weight=1.0)
    assert len(orders_small) == 1 and orders_small[0]["count"] * orders_small[0]["yes_price"] <= 25 + 1e-9
    # never more than MAX_MARKETS_PER_EVENT new strikes per event
    ms3 = ms + [dict(ticker="E-T88", event_ticker="E", strike_type="greater", floor_strike=88.5,
                     yes_bid_dollars="0.50", yes_ask_dollars="0.56", no_bid_dollars="0.44", no_ask_dollars="0.50", volume_24h_fp="10")]
    orders3, _ = strategy.plan_orders(fc, ms3, bankroll=10000, positions={}, threshold=0.06, event_spent=0, model_weight=1.0)
    assert len(orders3) == config.MAX_MARKETS_PER_EVENT
    # already at size -> no order
    orders2, _ = strategy.plan_orders(fc, ms[:1], 100, {"E-T85": 500}, 0.06, 0, model_weight=1.0)
    assert orders2 == []
    # holding the wrong side with a big reversed edge -> exit
    orders3, _ = strategy.plan_orders(_fc([90] * 9 + [80]), ms[:1], 100, {"E-T85": -10}, 0.06, 0, model_weight=1.0)
    assert orders3 and orders3[0]["action"] == "exit" and orders3[0]["outcome"] == "yes" and orders3[0]["count"] == 10


def test_available_at_reads_bids_correctly():
    ob = {"yes_dollars": [["0.40", "10.00"], ["0.38", "5.00"]], "no_dollars": [["0.55", "7.00"], ["0.50", "3.00"]]}
    # buy YES at 0.45: fills against NO bids at >= 0.55 -> 7
    assert strategy.available_at(ob, "yes", 0.45) == 7
    assert strategy.available_at(ob, "yes", 0.50) == 10
    # buy NO at yes-price 0.40: fills against YES bids at >= 0.40 -> 10
    assert strategy.available_at(ob, "no", 0.40) == 10
    assert strategy.available_at(ob, "no", 0.41) == 0
    assert strategy.available_at({}, "yes", 0.5) == 0


def test_price_band_blocks_extreme_prices():
    fc = _fc([90] * 9 + [80])
    m = dict(ticker="E-T85", event_ticker="E", strike_type="greater", floor_strike=84.5,
             yes_bid_dollars="0.00", yes_ask_dollars="0.01", no_bid_dollars="0.99", no_ask_dollars="1.00", volume_24h_fp="9000")
    assert strategy.evaluate(fc, m, 0.06) is None


def test_observations_paginate():
    pages = [{"features": [{"properties": {"timestamp": f"2026-09-13T{h:02d}:00:00+00:00",
                                            "temperature": {"value": 20 + h, "unitCode": "wmoUnit:degC"}}} for h in range(10)] * 50,
              "pagination": {"next": "https://x/next"}},
             {"features": [{"properties": {"timestamp": "2026-09-13T23:00:00+00:00",
                                            "temperature": {"value": 35.0, "unitCode": "wmoUnit:degC"}}}]}]
    calls = []

    class R:
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j

    def get(url, **kw):
        calls.append(url)
        return R(pages[len(calls) - 1])
    obs = weather.fetch_observations("KMDW", datetime(2026, 9, 13, 5, tzinfo=ZoneInfo("UTC")), mock.Mock(get=get))
    assert len(calls) == 2 and max(v for _, v in obs) == pytest.approx(95.0)


def test_no_averaging_down():
    fc = _fc([90] * 9 + [80])
    m = dict(ticker="E-T85", event_ticker="E", strike_type="greater", floor_strike=84.5,
             yes_bid_dollars="0.20", yes_ask_dollars="0.22", no_bid_dollars="0.78", no_ask_dollars="0.80", volume_24h_fp="10")
    orders, dec = strategy.plan_orders(fc, [m], 100, {"E-T85": 10}, 0.06, 0, costs={"E-T85": 0.55}, model_weight=1.0)
    assert orders == [] and "not adding" in dec[0]["reason"]
    orders, _ = strategy.plan_orders(fc, [m], 100, {"E-T85": 10}, 0.06, 0, costs={"E-T85": 0.25}, model_weight=1.0)
    assert orders and orders[0]["action"] == "buy"


def test_noise_cannot_cross_observed_floor():
    """Members that say 'no further warming' must not produce samples below the observed max."""
    hourly = np.array([[60 + h if h < 14 else 74 - (h - 14) for h in range(24)]] * 20, float)
    now = datetime(2026, 9, 13, 20, tzinfo=ZoneInfo("America/New_York"))
    obs = [(datetime(2026, 9, 13, 17, tzinfo=ZoneInfo("UTC")), 80.0)]
    fut, left = weather.nowcast(hourly, obs, date(2026, 9, 13), "America/New_York", "high", now)
    assert left == 4 and fut.max() < 80          # evening hours are cooler than the 80 already seen
    # low case: model ran 4F warm at dawn but is spot-on this afternoon -> dawn error must NOT
    # be carried into the evening. Members cool to 58 by 23:00; recent obs match members exactly.
    prof = [64 - abs(h - 5) * 0 + (0 if h < 15 else -(h - 15) * 0.9) for h in range(24)]   # flat 64 then cooling to ~56.8
    hourly_low = np.array([prof] * 20, float)
    ny = ZoneInfo("America/New_York")
    obs_low = [(datetime(2026, 9, 13, 5, 30, tzinfo=ny), 60.0)] + \
              [(datetime(2026, 9, 13, h, 30, tzinfo=ny), prof[h]) for h in (17, 18, 19)]
    fut_low, _ = weather.nowcast(hourly_low, obs_low, date(2026, 9, 13), "America/New_York", "low", now)
    assert fut_low.min() < 58            # evening cooling below the dawn low is still in play


def test_market_blend_tames_overconfidence():
    """Model 57% vs market 8c: blended 32%, edge ~23 -> still trades but far smaller; at 0.3 weight, no trade."""
    fc = _fc([56] * 57 + [60] * 43)
    m = dict(ticker="C-T57", event_ticker="C", strike_type="less", cap_strike=57,
             yes_bid_dollars="0.02", yes_ask_dollars="0.08", no_bid_dollars="0.92", no_ask_dollars="0.98", volume_24h_fp="500")
    full = strategy.evaluate(fc, m, 0.06, model_weight=1.0)
    half = strategy.evaluate(fc, m, 0.06, model_weight=0.5)
    low = strategy.evaluate(fc, m, 0.06, model_weight=0.3)
    assert full.p > half.p > low.p
    assert strategy.kelly_contracts(half.p, 0.08, 100, 0.5) < strategy.kelly_contracts(full.p, 0.08, 100, 0.5) / 2


def test_saturation_raises_threshold():
    fc = _fc([90] * 9 + [80])
    m = dict(ticker="E-T85", event_ticker="E", strike_type="greater", floor_strike=84.5,
             yes_bid_dollars="0.80", yes_ask_dollars="0.81", no_bid_dollars="0.19", no_ask_dollars="0.20", volume_24h_fp="50000")
    c = strategy.evaluate(fc, m, 0.06, model_weight=1.0)
    assert c.saturated and c.threshold == pytest.approx(0.09)


ENSEMBLE = {"hourly": {
    "time": [f"2026-09-13T{h:02d}:00" for h in range(24)] + [f"2026-09-14T{h:02d}:00" for h in range(24)],
    "temperature_2m": [70 + (h if h < 15 else 30 - h) for h in range(24)] * 2,
    "temperature_2m_member01": [72 + (h if h < 15 else 30 - h) for h in range(24)] * 2,
    "temperature_2m_member02": [68 + (h if h < 15 else 30 - h) for h in range(24)] * 2,
}}


def test_ensemble_daily_max_and_build_forecast_with_obs():
    members = weather.ensemble_daily_extremes(ENSEMBLE, date(2026, 9, 13), "high")
    assert sorted(members) == [83, 85, 87]
    obs_payload = {"features": [
        {"properties": {"timestamp": "2026-09-13T15:00:00+00:00", "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"}}},
        {"properties": {"timestamp": "2026-09-13T17:00:00+00:00", "temperature": {"value": 31.5, "unitCode": "wmoUnit:degC"}}}]}

    class R:
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j

    def get(url, **kw):
        return R(ENSEMBLE if "ensemble" in url else obs_payload)

    sess = mock.Mock(get=get)
    now = datetime(2026, 9, 13, 14, tzinfo=ZoneInfo("America/New_York"))
    fc = weather.build_forecast("KXHIGHNY", date(2026, 9, 13), "high", bias_f=0.8, session=sess, now=now)
    assert fc.observed_extreme == pytest.approx(88.7)
    assert fc.samples.min() >= 89          # floor at observed max (rounded)
    assert not fc.locked
    fc2 = weather.build_forecast("KXHIGHNY", date(2026, 9, 13), "high", bias_f=0.8, session=sess,
                                 now=now.replace(hour=23))
    # Late evening: members' remaining hours are cooler than the observed max -> collapses onto it.
    assert not fc2.locked and abs(fc2.median - 89) <= 1 and fc2.spread < 1.0
    fc3 = weather.build_forecast("KXHIGHNY", date(2026, 9, 13), "high", bias_f=0.8, session=sess,
                                 now=now.replace(day=14, hour=9))
    assert fc3.locked


def test_station_mismatch_skips_event(monkeypatch):
    from agent import agent as A
    k = mock.Mock()
    k.exchange_status.return_value = {"trading_active": True}
    k.balance.return_value = 100.0
    k.positions.return_value = []
    ev = {"event_ticker": "KXHIGHAUS-26SEP13", "markets": [
        dict(ticker="KXHIGHAUS-26SEP13-T99", event_ticker="KXHIGHAUS-26SEP13", status="open", strike_type="greater",
             floor_strike=99, yes_bid_dollars="0.40", yes_ask_dollars="0.45", no_bid_dollars="0.55",
             no_ask_dollars="0.60", volume_24h_fp="100", rules_primary="... Austin (CLIATT) ...")]}
    k.events.side_effect = lambda s, **kw: [ev] if s == "KXHIGHAUS" else []
    monkeypatch.setattr(A, "build_forecast", lambda *a, **kw: _fc([100] * 10))
    monkeypatch.setattr(A, "datetime", _FixedDT)
    ag = A.Agent.__new__(A.Agent)
    ag.k, ag.db, ag.http, ag.errors, ag.halted = k, DB(), None, 0, False
    ag.threshold, ag.active_series, ag.last_status, ag.model_weight = 0.06, ["KXHIGHAUS"], {}, 1.0
    ag.cycle()
    assert not k.place.called


def test_full_cycle_with_mocked_kalshi(monkeypatch):
    from agent import agent as A
    db = DB()
    k = mock.Mock()
    k.exchange_status.return_value = {"trading_active": True}
    k.balance.return_value = 100.0
    k.positions.return_value = []
    ev = {"event_ticker": "KXHIGHNY-26SEP13", "markets": [
        dict(ticker="KXHIGHNY-26SEP13-T85", event_ticker="KXHIGHNY-26SEP13", status="open", strike_type="greater",
             floor_strike=84.5, yes_bid_dollars="0.40", yes_ask_dollars="0.45", no_bid_dollars="0.55",
             no_ask_dollars="0.60", volume_24h_fp="100", rules_primary="... New York City (CLINYC) ...")]}
    k.events.side_effect = lambda s, **kw: [ev] if s == "KXHIGHNY" else []
    k.place.return_value = {"order_id": "o1", "fill_count": "5.00", "average_fill_price": "0.45"}
    k.orderbook.return_value = {"yes_dollars": [["0.40", "50.00"]], "no_dollars": [["0.55", "5.00"]]}
    fc = _fc([90] * 9 + [80])
    monkeypatch.setattr(A, "build_forecast", lambda *a, **kw: fc)
    monkeypatch.setattr(A, "datetime", _FixedDT)
    ag = A.Agent.__new__(A.Agent)
    ag.k, ag.db, ag.http, ag.errors, ag.halted = k, db, None, 0, False
    ag.threshold, ag.active_series, ag.last_status, ag.model_weight = 0.06, ["KXHIGHNY"], {}, 1.0
    ag.cycle()
    assert k.place.called
    args = k.place.call_args[0]
    assert args[0] == "KXHIGHNY-26SEP13-T85" and args[1] == "yes" and args[3] == 0.45
    assert args[2] == 5   # capped to book depth (5 NO bids at 0.55)
    assert db.rows("SELECT COUNT(*) n FROM orders")[0]["n"] == 1
    # settlement
    k.market.return_value = {"status": "settled", "result": "yes"}
    ag.reconcile = A.Agent.reconcile.__get__(ag)
    with mock.patch.object(A.Agent, "calibrate", lambda self: None):
        ag.reconcile()
    s = db.rows("SELECT * FROM settlements")[0]
    assert s["pnl"] == pytest.approx(5 * (1 - 0.45 - 0.02), abs=0.01)   # net of taker fee


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 13, 12, tzinfo=tz or ZoneInfo("UTC"))
