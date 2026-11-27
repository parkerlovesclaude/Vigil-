"""Pre-trade risk checks and trading-stop conditions. Exchange-independent.

Two kinds of stop:
  * Daily loss block: entries are blocked for the rest of the UTC day once the
    mark-to-market loss since the day's start reaches risk.max_daily_loss.
    Clears automatically at the next UTC day.
  * Halt: persists until an operator runs `vigil reset-halt`.
      category "risk"      - max drawdown breached. Exits still allowed.
      category "integrity" - state or exchange cannot be trusted (unresolved
                             order, reconciliation failure, repeated errors,
                             no valid data while in a position). Nothing is
                             sent to the exchange.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import RiskConfig
from .state import State


@dataclass
class RiskResult:
    ok: bool
    checks: list = field(default_factory=list)  # [{name, passed, value, limit}]
    loss_allowance: Decimal = Decimal(0)

    def failed(self) -> list[str]:
        return [c["name"] for c in self.checks if not c["passed"]]


def daily_loss(st: State, equity: Decimal) -> Decimal:
    if st.day_start_equity is None:
        return Decimal(0)
    return max(Decimal(0), st.day_start_equity - equity)


def drawdown(st: State, equity: Decimal) -> Decimal:
    if not st.peak_equity:
        return Decimal(0)
    return max(Decimal(0), (st.peak_equity - equity) / st.peak_equity)


def check_entry(st: State, equity: Decimal, cfg: RiskConfig, bar_close_ms: int,
                interval_ms: int) -> RiskResult:
    checks = []

    def add(name, passed, value=None, limit=None):
        checks.append({"name": name, "passed": bool(passed), "value": value, "limit": limit})

    add("not_halted", not st.halt.active, st.halt.reason or None)
    add("reconciled", not st.needs_reconcile)
    add("no_unresolved_orders", not st.unresolved_orders(), len(st.unresolved_orders()), 0)
    add("flat", st.position is None)
    dl = daily_loss(st, equity)
    add("daily_loss_below_limit", dl < cfg.max_daily_loss, dl, cfg.max_daily_loss)
    dd = drawdown(st, equity)
    add("drawdown_below_limit", dd < cfg.max_drawdown_fraction, dd, cfg.max_drawdown_fraction)
    # An entry needs room for its exit order too.
    add("order_budget_today", st.orders_today + 2 <= cfg.max_orders_per_day,
        st.orders_today, cfg.max_orders_per_day)
    bars_since_exit = (bar_close_ms - st.last_exit_bar_ms) // interval_ms if st.last_exit_bar_ms else None
    add("cooldown_elapsed",
        bars_since_exit is None or bars_since_exit >= cfg.cooldown_bars_after_exit,
        bars_since_exit, cfg.cooldown_bars_after_exit)
    add("equity_positive", equity > 0, equity, 0)
    ok = all(c["passed"] for c in checks)
    return RiskResult(ok, checks, max(Decimal(0), cfg.max_daily_loss - dl))


def check_exit(st: State) -> RiskResult:
    """Exits reduce risk, so only integrity problems block them."""
    checks = [
        {"name": "no_integrity_halt", "passed": not (st.halt.active and st.halt.category == "integrity"),
         "value": st.halt.reason or None, "limit": None},
        {"name": "reconciled", "passed": not st.needs_reconcile, "value": None, "limit": None},
        {"name": "no_unresolved_orders", "passed": not st.unresolved_orders(),
         "value": len(st.unresolved_orders()), "limit": 0},
        {"name": "has_position", "passed": st.position is not None, "value": None, "limit": None},
    ]
    return RiskResult(all(c["passed"] for c in checks), checks)


def halt_conditions(st: State, equity: Decimal, cfg: RiskConfig) -> tuple[str, str] | None:
    """Return (category, reason) if a new halt must be raised."""
    dd = drawdown(st, equity)
    if dd >= cfg.max_drawdown_fraction:
        return "risk", f"max_drawdown_breached: {dd:.4f} >= {cfg.max_drawdown_fraction}"
    if st.consecutive_errors >= cfg.max_consecutive_errors:
        return "integrity", f"consecutive_errors: {st.consecutive_errors}"
    return None
