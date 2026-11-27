"""Shared fixtures: a controllable clock, a scripted market, small fast configs."""

from __future__ import annotations

import copy
from decimal import Decimal as D
from pathlib import Path

from vigil.audit import AuditLog
from vigil.config import build_config
from vigil.engine import Engine
from vigil.exchange.paper import PaperExchange
from vigil.models import BookLevel, Candle, OrderBook, SymbolRules
from vigil.state import StateStore

HOUR = 3_600_000
T0 = 1_790_000_000_000 // HOUR * HOUR   # an hour boundary
RULES = SymbolRules("BTCUSDT", "BTC", "USDT", D("0.01"), D("0.0001"), D("0.0001"), D("5"))

FAST = {
    "strategy": {"entry_lookback": 5, "exit_lookback": 3, "trend_ema_period": 5, "atr_period": 3,
                 "stop_atr_mult": "2", "trail_atr_mult": "3"},
    "data": {"candle_fetch_limit": 60},
    "exchange": {"order_poll_seconds": "1", "order_resolve_timeout_seconds": 10},
}


def make_cfg(overrides: dict | None = None, base: dict | None = FAST):
    raw = copy.deepcopy(base or {})
    for sec, vals in (overrides or {}).items():
        raw.setdefault(sec, {}).update(vals)
    return build_config(raw)


class Clock:
    def __init__(self, ms: int):
        self.ms = ms

    def __call__(self) -> int:
        return self.ms

    def sleep(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


def bars_from_closes(closes, start_open_ms: int, interval_ms: int = HOUR, wick=D("0.5")):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        c, o = D(str(c)), D(str(prev))
        out.append(Candle(start_open_ms + i * interval_ms, start_open_ms + (i + 1) * interval_ms - 1,
                          o, max(o, c) + wick, min(o, c) - wick, c, D("10")))
        prev = c
    return out


def book_around(price, ts_ms: int, spread=D("0.02"), step=D("0.05"), qty=D("2"), levels=10):
    p = D(str(price))
    bids = tuple(BookLevel(p - spread / 2 - step * i, qty) for i in range(levels))
    asks = tuple(BookLevel(p + spread / 2 + step * i, qty) for i in range(levels))
    return OrderBook(ts_ms, bids, asks)


class FakeMarket:
    """Serves whatever candles/book the test sets. Book timestamps follow the clock unless frozen."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.bars: list[Candle] = []
        self.book_price = D("100")
        self.book_ts: int | None = None   # None = fresh at every call
        self.book_override: OrderBook | None = None

    def candles(self, symbol, interval, limit):
        return self.bars[-limit:]

    def order_book(self, symbol, depth):
        if self.book_override is not None:
            return self.book_override
        ts = self.clock() if self.book_ts is None else self.book_ts
        return book_around(self.book_price, ts)


def flat_then_breakout(n_flat=40, flat=100, jump=110):
    return [flat + (0.2 if i % 2 else -0.2) for i in range(n_flat)] + [jump]


class Rig:
    """Engine wired to a PaperExchange over a FakeMarket, all on a fake clock."""

    def __init__(self, tmp: Path, cfg=None, closes=None, now_offset_s: int = 10):
        self.cfg = cfg or make_cfg({"run": {"state_dir": str(tmp)}})
        closes = closes or flat_then_breakout()
        # Last closed bar closes at T0 - 1; clock is T0 + offset.
        self.clock = Clock(T0 + now_offset_s * 1000)
        self.market = FakeMarket(self.clock)
        self.market.bars = bars_from_closes(closes, T0 - len(closes) * HOUR)
        self.market.book_price = self.market.bars[-1].close
        self.tmp = Path(tmp)
        self.ex = PaperExchange(self.market, RULES, starting_quote=D("10000"), fee_bps=D("10"),
                                account_path=self.tmp / "paper_account.json", clock=self.clock)
        self.store = StateStore(self.tmp / "state.json")
        self.engine = self.new_engine()

    def new_engine(self):
        audit = AuditLog(self.tmp / "audit.jsonl", self.cfg.run.mode, self.cfg.run.symbol, self.clock)
        return Engine(self.cfg, self.ex, self.store, audit, RULES, clock=self.clock, sleep=self.clock.sleep)

    def add_bar(self, close):
        last = self.market.bars[-1]
        c, o = D(str(close)), last.close
        self.market.bars.append(Candle(last.open_time_ms + HOUR, last.close_time_ms + HOUR, o,
                                       max(o, c) + D("0.5"), min(o, c) - D("0.5"), c, D("10")))
        self.market.book_price = c
        self.clock.ms = self.market.bars[-1].close_time_ms + 1 + 10_000

    def audit_events(self):
        import json
        path = self.tmp / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
