"""Minimal Kalshi Trade API v2 client (RSA-PSS signed requests, V2 single-book orders)."""
import base64
import re
import logging
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config

log = logging.getLogger("kalshi")


def normalize_pem(text: str) -> bytes:
    """Rebuild a PEM whose newlines were flattened by an env-var editor.

    Accepts literal '\\n', CRLF, spaces instead of newlines, or surrounding quotes.
    """
    t = text.strip().strip('"').strip("'").replace("\\n", "\n").replace("\r", "")
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", t, re.S)
    if not m:
        raise RuntimeError("KALSHI_PRIVATE_KEY does not contain a BEGIN/END PEM block")
    label = m.group(1)
    body = re.sub(r"\s+", "", m.group(2))
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode()


def load_private_key(pem_text: str = "", path: str = ""):
    if pem_text:
        data = normalize_pem(pem_text)
    elif path:
        with open(path, "rb") as f:
            data = f.read()
    else:
        raise RuntimeError("No Kalshi private key: set KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH")
    return serialization.load_pem_private_key(data, password=None)


def sign(private_key, ts_ms: int, method: str, path: str) -> str:
    """Sign timestamp + METHOD + path (no query string). Path includes /trade-api/v2."""
    msg = f"{ts_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


class KalshiError(Exception):
    pass


class Kalshi:
    def __init__(self, key_id=None, private_key=None, base=None, session=None):
        self.key_id = key_id or config.KALSHI_KEY_ID
        self.base = (base or config.KALSHI_BASE).rstrip("/")
        self.prefix = self.base[self.base.index("/trade-api"):]  # "/trade-api/v2"
        self.pk = private_key or load_private_key(config.KALSHI_PRIVATE_KEY, config.KALSHI_PRIVATE_KEY_PATH)
        self.s = session or requests.Session()

    # -- transport ----------------------------------------------------------
    def _headers(self, method, path):
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign(self.pk, ts, method, self.prefix + path),
            "Content-Type": "application/json",
        }

    def _req(self, method, path, params=None, json=None, retries=2):
        url = self.base + path
        for attempt in range(retries + 1):
            r = self.s.request(method, url, params=params, json=json,
                               headers=self._headers(method, path), timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            if r.status_code >= 400:
                raise KalshiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise KalshiError(f"{method} {path}: retries exhausted")

    def _paged(self, path, key, params=None, max_pages=20):
        params = dict(params or {})
        out = []
        for _ in range(max_pages):
            data = self._req("GET", path, params=params)
            out.extend(data.get(key, []) or [])
            cur = data.get("cursor")
            if not cur:
                break
            params["cursor"] = cur
        return out

    # -- market data --------------------------------------------------------
    def exchange_status(self):
        return self._req("GET", "/exchange/status")

    def events(self, series_ticker, status="open", nested=True):
        return self._paged("/events", "events", dict(
            series_ticker=series_ticker, status=status,
            with_nested_markets=str(nested).lower(), limit=200))

    def markets(self, event_ticker=None, series_ticker=None, status="open"):
        p = dict(status=status, limit=1000)
        if event_ticker:
            p["event_ticker"] = event_ticker
        if series_ticker:
            p["series_ticker"] = series_ticker
        return self._paged("/markets", "markets", p)

    def market(self, ticker):
        return self._req("GET", f"/markets/{ticker}").get("market", {})

    def orderbook(self, ticker, depth=5):
        ob = self._req("GET", f"/markets/{ticker}/orderbook", params=dict(depth=depth))
        return ob.get("orderbook_fp") or ob.get("orderbook") or {}

    def series(self, ticker):
        return self._req("GET", f"/series/{ticker}").get("series", {})

    # -- portfolio ----------------------------------------------------------
    def balance(self) -> float:
        b = self._req("GET", "/portfolio/balance")
        if "balance_dollars" in b:
            return float(b["balance_dollars"])
        return float(b.get("balance", 0)) / 100.0

    def positions(self):
        return self._paged("/portfolio/positions", "market_positions",
                           dict(count_filter="position", limit=1000))

    def orders(self, status="resting"):
        return self._paged("/portfolio/orders", "orders", dict(status=status, limit=200))

    def cancel_all(self):
        return self._req("DELETE", "/portfolio/orders")

    # -- trading ------------------------------------------------------------
    def place(self, ticker, outcome, count, yes_price, tif="immediate_or_cancel"):
        """Buy `count` contracts of `outcome` ('yes'|'no').

        V2 single book: price is always on the YES scale.
          buy YES = side 'bid' at yes price
          buy NO  = side 'ask' at yes price (you receive 1 - price of exposure; cost = 1 - price)
        `yes_price` is the YES-scale price to trade at.
        """
        side = "bid" if outcome == "yes" else "ask"
        body = dict(
            ticker=ticker,
            side=side,
            count=f"{int(count)}.00",
            price=f"{yes_price:.4f}",
            time_in_force=tif,
            self_trade_prevention_type="taker_at_cross",
            client_order_id=str(uuid.uuid4()),
            post_only=False,
        )
        if config.DRY_RUN:
            log.info("DRY_RUN order %s", body)
            return dict(order_id="dry", fill_count="0.00", remaining_count=body["count"], dry_run=True)
        try:
            return self._req("POST", "/portfolio/events/orders", json=body)
        except KalshiError as e:
            if "404" not in str(e):
                raise
        # Fallback to the legacy order shape if the V2 path is not live on this host.
        legacy = dict(
            ticker=ticker, action="buy", side=outcome, type="limit", count=int(count),
            client_order_id=body["client_order_id"],
            time_in_force="immediate_or_cancel" if tif == "immediate_or_cancel" else None,
        )
        if outcome == "yes":
            legacy["yes_price"] = int(round(yes_price * 100))
        else:
            legacy["no_price"] = int(round((1 - yes_price) * 100))
        legacy = {k: v for k, v in legacy.items() if v is not None}
        return self._req("POST", "/portfolio/orders", json=legacy).get("order", {})
