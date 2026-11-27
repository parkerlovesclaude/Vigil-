# vigil

A small, auditable spot-crypto trading system that runs **one** documented strategy and
treats every order as an uncertain operation. Pure Python standard library (3.11+). No
third-party runtime dependencies.

> **No profitability claim is made.** The strategy is a hypothesis. vigil gives you the
> tools to test it reproducibly (backtest on real data with full costs, paper, testnet)
> and to run it safely, nothing more. Trading can lose all of the capital you allocate.

## What it does

- **Strategy** (`docs/STRATEGY.md`): long-only Donchian breakout with an EMA trend filter,
  ATR initial stop and an ATR trailing stop, evaluated on closed bars. Between bars it also
  runs a protective stop check.
- **Costs are part of the signal:** each candidate trade is priced by walking the live order
  book (spread + depth), plus a slippage margin and fees, on both entry and exit. Trades
  whose round-trip cost is too large relative to the stop distance are skipped. Size is
  set from the worst-case loss, costs included.
- **Risk limits:** per-trade risk, position caps, daily loss block, drawdown halt, order
  budget, error-streak halt, cooldown.
- **Order safety:** the intent is persisted before sending, client order ids are
  deterministic (no duplicates), timeouts are resolved by query and never resubmitted,
  IOC orders only, partial fills handled, each fill applied exactly once.
- **Recovery:** reconciliation against the exchange at every start and after any
  uncertainty. No trading until it passes.
- **Audit:** a hash-chained JSONL record of every evaluation and every order lifecycle event (`docs/AUDIT.md`).
- **Modes:** `paper` (default) → `testnet` → `live`, with a deliberate multi-gate live arming process (`docs/OPERATIONS.md`).

## Quick start

```bash
python3 -m pip install pytest          # only needed to run the tests
python3 -m pytest -q                   # 111 tests, about 1 s, no network
python3 -m vigil check-config examples/config.paper.toml
python3 -m vigil run examples/config.paper.toml          # paper trading on live public data
```

Backtest on real data:
```bash
python3 -m vigil fetch-candles examples/config.paper.toml --days 1500 --out btc_1h.csv
python3 -m vigil backtest examples/config.paper.toml --csv btc_1h.csv --spread-bps 2 \
    --tick-size 0.01 --step-size 0.00001 --min-notional 5 --out bt/
```
The backtest output records the data file's SHA-256 and the config fingerprint, so the
result can be reproduced exactly.

## Commands

| Command | Purpose |
|---|---|
| `check-config CFG` | validate; print mode and fingerprint. Contacts nothing |
| `run CFG [--live] [--max-ticks N]` | trade in the configured mode |
| `reconcile CFG` | compare local state with the exchange; exit 0 only if consistent |
| `status CFG` | position, halt, counters, unresolved orders |
| `reset-halt CFG --reason R --operator NAME` | clear a halt (audited; forces reconciliation) |
| `verify-audit CFG` | verify the audit log hash chain |
| `export-obsidian CFG --vault PATH` | write closed trades from the audit log as Obsidian notes (also automatic when `run.obsidian_vault` is set) |
| `fetch-candles CFG --days N --out F` | download closed candles (public data) |
| `backtest CFG --csv F [...]` | reproducible backtest with the live cost and sizing code |

## Layout

```
vigil/
  config.py       validated, bounded, typo-proof configuration; live gates; fingerprint
  models.py       Decimal-based data types
  marketdata.py   stale / malformed / conflicting data detection
  strategy.py     the strategy (pure function)
  costs.py        order-book cost model, tick/step rounding, marketable limit prices
  sizing.py       cost- and risk-aware position sizing
  risk.py         pre-trade checks, daily block, halts
  state.py        atomic, durable local state
  audit.py        hash-chained JSONL audit log
  orders.py       order lifecycle under uncertainty
  reconcile.py    local vs exchange consistency checks
  engine.py       the loop
  backtest.py     reproducible backtest using the same code
  obsidian.py     optional trade journal: closed trades -> Obsidian notes
  cli.py          commands
  exchange/
    base.py       adapter interface + error contract (the exchange boundary)
    paper.py      simulated account/fills (also used for failure-injection tests)
    binance.py    Binance Spot / Spot Testnet REST adapter (stdlib HTTP + HMAC)
tests/            strategy, costs, sizing, risk, config, data validation, orders,
                  engine end-to-end, recovery, adapter error mapping, backtest, state
docs/             STRATEGY.md, AUDIT.md, OPERATIONS.md
examples/         config.paper.toml
```

The strategy, cost model, sizing and risk layer import nothing exchange-specific. To add
a venue, implement `exchange/base.py:ExchangeAdapter`, including its error contract, which
is what the order manager relies on.

## Known limitations (read before using real money)

- The Binance adapter is tested against simulated HTTP responses only. Validate it on
  testnet (and on your actual venue) before going live.
- Stops are managed by the bot (bar-close rule plus a check every loop), not resting on
  the exchange. If the process or network is down, nothing protects the position. Gaps can
  exceed the stop.
- The exit cost estimate assumes exit liquidity similar to the current book.
- Fees paid in a third asset (e.g. BNB discounts) are estimated from the configured fee
  rate and flagged `fee_estimated` in the audit log.
- One symbol, one position, long only, per process.
- The audit hash chain detects tampering with individual lines, not a full rewrite. Ship
  the log off-host for stronger guarantees.

## License

MIT. See `LICENSE`.
