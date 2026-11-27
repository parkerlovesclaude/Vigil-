"""Order lifecycle: intent -> submit -> acknowledge -> resolve -> apply fills.

Every order is treated as an uncertain operation:
  * The intent is persisted to the state file and the audit log BEFORE any
    network call, with a deterministic client order id derived from the
    evaluation that produced it. The same evaluation can never produce a
    second order (duplicate submission prevention).
  * If the submit call times out or returns something unexpected, the order
    is marked UNKNOWN and is NEVER resubmitted. It is resolved by querying the
    exchange by client order id until it reaches a terminal state or the
    resolve timeout expires.
  * An order still unresolved after the timeout raises an integrity halt; the
    system will not send anything else until reconciliation resolves it.
  * Fills are applied exactly once (deduplicated by exchange trade id) and the
    position is derived only from fills, never from intended quantities.
  * Partial fills: orders are IOC, so any unfilled remainder expires. The
    position reflects only what filled; an unfinished exit stays pending and
    is retried with a new attempt number on the next loop.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Callable

from .audit import AuditLog
from .config import Config
from .exchange.base import ExchangeAdapter, OrderRejected, TransientError, UncertainOrderError
from .models import Fill, OrderRequest, OrderState, OrderStatus, Side, SymbolRules
from .state import OrderRecord, Position, State, StateStore

BPS = Decimal("10000")


def make_client_order_id(mode: str, evaluation_id: str, purpose: str) -> str:
    digest = hashlib.sha256(f"{evaluation_id}|{purpose}".encode()).hexdigest()[:24]
    return f"vg{mode[0]}-{digest}"   # 28 chars, [a-z0-9-], fits common exchange limits


class IntegrityError(Exception):
    """Local state can no longer be trusted without reconciliation."""


class OrderManager:
    def __init__(self, cfg: Config, adapter: ExchangeAdapter, state: State, store: StateStore,
                 audit: AuditLog, rules: SymbolRules, clock: Callable[[], int],
                 sleep: Callable[[float], None]):
        self.cfg, self.ex, self.st, self.store, self.audit = cfg, adapter, state, store, audit
        self.rules, self.clock, self.sleep = rules, clock, sleep

    def _save(self):
        self.store.save(self.st)

    # ---- submission ---------------------------------------------------------
    def submit(self, *, purpose: str, evaluation_id: str, side: Side, qty: Decimal,
               limit_price: Decimal, stop_price: Decimal | None = None,
               ref_price: Decimal | None = None) -> OrderRecord:
        coid = make_client_order_id(self.cfg.run.mode, evaluation_id, purpose)
        if coid in self.st.orders:
            self.audit.write("duplicate_order_prevented", {"existing_status": self.st.orders[coid].status},
                             evaluation_id=evaluation_id, client_order_id=coid)
            return self.st.orders[coid]
        now = self.clock()
        rec = OrderRecord(client_order_id=coid, evaluation_id=evaluation_id, purpose=purpose,
                          symbol=self.cfg.run.symbol, side=side.value, qty=qty, limit_price=limit_price,
                          status=OrderStatus.INTENDED.value, created_ms=now, updated_ms=now,
                          stop_price=stop_price, ref_price=ref_price)
        self.st.orders[coid] = rec
        self.st.orders_today += 1
        self._save()
        self.audit.write("order_intent", {"purpose": purpose, "side": side.value, "qty": qty,
                                          "limit_price": limit_price, "time_in_force": "IOC",
                                          "stop_price": stop_price, "ref_price": ref_price,
                                          "orders_today": self.st.orders_today},
                         evaluation_id=evaluation_id, client_order_id=coid)

        req = OrderRequest(coid, self.cfg.run.symbol, side, qty, limit_price, "IOC")
        rec.status = OrderStatus.SUBMITTING.value
        self._save()
        self.audit.write("order_submit", {"request": {"symbol": req.symbol, "side": side.value, "type": "LIMIT",
                                                      "time_in_force": "IOC", "qty": qty, "price": limit_price}},
                         evaluation_id=evaluation_id, client_order_id=coid)
        try:
            ack = self.ex.place_order(req)
        except OrderRejected as exc:
            self._set_status(rec, OrderStatus.REJECTED, error=exc.reason)
            self.st.consecutive_errors += 1
            self._save()
            self.audit.write("order_rejected", {"reason": exc.reason, "raw": exc.raw},
                             evaluation_id=evaluation_id, client_order_id=coid)
            return rec
        except UncertainOrderError as exc:
            self._set_status(rec, OrderStatus.UNKNOWN, error=str(exc))
            self.st.consecutive_errors += 1
            self._save()
            self.audit.write("order_uncertain", {"error": str(exc), "action": "query_by_client_order_id"},
                             evaluation_id=evaluation_id, client_order_id=coid)
            return self.resolve(rec)
        self.audit.write("order_ack", {"exchange_order_id": ack.exchange_order_id, "status": ack.status.value,
                                       "filled_qty": ack.filled_qty, "avg_price": ack.avg_price, "raw": ack.raw},
                         evaluation_id=evaluation_id, client_order_id=coid)
        self._apply_state(rec, ack)
        return self.resolve(rec)

    # ---- resolution -----------------------------------------------------------
    def resolve(self, rec: OrderRecord) -> OrderRecord:
        """Poll until terminal and all fills applied, or raise IntegrityError."""
        deadline = self.clock() + self.cfg.exchange.order_resolve_timeout_seconds * 1000
        poll = float(self.cfg.exchange.order_poll_seconds)
        cancel_sent = False
        while True:
            if OrderStatus(rec.status).is_terminal and self._fills_complete(rec):
                return rec
            try:
                if not OrderStatus(rec.status).is_terminal:
                    state = self.ex.get_order(rec.symbol, rec.client_order_id)
                    if state.status is OrderStatus.NOT_FOUND:
                        if rec.exchange_order_id:
                            raise IntegrityError(f"acknowledged order {rec.client_order_id} not found on exchange")
                        if self.clock() >= deadline:
                            # Never acknowledged and never visible for the whole resolve window:
                            # treat as not placed; balance reconciliation double-checks this.
                            self._set_status(rec, OrderStatus.NOT_FOUND, error="never visible on exchange")
                            self.st.needs_reconcile = True
                            self._save()
                            self.audit.write("order_status", {"status": "NOT_FOUND",
                                                              "note": "not visible within resolve window"},
                                             evaluation_id=rec.evaluation_id, client_order_id=rec.client_order_id)
                            return rec
                    else:
                        self._apply_state(rec, state)
                        if not state.status.is_terminal and not cancel_sent:
                            # IOC orders should never rest on the book; cancel defensively.
                            cancel_sent = True
                            c = self.ex.cancel_order(rec.symbol, rec.client_order_id)
                            self.audit.write("order_cancel", {"reason": "unexpected_resting_ioc_order",
                                                              "status": c.status.value},
                                             evaluation_id=rec.evaluation_id, client_order_id=rec.client_order_id)
                            self._apply_state(rec, c)
                if OrderStatus(rec.status).is_terminal:
                    self._sync_fills(rec)
                    if self._fills_complete(rec):
                        return rec
            except TransientError as exc:
                self.audit.write("order_query_error", {"error": str(exc)},
                                 evaluation_id=rec.evaluation_id, client_order_id=rec.client_order_id)
            if self.clock() >= deadline:
                self.st.needs_reconcile = True
                self._save()
                raise IntegrityError(f"order {rec.client_order_id} unresolved after "
                                     f"{self.cfg.exchange.order_resolve_timeout_seconds}s (status {rec.status})")
            self.sleep(poll)

    def _set_status(self, rec: OrderRecord, status: OrderStatus, error: str | None = None):
        rec.status = status.value
        rec.updated_ms = self.clock()
        if error:
            rec.error = error

    def _apply_state(self, rec: OrderRecord, state: OrderState):
        changed = state.status.value != rec.status or state.filled_qty != rec.filled_qty
        if state.exchange_order_id:
            rec.exchange_order_id = state.exchange_order_id
        if state.filled_qty < rec.filled_qty:
            raise IntegrityError(f"filled qty went backwards for {rec.client_order_id}")
        if state.filled_qty > rec.qty:
            raise IntegrityError(f"filled qty {state.filled_qty} exceeds order qty {rec.qty}")
        rec.filled_qty, rec.avg_price = state.filled_qty, state.avg_price
        self._set_status(rec, state.status)
        self._save()
        if changed:
            self.audit.write("order_status", {"status": state.status.value, "filled_qty": state.filled_qty,
                                              "avg_price": state.avg_price},
                             evaluation_id=rec.evaluation_id, client_order_id=rec.client_order_id)
        if state.status.is_terminal and state.filled_qty > 0:
            self._sync_fills(rec)

    def _fills_complete(self, rec: OrderRecord) -> bool:
        return self._applied_qty(rec) == rec.filled_qty

    def _applied_qty(self, rec: OrderRecord) -> Decimal:
        return sum((Decimal(q) for _, q in rec.applied_trade_ids), Decimal(0))

    def _sync_fills(self, rec: OrderRecord):
        fills = self.ex.get_fills(rec.symbol, rec.client_order_id, rec.exchange_order_id)
        seen = {tid for tid, _ in rec.applied_trade_ids}
        for f in fills:
            if f.trade_id in seen:
                continue
            self._apply_fill(rec, f)
            seen.add(f.trade_id)
        if self._applied_qty(rec) > rec.filled_qty:
            raise IntegrityError(f"fills exceed reported filled qty for {rec.client_order_id}")

    # ---- position accounting --------------------------------------------------
    def _fee_in_quote(self, f: Fill) -> tuple[Decimal, bool]:
        """Return (fee value in quote, estimated?)."""
        if f.fee_asset == self.rules.quote_asset:
            return f.fee, False
        if f.fee_asset == self.rules.base_asset:
            return f.fee * f.price, False
        return f.qty * f.price * self.cfg.costs.taker_fee_bps / BPS, True

    def _apply_fill(self, rec: OrderRecord, f: Fill):
        st, base = self.st, self.rules.base_asset
        fee_q, estimated = self._fee_in_quote(f)
        realized = None
        if rec.side == Side.BUY.value:
            net_qty = f.qty - f.fee if f.fee_asset == base else f.qty
            cost = f.qty * f.price + (fee_q if f.fee_asset != base else Decimal(0))
            if st.position is None:
                if rec.stop_price is None:
                    raise IntegrityError("entry fill without a stop price")
                st.position = Position(qty=net_qty, entry_price=cost / net_qty, cost_basis=cost,
                                       stop_price=rec.stop_price,
                                       highest_close=rec.ref_price or f.price,
                                       entry_client_order_id=rec.client_order_id, opened_ms=f.ts_ms)
            else:
                p = st.position
                p.qty += net_qty
                p.cost_basis += cost
                p.entry_price = p.cost_basis / p.qty
        else:
            p = st.position
            if p is None:
                raise IntegrityError("sell fill with no local position")
            sell_qty = min(f.qty, p.qty)
            avg_cost = p.cost_basis / p.qty
            proceeds = f.qty * f.price - (fee_q if f.fee_asset != base else Decimal(0))
            realized = proceeds - avg_cost * sell_qty
            p.qty -= sell_qty
            p.cost_basis -= avg_cost * sell_qty
            st.realized_pnl_today += realized
        rec.applied_trade_ids.append([f.trade_id, str(f.qty)])
        self._save()
        self.audit.write("order_fill", {
            "trade_id": f.trade_id, "side": rec.side, "price": f.price, "qty": f.qty, "fee": f.fee,
            "fee_asset": f.fee_asset, "fee_quote_equiv": fee_q, "fee_estimated": estimated,
            "realized_pnl": realized, "fill_ts_ms": f.ts_ms,
            "position_after": st.position}, evaluation_id=rec.evaluation_id, client_order_id=rec.client_order_id)
