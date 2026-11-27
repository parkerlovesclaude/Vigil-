"""Position sizing that includes execution costs and worst-case entry price.

Entry quantity = the smallest of
  (a) risk budget / worst-case loss per unit if the stop is hit,
  (b) the notional caps (absolute, fraction of equity),
  (c) what free quote balance can pay for at the worst-case (limit) price incl. fees,
  (d) what the visible book can fill within our limit price,
rounded DOWN to the exchange step size, then rejected if below exchange minimums.

Worst-case loss per unit if stopped =
    (limit_price - stop)                       # adverse entry up to our limit
  + mid  * entry_cost_bps                      # entry fee + unseen slippage
  + stop * exit_cost_bps                       # exit impact + fee + unseen slippage
where entry/exit costs come from walking the current book at the capped size,
which overestimates costs for the (smaller or equal) final size.

The trade is also rejected when expected round-trip costs are too large a
fraction of the stop distance (cost_to_risk > costs.max_cost_to_risk): the
trade would need to earn that many R just to break even.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .config import CostConfig, RiskConfig
from .costs import BPS, estimate_fill, floor_to_step, limit_price, spread_bps
from .models import OrderBook, Side, SymbolRules


@dataclass
class SizingResult:
    ok: bool
    qty: Decimal = Decimal(0)
    limit_price: Decimal | None = None
    reject_reason: str | None = None
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def size_entry(*, equity: Decimal, quote_free: Decimal, stop: Decimal, book: OrderBook,
               rules: SymbolRules, costs: CostConfig, risk: RiskConfig,
               loss_allowance: Decimal) -> SizingResult:
    d: dict = {"equity": equity, "quote_free": quote_free, "stop": stop,
               "best_bid": book.best_bid, "best_ask": book.best_ask, "mid": book.mid,
               "loss_allowance": loss_allowance}
    sp = spread_bps(book)
    d["spread_bps"] = sp
    if sp > costs.max_spread_bps:
        return SizingResult(False, reject_reason=f"spread_too_wide ({sp:.2f}bps)", details=d)

    lim = limit_price(book, Side.BUY, costs, rules.tick_size)
    mid = book.mid
    d["limit_price"] = lim
    if stop >= mid:
        return SizingResult(False, reject_reason="stop_not_below_price", details=d)

    fee = costs.taker_fee_bps / BPS
    cap_notional = min(risk.max_position_notional, equity * risk.max_position_fraction,
                       quote_free / (1 + fee))
    cap_qty = cap_notional / lim
    d.update(cap_notional=cap_notional, cap_qty_by_notional=cap_qty)
    if cap_qty <= 0:
        return SizingResult(False, reject_reason="no_capacity", details=d)

    entry_est = estimate_fill(book, Side.BUY, cap_qty, costs, price_limit=lim)
    exit_est = estimate_fill(book, Side.SELL, cap_qty, costs)
    cap_qty = min(cap_qty, entry_est.fillable_qty)
    d.update(cap_qty_by_depth=entry_est.fillable_qty,
             entry_impact_bps=entry_est.impact_bps, exit_impact_bps=exit_est.impact_bps,
             entry_cost_bps=entry_est.total_bps, exit_cost_bps=exit_est.total_bps)
    if not exit_est.fully_fillable:
        d["note"] = "visible bid depth smaller than size; exit cost estimate uses visible depth only"

    entry_cost_unit = mid * entry_est.total_bps / BPS
    exit_cost_unit = stop * exit_est.total_bps / BPS
    stop_distance = mid - stop
    cost_to_risk = (entry_cost_unit + exit_cost_unit) / stop_distance
    d.update(stop_distance=stop_distance, round_trip_cost_per_unit=entry_cost_unit + exit_cost_unit,
             cost_to_risk=cost_to_risk)
    if cost_to_risk > costs.max_cost_to_risk:
        return SizingResult(False, reject_reason=(
            f"costs_too_high: round trip {cost_to_risk:.3f}R > max {costs.max_cost_to_risk}R"), details=d)

    worst_loss_unit = (lim - stop) + entry_cost_unit + exit_cost_unit
    risk_budget = min(equity * risk.risk_per_trade_fraction, loss_allowance)
    d.update(worst_loss_per_unit=worst_loss_unit, risk_budget=risk_budget)
    if risk_budget <= 0:
        return SizingResult(False, reject_reason="no_risk_budget_left_today", details=d)
    qty_by_risk = risk_budget / worst_loss_unit
    qty = floor_to_step(min(qty_by_risk, cap_qty), rules.step_size)
    d.update(qty_by_risk=qty_by_risk, qty_final=qty, notional_at_limit=qty * lim,
             worst_case_loss=qty * worst_loss_unit,
             expected_round_trip_cost=qty * (entry_cost_unit + exit_cost_unit))
    if qty < rules.min_qty or qty * mid < rules.min_notional:
        return SizingResult(False, reject_reason="below_exchange_minimum", details=d)
    return SizingResult(True, qty=qty, limit_price=lim, details=d)


def size_exit(*, position_qty: Decimal, book: OrderBook, rules: SymbolRules,
              costs: CostConfig) -> SizingResult:
    """Exit sells the whole position (rounded down to step). Remainder below step is dust."""
    qty = floor_to_step(position_qty, rules.step_size)
    lim = limit_price(book, Side.SELL, costs, rules.tick_size)
    est = estimate_fill(book, Side.SELL, qty, costs, price_limit=lim) if qty > 0 else None
    d = {"position_qty": position_qty, "qty": qty, "dust": position_qty - qty,
         "best_bid": book.best_bid, "limit_price": lim,
         "exit_cost_bps": est.total_bps if est else None,
         "fillable_within_limit": est.fillable_qty if est else Decimal(0)}
    if qty < rules.min_qty or qty * book.best_bid < rules.min_notional:
        return SizingResult(False, reject_reason="position_below_exchange_minimum_dust", details=d)
    return SizingResult(True, qty=qty, limit_price=lim, details=d)
