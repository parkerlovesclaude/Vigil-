"""Binance Spot REST adapter (also used for Spot Testnet and as the paper-mode data feed).

Standard library only. All exchange-specific details (endpoints, signing,
payload formats, status names, error codes) stay in this file.

NOTE: this adapter was written against Binance's documented Spot API
(/api/v3). It is exercised by unit tests with recorded-style payloads, but
must be validated on Spot Testnet (run.mode = "testnet") before any live use.
api.binance.com is not available from some jurisdictions (e.g. the US);
set exchange.base_url to a venue you are permitted to use whose API is
compatible (for example Binance.US), and re-validate on that venue.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Callable

from ..models import (Balance, BookLevel, Candle, Fill, OrderBook, OrderRequest, OrderState,
                      OrderStatus, SymbolRules)
from .base import (ExchangeAdapter, MalformedResponse, OrderRejected, TransientError,
                   UncertainOrderError, to_decimal)

DEFAULT_URLS = {
    "paper": "https://api.binance.com",       # public market data only
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}

STATUS_MAP = {
    "NEW": OrderStatus.NEW, "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.NEW, "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED, "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}
ORDER_NOT_FOUND_CODES = {-2013, -2011}


def _tls_context() -> ssl.SSLContext:
    """Verified TLS. Uses the `certifi` CA bundle when installed (fixes Pythons that ship
    without system certificates, common on macOS); otherwise the system default.
    Verification is never disabled."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s


