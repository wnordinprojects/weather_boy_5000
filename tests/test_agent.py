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
    assert strategy.strike(dict(ticker="KXHIGHNY-26SEP13-T84")) == ("ge", 84.0, None)
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
    fc = _fc([90] * 9 + [80])  # 90% sure it's 90
    ms = [dict(ticker="E-T85", event_ticker="E", strike_type="greater", floor_strike=84.5,
               yes_bid_dollars="0.50", yes_ask_dollars="0.55", no_bid_dollars="0.45", no_ask_dollars="0.50", volume_24h_fp="10"),
          dict(ticker="E-T95", event_ticker="E", strike_type="greater", floor_strike=94.5,
               yes_bid_dollars="0.30", yes_ask_dollars="0.35", no_bid_dollars="0.65", no_ask_dollars="0.70", volume_24h_fp="10")]
    orders, decisions = strategy.plan_orders(fc, ms, bankroll=100, positions={}, threshold=0.06, event_spent=0)
    assert [o["outcome"] for o in orders] == ["yes", "no"]
    assert orders[0]["yes_price"] == 0.55                      # buy yes at ask
    assert orders[1]["yes_price"] == pytest.approx(0.30)       # buy no = ask on yes scale at 1 - no_ask
    spent = sum(o["count"] * (o["yes_price"] if o["outcome"] == "yes" else 1 - o["yes_price"]) for o in orders)
    assert spent <= config.MAX_EVENT_FRACTION * 100 + 1e-9
    # already at size -> no order
    orders2, _ = strategy.plan_orders(fc, ms[:1], 100, {"E-T85": 500}, 0.06, 0)
    assert orders2 == []
    # holding the wrong side with a big reversed edge -> exit
    orders3, _ = strategy.plan_orders(fc, ms[:1], 100, {"E-T85": -10}, 0.06, 0)
    assert orders3 and orders3[0]["action"] == "exit" and orders3[0]["outcome"] == "yes" and orders3[0]["count"] == 10


def test_saturation_raises_threshold():
    fc = _fc([90] * 9 + [80])
    m = dict(ticker="E-T85", event_ticker="E", strike_type="greater", floor_strike=84.5,
             yes_bid_dollars="0.80", yes_ask_dollars="0.81", no_bid_dollars="0.19", no_ask_dollars="0.20", volume_24h_fp="50000")
    c = strategy.evaluate(fc, m, 0.06)
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
                                 now=now.replace(hour=20))
    assert fc2.locked and abs(fc2.median - 89) <= 1


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
             no_ask_dollars="0.60", volume_24h_fp="100", rules_primary="... Central Park (KNYC) ...")]}
    k.events.side_effect = lambda s, **kw: [ev] if s == "KXHIGHNY" else []
    k.place.return_value = {"order_id": "o1", "fill_count": "5.00", "average_fill_price": "0.45"}
    fc = _fc([90] * 9 + [80])
    monkeypatch.setattr(A, "build_forecast", lambda *a, **kw: fc)
    monkeypatch.setattr(A, "datetime", _FixedDT)
    ag = A.Agent.__new__(A.Agent)
    ag.k, ag.db, ag.http, ag.errors, ag.halted = k, db, None, 0, False
    ag.threshold, ag.active_series, ag.last_status = 0.06, ["KXHIGHNY"], {}
    ag.cycle()
    assert k.place.called
    args = k.place.call_args[0]
    assert args[0] == "KXHIGHNY-26SEP13-T85" and args[1] == "yes" and args[3] == 0.45
    assert db.rows("SELECT COUNT(*) n FROM orders")[0]["n"] == 1
    # settlement
    k.market.return_value = {"status": "settled", "result": "yes"}
    ag.reconcile = A.Agent.reconcile.__get__(ag)
    with mock.patch.object(A.Agent, "calibrate", lambda self: None):
        ag.reconcile()
    s = db.rows("SELECT * FROM settlements")[0]
    assert s["pnl"] == pytest.approx(5 * (1 - 0.45), abs=0.01)


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 13, 12, tzinfo=tz or ZoneInfo("UTC"))
