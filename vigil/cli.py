"""Command-line entry point: `python -m vigil <command> <config.toml> [options]`.

Commands
  check-config   validate the config and print its fingerprint (nothing is contacted)
  reconcile      compare local state with the exchange; exit 0 only if all checks pass
  run            start trading in the configured mode (paper by default)
  status         print local state (position, halt, counters, unresolved orders)
  reset-halt     clear a halt after investigation (audited; forces reconciliation)
  verify-audit   verify the audit log hash chain
  export-obsidian  write closed trades from the audit log into an Obsidian vault
  fetch-candles  download historical candles to CSV (public data, for backtests)
  backtest       run the reproducible backtest on a CSV
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

from .audit import AuditLog, verify_chain
from .config import ConfigError, check_live_gates, load_config
from .engine import Engine, HaltedError
from .exchange.base import ExchangeError
from .exchange.binance import DEFAULT_URLS, BinanceSpot
from .exchange.paper import PaperExchange
from .state import Halt, StateStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _paths(cfg):
    d = Path(cfg.run.state_dir)
    return {"dir": d, "state": d / "state.json", "audit": d / "audit.jsonl",
            "paper": d / "paper_account.json", "lock": d / "vigil.lock"}


def _lock(path: Path):
    """One process per state directory, otherwise two loops could both send orders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another vigil process holds {path}; refusing to start")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def build_adapter(cfg):
    x = cfg.exchange
    url = x.base_url or DEFAULT_URLS[cfg.run.mode]
    if cfg.run.mode == "paper":
        market = BinanceSpot(url, timeout_s=x.request_timeout_seconds)
        rules = market.symbol_rules(cfg.run.symbol)
        ex = PaperExchange(market, rules, starting_quote=cfg.paper.starting_quote,
                           fee_bps=cfg.costs.taker_fee_bps, account_path=_paths(cfg)["paper"], clock=_now_ms)
        return ex, rules
    ex = BinanceSpot(url, api_key=os.environ.get(x.api_key_env, ""),
                     api_secret=os.environ.get(x.api_secret_env, ""),
                     timeout_s=x.request_timeout_seconds, recv_window_ms=x.recv_window_ms)
    return ex, ex.symbol_rules(cfg.run.symbol)


