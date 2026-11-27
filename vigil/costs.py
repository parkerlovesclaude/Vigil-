"""Execution-cost model. Costs are part of the trade decision, not an afterthought.

For a given side and quantity we walk the visible order book to get the
volume-weighted price we would expect to pay (this captures the half-spread
and the depth impact), then add a configured safety margin for slippage we
cannot see (latency, hidden liquidity moving) and the taker fee.

All costs are expressed in basis points of the mid price so that entry and
exit costs can be added and compared with the stop distance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import CostConfig
from .models import OrderBook, Side

BPS = Decimal("10000")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class FillEstimate:
    side: Side
    qty: Decimal
    fillable_qty: Decimal        # how much the visible book can absorb (capped at qty)
    vwap: Decimal | None         # expected average price for fillable_qty
    mid: Decimal
    impact_bps: Decimal          # adverse distance of vwap from mid (half-spread + depth)
    extra_slippage_bps: Decimal
    fee_bps: Decimal

    @property
    def total_bps(self) -> Decimal:
        return self.impact_bps + self.extra_slippage_bps + self.fee_bps

    @property
    def fully_fillable(self) -> bool:
        return self.fillable_qty >= self.qty


def spread_bps(book: OrderBook) -> Decimal:
    return (book.best_ask - book.best_bid) / book.mid * BPS


def estimate_fill(book: OrderBook, side: Side, qty: Decimal, cfg: CostConfig,
                  price_limit: Decimal | None = None) -> FillEstimate:
    """Walk the book for `qty`. Levels beyond `price_limit` (our limit price) are not usable."""
    levels = book.asks if side is Side.BUY else book.bids
    remaining = qty
    notional = Decimal(0)
    filled = Decimal(0)
    for lvl in levels:
        if remaining <= 0:
            break
        if price_limit is not None:
            if side is Side.BUY and lvl.price > price_limit:
                break
            if side is Side.SELL and lvl.price < price_limit:
                break
        take = min(remaining, lvl.qty)
        notional += take * lvl.price
        filled += take
        remaining -= take
    mid = book.mid
    if filled > 0:
        vwap = notional / filled
        impact = (vwap - mid) / mid * BPS if side is Side.BUY else (mid - vwap) / mid * BPS
    else:
        vwap = None
        impact = Decimal(0)
    return FillEstimate(side, qty, filled, vwap, mid, impact, cfg.extra_slippage_bps, cfg.taker_fee_bps)


def limit_price(book: OrderBook, side: Side, cfg: CostConfig, tick: Decimal) -> Decimal:
    """Marketable limit price: crosses the spread but caps the worst acceptable price."""
    if side is Side.BUY:
        return ceil_to_step(book.best_ask * (1 + cfg.limit_price_buffer_bps / BPS), tick)
    return floor_to_step(book.best_bid * (1 - cfg.limit_price_buffer_bps / BPS), tick)
