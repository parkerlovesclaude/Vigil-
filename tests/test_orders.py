"""Order lifecycle under failure: duplicates, rejects, timeouts, partial fills, delayed visibility."""

from decimal import Decimal as D

import pytest

from helpers import RULES, Rig
from vigil.models import OrderStatus, Side
from vigil.orders import IntegrityError, make_client_order_id


def om_and_state(tmp_path):
    rig = Rig(tmp_path)
    return rig, rig.engine.om, rig.engine.st


def buy(om, eval_id="E1", qty=D("1"), limit=D("110.5")):
    return om.submit(purpose="ENTER", evaluation_id=eval_id, side=Side.BUY, qty=qty,
                     limit_price=limit, stop_price=D("100"), ref_price=D("110"))


def test_client_order_id_is_deterministic_and_short():
    a = make_client_order_id("paper", "BTCUSDT:1h:123", "ENTER")
    assert a == make_client_order_id("paper", "BTCUSDT:1h:123", "ENTER")
    assert a != make_client_order_id("paper", "BTCUSDT:1h:124", "ENTER")
    assert len(a) <= 36


def test_normal_fill_creates_position_from_fills(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rec = buy(om)
    assert rec.status == "FILLED"
    assert st.position.qty == D("1") - D("1") * D("0.001")   # buy fee taken in base
    assert st.position.stop_price == D("100")
    assert rig.ex.balances()["BTC"].total == st.position.qty
    events = [e["event"] for e in rig.audit_events()]
    for ev in ("order_intent", "order_submit", "order_ack", "order_fill"):
        assert ev in events
    assert events.index("order_intent") < events.index("order_submit") < events.index("order_ack")


def test_duplicate_submission_is_prevented(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    first = buy(om)
    second = buy(om)  # same evaluation id
    assert second is first
    assert len(rig.ex.orders) == 1
    assert "duplicate_order_prevented" in [e["event"] for e in rig.audit_events()]


def test_rejected_order_changes_nothing(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rig.ex.faults["place"] = ["reject"]
    rec = buy(om)
    assert rec.status == "REJECTED" and st.position is None
    assert st.consecutive_errors == 1


def test_timeout_after_execution_is_resolved_not_resubmitted(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rig.ex.faults["place"] = ["timeout_after"]
    rec = buy(om)
    assert rec.status == "FILLED"
    assert len(rig.ex.orders) == 1                      # never resubmitted
    assert st.position.qty == D("0.999")                # fills applied exactly once
    assert "order_uncertain" in [e["event"] for e in rig.audit_events()]


def test_timeout_before_reaching_exchange_resolves_to_not_found(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rig.ex.faults["place"] = ["timeout_before"]
    rec = buy(om)
    assert rec.status == "NOT_FOUND"
    assert st.position is None and st.needs_reconcile   # balances get double-checked
    assert len(rig.ex.orders) == 0


def test_delayed_visibility_then_found(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rig.ex.faults["place"] = ["timeout_after"]
    rig.ex.faults["get"] = ["not_found", "timeout", "not_found"]
    rec = buy(om)
    assert rec.status == "FILLED" and st.position is not None


def test_partial_fill_position_reflects_only_filled_qty(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rig.ex.faults["place"] = ["partial:0.4"]
    rec = buy(om, qty=D("1"))
    assert rec.status == "EXPIRED"         # IOC remainder expired
    assert rec.filled_qty == D("0.4")
    assert st.position.qty == D("0.4") * D("0.999")


def test_partial_fill_via_limit_price(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rec = buy(om, qty=D("5"), limit=D("110.07"))   # only the first two ask levels (2 each) are within limit
    assert rec.filled_qty == D("4") and rec.status == "EXPIRED"


def test_unresolvable_order_raises_integrity_error(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rig.ex.faults["place"] = ["timeout_after"]
    rig.ex.faults["get"] = ["timeout"] * 100
    with pytest.raises(IntegrityError):
        buy(om)
    assert st.needs_reconcile
    assert st.orders[next(iter(st.orders))].status == "UNKNOWN"


def test_resolving_twice_does_not_double_apply_fills(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rec = buy(om)
    qty = st.position.qty
    om.resolve(rec)
    om._sync_fills(rec)
    assert st.position.qty == qty


def test_sell_realizes_pnl_net_of_fees(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    buy(om)
    rig.market.book_price = D("120")
    om.submit(purpose="EXIT", evaluation_id="X1", side=Side.SELL, qty=D("0.999"),
              limit_price=D("119"))
    assert st.position.qty == 0
    # bought 1 @110.01 (fee in BTC), sold 0.999 @119.99 minus 0.1% quote fee
    proceeds = D("0.999") * D("119.99") * D("0.999")
    assert st.realized_pnl_today == proceeds - D("110.01")


def test_acknowledged_order_disappearing_is_integrity_error(tmp_path):
    rig, om, st = om_and_state(tmp_path)
    rec = buy(om)
    rec.status = OrderStatus.NEW.value          # pretend we still think it's open
    rig.ex.orders.pop(rec.client_order_id)
    with pytest.raises(IntegrityError):
        om.resolve(rec)
