"""Plain data types shared by the strategy, risk layer and exchange adapters.

All prices and quantities are Decimal. Floats are never used for money, so
rounding to exchange tick/step sizes is exact and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    # Local-only states
    INTENDED = "INTENDED"      # persisted before any network call
    SUBMITTING = "SUBMITTING"  # request sent, no acknowledgment yet
    UNKNOWN = "UNKNOWN"        # request may or may not have reached the exchange
    # Exchange states (normalized)
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"      # includes IOC remainder expiry
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    NOT_FOUND = "NOT_FOUND"    # exchange says it has no such order

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.NOT_FOUND,
}


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class OrderBook:
    """Top-N levels. bids sorted best (highest) first, asks best (lowest) first."""

    ts_ms: int  # time the snapshot was received locally (or exchange time if provided)
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    limit_price: Decimal
    time_in_force: str = "IOC"


@dataclass(frozen=True)
class Fill:
    trade_id: str
    client_order_id: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str
    ts_ms: int


@dataclass
class OrderState:
    """What the exchange reports about one order (normalized by the adapter)."""

    client_order_id: str
    exchange_order_id: str | None
    status: OrderStatus
    filled_qty: Decimal
    avg_price: Decimal | None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked
