"""Reconciliation: prove local state matches the exchange before trading.

Runs at every startup, after any uncertain order outcome, and after an
operator resets a halt. Trading (in any mode) cannot proceed until every
check passes. On failure the system raises an integrity halt and stops.

Checks:
  clock_skew            |exchange time - local time| <= data.max_clock_skew_ms
  local_orders_resolved every non-terminal local order is resolved by querying
                        the exchange; fills are synced exactly once
  no_unexpected_open_orders  no open orders on the symbol (the strategy only
                        uses IOC orders, so anything open is unexplained)
  base_balance_matches  exchange base balance equals the local position
                        (within tolerance); when flat, any base holding must be
                        dust (below the exchange minimum notional). The account
                        is expected to be dedicated to this system.
  quote_balance_present quote asset balance exists
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from .exchange.base import ExchangeError
from .models import OrderStatus
from .orders import IntegrityError, OrderManager


def reconcile(eng) -> bool:
    cfg, ex, st, audit = eng.cfg, eng.ex, eng.st, eng.audit
    om: OrderManager = eng.om
    rules = eng.rules
    checks: list[dict] = []
    evidence: dict = {}

    def add(name, passed, detail=None):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    audit.write("reconcile_start", {"local_position": st.position,
                                    "unresolved_local_orders": [o.client_order_id for o in st.unresolved_orders()]})
    try:
        server = ex.server_time_ms()
        skew = server - eng.clock()
        add("clock_skew", abs(skew) <= cfg.data.max_clock_skew_ms, {"skew_ms": skew})

        unresolved = []
        for rec in list(st.orders.values()):
            needs = not OrderStatus(rec.status).is_terminal or \
                (rec.filled_qty > 0 and not om._fills_complete(rec))
            if not needs:
                continue
            try:
                om.resolve(rec)
            except IntegrityError as exc:
                unresolved.append({"client_order_id": rec.client_order_id, "error": str(exc)})
        add("local_orders_resolved", not unresolved, unresolved or None)

        open_orders = ex.open_orders(cfg.run.symbol)
        evidence["open_orders"] = [{"client_order_id": o.client_order_id, "status": o.status.value}
                                   for o in open_orders]
        add("no_unexpected_open_orders", not open_orders, evidence["open_orders"] or None)

        bals = ex.balances()
        base = bals.get(rules.base_asset)
        quote = bals.get(rules.quote_asset)
        base_total = base.total if base else Decimal(0)
        evidence["balances"] = {k: asdict(v) for k, v in bals.items()
                                if k in (rules.base_asset, rules.quote_asset)}
        add("quote_balance_present", quote is not None)

        book = ex.order_book(cfg.run.symbol, 5)
        price = book.best_bid
        if st.position is None:
            ok = base_total * price < rules.min_notional or base_total < rules.min_qty
            add("base_balance_matches", ok, {"expected": "dust only (flat)", "exchange_base": base_total,
                                             "value_quote": base_total * price})
        else:
            expected = st.position.qty
            tol = max(rules.step_size, expected * cfg.risk.position_tolerance_fraction)
            add("base_balance_matches", abs(base_total - expected) <= tol,
                {"expected": expected, "exchange_base": base_total, "tolerance": tol})
    except (ExchangeError, IntegrityError) as exc:
        add("exchange_reachable", False, str(exc))

    ok = all(c["passed"] for c in checks)
    now = eng.clock()
    if ok:
        st.needs_reconcile = False
        st.last_reconciled_ms = now
    else:
        st.needs_reconcile = True
        eng.raise_halt("integrity", "reconciliation_failed: " +
                       ", ".join(c["name"] for c in checks if not c["passed"]))
    eng.store.save(st)
    audit.write("reconcile_result", {"ok": ok, "checks": checks, "evidence": evidence,
                                     "local_position": st.position})
    return ok
