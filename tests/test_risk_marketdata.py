from dataclasses import replace
from decimal import Decimal as D

from helpers import HOUR, T0, bars_from_closes, book_around, make_cfg
from vigil.marketdata import closed_candles, validate_book, validate_candles
from vigil.models import BookLevel, OrderBook
from vigil.risk import check_entry, check_exit, halt_conditions
from vigil.state import Halt, OrderRecord, Position, State

CFG = make_cfg()
NOW = T0 + 10_000


def bars(n=30):
    return bars_from_closes([100] * n, T0 - n * HOUR)


def vc(candles, now=NOW):
    return validate_candles(candles, now, 3600, 16, CFG.data)


# ---- market data ---------------------------------------------------------------
def test_valid_data_passes():
    assert vc(bars()).ok
    assert validate_book(book_around(100, NOW), NOW, D("100"), CFG.data).ok


def test_unclosed_bar_is_dropped():
    b = bars_from_closes([100] * 31, T0 - 30 * HOUR)   # last bar is still open at NOW
    assert len(closed_candles(b, NOW)) == 30


def test_stale_candles_rejected():
    chk = vc(bars(), now=NOW + 3 * HOUR)
    assert any(i.startswith("stale_candles") for i in chk.issues)


def test_candles_valid_anywhere_between_bar_closes():
    assert vc(bars(), now=T0 + 40 * 60_000).ok     # 40 minutes into the next bar


def test_gap_and_order_rejected():
    b = bars()
    gapped = b[:10] + b[11:]
    assert any("candle_gap" in i for i in vc(gapped).issues)
    assert any("non_monotonic" in i for i in vc(b[:10] + [b[5]] + b[10:]).issues)


def test_malformed_candles_rejected():
    b = bars()
    b[-1] = replace(b[-1], high=D("1"))
    assert any("OHLC inconsistent" in i for i in vc(b).issues)
    b[-1] = replace(b[-1], close=D("NaN"))
    assert any("non-finite" in i for i in vc(b).issues)
    b[-1] = replace(b[-1], close=D("-1"), low=D("-2"))
    assert not vc(b).ok


def test_insufficient_history_flagged():
    assert any("insufficient_history" in i for i in vc(bars(5)).issues)


def test_book_problems():
    d = CFG.data
    assert "order_book_missing" in validate_book(None, NOW, None, d).issues
    stale = validate_book(book_around(100, NOW - 60_000), NOW, D("100"), d)
    assert any(i.startswith("stale_book") for i in stale.issues)
    crossed = OrderBook(NOW, (BookLevel(D("101"), D("1")),), (BookLevel(D("100"), D("1")),))
    assert any("crossed" in i for i in validate_book(crossed, NOW, None, d).issues)
    unsorted = OrderBook(NOW, (BookLevel(D("99"), D("1")), BookLevel(D("99.5"), D("1"))),
                         (BookLevel(D("100"), D("1")),))
    assert "unsorted_book_bids" in validate_book(unsorted, NOW, None, d).issues
    bad = OrderBook(NOW, (BookLevel(D("99"), D("0")),), (BookLevel(D("100"), D("1")),))
    assert "malformed_book_bid[0]" in validate_book(bad, NOW, None, d).issues


def test_book_vs_candle_conflict():
    chk = validate_book(book_around(120, NOW), NOW, D("100"), CFG.data)
    assert any("book_vs_candle_conflict" in i for i in chk.conflicts)
    assert chk.usable and not chk.ok   # usable for managing a position, not for new entries


# ---- risk --------------------------------------------------------------------------
def fresh_state(**kw):
    st = State(needs_reconcile=False, day_start_equity=D("10000"), peak_equity=D("10000"))
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def entry(st, equity=D("10000"), bar=T0 - 1):
    return check_entry(st, equity, CFG.risk, bar, HOUR)


def test_entry_allowed_when_clean():
    r = entry(fresh_state())
    assert r.ok and r.loss_allowance == CFG.risk.max_daily_loss


def test_daily_loss_blocks_entry():
    r = entry(fresh_state(), equity=D("9950"))
    assert not r.ok and "daily_loss_below_limit" in r.failed()


def test_drawdown_blocks_entry_and_halts():
    st = fresh_state(peak_equity=D("12000"), day_start_equity=D("10500"))
    eq = D("10700")  # 10.8% below peak, but not a daily loss breach
    assert "drawdown_below_limit" in entry(st, eq).failed()
    assert halt_conditions(st, eq, CFG.risk)[0] == "risk"


def test_order_budget_leaves_room_for_exit():
    r = entry(fresh_state(orders_today=CFG.risk.max_orders_per_day - 1))
    assert "order_budget_today" in r.failed()


def test_cooldown_after_exit():
    r = entry(fresh_state(last_exit_bar_ms=T0 - 1))
    assert "cooldown_elapsed" in r.failed()
    assert entry(fresh_state(last_exit_bar_ms=T0 - 1 - HOUR)).ok


def test_halt_unreconciled_unresolved_and_position_block_entry():
    rec = OrderRecord("x", "e", "ENTER", "BTCUSDT", "BUY", D(1), D(1), "UNKNOWN", 0, 0)
    st = fresh_state(halt=Halt(True, "risk", "dd", 0), needs_reconcile=True, orders={"x": rec},
                     position=Position(D(1), D(1), D(1), D(1), D(1), "c", 0))
    failed = entry(st).failed()
    for name in ("not_halted", "reconciled", "no_unresolved_orders", "flat"):
        assert name in failed


def test_exits_allowed_under_risk_halt_but_not_integrity_halt():
    pos = Position(D(1), D(1), D(1), D(1), D(1), "c", 0)
    st = fresh_state(position=pos, halt=Halt(True, "risk", "dd", 0))
    assert check_exit(st).ok
    st.halt = Halt(True, "integrity", "recon", 0)
    assert not check_exit(st).ok


def test_consecutive_errors_halt():
    st = fresh_state(consecutive_errors=CFG.risk.max_consecutive_errors)
    assert halt_conditions(st, D("10000"), CFG.risk)[0] == "integrity"
