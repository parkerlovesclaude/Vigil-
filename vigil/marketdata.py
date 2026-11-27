"""Market-data validation. Exchange-independent.

The strategy only ever sees data that passed these checks. Any failure makes
the evaluation a HOLD with the failure reasons recorded in the audit log;
the system never "fills in" missing data or trades on a best guess.

Two levels of result:
  issues    - malformed, stale or missing data. Nothing may use it.
  conflicts - each source is valid but they disagree (order book mid far from
              the last candle close). New entries are refused; managing an
              existing position (stop checks, exits) is still allowed, because
              a fast market move is exactly when the stop must work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import DataConfig
from .models import Candle, OrderBook

BPS = Decimal("10000")


@dataclass
class DataCheck:
    issues: list[str] = field(default_factory=list)      # data is malformed/stale: unusable
    conflicts: list[str] = field(default_factory=list)   # usable, but sources disagree

    @property
    def usable(self) -> bool:
        return not self.issues

    @property
    def ok(self) -> bool:
        return not self.issues and not self.conflicts


def closed_candles(candles: list[Candle], now_ms: int) -> list[Candle]:
    """Drop any candle that has not closed yet (exchanges return the in-progress bar)."""
    return [c for c in candles if c.close_time_ms < now_ms]


def validate_candles(candles: list[Candle], now_ms: int, interval_s: int,
                     min_bars: int, cfg: DataConfig) -> DataCheck:
    chk = DataCheck()
    if len(candles) < min_bars:
        chk.issues.append(f"insufficient_history: have {len(candles)} closed bars, need {min_bars}")
        if not candles:
            return chk
    step_ms = interval_s * 1000
    prev: Candle | None = None
    for i, c in enumerate(candles):
        vals = (c.open, c.high, c.low, c.close, c.volume)
        if any((not v.is_finite()) for v in vals):
            chk.issues.append(f"malformed_candle[{i}]: non-finite value")
            continue
        if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            chk.issues.append(f"malformed_candle[{i}]: non-positive price or negative volume")
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.high < c.low:
            chk.issues.append(f"malformed_candle[{i}]: OHLC inconsistent")
        if c.close_time_ms <= c.open_time_ms:
            chk.issues.append(f"malformed_candle[{i}]: close_time <= open_time")
        if prev is not None:
            delta = c.open_time_ms - prev.open_time_ms
            if delta <= 0:
                chk.issues.append(f"non_monotonic_candles at index {i}")
            elif delta != step_ms:
                chk.issues.append(f"candle_gap at index {i}: {delta // 1000}s instead of {interval_s}s")
        prev = c
    last = candles[-1]
    if last.close_time_ms >= now_ms:
        chk.issues.append("last_candle_not_closed")
    # The newest closed bar must be the one that most recently closed by the clock.
    expected_close = (now_ms // step_ms) * step_ms - 1
    if last.close_time_ms < expected_close:
        behind = (expected_close - last.close_time_ms) // 1000
        chk.issues.append(f"stale_candles: latest closed bar missing (feed is {behind}s behind)")
    return chk


def validate_book(book: OrderBook | None, now_ms: int, last_close: Decimal | None,
                  cfg: DataConfig) -> DataCheck:
    chk = DataCheck()
    if book is None:
        chk.issues.append("order_book_missing")
        return chk
    if not book.bids or not book.asks:
        chk.issues.append("order_book_empty_side")
        return chk
    for side_name, levels, descending in (("bid", book.bids, True), ("ask", book.asks, False)):
        for i, lvl in enumerate(levels):
            if not (lvl.price.is_finite() and lvl.qty.is_finite()) or lvl.price <= 0 or lvl.qty <= 0:
                chk.issues.append(f"malformed_book_{side_name}[{i}]")
        for a, b in zip(levels, levels[1:]):
            if (descending and b.price >= a.price) or (not descending and b.price <= a.price):
                chk.issues.append(f"unsorted_book_{side_name}s")
                break
    if chk.issues:
        return chk
    if book.best_bid >= book.best_ask:
        chk.issues.append(f"crossed_book: bid {book.best_bid} >= ask {book.best_ask}")
    age_s = (now_ms - book.ts_ms) / 1000
    if age_s > cfg.max_book_age_seconds:
        chk.issues.append(f"stale_book: {age_s:.1f}s old (max {cfg.max_book_age_seconds}s)")
    if age_s < -cfg.max_clock_skew_ms / 1000:
        chk.issues.append("book_timestamp_in_future")
    if last_close is not None and last_close > 0 and not chk.issues:
        dev_bps = abs(book.mid - last_close) / last_close * BPS
        if dev_bps > cfg.max_book_vs_close_bps:
            chk.conflicts.append(
                f"book_vs_candle_conflict: mid {book.mid} deviates {dev_bps:.0f}bps from last close {last_close}")
    return chk
