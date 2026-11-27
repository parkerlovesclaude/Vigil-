# Operating vigil

## Modes

| Mode | Market data | Orders | Account | Credentials |
|---|---|---|---|---|
| `paper` (default) | live public data (`exchange.base_url`) | simulated IOC fills against the live book | simulated, in `paper_account.json` | none |
| `testnet` | exchange testnet | real orders on the testnet | testnet account | testnet keys |
| `live` | exchange | **real orders, real money** | your account | live keys + all live gates |

Mode and adapter must agree (paper ⇔ `exchange.name="paper"`; testnet/live ⇔ `"binance"`).
The config validator rejects any other combination.

**Venue access:** `api.binance.com` blocks some jurisdictions, including the US. Use a
venue you are allowed to use. Set `exchange.base_url` to an API-compatible endpoint (for
example Binance.US, `https://api.binance.us`). Then re-run the whole testnet/paper
validation on that venue: fees, symbol rules and error codes can differ. Paper mode needs
outbound HTTPS to the market-data host.

## One-time setup

```bash
python3 --version                      # 3.11 or newer
cd vigil
python3 -m venv .venv                  # isolated environment (Homebrew/macOS Python requires this)
source .venv/bin/activate              # run this in every new Terminal window
python -m pip install pytest certifi   # certifi = CA certificates for HTTPS verification
python -m pytest -q
cp examples/config.paper.toml config.toml
python -m vigil check-config config.toml
```
`CERTIFICATE_VERIFY_FAILED` means this Python can't find trusted CA certificates;
installing `certifi` in the venv fixes it (vigil uses it automatically). Never turn TLS
verification off.

Use an account (or sub-account) **dedicated** to vigil. Reconciliation treats any base
asset holding it doesn't know about as a mismatch and refuses to trade.

## Paper trading

```bash
python3 -m vigil run config.toml           # Ctrl-C to stop; restart any time
python3 -m vigil status config.toml        # position, halt, counters
python3 -m vigil verify-audit config.toml
```
Run it under a supervisor that restarts on crash (systemd, launchd, Docker
`restart: on-failure`). The supervisor must **not** restart after exit code 1 (halted)
or 3 (live not armed) without a person looking. Every start begins with reconciliation.

Paper is the realistic test of the plumbing, and of the cost estimates against simulated
fills. It is not evidence of profitability: simulated fills don't move the market and
never suffer queueing or outages.

## Testnet

1. Create testnet API keys (trade permission only, **no withdrawal permission**).
2. In the config: `run.mode = "testnet"`, `exchange.name = "binance"`, and a separate `run.state_dir`.
3. `export VIGIL_API_KEY=… VIGIL_API_SECRET=…`
4. `python3 -m vigil reconcile config.toml`, then `run`.
5. Watch at least one full entry and exit. Compare the audit log's order lifecycle with the
   testnet's order history. Then deliberately kill the process mid-run, restart, and confirm
   reconciliation passes.

## Enabling live trading (deliberate, multi-step)

Live trading needs **all** of the following. Any one missing and `run` exits with code 3,
listing what is missing:

1. Config: `run.mode = "live"`, `exchange.name = "binance"` **and** `live.enabled = true`
   (the validator rejects half-armed configs, e.g. `live.enabled = true` with mode paper).
2. Command line: `--live`.
3. Environment: `VIGIL_LIVE_ACK` equal to this config's fingerprint. Get it from
   `check-config`. **Any** later config edit changes the fingerprint and disarms live
   until you acknowledge again.
4. Environment: API key and secret. These are never stored in the config file.
5. Startup reconciliation passes: clock skew, no unexplained open orders, balances match
   the local position.

Checklist before step 3:
- [ ] Backtest on real data with your actual fee tier and pessimistic costs (see STRATEGY.md "Evidence").
- [ ] Paper ran for a meaningful period, and estimated costs in `sizing.details` match actual fills.
- [ ] Testnet drills done: normal trade, kill/restart, halt and reset.
- [ ] Keys: trade-only, no withdrawals, IP-restricted if the venue supports it.
- [ ] `max_position_notional` and `max_daily_loss` set to amounts you can afford to lose entirely.
- [ ] Separate `state_dir` for live; audit log shipped off-host.
- [ ] Someone will look at alerts. A halt stops trading but does not close positions.

```bash
python3 -m vigil check-config live.toml          # prints the fingerprint
export VIGIL_LIVE_ACK=<fingerprint> VIGIL_API_KEY=… VIGIL_API_SECRET=…
python3 -m vigil run live.toml --live
```

## Halts and recovery

| Exit code | Meaning |
|---|---|
| 0 | clean stop |
| 1 | halted, or startup reconciliation failed |
| 2 | invalid configuration |
| 3 | live gates not satisfied / `--live` used with a non-live config |

**Daily loss block:** entries pause until the next UTC day by themselves. Nothing to do.

**Risk halt (drawdown):** the process keeps running and manages exits, but takes no new
entries. Review performance before resetting.

**Integrity halt:** the process stops. Then:
1. `status` shows the reason and any unresolved orders.
2. Read the audit log around the halt: `jq -c 'select(.event=="halt" or .event=="error" or .event=="reconcile_result")'`.
3. Compare with the exchange UI: open orders, recent trades, balances.
4. Fix the cause. That may mean cancelling a stray order by hand, or moving unrelated
   holdings out of the dedicated account.
5. `python3 -m vigil reset-halt config.toml --reason "…" --operator <name>` (audited).
6. Restart. Reconciliation runs first and refuses to trade if anything still disagrees.

**What reconciliation does after an interruption:**
- Every order not in a terminal state locally is queried by its client order id; missing
  fills are applied exactly once.
- An order never acknowledged and still not visible after `order_resolve_timeout_seconds`
  is treated as never placed. The balance check then confirms it.
- Open orders on the symbol, or base balances that don't match the local position, block trading.

**Never** delete `state.json` to "unstick" the system while a position or unresolved order
might exist. That throws away the record of what was sent. If the state file is truly
lost: flatten the account by hand, delete it, and start fresh (reconciliation then expects
an empty base balance).

## Running more than one

One process per `state_dir`. A lock file enforces this. Use a separate config and
`state_dir` per symbol and per mode. Never point two configs at the same account.
