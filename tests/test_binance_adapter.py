"""Binance adapter error mapping and parsing, using a fake HTTP transport (no network)."""

import json
import socket
import urllib.parse
from decimal import Decimal as D

import pytest

from vigil.exchange.base import MalformedResponse, OrderRejected, TransientError, UncertainOrderError
from vigil.exchange.binance import BinanceSpot
from vigil.models import OrderRequest, OrderStatus, Side


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers):
        self.calls.append((method, url, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body = r
        return status, body if isinstance(body, str) else json.dumps(body)


def ex(*responses):
    t = Transport(*responses)
    return BinanceSpot("https://example.test", api_key="KEY", api_secret="SECRET",
                       clock=lambda: 1_700_000_000_000, transport=t), t


REQ = OrderRequest("vgp-abc", "BTCUSDT", Side.BUY, D("0.001"), D("50000.5"))
ORDER_OK = {"orderId": 7, "clientOrderId": "vgp-abc", "status": "FILLED", "executedQty": "0.00100000",
            "cummulativeQuoteQty": "50.0"}


def test_order_is_signed_and_parsed():
    e, t = ex((200, ORDER_OK))
    st = e.place_order(REQ)
    method, url, headers = t.calls[0]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert method == "POST" and headers["X-MBX-APIKEY"] == "KEY"
    assert q["signature"] and q["newClientOrderId"] == ["vgp-abc"] and q["timeInForce"] == ["IOC"]
    assert q["quantity"] == ["0.001"] and q["price"] == ["50000.5"]
    assert st.status is OrderStatus.FILLED and st.avg_price == D("50000")


@pytest.mark.parametrize("response", [
    socket.timeout("timed out"), ConnectionResetError("reset"), (502, "bad gateway"), (503, {"msg": "x"}),
    (429, {"code": -1003}), (200, "not json"),
    (200, {**ORDER_OK, "status": "SOMETHING_NEW"}),
])
def test_uncertain_outcomes_on_order_write(response):
    e, _ = ex(response)
    with pytest.raises(UncertainOrderError):
        e.place_order(REQ)


def test_business_rejection():
    e, _ = ex((400, {"code": -2010, "msg": "Account has insufficient balance"}))
    with pytest.raises(OrderRejected) as r:
        e.place_order(REQ)
    assert "-2010" in r.value.reason


def test_mismatched_client_id_is_uncertain():
    e, _ = ex((200, {**ORDER_OK, "clientOrderId": "someone-else"}))
    with pytest.raises(UncertainOrderError):
        e.place_order(REQ)


def test_get_order_not_found_and_errors():
    e, _ = ex((400, {"code": -2013, "msg": "Order does not exist."}))
    assert e.get_order("BTCUSDT", "vgp-abc").status is OrderStatus.NOT_FOUND
    e, _ = ex(socket.timeout())
    with pytest.raises(TransientError):
        e.get_order("BTCUSDT", "vgp-abc")
    e, _ = ex((200, "<html>"))
    with pytest.raises(MalformedResponse):
        e.get_order("BTCUSDT", "vgp-abc")


def test_ioc_expired_maps_to_terminal():
    e, _ = ex((200, {**ORDER_OK, "status": "EXPIRED", "executedQty": "0.0004", "cummulativeQuoteQty": "20"}))
    st = e.place_order(REQ)
    assert st.status is OrderStatus.EXPIRED and st.filled_qty == D("0.0004")


def test_market_data_parsing():
    e, _ = ex((200, {"lastUpdateId": 1, "bids": [["100.0", "1.5"]], "asks": [["100.1", "2"]]}),
              (200, [[0, "1", "2", "0.5", "1.5", "10", 3599999, "0", 0, "0", "0", "0"]]),
              (200, {"bids": [["x", "1"]], "asks": []}))
    book = e.order_book("BTCUSDT", 5)
    assert book.best_bid == D("100.0") and book.best_ask == D("100.1")
    c = e.candles("BTCUSDT", "1h", 1)[0]
    assert c.close == D("1.5") and c.close_time_ms == 3599999
    with pytest.raises(MalformedResponse):
        e.order_book("BTCUSDT", 5)


def test_symbol_rules():
    info = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                                     {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001"},
                                     {"filterType": "NOTIONAL", "minNotional": "5.0"}]}]}
    e, _ = ex((200, info))
    r = e.symbol_rules("BTCUSDT")
    assert r.tick_size == D("0.01") and r.step_size == D("0.00001") and r.min_notional == D("5")


def test_requires_https():
    with pytest.raises(ValueError):
        BinanceSpot("http://insecure.test")