def _engine(cfg, live_armed=False):
    p = _paths(cfg)
    ex, rules = build_adapter(cfg)
    audit = AuditLog(p["audit"], cfg.run.mode, cfg.run.symbol, _now_ms)
    return Engine(cfg, ex, StateStore(p["state"]), audit, rules, live_armed=live_armed)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vigil")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check-config", "reconcile", "status", "verify-audit"):
        sub.add_parser(name).add_argument("config")
    r = sub.add_parser("run")
    r.add_argument("config")
    r.add_argument("--live", action="store_true", help="required (with other gates) for live mode")
    r.add_argument("--max-ticks", type=int, default=None)
    h = sub.add_parser("reset-halt")
    h.add_argument("config")
    h.add_argument("--reason", required=True)
    h.add_argument("--operator", required=True)
    f = sub.add_parser("fetch-candles")
    f.add_argument("config")
    f.add_argument("--days", type=int, required=True)
    f.add_argument("--out", required=True)
    o = sub.add_parser("export-obsidian")
    o.add_argument("config")
    o.add_argument("--vault", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("config")
    b.add_argument("--csv", required=True)
    b.add_argument("--out", default="backtest_out")
    b.add_argument("--spread-bps", default="2")
    b.add_argument("--depth-quote", default="250000")
    b.add_argument("--tick-size", default="0.01")
    b.add_argument("--step-size", default="0.00001")
    b.add_argument("--min-notional", default="5")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.cmd == "check-config":
        print(f"config OK  mode={cfg.run.mode}  symbol={cfg.run.symbol}  interval={cfg.run.interval}")
        print(f"fingerprint={cfg.fingerprint()}")
        if cfg.run.mode == "live":
            print(f"to arm live trading for THIS config: export VIGIL_LIVE_ACK={cfg.fingerprint()} "
                  f"and run with --live")
        return 0

    p = _paths(cfg)
    if args.cmd == "verify-audit":
        ok, msg = verify_chain(p["audit"]) if p["audit"].exists() else (True, "no audit log yet")
        print(("OK: " if ok else "FAILED: ") + msg)
        return 0 if ok else 1

    if args.cmd == "status":
        st = StateStore(p["state"]).load()
        print(json.dumps({"position": st.position, "halt": st.halt, "day": st.day,
                          "orders_today": st.orders_today, "realized_pnl_today": st.realized_pnl_today,
                          "peak_equity": st.peak_equity, "needs_reconcile": st.needs_reconcile,
                          "unresolved_orders": [o.client_order_id for o in st.unresolved_orders()]},
                         default=lambda o: o.__dict__ if hasattr(o, "__dict__") else str(o), indent=2))
        return 0

    if args.cmd == "export-obsidian":
        from .obsidian import export_trades
        notes = export_trades(p["audit"], args.vault) if p["audit"].exists() else []
        print(f"wrote {len(notes)} new trade note(s)")
        return 0

    if args.cmd == "fetch-candles":
        return _fetch(cfg, args.days, args.out)

    if args.cmd == "backtest":
        from .backtest import load_candles_csv, run_backtest, write_report
        from .models import SymbolRules
        candles, sha = load_candles_csv(args.csv, cfg.run.interval_seconds)
        quote = next((q for q in ("USDT", "USDC", "FDUSD", "USD", "BTC", "ETH") if cfg.run.symbol.endswith(q)), None)
        if quote is None:
            print("cannot infer quote asset from symbol", file=sys.stderr)
            return 2
        rules = SymbolRules(cfg.run.symbol, cfg.run.symbol[: -len(quote)], quote, Decimal(args.tick_size),
                            Decimal(args.step_size), Decimal(args.step_size), Decimal(args.min_notional))
        res = run_backtest(cfg, candles, rules, data_sha256=sha, spread_bps=Decimal(args.spread_bps),
                           depth_quote=Decimal(args.depth_quote))
        sp, tp = write_report(res, args.out)
        from .backtest import _q
        print(json.dumps(_q(res["results"]), indent=2, default=str))
        print("NOTE: a backtest is evidence about this data under these cost assumptions only.")
        print(f"wrote {sp} and {tp}")
        return 0

    lock = _lock(p["lock"])  # noqa: F841 - held for process lifetime
    if args.cmd == "reset-halt":
        store = StateStore(p["state"])
        st = store.load()
        audit = AuditLog(p["audit"], cfg.run.mode, cfg.run.symbol, _now_ms)
        audit.write("halt_reset", {"previous": st.halt, "reason": args.reason, "operator": args.operator})
        st.halt, st.needs_reconcile, st.consecutive_errors = Halt(), True, 0
        store.save(st)
        print("halt cleared; reconciliation will run before any trading")
        return 0

    live_armed = False
    if cfg.run.mode == "live":
        unmet = check_live_gates(cfg, args.live)
        if unmet:
            print("LIVE TRADING NOT ARMED:\n  - " + "\n  - ".join(unmet), file=sys.stderr)
            return 3
        live_armed = True
    elif getattr(args, "live", False):
        print("--live given but config is not in live mode; refusing (fix the config, don't guess)",
              file=sys.stderr)
        return 3

    try:
        eng = _engine(cfg, live_armed)
    except ExchangeError as exc:
        print(f"cannot reach the exchange: {exc}", file=sys.stderr)
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            print("fix: inside your venv run `python -m pip install certifi` (see docs/OPERATIONS.md)",
                  file=sys.stderr)
        return 1
    if args.cmd == "reconcile":
        eng.st.needs_reconcile = True
        from .reconcile import reconcile
        ok = reconcile(eng)
        print("reconciliation " + ("PASSED" if ok else "FAILED (see audit log)"))
        return 0 if ok else 1

    if not eng.startup():
        print(f"startup reconciliation failed or halt active: {eng.st.halt.reason}", file=sys.stderr)
        return 1
    def _sigterm(*_):  # platforms (Railway, Docker, systemd) stop processes with SIGTERM
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)
    print(f"running mode={cfg.run.mode} symbol={cfg.run.symbol} fingerprint={cfg.fingerprint()}", flush=True)
    try:
        eng.run(args.max_ticks)
    except HaltedError as exc:
        print(f"HALTED: {exc}. Investigate, then `vigil reset-halt`.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped by operator")
    return 0


def _fetch(cfg, days: int, out: str) -> int:
    url = cfg.exchange.base_url or DEFAULT_URLS["paper"]
    ex = BinanceSpot(url, timeout_s=cfg.exchange.request_timeout_seconds)
    step = cfg.run.interval_seconds * 1000
    start = _now_ms() - days * 86_400_000
    rows, cursor = [], start
    while True:
        data = ex._request("GET", "/api/v3/klines", {"symbol": cfg.run.symbol, "interval": cfg.run.interval,
                                                     "startTime": cursor, "limit": 1000})
        if not data or not isinstance(data, list):
            break
        rows.extend(data)
        cursor = int(data[-1][0]) + step
        if len(data) < 1000:
            break
    now = _now_ms()
    with open(out, "w") as f:
        f.write("open_time_ms,open,high,low,close,volume\n")
        for k in rows:
            if int(k[6]) < now:  # closed bars only
                f.write(f"{k[0]},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]}\n")
    print(f"wrote {len(rows)} candles to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