class BinanceSpot(ExchangeAdapter):
    name = "binance"

    def __init__(self, base_url: str, *, api_key: str = "", api_secret: str = "",
                 timeout_s: int = 10, recv_window_ms: int = 5000,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 transport: Callable | None = None):
        if not base_url.startswith("https://"):
            raise ValueError("base_url must be https")
        self.base, self.key, self.secret = base_url.rstrip("/"), api_key, api_secret.encode()
        self.timeout, self.recv_window, self.clock = timeout_s, recv_window_ms, clock
        self._transport = transport or self._urllib_transport
        self._ssl = _tls_context()

    # ---- HTTP -------------------------------------------------------------
    def _urllib_transport(self, method: str, url: str, headers: dict) -> tuple[int, str]:
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False,
                 is_order_write: bool = False):
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise TransientError("API credentials not configured")
            params["timestamp"] = self.clock()
            params["recvWindow"] = self.recv_window
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
            qs = f"{qs}&signature={sig}"
            headers["X-MBX-APIKEY"] = self.key
        else:
            qs = urllib.parse.urlencode(params)
        url = f"{self.base}{path}" + (f"?{qs}" if qs else "")
        try:
            status, body = self._transport(method, url, headers)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            if is_order_write:
                raise UncertainOrderError(f"network error on order request: {exc}") from exc
            raise TransientError(f"network error: {exc}") from exc
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = None
        if status >= 500 or data is None:
            # Binance: 5xx means the execution status is UNKNOWN.
            msg = f"HTTP {status}: {body[:200]!r}"
            if is_order_write:
                raise UncertainOrderError(msg)
            raise MalformedResponse(msg) if data is None else TransientError(msg)
        if status in (418, 429):
            if is_order_write:
                raise UncertainOrderError(f"rate limited HTTP {status}")
            raise TransientError(f"rate limited HTTP {status}")
        if status >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            return {"__error__": True, "status": status, "code": code,
                    "msg": data.get("msg") if isinstance(data, dict) else str(data)}
        return data

    @staticmethod
    def _is_error(data) -> bool:
        return isinstance(data, dict) and data.get("__error__") is True

    # ---- public -------------------------------------------------------------
    def server_time_ms(self) -> int:
        data = self._request("GET", "/api/v3/time")
        if self._is_error(data) or "serverTime" not in data:
            raise MalformedResponse(f"time: {data}")
        return int(data["serverTime"])

    def symbol_rules(self, symbol: str) -> SymbolRules:
        data = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        if self._is_error(data):
            raise TransientError(f"exchangeInfo error: {data}")
        try:
            s = data["symbols"][0]
            if s.get("status") != "TRADING":
                raise TransientError(f"symbol {symbol} status {s.get('status')}")
            filters = {f["filterType"]: f for f in s["filters"]}
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {"minNotional": "0"}
            return SymbolRules(
                symbol=s["symbol"], base_asset=s["baseAsset"], quote_asset=s["quoteAsset"],
                tick_size=to_decimal(filters["PRICE_FILTER"]["tickSize"], "tickSize").normalize(),
                step_size=to_decimal(filters["LOT_SIZE"]["stepSize"], "stepSize").normalize(),
                min_qty=to_decimal(filters["LOT_SIZE"]["minQty"], "minQty"),
                min_notional=to_decimal(notional["minNotional"], "minNotional"))
        except (KeyError, IndexError, TypeError) as exc:
            raise MalformedResponse(f"exchangeInfo shape: {exc}") from exc

    def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        data = self._request("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if self._is_error(data) or not isinstance(data, list):
            raise MalformedResponse(f"klines: {str(data)[:200]}")
        out = []
        try:
            for k in data:
                out.append(Candle(int(k[0]), int(k[6]), to_decimal(k[1], "open"), to_decimal(k[2], "high"),
                                  to_decimal(k[3], "low"), to_decimal(k[4], "close"),
                                  to_decimal(k[5], "volume")))
        except (IndexError, TypeError, ValueError) as exc:
            raise MalformedResponse(f"kline shape: {exc}") from exc
        return out

    def order_book(self, symbol: str, depth: int) -> OrderBook:
        valid = [5, 10, 20, 50, 100, 500, 1000, 5000]
        lim = next((v for v in valid if v >= depth), 5000)
        data = self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": lim})
        received = self.clock()
        if self._is_error(data):
            raise TransientError(f"depth error: {data}")
        try:
            bids = tuple(BookLevel(to_decimal(p, "bid"), to_decimal(q, "bidqty")) for p, q in data["bids"][:depth])
            asks = tuple(BookLevel(to_decimal(p, "ask"), to_decimal(q, "askqty")) for p, q in data["asks"][:depth])
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponse(f"depth shape: {exc}") from exc
        return OrderBook(received, bids, asks)

    # ---- private ------------------------------------------------------------
    def balances(self) -> dict[str, Balance]:
        data = self._request("GET", "/api/v3/account", signed=True)
        if self._is_error(data):
            raise TransientError(f"account error: {data}")
        try:
            return {b["asset"]: Balance(b["asset"], to_decimal(b["free"], "free"), to_decimal(b["locked"], "locked"))
                    for b in data["balances"]}
        except (KeyError, TypeError) as exc:
            raise MalformedResponse(f"account shape: {exc}") from exc

    def _order_state(self, data: dict, coid: str) -> OrderState:
        try:
            status = STATUS_MAP.get(data["status"])
            if status is None:
                raise MalformedResponse(f"unknown order status {data['status']!r}")
            if data.get("clientOrderId") not in (None, coid) and data.get("origClientOrderId") != coid:
                raise MalformedResponse("response client id does not match request")
            filled = to_decimal(data["executedQty"], "executedQty")
            quote = to_decimal(data.get("cummulativeQuoteQty", "0"), "cummulativeQuoteQty")
            return OrderState(coid, str(data["orderId"]), status, filled,
                              (quote / filled) if filled > 0 else None, raw=data)
        except KeyError as exc:
            raise MalformedResponse(f"order shape: missing {exc}") from exc

    def place_order(self, req: OrderRequest) -> OrderState:
        params = {"symbol": req.symbol, "side": req.side.value, "type": "LIMIT",
                  "timeInForce": req.time_in_force, "quantity": fmt(req.qty),
                  "price": fmt(req.limit_price), "newClientOrderId": req.client_order_id,
                  "newOrderRespType": "RESULT"}
        data = self._request("POST", "/api/v3/order", params, signed=True, is_order_write=True)
        if self._is_error(data):
            raise OrderRejected(f"code {data['code']}: {data['msg']}", data)
        try:
            return self._order_state(data, req.client_order_id)
        except MalformedResponse as exc:
            raise UncertainOrderError(f"unparseable order response: {exc}") from exc

    def get_order(self, symbol: str, client_order_id: str) -> OrderState:
        data = self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)
        if self._is_error(data):
            if data["code"] in ORDER_NOT_FOUND_CODES:
                return OrderState(client_order_id, None, OrderStatus.NOT_FOUND, Decimal(0), None, raw=data)
            raise TransientError(f"get_order error: {data}")
        return self._order_state(data, client_order_id)

    def get_fills(self, symbol: str, client_order_id: str, exchange_order_id: str | None) -> list[Fill]:
        if not exchange_order_id:
            return []
        data = self._request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": exchange_order_id},
                             signed=True)
        if self._is_error(data) or not isinstance(data, list):
            raise TransientError(f"myTrades error: {str(data)[:200]}")
        try:
            return [Fill(str(t["id"]), client_order_id, to_decimal(t["price"], "price"), to_decimal(t["qty"], "qty"),
                         to_decimal(t["commission"], "commission"), t["commissionAsset"], int(t["time"]))
                    for t in data]
        except (KeyError, TypeError) as exc:
            raise MalformedResponse(f"trade shape: {exc}") from exc

    def open_orders(self, symbol: str) -> list[OrderState]:
        data = self._request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
        if self._is_error(data) or not isinstance(data, list):
            raise TransientError(f"openOrders error: {str(data)[:200]}")
        return [self._order_state(o, o.get("clientOrderId", "")) for o in data]

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderState:
        data = self._request("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)
        if self._is_error(data):
            if data["code"] in ORDER_NOT_FOUND_CODES:
                return self.get_order(symbol, client_order_id)
            raise TransientError(f"cancel error: {data}")
        return self._order_state(data, client_order_id)
