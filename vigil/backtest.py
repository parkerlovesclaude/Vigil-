"""Reproducible backtest of the same strategy, cost model and sizing code used live.

This is the only basis on which vigil makes any statement about profitability:
results are produced from a CSV whose SHA-256 is recorded alongside the
config fingerprint, so anyone can re-run and get identical numbers.

Execution model (conservative, no look-ahead):
  * Signals are computed on bar t's close; orders fill at bar t+1's open.
  * A synthetic order book is built around the fill price with
    `spread_bps` total spread and `levels` levels of `depth_quote` each,
    so impact, fees and the extra-slippage margin are charged exactly as the
    live sizing code estimates them.
  * Stops are checked intrabar: if a bar's low reaches the stop, the exit
    fills at min(bar open, stop) (gaps through the stop are honored), minus costs.
  * Each signal sees the same window as the live bot: the last
    `data.candle_fetch_limit` closed candles.
  * Daily-loss/drawdown halts are NOT simulated; position sizing is.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .config import Config
from .costs import BPS, estimate_fill
from .models import BookLevel, Candle, OrderBook, Side, SymbolRules
from .sizing import size_entry
from .strategy import Action, PositionContext, evaluate


def load_candles_csv(path: str | Path, interval_s: int) -> tuple[list[Candle], str]:
    """Accepts a header CSV (open_time_ms,open,high,low,close,volume) or raw Binance kline dumps."""
    raw = Path(path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    out = []
    for row in csv.reader(raw.decode().splitlines()):
        if not row or not row[0].strip().lstrip("-").isdigit():
            continue  # header or blank
        t = int(row[0])
        if t > 10**14:      # microseconds (newer Binance dumps)
            t //= 1000
        o, h, l, c, v = (Decimal(x) for x in row[1:6])
        out.append(Candle(t, t + interval_s * 1000 - 1, o, h, l, c, v))
    out.sort(key=lambda k: k.open_time_ms)
    return out, sha


def synthetic_book(price: Decimal, spread_bps: Decimal, depth_quote: Decimal, levels: int,
                   tick: Decimal) -> OrderBook:
    half = spread_bps / 2 / BPS
    gap = max(spread_bps, Decimal(1)) / BPS
    bids, asks = [], []
    for i in range(levels):
        bp = price * (1 - half - gap * i)
        ap = price * (1 + half + gap * i)
        bids.append(BookLevel(bp, depth_quote / bp))
        asks.append(BookLevel(ap, depth_quote / ap))
    return OrderBook(0, tuple(bids), tuple(asks))


@dataclass
class _Pos:
    qty: Decimal
    cost: Decimal
    entry_px: Decimal
    stop: Decimal
    highest: Decimal
    entry_time: int
    initial_risk: Decimal
    fees: Decimal
    slippage: Decimal


def run_backtest(cfg: Config, candles: list[Candle], rules: SymbolRules, *, data_sha256: str,
                 spread_bps: Decimal = Decimal("2"), depth_quote: Decimal = Decimal("250000"),
                 levels: int = 10) -> dict:
    s, costs, risk = cfg.strategy, cfg.costs, cfg.risk
    fee = costs.taker_fee_bps / BPS
    extra = costs.extra_slippage_bps / BPS
    cash = cfg.paper.starting_quote
    pos: _Pos | None = None
    pending: tuple | None = None
    trades, equity_curve = [], []
    peak, max_dd, bars_in_pos = cash, Decimal(0), 0
    start = s.min_history_bars - 1

    def book_at(px):
        return synthetic_book(px, spread_bps, depth_quote, levels, rules.tick_size)

    def do_exit(t, px_ref, reason):
        nonlocal cash, pos
        book = book_at(px_ref)
        est = estimate_fill(book, Side.SELL, pos.qty, costs)
        px = est.vwap * (1 - extra)
        gross = pos.qty * px
        f = gross * fee
        cash += gross - f
        pos.fees += f
        pos.slippage += pos.qty * (px_ref - px)
        net = gross - f - pos.cost
        trades.append({"entry_time_ms": pos.entry_time, "exit_time_ms": t, "entry_price": pos.entry_px,
                       "exit_price": px, "qty": pos.qty, "net_pnl": net, "fees": pos.fees,
                       "slippage_cost": pos.slippage, "r_multiple": net / pos.initial_risk,
                       "exit_reason": reason})
        pos = None

    for t in range(start, len(candles)):
        bar = candles[t]
        if pending is not None:
            kind, payload = pending
            pending = None
            if kind == "ENTER" and pos is None:
                book = book_at(bar.open)
                equity = cash
                sz = size_entry(equity=equity, quote_free=cash, stop=payload["stop"], book=book, rules=rules,
                                costs=costs, risk=risk, loss_allowance=equity)
                if sz.ok:
                    est = estimate_fill(book, Side.BUY, sz.qty, costs, price_limit=sz.limit_price)
                    px = est.vwap * (1 + extra)
                    cost = sz.qty * px
                    net_qty = sz.qty * (1 - fee)
                    cash -= cost
                    pos = _Pos(net_qty, cost, px, payload["stop"], payload["close"], bar.open_time_ms,
                               sz.details["worst_case_loss"], sz.qty * fee * px,
                               sz.qty * (px - bar.open))
            elif kind == "EXIT" and pos is not None:
                do_exit(bar.open_time_ms, bar.open, payload)
        if pos is not None and bar.low <= pos.stop:
            do_exit(bar.open_time_ms, min(bar.open, pos.stop), "stop_intrabar")
        if pos is not None:
            bars_in_pos += 1
        eq = cash + (pos.qty * bar.close if pos else Decimal(0))
        equity_curve.append(eq)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
        if t == len(candles) - 1:
            break
        ctx = PositionContext(pos is not None, pos.entry_px if pos else None,
                              pos.stop if pos else None, pos.highest if pos else None)
        # Same window the live bot sees: the last data.candle_fetch_limit closed candles.
        sig = evaluate(candles[max(0, t + 1 - cfg.data.candle_fetch_limit): t + 1], ctx, s)
        if pos is not None:
            pos.stop = max(pos.stop, sig.stop_price or pos.stop)
            pos.highest = max(pos.highest, sig.highest_close or pos.highest)
            if sig.action is Action.EXIT:
                pending = ("EXIT", sig.reason)
        elif sig.action is Action.ENTER:
            pending = ("ENTER", {"stop": sig.stop_price, "close": sig.reference_price})

    if pos is not None:
        do_exit(candles[-1].close_time_ms, candles[-1].close, "end_of_data")
    final = cash
    wins = [x for x in trades if x["net_pnl"] > 0]
    losses = [x for x in trades if x["net_pnl"] <= 0]
    gp = sum((x["net_pnl"] for x in wins), Decimal(0))
    gl = -sum((x["net_pnl"] for x in losses), Decimal(0))
    tested = candles[start:]
    return {
        "reproducibility": {"data_sha256": data_sha256, "config_fingerprint": cfg.fingerprint(),
                            "bars": len(candles), "first_bar_ms": candles[0].open_time_ms if candles else None,
                            "last_bar_ms": candles[-1].open_time_ms if candles else None},
        "assumptions": {"spread_bps": spread_bps, "depth_quote_per_level": depth_quote, "levels": levels,
                        "taker_fee_bps": costs.taker_fee_bps, "extra_slippage_bps": costs.extra_slippage_bps,
                        "fills": "next bar open; stops intrabar at min(open, stop)",
                        "not_simulated": ["daily loss block", "drawdown halt", "latency", "outages"]},
        "results": {
            "starting_equity": cfg.paper.starting_quote, "final_equity": final,
            "net_return": (final - cfg.paper.starting_quote) / cfg.paper.starting_quote,
            "buy_and_hold_return": (tested[-1].close - tested[0].close) / tested[0].close if tested else None,
            "trades": len(trades), "win_rate": (Decimal(len(wins)) / len(trades)) if trades else None,
            "avg_r": (sum((x["r_multiple"] for x in trades), Decimal(0)) / len(trades)) if trades else None,
            "profit_factor": (gp / gl) if gl > 0 else None,
            "total_fees": sum((x["fees"] for x in trades), Decimal(0)),
            "total_slippage_cost": sum((x["slippage_cost"] for x in trades), Decimal(0)),
            "max_drawdown": max_dd,
            "exposure": Decimal(bars_in_pos) / len(tested) if tested else None,
        },
        "trades": trades,
    }


def _q(v):
    if isinstance(v, Decimal):
        return v.quantize(Decimal("0.00000001"))
    if isinstance(v, dict):
        return {k: _q(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_q(x) for x in v]
    return v


def write_report(result: dict, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = _q(result)
    summary = {k: v for k, v in result.items() if k != "trades"}
    sp = out / "backtest_summary.json"
    sp.write_text(json.dumps(summary, indent=2, default=str))
    tp = out / "backtest_trades.csv"
    with open(tp, "w", newline="") as f:
        w = csv.writer(f)
        cols = ["entry_time_ms", "exit_time_ms", "entry_price", "exit_price", "qty", "net_pnl", "fees",
                "slippage_cost", "r_multiple", "exit_reason"]
        w.writerow(cols)
        for t in result["trades"]:
            w.writerow([t[c] for c in cols])
    return sp, tp
