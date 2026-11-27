"""Paper exchange: real (or supplied) market data, simulated account and fills.

Fills are simulated by walking the current order book within the order's
limit price (IOC semantics: whatever cannot fill immediately expires). Fees
follow the common spot convention: buys pay the fee in the base asset, sells
in the quote asset. The simulated account is persisted to a JSON file so a
restarted process can be reconciled against it exactly like a real exchange.

`faults` lets tests (and drills) inject failures:
  place: "reject" | "timeout_before" | "timeout_after" | "partial:<fraction>"
  get:   "not_found" (one stale NOT_FOUND reply) | "timeout"
  book:  "timeout" ;  candles: "timeout"
Each entry is consumed once, in order.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Callable

from ..costs import floor_to_step
from ..models import (Balance, Fill, OrderBook, OrderRequest, OrderState, OrderStatus, Side,
                      SymbolRules)
from ..state import atomic_write
from .base import ExchangeAdapter, OrderRejected, TransientError, UncertainOrderError

BPS = Decimal("10000")


class PaperExchange(ExchangeAdapter):
    name = "paper"

    def __init__(self, market, rules: SymbolRules, *, starting_quote: Decimal, fee_bps: Decimal,
                 account_path: str | Path | None, clock: Callable[[], int]):
        self.market, self.rules, self.fee = market, rules, fee_bps / BPS
        self.clock = clock
        self.path = Path(account_path) if account_path else None
        self.faults: dict[str, list[str]] = {"place": [], "get": [], "book": [], "candles": []}
        self.balances_: dict[str, Decimal] = {rules.quote_asset: starting_quote, rules.base_asset: Decimal(0)}
        self.orders: dict[str, OrderState] = {}
        self.fills: dict[str, list[Fill]] = {}
        self._trade_seq = 0
        if self.path and self.path.exists():
            self._load()

    # ---- persistence ----------------------------------------------------
    def _save(self) -> None:
        if not self.path:
            return
        data = {
            "balances": {k: str(v) for k, v in self.balances_.items()},
            "trade_seq": self._trade_seq,
            "orders": {k: {"exchange_order_id": o.exchange_order_id, "status": o.status.value,
                           "filled_qty": str(o.filled_qty),
                           "avg_price": str(o.avg_price) if o.avg_price is not None else None}
                       for k, o in self.orders.items()},
            "fills": {k: [{"trade_id": f.trade_id, "price": str(f.price), "qty": str(f.qty),
                           "fee": str(f.fee), "fee_asset": f.fee_asset, "ts_ms": f.ts_ms} for f in v]
                      for k, v in self.fills.items()},
        }
        atomic_write(self.path, json.dumps(data, indent=1))

    def _load(self) -> None:
        d = json.loads(self.path.read_text())
        self.balances_ = {k: Decimal(v) for k, v in d["balances"].items()}
        self._trade_seq = d["trade_seq"]
        for k, o in d["orders"].items():
            self.orders[k] = OrderState(k, o["exchange_order_id"], OrderStatus(o["status"]),
                                        Decimal(o["filled_qty"]),
                                        Decimal(o["avg_price"]) if o["avg_price"] else None)
        for k, fl in d["fills"].items():
            self.fills[k] = [Fill(f["trade_id"], k, Decimal(f["price"]), Decimal(f["qty"]),
                                  Decimal(f["fee"]), f["fee_asset"], f["ts_ms"]) for f in fl]

    def _fault(self, op: str) -> str | None:
        q = self.faults.get(op)
        return q.pop(0) if q else None

    # ---- market data (delegated) -----------------------------------------
    def server_time_ms(self) -> int:
        return self.clock()

    def symbol_rules(self, symbol: str) -> SymbolRules:
        return self.rules

    def candles(self, symbol, interval, limit):
        if self._fault("candles") == "timeout":
            raise TransientError("simulated candles timeout")
        return self.market.candles(symbol, interval, limit)

    def order_book(self, symbol, depth) -> OrderBook:
        if self._fault("book") == "timeout":
            raise TransientError("simulated book timeout")
        return self.market.order_book(symbol, depth)

    # ---- account ----------------------------------------------------------
    def balances(self) -> dict[str, Balance]:
        return {k: Balance(k, v, Decimal(0)) for k, v in self.balances_.items()}

    def place_order(self, req: OrderRequest) -> OrderState:
        fault = self._fault("place")
        if req.client_order_id in self.orders:
            raise OrderRejected("duplicate client_order_id")
        if fault == "reject":
            raise OrderRejected("simulated rejection")
        if fault == "timeout_before":
            raise UncertainOrderError("simulated timeout (order never reached exchange)")
        if req.time_in_force != "IOC":
            raise OrderRejected("paper exchange supports IOC only")
        if req.qty < self.rules.min_qty or req.qty != floor_to_step(req.qty, self.rules.step_size):
            raise OrderRejected("invalid quantity")
        fraction = Decimal(fault.split(":")[1]) if fault and fault.startswith("partial:") else Decimal(1)
        state = self._execute(req, fraction)
        self._save()
        if fault == "timeout_after":
            raise UncertainOrderError("simulated timeout (order was executed)")
        return state

    def _execute(self, req: OrderRequest, fraction: Decimal) -> OrderState:
        book = self.market.order_book(req.symbol, 100)
        levels = book.asks if req.side is Side.BUY else book.bids
        want = floor_to_step(req.qty * fraction, self.rules.step_size)
        base, quote = self.rules.base_asset, self.rules.quote_asset
        fills, filled, notional = [], Decimal(0), Decimal(0)
        for lvl in levels:
            if filled >= want:
                break
            if (req.side is Side.BUY and lvl.price > req.limit_price) or \
               (req.side is Side.SELL and lvl.price < req.limit_price):
                break
            take = min(want - filled, lvl.qty)
            cost = take * lvl.price
            if req.side is Side.BUY:
                if self.balances_[quote] < cost:
                    break
                fee = take * self.fee
                self.balances_[quote] -= cost
                self.balances_[base] += take - fee
                fee_asset = base
            else:
                if self.balances_[base] < take:
                    break
                fee = cost * self.fee
                self.balances_[base] -= take
                self.balances_[quote] += cost - fee
                fee_asset = quote
            self._trade_seq += 1
            fills.append(Fill(f"p{self._trade_seq}", req.client_order_id, lvl.price, take, fee,
                              fee_asset, self.clock()))
            filled += take
            notional += cost
        status = OrderStatus.FILLED if filled >= req.qty else OrderStatus.EXPIRED
        st = OrderState(req.client_order_id, f"P{len(self.orders) + 1}", status, filled,
                        notional / filled if filled else None)
        self.orders[req.client_order_id] = st
        self.fills[req.client_order_id] = fills
        return st

    def get_order(self, symbol, client_order_id) -> OrderState:
        fault = self._fault("get")
        if fault == "timeout":
            raise TransientError("simulated get_order timeout")
        if fault == "not_found" or client_order_id not in self.orders:
            return OrderState(client_order_id, None, OrderStatus.NOT_FOUND, Decimal(0), None)
        return self.orders[client_order_id]

    def get_fills(self, symbol, client_order_id, exchange_order_id):
        return list(self.fills.get(client_order_id, []))

    def open_orders(self, symbol):
        return [o for o in self.orders.values() if not o.status.is_terminal]

    def cancel_order(self, symbol, client_order_id) -> OrderState:
        o = self.orders.get(client_order_id)
        if o is None:
            return OrderState(client_order_id, None, OrderStatus.NOT_FOUND, Decimal(0), None)
        if not o.status.is_terminal:
            o.status = OrderStatus.CANCELED
            self._save()
        return o
