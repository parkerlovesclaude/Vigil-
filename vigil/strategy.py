"""The one strategy: long-only Donchian breakout with a trend filter and ATR stops.

Exchange-independent and side-effect free: given validated closed candles and
the current position context, return a Signal. Nothing here knows about
orders, fees, balances or the network. See docs/STRATEGY.md for the rationale.

Rules (evaluated once per closed bar):
  Flat:
    ENTER  if close > highest high of the previous `entry_lookback` bars
           and close > EMA(close, trend_ema_period)
           and ATR > 0.
           Initial stop = close - stop_atr_mult * ATR.
  Long:
    stop   = max(previous stop, highest close since entry - trail_atr_mult * ATR)
             (the stop only ever moves up)
    EXIT   if close <= stop                                   ("trailing_stop")
           or close < lowest low of previous `exit_lookback` bars ("channel_exit")
    else HOLD.
Between bar closes the engine also exits if the best bid trades at/below the
stop (protective intrabar check, `stop_breached`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .config import StrategyConfig
from .models import Candle


class Action(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class PositionContext:
    in_position: bool
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    highest_close: Decimal | None = None


@dataclass
class Signal:
    action: Action
    reason: str
    bar_close_time_ms: int
    reference_price: Decimal
    stop_price: Decimal | None = None
    highest_close: Decimal | None = None
    indicators: dict = field(default_factory=dict)


def ema(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise ValueError("not enough values for EMA")
    k = Decimal(2) / Decimal(period + 1)
    e = sum(values[:period]) / Decimal(period)  # seed with SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles: list[Candle], period: int) -> Decimal:
    """Wilder's ATR."""
    if len(candles) < period + 1:
        raise ValueError("not enough candles for ATR")
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    a = sum(trs[:period]) / Decimal(period)
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / Decimal(period)
    return a


def evaluate(candles: list[Candle], pos: PositionContext, cfg: StrategyConfig) -> Signal:
    """Pure function: the same inputs always produce the same Signal."""
    last = candles[-1]
    close = last.close
    if len(candles) < cfg.min_history_bars:
        return Signal(Action.HOLD, "insufficient_history", last.close_time_ms, close,
                      pos.stop_price, pos.highest_close)

    prior = candles[:-1]
    ema_v = ema([c.close for c in candles], cfg.trend_ema_period)
    atr_v = atr(candles, cfg.atr_period)
    entry_high = max(c.high for c in prior[-cfg.entry_lookback:])
    exit_low = min(c.low for c in prior[-cfg.exit_lookback:])
    ind = {"close": close, "ema": ema_v, "atr": atr_v,
           "entry_channel_high": entry_high, "exit_channel_low": exit_low}

    if atr_v <= 0:
        return Signal(Action.HOLD, "atr_zero", last.close_time_ms, close,
                      pos.stop_price, pos.highest_close, ind)

    if not pos.in_position:
        if close > entry_high and close > ema_v:
            stop = close - cfg.stop_atr_mult * atr_v
            if stop <= 0:
                return Signal(Action.HOLD, "stop_nonpositive", last.close_time_ms, close, None, None, ind)
            return Signal(Action.ENTER, "breakout_above_channel_with_trend", last.close_time_ms,
                          close, stop, close, ind)
        reason = "no_breakout" if close <= entry_high else "below_trend_filter"
        return Signal(Action.HOLD, reason, last.close_time_ms, close, None, None, ind)

    # In position: the context must be complete, otherwise we refuse to guess.
    if pos.stop_price is None or pos.highest_close is None:
        return Signal(Action.HOLD, "position_context_incomplete", last.close_time_ms, close,
                      pos.stop_price, pos.highest_close, ind)
    highest = max(pos.highest_close, close)
    trail = highest - cfg.trail_atr_mult * atr_v
    stop = max(pos.stop_price, trail)
    ind["trail_candidate"] = trail
    if close <= stop:
        return Signal(Action.EXIT, "trailing_stop", last.close_time_ms, close, stop, highest, ind)
    if close < exit_low:
        return Signal(Action.EXIT, "channel_exit", last.close_time_ms, close, stop, highest, ind)
    return Signal(Action.HOLD, "in_position_hold", last.close_time_ms, close, stop, highest, ind)
