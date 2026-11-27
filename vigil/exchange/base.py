"""The boundary between the exchange-independent core and exchange adapters.

An adapter translates one exchange's API into these normalized calls and
error types. The engine only ever talks to this interface.

Error contract (this is what makes order handling safe):
  TransientError        A read failed (timeout, 5xx, rate limit). Safe to retry later.
  OrderRejected         The exchange definitively refused the order. Nothing happened.
  UncertainOrderError   An order request may or may not have been executed
                        (timeout after sending, 5xx, unparseable response).
                        The caller must NOT resubmit; it must query the order.
  MalformedResponse     The exchange returned something we do not understand.
                        Treated as transient for reads, uncertain for writes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ..models import Balance, Candle, Fill, OrderBook, OrderRequest, OrderState, SymbolRules


class ExchangeError(Exception):
    pass


class TransientError(ExchangeError):
    pass


class MalformedResponse(TransientError):
    pass


class OrderRejected(ExchangeError):
    def __init__(self, reason: str, raw: dict | None = None):
        super().__init__(reason)
        self.reason, self.raw = reason, raw or {}


class UncertainOrderError(ExchangeError):
    pass


class ExchangeAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def server_time_ms(self) -> int: ...

    @abstractmethod
    def symbol_rules(self, symbol: str) -> SymbolRules: ...

    @abstractmethod
    def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]: ...

    @abstractmethod
    def order_book(self, symbol: str, depth: int) -> OrderBook: ...

    @abstractmethod
    def balances(self) -> dict[str, Balance]: ...

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderState: ...

    @abstractmethod
    def get_order(self, symbol: str, client_order_id: str) -> OrderState:
        """Return the order, or an OrderState with status NOT_FOUND."""

    @abstractmethod
    def get_fills(self, symbol: str, client_order_id: str, exchange_order_id: str | None) -> list[Fill]: ...

    @abstractmethod
    def open_orders(self, symbol: str) -> list[OrderState]: ...

    @abstractmethod
    def cancel_order(self, symbol: str, client_order_id: str) -> OrderState: ...


def to_decimal(value, field: str) -> Decimal:
    """Strict numeric parsing for exchange payloads."""
    try:
        d = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise MalformedResponse(f"bad numeric field {field}={value!r}") from exc
    if not d.is_finite():
        raise MalformedResponse(f"non-finite field {field}={value!r}")
    return d
