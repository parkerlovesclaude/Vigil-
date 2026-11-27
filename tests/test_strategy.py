from decimal import Decimal as D

from helpers import FAST, HOUR, T0, bars_from_closes, flat_then_breakout, make_cfg
from vigil.strategy import Action, PositionContext, atr, ema, evaluate

CFG = make_cfg().strategy
FLAT = PositionContext(False)


def bars(closes):
    return bars_from_closes(closes, T0 - len(closes) * HOUR)


def test_indicator_math():
    assert ema([D(1), D(2), D(3)], 3) == D(2)
    assert ema([D(1), D(2), D(3), D(4)], 3) == D(3)  # 4*0.5 + 2*0.5
    b = bars([10, 10, 10, 10])
    assert atr(b, 3) == D("1.0")  # high-low = 1 with 0.5 wicks


def test_insufficient_history_holds():
    sig = evaluate(bars([100] * 10), FLAT, CFG)
    assert sig.action is Action.HOLD and sig.reason == "insufficient_history"


def test_breakout_with_trend_enters_with_atr_stop():
    sig = evaluate(bars(flat_then_breakout()), FLAT, CFG)
    assert sig.action is Action.ENTER
    assert sig.stop_price == sig.reference_price - CFG.stop_atr_mult * sig.indicators["atr"]
    assert sig.stop_price < sig.reference_price


def test_no_breakout_holds():
    sig = evaluate(bars(flat_then_breakout(jump=100.1)), FLAT, CFG)
    assert sig.action is Action.HOLD and sig.reason == "no_breakout"


def test_breakout_below_trend_filter_holds():
    # Long decline then a bounce that clears the short channel but stays below the EMA.
    closes = [400 - i * 3 for i in range(100)] + [102, 101, 100, 99, 98, 97] + [104]
    cfg = make_cfg({"strategy": {"trend_ema_period": 30}, "data": {"candle_fetch_limit": 200}}).strategy
    sig = evaluate(bars(closes), FLAT, cfg)
    assert sig.action is Action.HOLD and sig.reason == "below_trend_filter"


def test_trailing_stop_only_moves_up():
    b = bars(flat_then_breakout() + [111])
    pos = PositionContext(True, D("110"), D("200"), D("111"))  # stop already above trail candidate
    sig = evaluate(b, pos, CFG)
    assert sig.stop_price == D("200")
    assert sig.action is Action.EXIT and sig.reason == "trailing_stop"


def test_channel_exit():
    closes = flat_then_breakout() + [112, 113, 114, 109]
    pos = PositionContext(True, D("110"), D("50"), D("114"))
    cfg = make_cfg({"strategy": {"trail_atr_mult": "10", "stop_atr_mult": "1"}}).strategy
    sig = evaluate(bars(closes), pos, cfg)
    assert sig.action is Action.EXIT and sig.reason == "channel_exit"


def test_hold_in_position_raises_stop():
    closes = flat_then_breakout() + [112, 114]
    pos = PositionContext(True, D("110"), D("90"), D("110"))
    sig = evaluate(bars(closes), pos, CFG)
    assert sig.action is Action.HOLD
    assert sig.stop_price > D("90") and sig.highest_close == D("114")


def test_incomplete_position_context_refuses_to_guess():
    sig = evaluate(bars(flat_then_breakout()), PositionContext(True, D("110"), None, None), CFG)
    assert sig.action is Action.HOLD and sig.reason == "position_context_incomplete"


def test_deterministic():
    b = bars(flat_then_breakout())
    assert evaluate(b, FLAT, CFG) == evaluate(b, FLAT, CFG)
