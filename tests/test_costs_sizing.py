from decimal import Decimal as D

from helpers import RULES, book_around, make_cfg
from vigil.costs import ceil_to_step, estimate_fill, floor_to_step, limit_price, spread_bps
from vigil.models import BookLevel, OrderBook, Side
from vigil.sizing import size_entry, size_exit

CFG = make_cfg()
COSTS, RISK = CFG.costs, CFG.risk


def kw(**over):
    base = dict(equity=D("10000"), quote_free=D("10000"), stop=D("90"), book=book_around(100, 0),
                rules=RULES, costs=COSTS, risk=RISK, loss_allowance=D("1000"))
    base.update(over)
    return base


def test_rounding_helpers():
    assert floor_to_step(D("1.23456"), D("0.001")) == D("1.234")
    assert ceil_to_step(D("1.23401"), D("0.01")) == D("1.24")


def test_book_walk_vwap_and_impact():
    book = OrderBook(0, (BookLevel(D("99"), D("1")),), (BookLevel(D("101"), D("1")), BookLevel(D("103"), D("1"))))
    est = estimate_fill(book, Side.BUY, D("2"), COSTS)
    assert est.vwap == D("102")
    assert est.impact_bps == D("200")      # (102-100)/100
    assert est.total_bps == D("200") + COSTS.extra_slippage_bps + COSTS.taker_fee_bps
    assert est.fully_fillable


def test_book_walk_respects_limit_and_depth():
    book = OrderBook(0, (BookLevel(D("99"), D("1")),), (BookLevel(D("101"), D("1")), BookLevel(D("103"), D("1"))))
    est = estimate_fill(book, Side.BUY, D("2"), COSTS, price_limit=D("102"))
    assert est.fillable_qty == D("1") and not est.fully_fillable


def test_limit_price_buffer_and_tick():
    book = book_around(100, 0)
    assert limit_price(book, Side.BUY, COSTS, D("0.01")) == ceil_to_step(D("100.01") * D("1.0025"), D("0.01"))
    assert limit_price(book, Side.SELL, COSTS, D("0.01")) == floor_to_step(D("99.99") * D("0.9975"), D("0.01"))


def test_size_is_risk_bound_and_includes_costs():
    r = size_entry(**kw())
    assert r.ok
    d = r.details
    expected_unit = (d["limit_price"] - D("90")) + D("100") * d["entry_cost_bps"] / 10000 \
        + D("90") * d["exit_cost_bps"] / 10000
    assert d["worst_loss_per_unit"] == expected_unit
    assert r.qty == floor_to_step(D("50") / expected_unit, RULES.step_size)  # 0.5% of 10000
    assert r.qty * expected_unit <= D("50")


def test_costs_reduce_size_versus_cost_free():
    with_costs = size_entry(**kw()).qty
    free = make_cfg({"costs": {"taker_fee_bps": "0", "extra_slippage_bps": "0"}}).costs
    no_costs = size_entry(**kw(costs=free)).qty
    assert with_costs < no_costs


def test_notional_and_fraction_caps():
    r = size_entry(**kw(stop=D("99.5")))  # tiny stop -> risk size huge, caps bind
    assert r.ok is False or r.qty * r.limit_price <= RISK.max_position_notional
    tight = make_cfg({"risk": {"max_position_notional": "200"}}).risk
    r = size_entry(**kw(risk=tight, stop=D("97")))
    assert r.ok and r.qty * r.limit_price <= D("200")


def test_cash_cap_includes_fee():
    r = size_entry(**kw(quote_free=D("100"), stop=D("80")))
    assert r.ok
    assert r.qty * r.limit_price * (1 + COSTS.taker_fee_bps / 10000) <= D("100")


def test_rejects_wide_spread():
    r = size_entry(**kw(book=book_around(100, 0, spread=D("1"))))
    assert not r.ok and r.reject_reason.startswith("spread_too_wide")


def test_rejects_when_costs_too_large_relative_to_stop():
    r = size_entry(**kw(stop=D("99.7")))
    assert not r.ok and r.reject_reason.startswith("costs_too_high")


def test_rejects_below_exchange_minimum():
    r = size_entry(**kw(loss_allowance=D("0.01")))
    assert not r.ok and r.reject_reason == "below_exchange_minimum"


def test_no_risk_budget_left():
    r = size_entry(**kw(loss_allowance=D("0")))
    assert not r.ok and r.reject_reason == "no_risk_budget_left_today"


def test_depth_caps_size():
    thin = book_around(100, 0, qty=D("0.02"), levels=3)
    r = size_entry(**kw(book=thin, stop=D("80")))
    assert r.ok and r.qty == D("0.06")   # risk alone would allow far more


def test_stop_above_price_rejected():
    assert size_entry(**kw(stop=D("101"))).reject_reason == "stop_not_below_price"


def test_spread_bps():
    assert spread_bps(book_around(100, 0, spread=D("0.1"))) == D("10")


def test_exit_sizing_and_dust():
    r = size_exit(position_qty=D("1.23456"), book=book_around(100, 0), rules=RULES, costs=COSTS)
    assert r.ok and r.qty == D("1.2345") and r.details["dust"] == D("0.00006")
    dust = size_exit(position_qty=D("0.00005"), book=book_around(100, 0), rules=RULES, costs=COSTS)
    assert not dust.ok and "dust" in dust.reject_reason
